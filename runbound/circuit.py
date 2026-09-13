"""The provider circuit breaker: stop paying for a provider that is down.

A retry storm is a money blunder. An agent whose provider answers 429 or 503
retries, the framework around it retries, and the loop above that retries
again — every attempt costing latency, and the successful ones costing money —
while nothing that could possibly work is happening. This module holds the two
pure pieces of the answer: what kind of failure this exception is — the
provider's, the network's, or the caller's own — and has that provider failed
often enough to stop calling it for a while.

It decides nothing about the host's traffic. Opening the circuit is a signal —
the engine alerts on it and, when the customer opted in, the api raises
:class:`~runbound.exceptions.CircuitOpen` before the next call goes out so
the app can run its own fallback. runbound never routes a request itself.

No I/O, no session state, no configuration: plain data in, plain answers out,
with an injectable clock so every window and cooldown is testable by hand.
"""

import threading
import time
from collections import deque
from collections.abc import Callable

#: 4xx statuses that mean "the provider is busy or slow", not "we sent rubbish".
_PROVIDER_SIDE_4XX = frozenset({408, 425, 429})

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

#: The provider could not answer: it was slow, busy, or broken.
PROVIDER = "provider"
#: The request never reached the provider: reset, refused, DNS, TLS.
TRANSPORT = "transport"
#: The caller's own mistake: a bad request, or a bug in their code.
APPLICATION = "application"
#: The host cancelled the call. Not a failure of anything.
CANCEL = "cancel"

#: The two classes a circuit counts. Both mean the *next* call cannot succeed
#: either, which is the only question a breaker asks; the other two say nothing
#: about the provider's health and must never open a circuit.
CIRCUIT_FAULTS = frozenset({PROVIDER, TRANSPORT})

# The name sets below are matched against every class name in an exception's
# MRO, never against an imported type: the SDK must import cleanly with neither
# `openai` nor `anthropic` installed, and a customer who has them installed
# must not get a different answer from a customer who does not. This is the
# same discipline `plane_types.error_class_of` follows for the wire — an
# exception is identified by the name of its class and nothing else.

#: Timeouts, by class name. Includes the builtin (which ``socket.timeout`` and,
#: since 3.11, ``asyncio.TimeoutError`` alias), openai's and anthropic's
#: ``APITimeoutError``, and the httpx/requests/urllib3/aiohttp spellings.
_PROVIDER_NAMES = frozenset(
    {
        "TimeoutError",
        "APITimeoutError",
        "Timeout",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ServerTimeoutError",
    }
)

#: Connection failures, by class name. ``OSError`` is the builtin root of
#: ``ConnectionError``, ``socket.gaierror``, ``ssl.SSLError`` and
#: ``urllib.error.URLError``, so the MRO walk catches those whether or not they
#: are spelled out; the rest are the HTTP stacks' own hierarchies, which hang
#: off ``Exception`` instead.
_TRANSPORT_NAMES = frozenset(
    {
        "OSError",
        "ConnectionError",
        "gaierror",
        "herror",
        "SSLError",
        "APIConnectionError",
        "ConnectError",
        "NetworkError",
        "ProtocolError",
        "RemoteProtocolError",
        "ProxyError",
        "ReadError",
        "WriteError",
        "NewConnectionError",
        "MaxRetryError",
        "IncompleteRead",
        "ClientConnectionError",
        "ClientConnectorError",
        "ServerDisconnectedError",
    }
)

#: The caller's own mistakes, by class name. ``ValidationError`` is pydantic's
#: — a request the customer's code built wrong, which no amount of waiting
#: fixes.
_APPLICATION_NAMES = frozenset({"TypeError", "ValueError", "KeyError", "ValidationError"})

#: Cancellation, by class name. ``asyncio.CancelledError`` (which
#: ``concurrent.futures.CancelledError`` aliases) is a ``BaseException``, so it
#: normally travels past the wrappers untouched; naming it here is the second
#: lock on a door that is already shut.
_CANCEL_NAMES = frozenset({"CancelledError"})


