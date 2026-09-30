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
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .plane_types import (
    FLEET_STATE_UNAVAILABLE_CAUSE,
    EntryDecision,
    HelloReply,
    PlaneUnavailable,
    TripReport,
    from_wire,
    to_wire,
)

_LOG = logging.getLogger("runbound")

#: Per-call timeouts, in seconds. ``enter`` uses the instance's ``timeout_s``.
HELLO_TIMEOUT_S = 2.0
EVENTS_TIMEOUT_S = 5.0
POLICY_TIMEOUT_S = 2.0
CONTROLS_TIMEOUT_S = 2.0
CLEAR_TIMEOUT_S = 0.5

#: At most one warning per this many seconds, per client, per kind of problem.
WARN_INTERVAL_S = 60.0

#: How long :meth:`Poller.stop` waits for the heartbeat thread to notice.
POLLER_JOIN_TIMEOUT_S = 1.0

#: HTTP statuses that mean "your key is no good", as opposed to "try later".
REJECTED_STATUSES = frozenset({401, 403})

#: The ``"error"`` value every SDK-path 503 carries, whether or not the
#: plane can say more about why — see :class:`~runbound.plane_types.
#: PlaneUnavailable`. Alone, this means only "the plane could not answer
#: normally," which covers a saturated connection pool, a handler bug, a
#: failed batch write — anything its own generic fail-open path catches.
#: :meth:`PlaneClient._plane_loss_kind` reports that alone as
#: ``"plane_unavailable"``, and only a body whose ``cause`` is
#: :data:`~runbound.plane_types.FLEET_STATE_UNAVAILABLE_CAUSE` as the more
#: specific ``"plane_loss"`` — the live store itself is gone, not merely
#: the plane's process. Before that ``cause`` field existed, every 503
#: shaped like this looked identical, so a plane whose database pool was
#: merely saturated (Redis perfectly healthy) read the same "fleet state
#: unavailable" a real state outage would; that mislabel is what this
#: split closes.
PLANE_LOSS_ERROR_CODE = "plane_unavailable"


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
        #: Why the *last* call to this client failed — ``"timeout"`` (the
        #: socket itself timed out), ``"plane_loss"`` (a 503 naming
        #: :data:`~runbound.plane_types.FLEET_STATE_UNAVAILABLE_CAUSE` —
        #: the plane's own state is unreachable, not merely this call),
        #: ``"plane_unavailable"`` (a 503 shaped like plane loss but naming
        #: no such cause — a saturated pool, a handler bug, anything else
        #: the plane's own generic fail-open catches), or ``"error"``
        #: (anything else: connection refused, a malformed reply, a
        #: rejected key). ``None`` before any call has failed, and cleared
        #: on every success — see :meth:`_succeeded`.
        #: :meth:`~runbound.shared.RemoteState._last_known_cause` reads
        #: this to attribute the entry window; nothing else in this module
        #: consumes it.
        self.last_failure_kind: str | None = None
        self._now = now
        self._sdk_version = sdk_version
        self._lock = threading.Lock()
        self._failure_warning = _PeriodicWarning(WARN_INTERVAL_S, now)
        self._key_warning = _PeriodicWarning(float("inf"), now)

    # --- the calls --------------------------------------------------------

    def hello(self, payload: dict) -> HelloReply | None:
        """Announce this worker and read back org state. ``None`` on failure."""
        data, _kind = self._call("POST", "/v1/hello", payload, HELLO_TIMEOUT_S)
        return self._parse(HelloReply, data)

    def enter(self, payload: dict) -> EntryDecision | None:
        """Ask whether a session may start. ``None`` means "decide locally".

        See :meth:`enter_with_kind` for a caller that also needs to know
        *why*, without racing :attr:`last_failure_kind`.
        """
        decision, _kind = self.enter_with_kind(payload)
        return decision

    def enter_with_kind(self, payload: dict) -> tuple[EntryDecision | None, str | None]:
        """Like :meth:`enter`, but also returns *this call's own* failure
        kind -- ``None`` on success, else one of :attr:`last_failure_kind`'s
        values.

        :attr:`last_failure_kind` is whole-client state: any call on this
        client can set or clear it, including one running concurrently on
        another thread (the heartbeat, in practice). Reading it back after
        this call returns is a race a concurrent success can win, clearing
        it (or overwriting it with an unrelated kind) before the read -- a
        real state outage's own run surfaced exactly this, misreading a
        plane-loss 503 as a timeout about one call in thirty. This method
        captures the kind synchronously, in this call's own stack, so
        :meth:`~runbound.shared.RemoteState.enter` (its only caller that
        needs the specific reason) never has to read it back at all.
        """
        data, kind = self._call("POST", "/v1/enter", payload, self.timeout_s)
        if data is None:
            return None, kind
        decision = self._parse(EntryDecision, data)
        if decision is None:
            return None, "error"
        return decision, None

    def trip(self, report: TripReport) -> bool:
        """Report a trip to the fleet. ``False`` if it did not get through."""
        try:
            body = to_wire(report)
        except Exception:
            self._failed("/v1/trip", exc_info=True)
            return False
        data, _kind = self._call("POST", "/v1/trip", body, self.timeout_s)
        return data is not None

    def events(self, batch: dict) -> bool:
        """Ship one batch of telemetry. ``False`` if it did not get through."""
        data, _kind = self._call("POST", "/v1/events", batch, EVENTS_TIMEOUT_S)
        return data is not None

    def policy(self, service: str) -> dict | None:
        """Fetch the org policy for ``service``. ``None`` on failure."""
        path = "/v1/policy?" + urlencode({"service": service})
        data, _kind = self._call("GET", path, None, POLICY_TIMEOUT_S)
        return data

    def controls(self, service: str) -> dict | None:
        """Fetch the Controls envelope for ``service``. ``None`` on
        failure. The reply is ``{"version", "dry_run", "controls"}``,
        exactly :meth:`policy`'s own shape."""
        path = "/v1/controls?" + urlencode({"service": service})
        data, _kind = self._call("GET", path, None, CONTROLS_TIMEOUT_S)
        return data

    def clear(self, key_hash: str) -> int | None:
        """Clear a key's fleet state; returns how many latches were cleared."""
        data, _kind = self._call("POST", "/v1/clear", {"key_hash": key_hash}, CLEAR_TIMEOUT_S)
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

    def _call(
        self, method: str, path: str, body: dict | None, timeout: float
    ) -> tuple[dict | None, str | None]:
        """One request. Returns ``(decoded object, None)`` on success, or
        ``(None, kind)`` for any failure -- ``kind`` is this call's own
        classification, the same values :attr:`last_failure_kind` takes.

        A response with no body decodes to ``{}`` — that is a success, and the
        boolean calls rely on it. Anything else that goes wrong (a bad key, a
        socket error, a body that is not a JSON object, a payload that will not
        serialize) is a failure: counted, warned about at most once a minute,
        and reported as ``(None, kind)``.

        Every caller still has :attr:`last_failure_kind` for the whole
        client's own last outcome (see its docstring); ``kind`` here is
        this call's own answer, for a caller (:meth:`enter_with_kind`) that
        cannot afford to read shared state a concurrent call might already
        have changed.
        """
        if self.key_state == "invalid":
            return None, "error"
        try:
            request = self._request(method, path, body)
        except Exception:
            self._failed(path, exc_info=True)
            return None, "error"
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as error:
            if getattr(error, "code", None) in REJECTED_STATUSES:
                self._rejected()
                return None, "error"
            kind = self._plane_loss_kind(error)
            self._failed(path, exc_info=True, kind=kind)
            return None, kind
        except TimeoutError as error:
            self._failed(path, exc_info=True, kind="timeout")
            return None, "timeout"
        except URLError as error:
            kind = "timeout" if isinstance(error.reason, TimeoutError) else "error"
            self._failed(path, exc_info=True, kind=kind)
            return None, kind
        except Exception:
            self._failed(path, exc_info=True)
            return None, "error"
        data = self._decode(raw, path)
        return data, (None if data is not None else "error")

    def _plane_loss_kind(self, error: HTTPError) -> str:
        """``"plane_loss"`` for a 503 whose body names
        :data:`~runbound.plane_types.FLEET_STATE_UNAVAILABLE_CAUSE`,
        ``"plane_unavailable"`` for the same shape with no matching cause
        (or none at all — an SDK-side default for a plane one version
        behind this field), else ``"error"``.

        Reading an ``HTTPError``'s body is exactly what ``urlopen`` handed
        this client in the first place (it is itself a file-like response
        object); a body that cannot be read or parsed — including a test
        double built with no body at all — is simply not this shape, and
        this returns ``"error"`` rather than raising, same as every other
        failure classifier in this module. Parsed through
        :func:`~runbound.plane_types.from_wire` like every other reply this
        client reads, not by hand: a plane one version ahead sending fields
        this SDK does not know about must not break this classification.
        """
        if getattr(error, "code", None) != 503:
            return "error"
        try:
            raw = json.loads(error.read().decode("utf-8"))
        except Exception:
            return "error"
        if not isinstance(raw, dict) or raw.get("error") != PLANE_LOSS_ERROR_CODE:
            return "error"
        body = from_wire(PlaneUnavailable, raw)
        if body.cause == FLEET_STATE_UNAVAILABLE_CAUSE:
            return "plane_loss"
        return "plane_unavailable"

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
            self.last_failure_kind = None
            if self.key_state != "invalid":
                self.key_state = "valid"

    def _failed(self, path: str, exc_info: bool = False, kind: str = "error") -> None:
        with self._lock:
            self.consecutive_failures += 1
            failures = self.consecutive_failures
            self.last_failure_kind = kind
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
