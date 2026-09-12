"""Tests for the exporter's behavior when the plane is down.

T55: a failed POST used to be dropped, never requeued — the exact bug the
Acme fleet scorecard caught in scenario 8 ("plane down, agent guarded"): the
ledger stayed flat after the plane came back because the backlog built up
during the outage had already been thrown away. These tests pin the fix:
a batch the client could not deliver goes back to the front of its lane,
intact and in order, and is retried — with exponential backoff so a dead
plane is not hammered — until it either ships or is bumped by the queue's
own bound.

Same conventions as ``test_export.py``: no network, a fake client whose
``events()`` can be toggled to fail, and both clocks are stood in for.
"""

import logging
import threading
import time

import pytest

from runbound import export as export_module
from runbound.export import Exporter
from runbound.events import Anomaly, Event
from runbound.plane_types import ExitDelta
from runbound.state import SessionState


class MovableClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeClient:
    """Stands in for PlaneClient: ``ok`` toggles whether ``events()`` lands."""

    def __init__(self, ok: bool = True) -> None:
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.key_state = "unknown"
        self.ok = ok
        self.batches: list[dict] = []
        self._lock = threading.Lock()

    def events(self, batch: dict) -> bool:
        with self._lock:
            self.batches.append(batch)
        return self.ok


@pytest.fixture
def wall(monkeypatch) -> MovableClock:
    clock = MovableClock(start=1_700_000_000.0)
    monkeypatch.setattr(export_module, "_wall", clock)
    return clock


def session(key: str | None = "user-9") -> SessionState:
    return SessionState("sess-1", key=key)


def event(step: int = 1, ts: float = 1000.0, **kwargs) -> Event:
    fields = {"kind": "llm_call", "ts": ts, "step": step, "tokens_in": 10, "tokens_out": 5}
    fields.update(kwargs)
    return Event(**fields)


def anomaly(detector: str = "budget") -> Anomaly:
    return Anomaly(detector=detector, severity="critical", message="over", details={"spend": 3})


def delta(seq: int = 1) -> ExitDelta:
    return ExitDelta(
        key_hash="abc",
        seq=seq,
        spend_delta_usd=0.25,
        tokens_delta=100,
        steps_delta=3,
        tool_calls={"search": 2},
    )


def exporter(client: FakeClient, **kwargs) -> Exporter:
    options = {"maxlen": 100, "flush_every_s": 60.0, "batch_size": 200}
    options.update(kwargs)
    return Exporter(client, **options)


# --- a failed batch is retried, not dropped ---------------------------------


def test_a_failed_batch_is_kept_intact_and_pending(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=100)
    sink.on_event(session(), event(step=1))
    sink.on_event(session(), event(step=2))
    sink.on_exit(delta(seq=9))
    sink.on_circuit("openai", "open", 1, 5.0)

    sink.flush()

    assert len(client.batches) == 1  # one attempt, and it failed
    assert sink.pending == 4  # nothing was lost
    assert sink.backlog() == 4
    assert sink.consecutive_failures == 1


def test_a_failed_batch_is_delivered_intact_once_the_plane_recovers(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=100)
    sink.on_event(session(), event(step=1))
    sink.on_event(session(), event(step=2))
    sink.on_exit(delta(seq=9))
    sink.on_circuit("openai", "open", 1, 5.0)

    sink.flush()
    client.ok = True
    sink.flush()

    assert sink.pending == 0
    assert sink.consecutive_failures == 0
    assert len(client.batches) == 2  # the failed attempt, then the delivery
    delivered = client.batches[1]
    assert [w["seq"] for w in delivered["exits"]] == [9]  # priority lane first
    assert [w["label"] for w in delivered["circuits"]] == ["openai"]
    assert [w["step"] for w in delivered["events"]] == [1, 2]  # order preserved


def test_a_multi_batch_backlog_drains_in_order_after_recovery(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=2)
    for step in range(5):
        sink.on_event(session(), event(step=step))

    sink.flush()  # first chunk (0, 1) fails; the flush stops there
    assert sink.pending == 5
    assert [w["step"] for w in client.batches[0]["events"]] == [0, 1]

    client.ok = True
    sink.flush()

    delivered_steps = [
        wire["step"] for batch in client.batches[1:] for wire in batch["events"]
    ]
    assert delivered_steps == [0, 1, 2, 3, 4]
    assert sink.pending == 0


# --- exponential backoff ----------------------------------------------------