def classify_failure(exc: BaseException) -> str:
    """What kind of failure ``exc`` is: the answer a circuit is asking for.

    One of :data:`PROVIDER` (the provider was slow, busy or broken: status 408,
    425, 429 or any 5xx, and every flavour of timeout), :data:`TRANSPORT` (the
    request never got there: reset, refused, DNS, TLS), :data:`APPLICATION`
    (the caller's own mistake: ``TypeError``, ``ValueError``, ``KeyError``,
    pydantic's ``ValidationError``, and any 4xx that is not one of the three
    above) or :data:`CANCEL` (the host cancelled; nothing failed).

    Read in that order: a readable status decides first, because a status means
    the provider answered and its own verdict beats any guess of ours; then the
    class name, walking the whole MRO, so ``anthropic.APITimeoutError`` is a
    timeout before it is the ``APIConnectionError`` it inherits from, and
    ``ssl.SSLCertVerificationError`` is transport before it is the
    ``ValueError`` it also inherits from. Provider SDK types are matched by
    class *name*, never imported: the answer is the same whether or not the
    customer has those packages installed.

    Two edge cases are deliberate. An exception carrying a ``status_code`` that
    cannot be read is :data:`PROVIDER`: it came off an HTTP response, and that
    is more than we know about anything else. Everything else unrecognized is
    :data:`APPLICATION` — the failures a retry storm is made of all have a
    status, a timeout or a socket underneath them, so what is left is
    overwhelmingly the host's own code, and refusing to open a circuit over an
    exception we do not understand is the fail-open answer.

    Never raises: an exception whose ``__class__`` lies, whose attributes throw
    or which is not an exception at all is :data:`APPLICATION`.
    """
    try:
        names = _class_names(exc)
        if names & _CANCEL_NAMES:
            return CANCEL
        status = _status_of(exc)
        if status is not None:
            if status in _PROVIDER_SIDE_4XX or status >= 500:
                return PROVIDER
            if 400 <= status < 500:
                return APPLICATION
        if names & _PROVIDER_NAMES:
            return PROVIDER
        if names & _TRANSPORT_NAMES:
            return TRANSPORT
        if names & _APPLICATION_NAMES:
            return APPLICATION
        if _carries_status(exc):
            return PROVIDER
        return APPLICATION
    except BaseException:  # pragma: no cover - the classifier owes an answer
        return APPLICATION


def is_provider_failure(exc: BaseException) -> bool:
    """Does ``exc`` count against the provider's circuit?

    True for :data:`PROVIDER` and :data:`TRANSPORT` alike: a provider that
    answers 503 and a connection that never reaches it both mean the next call
    cannot succeed, and failing fast is right either way. False for the
    caller's own mistakes and for cancellation, which say nothing about the
    provider's health — opening a circuit over those would stop calls to a
    provider that is working perfectly.

    See :func:`classify_failure`, which decides; this only reads its answer.
    """
    return classify_failure(exc) in CIRCUIT_FAULTS


def _class_names(exc: BaseException) -> frozenset[str]:
    """Every class name in ``exc``'s inheritance chain, bases included.

    Read from ``type(exc)`` rather than ``exc.__class__``, which an object may
    define as anything it likes; an empty set when the chain cannot be read.
    """
    try:
        return frozenset(base.__name__ for base in type(exc).__mro__)
    except Exception:
        return frozenset()


def _status_of(exc: BaseException) -> int | None:
    """``exc.status_code`` as an int, or ``None`` if it has no usable one."""
    try:
        status = getattr(exc, "status_code", None)
        return None if status is None else int(status)
    except Exception:
        return None


def _carries_status(exc: BaseException) -> bool:
    """Does ``exc`` carry a ``status_code`` at all, readable or not?

    An attribute that raises when read still answers the only question here —
    this object was built from an HTTP response — so it counts as carried.
    """
    try:
        return getattr(exc, "status_code", None) is not None
    except Exception:
        return True


class _Key:
    """One provider's failure history and, when it is open, since when.

    ``until`` is set only when somebody forced the circuit open: it is the
    absolute deadline that replaces the configured cooldown for this opening.
    """

    __slots__ = ("failures", "opened_at", "probe_taken", "until")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()
        self.opened_at: float | None = None
        self.probe_taken = False
        self.until: float | None = None


