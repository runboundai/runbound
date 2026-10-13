"""The refusal record cap: how many identical refusals one session records one by one.

A refusal is evidence, so every one is exported as its own anomaly and Decision,
up to :data:`REFUSAL_RECORD_CAP` per ``(session, detector, rule, tool)``. Past it
they are counted, and one summary anomaly per key carrying
``details["suppressed_count"]`` is reported when the session block exits. Two
rules, or two tools, storming at once never hide each other.
"""

import threading
from collections import OrderedDict

from .events import Anomaly

REFUSAL_RECORD_CAP = 100

#: How many ``(session, detector, rule, tool)`` counters one log keeps.
_COUNTERS_MAX = 20_000

#: The details a summary keeps of the refusals it stands for (never a Decision).
_SHAPE_KEYS = ("session_id", "key", "tags", "tool", "rule")


def _detail(anomaly: Anomaly, key: str):
    details = anomaly.details
    return details.get(key) if isinstance(details, dict) else None


class RefusalLog:
    """Counts refusals per ``(session, detector, rule, tool)``; thread-safe."""

    def __init__(self) -> None:
        # key -> [refusals seen, suppressed since the last summary, reacted, shape]
        self._counts: "OrderedDict[tuple, list]" = OrderedDict()
        self._lock = threading.Lock()

    def admit(self, session_id: str, anomaly: Anomaly, reacted: str | None) -> bool:
        """Count one refusal; True while it is still within the record cap."""
        # a dry run keeps its own budget: a row promoted to enforced in the middle of a session starts counting afresh
        key = (session_id, anomaly.detector, _detail(anomaly, "rule"), _detail(anomaly, "tool"), reacted == "dry_run")
        with self._lock:
            entry = self._counts.get(key)
            if entry is None:
                shape = {k: v for k, v in (anomaly.details or {}).items() if k in _SHAPE_KEYS}
                entry = [0, 0, reacted or "blocked", (anomaly.detector, shape)]
                self._counts[key] = entry
                while len(self._counts) > _COUNTERS_MAX:
                    self._counts.popitem(last=False)
            else:
                self._counts.move_to_end(key)
            entry[0] += 1
            if entry[0] <= REFUSAL_RECORD_CAP:
                return True
            entry[1] += 1
            return False

    def summaries(self, session_id: str) -> "list[tuple[Anomaly, str]]":
        """``(summary anomaly, reacted)`` for what this session suppressed since the last call."""
        due = []
        with self._lock:
            for key, entry in self._counts.items():
                if key[0] == session_id and entry[1] > 0:
                    due.append((entry[1], entry[2], entry[3]))
                    entry[1] = 0
        return [(_summary(shape, count, reacted), reacted) for count, reacted, shape in due]


def _summary(shape: tuple, count: int, reacted: "str | None" = None) -> Anomaly:
    """The one anomaly that stands for ``count`` refusals past the record cap.

    A dry run's summary says so: it stands for calls that WOULD have been refused (and ran), reacted ``dry_run``, with
    ``details["dry_run"] = True``, so nothing can read it as a refusal.
    """
    detector, base = shape
    details = dict(base)
    details["suppressed_count"] = int(count)
    dry = reacted == "dry_run"
    if dry:
        details["dry_run"] = True
    tool = details.get("tool")
    return Anomaly(
        detector=detector,
        severity="warn",
        message=(
            f"{count} more dry-run would-refuse calls of the same kind ({detector}"
            f"{'' if tool is None else ', tool ' + repr(tool)}); they ran, and were not recorded one by one"
            if dry
            else f"{count} more refusals of the same kind ({detector}"
            f"{'' if tool is None else ', tool ' + repr(tool)}) were not recorded one by one"
        ),
        details=details,
    )
