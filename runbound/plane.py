"""The control-plane HTTP client — the SDK's only outbound path to a fleet.

Everything in here is best-effort by construction. A control plane that is
slow, down, or unreachable must cost the agent nothing but a short timeout and
one warning a minute: every method catches everything, returns ``None`` (or
``False``), and lets the caller fall back to the local answer. The class holds
just enough state for a caller to tell "we are talking to the plane" from "we
have not heard from it in a while":

``key_state``
    ``"unknown"`` until a call succeeds (``"valid"``) or the plane rejects the
    key with 401/403 (``"invalid"``). An invalid key is terminal until
    :meth:`PlaneClient.reset_key` — every later call short-circuits without
    opening a socket, so a bad key costs one request, not one per session.
``consecutive_failures`` / ``last_success``
    What a caller degrades on. Reset on every success.

Timeouts are per call and deliberately uneven: the entry decision sits in the
hot path of a session and gets ``timeout_s`` (150 ms by default), while the
background calls — hello, event batches, policy — can afford to wait.

Only the standard library is used: ``urllib.request`` for the transport and
one daemon :class:`Poller` thread for the heartbeat.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .plane_types import EntryDecision, HelloReply, TripReport, from_wire, to_wire

_LOG = logging.getLogger("runbound")

#: Per-call timeouts, in seconds. ``enter`` uses the instance's ``timeout_s``.
HELLO_TIMEOUT_S = 2.0
EVENTS_TIMEOUT_S = 5.0
POLICY_TIMEOUT_S = 2.0
CLEAR_TIMEOUT_S = 0.5

#: At most one warning per this many seconds, per client, per kind of problem.
WARN_INTERVAL_S = 60.0

#: How long :meth:`Poller.stop` waits for the heartbeat thread to notice.
POLLER_JOIN_TIMEOUT_S = 1.0

#: HTTP statuses that mean "your key is no good", as opposed to "try later".
REJECTED_STATUSES = frozenset({401, 403})


class _PeriodicWarning:
    """A warning that fires at most once per ``interval_s``.

    A control plane that is down is down for every call: without this, one
    outage would write a log line per session per second. Suppressed warnings
    are simply dropped — the counters on the client are the durable signal.
    """

    def __init__(self, interval_s: float, now: Callable[[], float]) -> None:
        self._interval_s = interval_s
        self._now = now
        self._last: float | None = None
        self._lock = threading.Lock()

    def due(self) -> bool:
        """True at most once per interval; records the firing when it says so."""
        with self._lock:
            now = self._now()
            if self._last is not None and now - self._last < self._interval_s:
                return False
            self._last = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._last = None


def warn_periodically(gate: _PeriodicWarning, message: str, *args, **kwargs) -> None:
    """Log ``message`` at warning level if ``gate`` allows it right now."""
    if gate.due():
        _LOG.warning(message, *args, **kwargs)


class PlaneClient:
    """Talks to the control plane over HTTP. Never raises, never retries.

    ``now`` is the monotonic clock used for ``last_success`` and for the
    once-a-minute warning; tests hand in a fake. ``sdk_version`` defaults to
    the installed version of the package, read on first use so importing this
    module never depends on the package's own import order.
    """

    def __init__(
        self,
        url: str,
        api_key: str | None,
        service: str,
        worker_id: str,
        timeout_s: float = 0.15,
        now: Callable[[], float] = time.monotonic,
        sdk_version: str | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key or ""
        self.service = service
        self.worker_id = worker_id
        self.timeout_s = timeout_s
        self.key_state = "unknown"
        self.consecutive_failures = 0
        self.last_success: float | None = None
        self._now = now
        self._sdk_version = sdk_version
        self._lock = threading.Lock()
        self._failure_warning = _PeriodicWarning(WARN_INTERVAL_S, now)
        self._key_warning = _PeriodicWarning(float("inf"), now)

    # --- the calls --------------------------------------------------------

    def hello(self, payload: dict) -> HelloReply | None:
        """Announce this worker and read back org state. ``None`` on failure."""
        data = self._call("POST", "/v1/hello", payload, HELLO_TIMEOUT_S)
        return self._parse(HelloReply, data)

    def enter(self, payload: dict) -> EntryDecision | None:
        """Ask whether a session may start. ``None`` means "decide locally"."""
        data = self._call("POST", "/v1/enter", payload, self.timeout_s)
        return self._parse(EntryDecision, data)

    def trip(self, report: TripReport) -> bool:
        """Report a trip to the fleet. ``False`` if it did not get through."""
        try:
            body = to_wire(report)
        except Exception:
            self._failed("/v1/trip", exc_info=True)
            return False
        return self._call("POST", "/v1/trip", body, self.timeout_s) is not None

    def events(self, batch: dict) -> bool:
        """Ship one batch of telemetry. ``False`` if it did not get through."""
        return self._call("POST", "/v1/events", batch, EVENTS_TIMEOUT_S) is not None

    def policy(self, service: str) -> dict | None:
        """Fetch the org policy for ``service``. ``None`` on failure."""
        path = "/v1/policy?" + urlencode({"service": service})
        return self._call("GET", path, None, POLICY_TIMEOUT_S)

    def clear(self, key_hash: str) -> int | None:
        """Clear a key's fleet state; returns how many latches were cleared."""
        data = self._call("POST", "/v1/clear", {"key_hash": key_hash}, CLEAR_TIMEOUT_S)
        if data is None:
            return None
        cleared = data.get("cleared", 0)
        return cleared if isinstance(cleared, int) else 0

    def reset_key(self) -> None:
        """Forget a rejection so the next call tries the network again."""
        with self._lock:
            self.key_state = "unknown"
        self._key_warning.reset()

    # --- the transport ----------------------------------------------------

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> dict | None:
        """One request. Returns the decoded object, or ``None`` for any failure.

        A response with no body decodes to ``{}`` — that is a success, and the
        boolean calls rely on it. Anything else that goes wrong (a bad key, a
        socket error, a body that is not a JSON object, a payload that will not
        serialize) is a failure: counted, warned about at most once a minute,
        and reported as ``None``.
        """
        if self.key_state == "invalid":
            return None
        try:
            request = self._request(method, path, body)
        except Exception:
            self._failed(path, exc_info=True)
            return None
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as error:
            if getattr(error, "code", None) in REJECTED_STATUSES:
                self._rejected()
            else:
                self._failed(path, exc_info=True)
            return None
        except Exception:
            self._failed(path, exc_info=True)
            return None
        return self._decode(raw, path)

    def _request(self, method: str, path: str, body: dict | None) -> Request:
        data = None if body is None else json.dumps(body).encode("utf-8")
        return Request(self.url + path, data=data, headers=self._headers(), method=method)

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Runbound-Service": self.service,
            "X-Runbound-Worker": self.worker_id,
            "X-Runbound-SDK": self._version(),
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _version(self) -> str:
        """The SDK version, read lazily so import order never matters."""
        if self._sdk_version is None:
            try:
                from . import __version__

                self._sdk_version = __version__
            except Exception:
                self._sdk_version = "unknown"
        return self._sdk_version

    def _decode(self, raw: bytes, path: str) -> dict | None:
        if not raw or not raw.strip():
            self._succeeded()
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except Exception:
            self._failed(path, exc_info=True)
            return None
        if not isinstance(decoded, dict):
            self._failed(path)
            return None
        self._succeeded()
        return decoded

    def _parse(self, cls: type, data: dict | None):
        """Tolerantly build a wire dataclass; a broken reply is a failure."""
        if data is None:
            return None
        try:
            return from_wire(cls, data)
        except Exception:
            self._failed("reply", exc_info=True)
            return None

    # --- bookkeeping ------------------------------------------------------

    def _succeeded(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.last_success = self._now()
            if self.key_state != "invalid":
                self.key_state = "valid"

    def _failed(self, path: str, exc_info: bool = False) -> None:
        with self._lock:
            self.consecutive_failures += 1
            failures = self.consecutive_failures
        warn_periodically(
            self._failure_warning,
            "runbound: control plane call %s failed (%d in a row); using local state",
            path,
            failures,
            exc_info=exc_info,
        )

    def _rejected(self) -> None:
        with self._lock:
            self.key_state = "invalid"
            self.consecutive_failures += 1
        warn_periodically(
            self._key_warning,
            "runbound: control plane rejected the token; running local-only",
        )


class Poller(threading.Thread):
    """Says hello to the plane every ``poll_s`` seconds, forever.

    The first call goes out immediately — a worker should not spend its first
    poll interval unaware that the fleet is halted. Replies are handed to
    ``on_reply``; a reply that never came, a callback that raised, and a client
    that raised are all logged and shrugged off, because a broken heartbeat
    must not stop the next one.

    The thread is a daemon, so it never keeps a host process alive, and
    :meth:`stop` is idempotent and bounded.
    """

    def __init__(
        self,
        client: PlaneClient,
        poll_s: float,
        on_reply: Callable[[HelloReply], None],
        payload_fn: Callable[[], dict],
    ) -> None:
        super().__init__(daemon=True, name="runbound-plane-poller")
        self._client = client
        self._poll_s = max(float(poll_s), 0.0)
        self._on_reply = on_reply
        self._payload_fn = payload_fn
        self._stopped = threading.Event()
        self._warning = _PeriodicWarning(WARN_INTERVAL_S, time.monotonic)

    def run(self) -> None:
        while not self._stopped.is_set():
            self._tick()
            if self._stopped.wait(self._poll_s):
                return

    def _tick(self) -> None:
        """One heartbeat: build the payload, say hello, deliver the reply."""
        try:
            reply = self._client.hello(self._payload_fn())
            if reply is not None:
                self._on_reply(reply)
        except Exception:
            warn_periodically(
                self._warning, "runbound: control plane poll failed", exc_info=True
            )

    def stop(self) -> None:
        """Ask the thread to finish and wait up to a second for it to do so."""
        self._stopped.set()
        if self.is_alive():
            self.join(POLLER_JOIN_TIMEOUT_S)
