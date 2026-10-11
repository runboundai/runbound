"""Local, in-process telemetry ("local telemetry is free"): an
in-memory ring of this process's own
anomalies, refusals and Decisions, read back by
:func:`runbound.events` and :func:`runbound.decisions`.

Nothing here is written to disk, nothing leaves the process on its own
(a connected exporter is a sink, below), and no record ever carries a call
argument, a prompt or a reply — the same content
independence every :class:`~runbound.events.Anomaly` and
:class:`~runbound.events.Decision` already hold to (invariant 1). This
module is the free, local half of what the control plane's own centralized
event log charges for: durable centralized records, timelines, export.

Two rings, one clock (:func:`time.time`, wall time — these are for a human
or a dashboard to read, never for rate/window logic): ``_EVENTS`` holds
anomalies, refusals and runtime changes (a model,
provider, policy version or Controls version this process runs on moving
from one value to another); ``_DECISIONS`` holds
:class:`~runbound.events.Decision` bodies. Both are process-lifetime, like
:func:`runbound.coverage`'s own counters — :func:`runbound.init` and
:func:`runbound.reset` do not clear them, only :func:`_teardown_for_tests`
does (through :func:`clear_for_tests`).

Besides the ``on_event=`` callback, in-process listeners can register as
*sinks* (:func:`add_sink`): the exporter is one, and it is how a
runtime change reaches a connected control plane. Every sink
is fail-open, exactly like the callback.
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
#: ``tool_result`` records: one per tool call, so a ring of their own, or they would push every
#: refusal out of ``_EVENTS``. Read by :func:`tool_results`.
_RESULTS = _Ring()
_LOCK = threading.Lock()
_ON_EVENT: "Callable[[dict], None] | None" = None
_SINKS: "list[Callable[[dict], None]]" = []
#: The last value seen per runtime ``what`` ("model", "provider",
#: "policy_version", ...), for :func:`note_runtime_value`.
_LAST_VALUES: dict = {}


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


def add_sink(sink: "Callable[[dict], None]") -> None:
    """Tell ``sink`` about every record from now on, after ``on_event``.

    Adding the same callable twice registers it once.
    """
    with _LOCK:
        if sink not in _SINKS:
            _SINKS.append(sink)


def remove_sink(sink: "Callable[[dict], None]") -> None:
    """Stop telling ``sink``. Removing one that is not registered is a no-op."""
    with _LOCK:
        if sink in _SINKS:
            _SINKS.remove(sink)


def clear_for_tests() -> None:
    """Wipe both rings, the callback, the sinks and every last runtime
    value. Test isolation only."""
    _EVENTS.clear()
    _DECISIONS.clear()
    _RESULTS.clear()
    configure(None)
    with _LOCK:
        _SINKS.clear()
        _LAST_VALUES.clear()


def _dispatch(record: dict, key: "str | None" = None) -> None:
    """Fail-open: a raising ``on_event`` callback or sink never reaches the
    caller whose action produced this record, and never stops the next sink
    from hearing it — the same golden rule every other observer in this SDK
    is held to.

    ``key`` is the raw session key a record is about, when the record itself
    carries none (an anomaly): it goes to the sinks only, as ``"key"``, so a
    sink that leaves the process can redact it — never into a ring or to
    ``on_event``."""
    with _LOCK:
        callback = _ON_EVENT
        sinks = list(_SINKS)
    if callback is not None:
        try:
            callback(dict(record))
        except Exception:
            _LOG.warning("runbound: on_event callback raised; ignoring", exc_info=True)
    sink_record = record if key is None or "key" in record else {**record, "key": key}
    for sink in sinks:
        try:
            sink(dict(sink_record))
        except Exception:
            _LOG.warning("runbound: an event sink raised; ignoring", exc_info=True)


def record_anomaly(
    session_id: "str | None", anomaly: Any, reacted: "str | None", key: "str | None" = None
) -> None:
    """Append one delivered :class:`~runbound.events.Anomaly` as an
    ``"anomaly"`` event, and also as a ``"refusal"`` event when it actually
    stopped *this* call — ``reacted`` is ``"raise"`` (a wall trip) or
    ``"door"`` (one of the envelope's own door stages), the two
    values :meth:`~runbound.engine.Engine.refuse` ever passes in. A
    ``"blocked"`` transition is real news but not a refusal of the
    call that is reporting it (the call already went out)."""
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
    _dispatch(record, key)
    if reacted in ("raise", "door", "blocked"):
        refusal = dict(record)
        refusal["kind"] = "refusal"
        _EVENTS.append(refusal)
        _dispatch(refusal, key)
    decision = details.get("decision")
    if isinstance(decision, dict):
        stamped = dict(decision)
        stamped.setdefault("at", record["at"])
        _DECISIONS.append(stamped)
        _dispatch({"kind": "decision", **stamped}, key)


def record_result(session_id: "str | None", key: "str | None", event: Any) -> None:
    """Append one ``tool_result`` (how a tool call ended) to its own ring, and tell ``on_event``
    and the sinks. The record says how long, how it ended and what the post chain did; it never
    carries the result or an argument."""
    record = {
        "kind": "tool_result",
        "at": time.time(),
        "session_id": session_id,
        "tool": event.tool_name,
        "outcome": event.outcome,
        "duration_s": event.duration_s,
        "flags": list(event.flags),
        "flag_score": event.flag_score,
        "flagged": event.flagged,
        "tags": list(event.tags),
        "truncated": event.truncated,
        "result_chars": event.result_chars,
        "result_chars_out": event.result_chars_out,
    }
    _RESULTS.append(record)
    _dispatch(record, key)


def record_runtime_change(what: str, previous: object, current: object) -> None:
    """Append one ``"runtime_change"``: ``what`` moved from ``previous`` to
    ``current``.

    ``what`` names a value this process runs on, never anything a call
    carried: a model or provider label, a policy or Controls version.
    """
    record = {
        "kind": "runtime_change",
        "at": time.time(),
        "what": what,
        "from": previous,
        "to": current,
    }
    _EVENTS.append(record)
    _dispatch(record)


def record_plane_degraded(previous: "str | None", current: "str | None") -> None:
    """Append one ``"plane_degraded"``: what the plane says is wrong with itself moved from ``previous`` to ``current``
    (``None`` is "nothing"; ``current is None`` is the plane saying it recovered). Local only: it is not exported."""
    record = {
        "kind": "plane_degraded",
        "at": time.time(),
        "from": previous,
        "to": current,
        "cleared": current is None,
    }
    _EVENTS.append(record)
    _dispatch(record)


def note_runtime_value(what: str, value: object) -> bool:
    """Remember ``value`` as the current ``what``; record a change if it moved.

    True when a ``"runtime_change"`` was recorded. Never on first sight — a
    process that has only ever seen one model has nothing to have changed
    from — and never while the value stays the same. ``None`` is "not known
    for this call": it is neither recorded nor remembered, so a call that
    names no model does not hide the change between the calls around it.
    Process-wide, like the rings.
    """
    if value is None:
        return False
    with _LOCK:
        previous = _LAST_VALUES.get(what)
        _LAST_VALUES[what] = value
    if previous is None or previous == value:
        return False
    record_runtime_change(what, previous, value)
    return True


def events(n: int = 100) -> list:
    """The trailing ``n`` anomalies, refusals, runtime changes and
    ``plane_degraded`` transitions, in the order they happened (oldest of the ``n``
    first)."""
    return _EVENTS.tail(n)


def tool_results(n: int = 100) -> list:
    """The trailing ``n`` ``tool_result`` records, oldest of the ``n`` first."""
    return _RESULTS.tail(n)


def decisions(n: int = 100) -> list:
    """The trailing ``n`` :class:`~runbound.events.Decision` bodies (every
    one stamped onto a refusal by :meth:`~runbound.engine.Engine.refuse`),
    in the order they happened."""
    return _DECISIONS.tail(n)