def test_backoff_doubles_then_caps_and_resets_on_success(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=100, flush_every_s=1.0)
    sink.on_event(session(), event())

    delays = []
    for _ in range(6):
        sink.flush()
        delays.append(sink._backoff_s())
        sink.on_event(session(), event())  # keep the lane non-empty for the next round

    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 10.0]

    client.ok = True
    sink.flush()

    assert sink.consecutive_failures == 0
    assert sink._backoff_s() == pytest.approx(1.0)  # back to flush_every_s


def test_a_fresh_exporter_backs_off_at_its_normal_cadence(wall):
    sink = exporter(FakeClient(), flush_every_s=2.5)
    assert sink.consecutive_failures == 0
    assert sink._backoff_s() == pytest.approx(2.5)


# --- bounded queues under a long outage -------------------------------------


def test_overflow_during_requeue_drops_the_oldest_and_counts_it(wall):
    client = FakeClient()
    sink = exporter(client, maxlen=3, batch_size=100)

    # Build the wire records an earlier, now-failed batch would have held.
    scratch = exporter(FakeClient(), maxlen=100, batch_size=100)
    for step in (0, 1, 2):
        scratch.on_event(session(), event(step=step))
    _batch, _priority, _anomalies, older_events = scratch._take()

    # New telemetry arrived and filled the (small) lane while that batch was
    # still in flight to the plane.
    for step in (10, 11, 12):
        sink.on_event(session(), event(step=step))

    sink._requeue([], [], older_events)

    assert sink.dropped == 3  # the older batch lost the fight for room
    assert [w["step"] for w in list(sink._events)] == [10, 11, 12]


def test_a_long_outage_drops_oldest_and_counts_dropped_end_to_end(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, maxlen=3, batch_size=100)
    for step in range(3):
        sink.on_event(session(), event(step=step))

    sink.flush()  # fails, the batch of 3 goes right back
    assert sink.pending == 3

    for step in range(3, 6):  # more telemetry keeps arriving during the outage
        sink.on_event(session(), event(step=step))

    assert sink.dropped == 3
    assert sink.pending == 3

    client.ok = True
    sink.flush()
    assert [w["step"] for w in client.batches[-1]["events"]] == [3, 4, 5]


# --- an invalid key still drops without retry -------------------------------


def test_an_invalid_key_still_drops_without_retry(wall):
    client = FakeClient()
    client.key_state = "invalid"
    sink = exporter(client)

    sink.on_event(session(), event())
    sink.on_exit(delta())
    sink.flush()

    assert client.batches == []
    assert sink.dropped == 2
    assert sink.pending == 0
    assert sink.consecutive_failures == 0  # never even tried the network


# --- flush() and stop() never hang ------------------------------------------


def test_flush_returns_within_its_budget_during_a_long_outage(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=100)
    for step in range(50):
        sink.on_event(session(), event(step=step))

    started = time.monotonic()
    sink.flush(timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert sink.pending == 50  # nothing delivered, nothing lost


def test_stop_does_not_hang_during_a_persistent_outage(wall):
    client = FakeClient(ok=False)
    sink = Exporter(client, maxlen=100, flush_every_s=0.01, batch_size=200)
    sink.start()
    sink.on_event(session(), event())

    started = time.monotonic()
    sink.stop(timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert not sink._thread.is_alive()


def test_the_pending_backlog_is_logged_once_as_a_warning(wall, caplog):
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=100)
    sink.on_event(session(), event())

    with caplog.at_level(logging.WARNING, logger="runbound"):
        sink.flush()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1" in warnings[0].getMessage() or "pending" in warnings[0].getMessage()


# --- everything else about the exporter is unaffected -----------------------


def test_eight_producer_threads_still_lose_nothing_under_the_queue_bound(wall):
    client = FakeClient(ok=True)
    sink = exporter(client, maxlen=10_000, batch_size=500)
    errors: list[BaseException] = []

    def produce() -> None:
        try:
            state = session()
            for step in range(200):
                sink.on_event(state, event(step=step))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=produce) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sink.dropped == 0
    assert sink.pending == 1600
    sink.flush()
    assert sum(len(batch["events"]) for batch in client.batches) == 1600


def test_on_trip_and_include_events_are_unaffected_by_the_retry_path(wall):
    client = FakeClient(ok=False)
    sink = exporter(client, include_events=False)

    sink.on_trip(session(), anomaly(), "door")
    sink.on_event(session(), event())  # dropped: include_events is off

    assert sink.pending == 1  # only the trip; the plain event never queued

    client.ok = True
    sink.flush()
    assert [record["reacted"] for record in client.batches[0]["anomalies"]] == ["door"]
