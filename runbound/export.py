"""Telemetry export — batches what the engine sees and ships it to the plane.

The exporter is an *observer*: the engine calls it after it has already done
its work, and nothing it does can change what the agent sees. That shapes
every decision here.

* **Never raises.** Every observer method catches everything; a record that
  cannot be encoded is counted and dropped, never propagated.
* **Never blocks the caller.** Observer methods only append to an in-memory
  deque; a daemon thread does the posting. Telemetry is lossy on purpose.
* **Bounded.** Each lane is a deque of ``maxlen``; when it is full the oldest
  record is dropped and ``dropped`` counts it. A plane that is down for an
  hour costs a fixed amount of memory, not an OOM.
* **Retried, not dropped.** A batch the client could not deliver goes back to
  the front of its lanes, intact and in order, instead of being thrown away —
  the whole point of surviving an outage is that the backlog drains once the
  plane comes back. Retries back off exponentially so a dead plane is polled
  less often, not hammered; an invalid key is the one failure that still
  drops on the spot, since retrying past a rejected key would never help.
* **Trips first.** Exits, trips and circuit transitions go to a priority lane
  that is drained before ordinary events, so the record of an incident is not
  stuck behind a queue of routine token counts.

At interpreter exit the queue is drained once, within the same few seconds the
alert module already budgets for: the flusher thread is registered through
:func:`runbound.alerts._track` so that drain waits for it too, and this
module's own hook — registered later, so it runs first — stops the thread and
posts what is left.

``dropped`` is cumulative for the life of the process and is reported on every
batch, so the plane can see loss as a counter rather than a one-off event.
"""

import atexit
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from . import alerts
from .plane import WARN_INTERVAL_S, _PeriodicWarning, warn_periodically
from .plane_types import ExitDelta, anomaly_to_wire, event_to_wire, key_hash, to_wire

_LOG = logging.getLogger("runbound")

#: Total seconds a drain (at stop, or at exit) may spend posting.
DRAIN_TIMEOUT_S = 3.0

#: Backoff schedule while the plane keeps refusing batches: doubles from here,
#: capped at the max, and reset to the normal flush cadence on success — so a
#: dead plane is polled less and less often instead of hammered.
_BACKOFF_INITIAL_S = 0.5
_BACKOFF_MAX_S = 10.0

#: Wall clock used for the ISO-8601 stamps. Read off the module namespace at
#: call time, so a test can freeze it.
_wall = time.time


