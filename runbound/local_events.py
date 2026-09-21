"""Local, in-process telemetry ("local telemetry is free"): an
in-memory ring of this process's own
anomalies, refusals, posture transitions and Decisions, read back by
:func:`runbound.events` and :func:`runbound.decisions`.

Nothing here is written to disk, nothing leaves the process, and no record
ever carries a call argument, a prompt or a reply — the same content
independence every :class:`~runbound.events.Anomaly` and
:class:`~runbound.events.Decision` already hold to (invariant 1). This
module is the free, local half of what the control plane's own centralized
event log charges for: durable centralized records, timelines, export.

Two rings, one clock (:func:`time.time`, wall time — these are for a human
or a dashboard to read, never for rate/window logic): ``_EVENTS`` holds
anomalies, refusals and posture transitions; ``_DECISIONS`` holds
:class:`~runbound.events.Decision` bodies. Both are process-lifetime, like
:func:`runbound.coverage`'s own counters — :func:`runbound.init` and
:func:`runbound.reset` do not clear them, only :func:`_teardown_for_tests`
does (through :func:`clear_for_tests`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

_LOG = logging.getLogger("runbound")

#: How many records each ring keeps. ``events(n=100)``/``decisions(n=100)``
#: read the trailing ``n`` of whatever is here; a process that never calls
#: either just accumulates up to this many and drops the oldest.
_CAPACITY = 1000


class _Ring:
    """A bounded, thread-safe FIFO of plain dicts."""

    def __init__(self, capacity: int = _CAPACITY) -> None:
        self._items: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, item: dict) -> None:
        with self._lock:
            self._items.append(item)

    def tail(self, n: int) -> list:
        with self._lock:
            items = list(self._items)
        if n is None or n < 0:
            return items
        if n == 0:
            return []
        return items[-n:]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_EVENTS = _Ring()
_DECISIONS = _Ring()
_LOCK = threading.Lock()
_ON_EVENT: "Callable[[dict], None] | None" = None


def configure(on_event: "Callable[[dict], None] | None") -> None:
    """Set (or clear) this process's ``on_event=`` callback.

    Called from :func:`runbound.init` with ``config.on_event``. The rings
    themselves are not touched — they are process-lifetime, like
    :func:`runbound.coverage`'s counters, so a reconfiguring ``init()`` does
    not erase history a caller may still want to read.
    """
    global _ON_EVENT
    with _LOCK:
        _ON_EVENT = on_event


def clear_for_tests() -> None:
    """Wipe both rings and the callback. Test isolation only."""
    _EVENTS.clear()
    _DECISIONS.clear()
    configure(None)


def _dispatch(record: dict) -> None:
    """Fail-open: a raising ``on_event`` callback never reaches the caller
    whose action produced this record — the same golden rule every other
    observer in this SDK is held to."""
    with _LOCK:
        callback = _ON_EVENT
    if callback is None:
        return
    try:
        callback(dict(record))
    except Exception:
        _LOG.warning("runbound: on_event callback raised; ignoring", exc_info=True)


def record_anomaly(session_id: "str | None", anomaly: Any, reacted: "str | None") -> None:
    """Append one delivered :class:`~runbound.events.Anomaly` as an
    ``"anomaly"`` event, and also as a ``"refusal"`` event when it actually
    stopped *this* call — ``reacted`` is ``"raise"`` (a wall trip) or
    ``"door"`` (one of the envelope's own door stages), the two
    values :meth:`~runbound.engine.Engine.refuse` ever passes in. A
    ``"blocked"`` circuit transition is real news but not a refusal of the
    call that is reporting it (the call already went out); the circuit's
    own admission refusal (:meth:`~runbound.engine.Engine._admit_circuit`)
    raises directly and is deliberately outside :meth:`refuse`, per its own
    docstring."""
    details = dict(getattr(anomaly, "details", None) or {})
    record = {
        "kind": "anomaly",
        "at": time.time(),
        "session_id": session_id,
        "detector": getattr(anomaly, "detector", None),
        "severity": getattr(anomaly, "severity", None),
        "message": getattr(anomaly, "message", None),
        "details": details,
        "reacted": reacted,
    }
    _EVENTS.append(record)
    _dispatch(record)
    if reacted in ("raise", "door"):
        refusal = dict(record)
        refusal["kind"] = "refusal"
        _EVENTS.append(refusal)
        _dispatch(refusal)
    decision = details.get("decision")
    if isinstance(decision, dict):
        stamped = dict(decision)
        stamped.setdefault("at", record["at"])
        _DECISIONS.append(stamped)
        _dispatch({"kind": "decision", **stamped})


def record_posture(source: str, posture: str, reason: object, session_id: "str | None" = None) -> None:
    """Append one posture transition — a manual or automatic narrowing or
    lift, process- or session-scoped."""
    record = {
        "kind": "posture",
        "at": time.time(),
        "session_id": session_id,
        "source": source,
        "posture": posture,
        "reason": reason if isinstance(reason, str) else str(reason),
    }
    _EVENTS.append(record)
    _dispatch(record)


def events(n: int = 100) -> list:
    """The trailing ``n`` anomalies, refusals and posture transitions, in
    the order they happened (oldest of the ``n`` first)."""
    return _EVENTS.tail(n)


def decisions(n: int = 100) -> list:
    """The trailing ``n`` :class:`~runbound.events.Decision` bodies (every
    one stamped onto a refusal by :meth:`~runbound.engine.Engine.refuse`),
    in the order they happened."""
    return _DECISIONS.tail(n)
