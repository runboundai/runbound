"""What the SDK could not deliver, counted (AAI-1 FR-4, RFC section 6).

A telemetry lane that fails must never change what a worker enforces, and its loss must not be invisible. This module is the one place the lanes record it:

* ``dropped(lane, reason, n)``: items thrown away because retrying cannot help, by lane (``events``, ``spans``, ``approval_reports``) and reason (``http_400``, ``too_large``,
  ``plane_too_old``, ``ledger_unavailable``, ...);
* ``rejected_spans``: spans the receiver accepted the request for but reported as rejected (OTLP ``partialSuccess.rejectedSpans``);
* ``lane_stopped(lane, reason)`` / ``lane_open(lane)``: a lane that has stopped sending (a rejected key), with when and why.

Counters only; nothing here reads or sends anything. Exposing them to the plane in the hello is a later task (03m).
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_DROPPED: dict = {}
_REJECTED_SPANS = 0
_LANES: dict = {}


def dropped(lane: str, reason: str, n: int = 1) -> None:
    if n <= 0:
        return
    with _LOCK:
        _DROPPED[(lane, reason)] = _DROPPED.get((lane, reason), 0) + int(n)


def rejected_spans(n: int) -> None:
    global _REJECTED_SPANS
    if n > 0:
        with _LOCK:
            _REJECTED_SPANS += int(n)


def lane_stopped(lane: str, reason: str) -> None:
    with _LOCK:
        if _LANES.get(lane, {}).get("state") != "stopped":
            _LANES[lane] = {"state": "stopped", "reason": reason, "since": time.time()}


def lane_open(lane: str) -> None:
    with _LOCK:
        _LANES.pop(lane, None)


def snapshot() -> dict:
    """A copy of every counter: ``{"dropped": {lane: {reason: n}}, "rejected_spans": n, "stopped_lanes": {lane: {reason, since}}}``."""
    with _LOCK:
        by_lane: dict = {}
        for (lane, reason), n in _DROPPED.items():
            by_lane.setdefault(lane, {})[reason] = n
        return {"dropped": by_lane, "rejected_spans": _REJECTED_SPANS, "stopped_lanes": {lane: dict(info) for lane, info in _LANES.items() if info.get("state") == "stopped"}}


def _reset_for_tests() -> None:
    global _REJECTED_SPANS
    with _LOCK:
        _DROPPED.clear()
        _LANES.clear()
        _REJECTED_SPANS = 0