def _iso(wall_seconds: float) -> str:
    """Format a wall-clock epoch as ISO-8601 UTC with a ``Z`` suffix."""
    stamp = datetime.fromtimestamp(wall_seconds, tz=timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Exporter:
    """Buffers observations and posts them to the control plane in batches.

    ``now`` is the monotonic clock (used to age events into wall time and to
    rate-limit warnings); ``flush_every_s`` is how long the flusher waits when
    nothing has filled a batch, and a full batch wakes it immediately.

    ``include_events`` is the customer's ``export_events`` setting, and it
    covers telemetry only: with it off, events and detector verdicts are
    dropped where they are handed over, while exits, circuit transitions and
    trips still queue and still ship. Those are fleet *state* — a worker that
    stopped reporting its exits would stop contributing to the shared spend,
    which is the one thing fleet mode exists to get right.

    ``send_session_keys`` is the customer's setting of the same name, and off
    (the default) it is what keeps a raw key out of an exported anomaly: see
    :func:`~runbound.plane_types.anomaly_to_wire`.
    """

    def __init__(
        self,
        client,
        maxlen: int = 10_000,
        flush_every_s: float = 1.0,
        batch_size: int = 200,
        now: Callable[[], float] = time.monotonic,
        include_events: bool = True,
        send_session_keys: bool = False,
    ) -> None:
        self._client = client
        #: Whether an exported anomaly may carry raw session keys.
        self.send_session_keys = bool(send_session_keys)
        #: Whether the telemetry lanes are open. Read by
        #: :class:`~runbound.shared.RemoteState` too, which installs this as
        #: an engine observer only when there is something for it to observe.
        self.include_events = bool(include_events)
        self._maxlen = max(int(maxlen), 1)
        self._flush_every_s = max(float(flush_every_s), 0.0)
        self._batch_size = max(int(batch_size), 1)
        self._now = now
        self._events: deque = deque()
        self._anomalies: deque = deque()
        self._priority: deque = deque()  # (lane, wire) with lane in exits|circuits
        self.dropped = 0
        #: Consecutive failed posts, for diagnostics and the backoff below.
        #: Reset to 0 on the next successful post.
        self.consecutive_failures = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._exit_registered = False
        self._warning = _PeriodicWarning(WARN_INTERVAL_S, now)

    # --- the observer protocol -------------------------------------------

    def on_event(self, session, event) -> None:
        """Queue one observed event. Never raises.

        Nothing at all with ``include_events`` off, and not a drop either: an
        event the customer asked us not to export is not telemetry we lost.
        """
        if not self.include_events or self._discarding():
            return
        try:
            wire = to_wire(
                event_to_wire(event, self._key_hash(session), self._stamp_for(event.ts))
            )
        except Exception:
            self._unencodable()
            return
        self._enqueue(self._events, wire)

    def on_anomaly(self, session, anomaly, reacted: str) -> None:
        """Queue one detector verdict and how the SDK reacted. Never raises.

        Telemetry, so ``include_events`` off silences it. The trip that a
        latch is made of travels on :meth:`on_trip` instead, which does not
        ask.
        """
        if not self.include_events:
            return
        self._queue_anomaly(session, anomaly, reacted)

    def on_trip(self, session, anomaly, reacted: str) -> None:
        """Queue a trip the plane would not take synchronously. Never raises.

        The fallback behind :meth:`runbound.shared.RemoteState.trip`: a trip
        is what makes the fleet's other workers refuse a key, so it is queued
        whatever ``include_events`` says. Same lane and same wire form as an
        exported anomaly — the plane reads one shape, not two.
        """
        self._queue_anomaly(session, anomaly, reacted)

    def _queue_anomaly(self, session, anomaly, reacted: str) -> None:
        """Put one anomaly, in its wire form, on the anomalies lane.

        The session's key goes to :func:`~runbound.plane_types.anomaly_to_wire`
        so it can be redacted out of the message and the details — that is the
        only reason the raw key is read here at all.
        """
        if self._discarding():
            return
        try:
            key = getattr(session, "key", None)
            wire = to_wire(
                anomaly_to_wire(
                    anomaly,
                    reacted,
                    None if key is None else key_hash(key),
                    _iso(_wall()),
                    send_session_keys=self.send_session_keys,
                    key=key,
                )
            )
        except Exception:
            self._unencodable()
            return
        self._enqueue(self._anomalies, wire)

    def on_exit(self, delta: ExitDelta) -> None:
        """Queue a session's spend/token delta on the priority lane."""
        if self._discarding():
            return
        try:
            wire = to_wire(delta)
        except Exception:
            self._unencodable()
            return
        self._enqueue(self._priority, ("exits", wire))

    def on_circuit(self, label, state, failures, cooldown_s) -> None:
        """Queue a provider circuit transition on the priority lane."""
        if self._discarding():
            return
        try:
            wire = {
                "ts_wall": _iso(_wall()),
                "label": str(label),
                "state": str(state),
                "failures": int(failures),
                "cooldown_s": float(cooldown_s),
            }
        except Exception:
            self._unencodable()
            return
        self._enqueue(self._priority, ("circuits", wire))

    # --- the queue --------------------------------------------------------

    @property
    def pending(self) -> int:
        """How many records are waiting to be posted, across all lanes."""
        with self._lock:
            return len(self._events) + len(self._anomalies) + len(self._priority)

    def backlog(self) -> int:
        """Diagnostic alias for :attr:`pending`: how much is waiting to ship."""
        return self.pending

    def _enqueue(self, queue: deque, item) -> None:
        with self._lock:
            if len(queue) >= self._maxlen:
                queue.popleft()
                self.dropped += 1
            queue.append(item)
            pending = len(self._events) + len(self._anomalies) + len(self._priority)
        if pending >= self._batch_size:
            self._wake.set()

    def _key_hash(self, session) -> str | None:
        key = getattr(session, "key", None)
        return None if key is None else key_hash(key)

    def _stamp_for(self, ts: float) -> str:
        """Wall-clock stamp for a monotonic event timestamp."""
        return _iso(_wall() - (self._now() - ts))

    def _discarding(self) -> bool:
        """True when the plane has rejected our key: count, drop, stay off the wire."""
        if getattr(self._client, "key_state", None) != "invalid":
            return False
        with self._lock:
            self.dropped += 1
        return True

    def _unencodable(self) -> None:
        with self._lock:
            self.dropped += 1
        warn_periodically(
            self._warning,
            "runbound: export could not encode a record; dropping it",
            exc_info=True,
        )

    def _take(self) -> tuple[dict, list, list, list] | None:
        """Pop up to ``batch_size`` records — priority, then anomalies, then events.

        Returns the wire batch alongside the raw per-lane lists that made it,
        so a failed post can hand them straight back to :meth:`_requeue`.
        """
        with self._lock:
            budget = self._batch_size
            priority = []
            while self._priority and budget:
                priority.append(self._priority.popleft())
                budget -= 1
            anomalies = []
            while self._anomalies and budget:
                anomalies.append(self._anomalies.popleft())
                budget -= 1
            events = []
            while self._events and budget:
                events.append(self._events.popleft())
                budget -= 1
            dropped = self.dropped
        if not (priority or anomalies or events):
            return None
        batch = {
            "service": getattr(self._client, "service", ""),
            "worker_id": getattr(self._client, "worker_id", ""),
            "sent_at": _iso(_wall()),
            "events": events,
            "anomalies": anomalies,
            "exits": [wire for lane, wire in priority if lane == "exits"],
            "circuits": [wire for lane, wire in priority if lane == "circuits"],
            "dropped": dropped,
        }
        return batch, priority, anomalies, events

    def _abandon_queue(self) -> None:
        """Throw the queue away, counting it, when the key has been rejected."""
        with self._lock:
            self.dropped += len(self._events) + len(self._anomalies) + len(self._priority)
            self._events.clear()
            self._anomalies.clear()
            self._priority.clear()

    def _requeue(self, priority: list, anomalies: list, events: list) -> None:
        """Put a batch the plane refused back, intact and in order.

        Each lane gets its own slice back at the front — ahead of anything
        queued while the post was in flight — so retrying does not reorder
        what is waiting. Still bounded by ``maxlen``: if the lane overflows
        (new records kept arriving during the failed attempt), the oldest
        records lose the room, same accounting as :meth:`_enqueue`.
        """
        with self._lock:
            self._requeue_lane(self._priority, priority)
            self._requeue_lane(self._anomalies, anomalies)
            self._requeue_lane(self._events, events)

    def _requeue_lane(self, queue: deque, items: list) -> None:
        """Put ``items`` back at the front of ``queue``, oldest first."""
        for item in reversed(items):
            queue.appendleft(item)
        while len(queue) > self._maxlen:
            queue.popleft()
            self.dropped += 1

    # --- posting ----------------------------------------------------------

    def flush(self, timeout: float = 3.0) -> None:
        """Post queued batches on the calling thread, for at most ``timeout``.

        The budget is checked between batches, so one slow POST can overrun it
        by that POST's own timeout and no more. Draining continues only while
        batches keep landing: the first one the plane refuses is put back —
        intact, at the front of its lanes — and this call stops rather than
        hammering a plane that is down. The next scheduled flush (the
        background thread's backoff wait, or another explicit call) picks the
        backlog back up.
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            if getattr(self._client, "key_state", None) == "invalid":
                self._abandon_queue()
                return
            taken = self._take()
            if taken is None:
                return
            batch, priority, anomalies, events = taken
            if self._post(batch):
                self._on_success()
            else:
                self._requeue(priority, anomalies, events)
                self._on_failure()
                self._warn_pending()
                return
            if time.monotonic() >= deadline:
                return

    def _post(self, batch: dict) -> bool:
        """Post one batch. Never raises.

        ``True`` means the batch is gone — delivered, or dropped because the
        client broke its documented no-raise contract (a client bug, not a
        down plane, so it is warned about and dropped rather than retried
        forever). ``False`` means the plane simply refused it and it must be
        requeued.
        """
        try:
            delivered = self._client.events(batch)
        except Exception:
            warn_periodically(
                self._warning, "runbound: export batch could not be sent", exc_info=True
            )
            return True
        return bool(delivered)

    def _on_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0

    def _on_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1

    def _backoff_s(self) -> float:
        """How long the flusher should wait before trying again.

        The normal cadence while things are healthy; once failures start,
        this doubles from :data:`_BACKOFF_INITIAL_S`, capped at
        :data:`_BACKOFF_MAX_S`, so a dead plane is polled less and less often
        instead of hammered.
        """
        if self.consecutive_failures <= 0:
            return self._flush_every_s
        delay = _BACKOFF_INITIAL_S * (2 ** (self.consecutive_failures - 1))
        return min(delay, _BACKOFF_MAX_S)

    def _warn_pending(self) -> None:
        remaining = self.backlog()
        if not remaining:
            return
        warn_periodically(
            self._warning,
            "runbound: control plane unreachable; %d telemetry record(s) pending",
            remaining,
        )

    # --- the flusher thread -----------------------------------------------

    def start(self) -> None:
        """Start the flusher thread, once. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        thread = threading.Thread(target=self._run, name="runbound-exporter", daemon=True)
        self._thread = thread
        thread.start()
        try:
            alerts._track(thread)
        except Exception:
            _LOG.warning("runbound: exporter thread could not be tracked", exc_info=True)
        if not self._exit_registered:
            atexit.register(self._drain_at_exit)
            self._exit_registered = True

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.wait(self._backoff_s())
            self._wake.clear()
            self.flush(DRAIN_TIMEOUT_S)
        self.flush(DRAIN_TIMEOUT_S)

    def stop(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Stop the flusher and drain what is left, within ``timeout`` total."""
        deadline = time.monotonic() + max(timeout, 0.0)
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(deadline - time.monotonic(), 0.0))
        self.flush(max(deadline - time.monotonic(), 0.0))

    def _drain_at_exit(self) -> None:
        """The ``atexit`` hook: stop and drain, silently, whatever happens.

        Registered after :mod:`runbound.alerts` registers its own drain, so
        it runs first (``atexit`` is last-registered-first) and leaves the
        flusher thread finished by the time that drain joins it.
        """
        try:
            self.stop(DRAIN_TIMEOUT_S)
        except Exception:
            # Interpreter shutdown: logging may already be gone, and an
            # exception here would print at the host process's death.
            pass