class CircuitBreaker:
    """Failure counting and cooldown per provider key, thread-safe.

    Three states per key. **Closed**: everything is allowed and failures inside
    the trailing ``window_seconds`` are counted; the ``failure_threshold``-th
    one opens it. **Open**: nothing is allowed until ``cooldown_seconds`` have
    passed. **Half-open**: exactly one probe is allowed through — its result
    decides, closing the breaker on success and re-opening it with a fresh
    cooldown on failure, while every other caller is still refused.

    An operator can also put a key into either resting state by hand —
    :meth:`force_open` and :meth:`force_close` — and :meth:`snapshot` reads
    every key back as plain data.

    Agents call from threads and from asyncio tasks, so all of it happens under
    one lock; nothing here does I/O or calls user code while holding it.
    """

    def __init__(
        self,
        failure_threshold: int,
        window_seconds: float,
        cooldown_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = int(failure_threshold)
        self.window_seconds = float(window_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self._now = now
        self._lock = threading.Lock()
        self._keys: dict[str, _Key] = {}

    def record_failure(self, key: str) -> bool:
        """Count one failure for ``key``. True only if *this* one opened it.

        A failure while the breaker is already open (a probe that failed, or a
        call that was in flight when it opened) restarts the cooldown rather
        than opening it again — the on-call was told the first time, and one
        provider outage is one incident.
        """
        now = self._now()
        with self._lock:
            entry = self._keys.setdefault(key, _Key())
            if entry.opened_at is not None:
                self._open(entry, now)
                return False
            self._prune(entry, now)
            entry.failures.append(now)
            if len(entry.failures) < self.failure_threshold:
                return False
            self._open(entry, now)
            return True

    def record_success(self, key: str) -> None:
        """Report a call that worked: the breaker closes and forgets.

        A working provider is a working provider: this closes a circuit that
        was forced open too, and drops its remaining hold.
        """
        with self._lock:
            entry = self._keys.get(key)
            if entry is None:
                return
            self._close(entry)

    def force_open(self, key: str, until_s: float | None = None) -> None:
        """Stop calling ``key`` for ``until_s`` seconds, whatever its history.

        For an operator (or the control plane on their behalf) saying "this
        provider is down, stop paying to find out". ``None`` holds it for the
        configured ``cooldown_seconds``. The hold behaves exactly like a
        natural opening: when it expires the circuit half-opens and one probe
        decides, a success closes it early, and a failure meanwhile restarts
        the cooldown without ever ending a longer hold early.
        """
        now = self._now()
        hold = self.cooldown_seconds if until_s is None else float(until_s)
        with self._lock:
            entry = self._keys.setdefault(key, _Key())
            entry.opened_at = now
            entry.probe_taken = False
            entry.failures.clear()
            entry.until = now + hold

    def force_close(self, key: str) -> None:
        """Resume calling ``key`` now: the circuit closes and forgets.

        Clears the failure window as well as the opening, so the next failure
        starts a fresh count rather than immediately re-opening the circuit.
        An unknown key is already closed and nothing happens.
        """
        with self._lock:
            entry = self._keys.get(key)
            if entry is not None:
                self._close(entry)

    def allow(self, key: str) -> bool:
        """May a call to ``key`` go out right now?

        True while closed, False while open, and True for exactly one caller
        once the cooldown has elapsed — the probe. Everyone else keeps being
        refused until that probe reports back through
        :meth:`record_success` or :meth:`record_failure`.
        """
        now = self._now()
        with self._lock:
            entry = self._keys.get(key)
            if entry is None or entry.opened_at is None:
                return True
            if now < self._deadline(entry):
                return False
            if entry.probe_taken:
                return False
            entry.probe_taken = True
            return True

    def state(self, key: str) -> str:
        """``"closed"``, ``"open"`` or ``"half_open"`` for one provider."""
        now = self._now()
        with self._lock:
            entry = self._keys.get(key)
            return self._state(entry, now)

    def snapshot(self) -> dict[str, dict]:
        """Every key this breaker knows about, as plain data.

        ``{key: {"state", "until_s_remaining", "failures"}}`` — the state as
        :meth:`state` reports it, the seconds left before an open circuit may
        be probed (0.0 when it is not open), and how many failures are still
        inside the trailing window. A fresh copy the caller may keep, hand to
        the control plane, or print.
        """
        now = self._now()
        with self._lock:
            return {
                key: {
                    "state": self._state(entry, now),
                    "until_s_remaining": max(0.0, self._deadline(entry) - now)
                    if entry.opened_at is not None
                    else 0.0,
                    "failures": sum(
                        1 for at in entry.failures if at >= now - self.window_seconds
                    ),
                }
                for key, entry in self._keys.items()
            }

    def _state(self, entry: "_Key | None", now: float) -> str:
        """One entry's state at ``now``. Caller holds the lock."""
        if entry is None or entry.opened_at is None:
            return CLOSED
        return OPEN if now < self._deadline(entry) else HALF_OPEN

    def _deadline(self, entry: _Key) -> float:
        """When an open circuit may be probed: a forced hold, else the cooldown."""
        if entry.until is not None:
            return entry.until
        return (entry.opened_at or 0.0) + self.cooldown_seconds

    def _open(self, entry: _Key, now: float) -> None:
        """Open (or re-open) one key's breaker. Caller holds the lock.

        A failure restarts the cooldown but never shortens a forced hold that
        outlasts it — the operator's instruction stands until it expires.
        """
        entry.opened_at = now
        entry.probe_taken = False
        entry.failures.clear()
        if entry.until is not None and entry.until <= now + self.cooldown_seconds:
            entry.until = None

    def _close(self, entry: _Key) -> None:
        """Close one key's breaker and forget its failures. Caller holds the lock."""
        entry.failures.clear()
        entry.opened_at = None
        entry.probe_taken = False
        entry.until = None

    def _prune(self, entry: _Key, now: float) -> None:
        """Drop failures that have aged out of the window. Caller holds the lock."""
        cutoff = now - self.window_seconds
        while entry.failures and entry.failures[0] < cutoff:
            entry.failures.popleft()
