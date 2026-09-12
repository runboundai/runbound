"""The provider circuit breaker: stop paying for a provider that is down.

A retry storm is a money blunder. An agent whose provider answers 429 or 503
retries, the framework around it retries, and the loop above that retries
again — every attempt costing latency, and the successful ones costing money —
while nothing that could possibly work is happening. This module holds the two
pure pieces of the answer: is this exception the *provider's* fault, and has
that provider failed often enough to stop calling it for a while.

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


def is_provider_failure(exc: BaseException) -> bool:
    """Is ``exc`` the provider failing, rather than the caller's own mistake?

    Duck-typed on ``status_code``, so every OpenAI-compatible SDK and every
    hand-rolled client is classified by the same rule and no provider package
    is imported: 408, 425 and 429 (busy, too early, rate limited) and anything
    in 5xx are the provider; every other 4xx is a bad request, a bad key or a
    missing model, and retrying it or opening a circuit over it would hide a
    bug in the caller's own code.

    Anything with no readable status — a timeout, a dropped connection, an SDK
    error object that raises when read — counts as the provider. Those are the
    failures a retry storm is actually made of, and the classifier's job is to
    be useful, not to be certain.
    """
    status = _status_of(exc)
    if status is None:
        return True
    if status in _PROVIDER_SIDE_4XX or status >= 500:
        return True
    return not 400 <= status < 500


def _status_of(exc: BaseException) -> int | None:
    """``exc.status_code`` as an int, or ``None`` if it has no usable one."""
    try:
        status = getattr(exc, "status_code", None)
        return None if status is None else int(status)
    except Exception:
        return None


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
