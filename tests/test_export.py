"""Tests for the batching exporter.

No network and no sleeping for its own sake: the client is a fake that records
batches, both clocks (monotonic and wall) are fakes, and the flusher thread is
only started in the tests that are about the thread — where it is woken by an
event and joined, never waited out.
"""

import logging
import threading
import time

import pytest

from runbound import alerts
from runbound import export as export_module
from runbound.events import Anomaly, Event
from runbound.export import Exporter
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
    """Stands in for PlaneClient: records batches, answers True."""

    def __init__(self, ok: bool = True, raises: bool = False) -> None:
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.key_state = "unknown"
        self.ok = ok
        self.raises = raises
        self.batches: list[dict] = []
        self.posted = threading.Event()
        self._lock = threading.Lock()

    def events(self, batch: dict) -> bool:
        if self.raises:
            raise RuntimeError("client exploded")
        with self._lock:
            self.batches.append(batch)
        self.posted.set()
        return self.ok


@pytest.fixture
def wall(monkeypatch) -> MovableClock:
    """Freeze the wall clock the exporter stamps events with."""
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


def warnings_matching(caplog, *fragments: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and all(fragment in record.getMessage() for fragment in fragments)
    ]


# --- what goes on the wire -------------------------------------------------


def test_an_event_is_hashed_stamped_and_batched(wall):
    client = FakeClient()
    clock = MovableClock()
    sink = exporter(client, now=clock)

    clock.advance(2.5)  # the event happened 2.5 s before we look at it
    sink.on_event(session(), event(step=4, ts=clock.value - 2.5))
    sink.flush()

    batch = client.batches[0]
    assert batch["service"] == "checkout"
    assert batch["worker_id"] == "host-1:42"
    assert batch["dropped"] == 0
    assert batch["anomalies"] == batch["exits"] == batch["circuits"] == []

    (wire,) = batch["events"]
    assert wire["step"] == 4
    assert wire["kind"] == "llm_call"
    assert wire["key_hash"] and wire["key_hash"] != "user-9"
    # ts_wall = wall now minus the age of the event, in ISO-8601 UTC.
    assert wire["ts_wall"].startswith("2023-11-14T")
    assert wire["ts_wall"].endswith("Z")


def test_an_unkeyed_session_exports_a_null_key_hash(wall):
    client = FakeClient()
    sink = exporter(client)
    sink.on_event(session(key=None), event())
    sink.flush()
    assert client.batches[0]["events"][0]["key_hash"] is None


def test_an_anomaly_carries_the_reaction(wall):
    client = FakeClient()
    sink = exporter(client)
    sink.on_anomaly(session(), anomaly(), "raise")
    sink.flush()

    (wire,) = client.batches[0]["anomalies"]
    assert wire["detector"] == "budget"
    assert wire["reacted"] == "raise"
    assert wire["ts_wall"].endswith("Z")


def test_an_exit_and_a_circuit_ride_the_priority_lane(wall):
    client = FakeClient()
    sink = exporter(client)
    sink.on_exit(delta(seq=7))
    sink.on_circuit("openai", "open", 5, 30.0)
    sink.flush()

    batch = client.batches[0]
    assert batch["exits"][0]["seq"] == 7
    assert batch["exits"][0]["key_hash"] == "abc"
    circuit = batch["circuits"][0]
    assert circuit["label"] == "openai"
    assert circuit["state"] == "open"
    assert circuit["failures"] == 5
    assert circuit["cooldown_s"] == 30.0
    assert circuit["ts_wall"].endswith("Z")


def test_nothing_is_posted_when_there_is_nothing_to_say(wall):
    client = FakeClient()
    exporter(client).flush()
    assert client.batches == []


# --- batching --------------------------------------------------------------


def test_a_flush_splits_the_queue_into_batches_of_batch_size(wall):
    client = FakeClient()
    sink = exporter(client, batch_size=3)
    for step in range(7):
        sink.on_event(session(), event(step=step))
    sink.flush()

    assert [len(batch["events"]) for batch in client.batches] == [3, 3, 1]
    assert [wire["step"] for wire in client.batches[0]["events"]] == [0, 1, 2]


def test_the_priority_lane_goes_out_before_queued_events(wall):
    client = FakeClient()
    sink = exporter(client, batch_size=2)
    for step in range(5):
        sink.on_event(session(), event(step=step))
    sink.on_exit(delta())
    sink.on_circuit("openai", "open", 1, 5.0)
    sink.flush()

    first = client.batches[0]
    assert len(first["exits"]) == 1
    assert len(first["circuits"]) == 1
    assert first["events"] == []


def test_anomalies_go_out_before_plain_events(wall):
    client = FakeClient()
    sink = exporter(client, batch_size=1)
    sink.on_event(session(), event())
    sink.on_anomaly(session(), anomaly(), "warn")
    sink.flush()

    assert len(client.batches[0]["anomalies"]) == 1
    assert client.batches[0]["events"] == []
    assert len(client.batches[1]["events"]) == 1


def test_a_failed_post_is_requeued_intact_instead_of_dropped(wall):
    """T55: a batch the plane refuses is kept, not thrown away.

    This used to assert the opposite — that three failed posts still ended
    with an empty, fully-drained queue — which was this exact bug (a
    down plane silently dropping its backlog) passing as a spec. See
    tests/test_export_requeue.py for the full retry/backoff behavior.
    """
    client = FakeClient(ok=False)
    sink = exporter(client, batch_size=1)
    for step in range(3):
        sink.on_event(session(), event(step=step))
    sink.flush()

    assert len(client.batches) == 1  # one attempt; flush() stops on failure
    assert sink.pending == 3  # nothing lost — it is requeued, not dropped


# --- bounded queues --------------------------------------------------------


def test_the_oldest_events_are_dropped_and_counted(wall):
    client = FakeClient()
    sink = exporter(client, maxlen=3, batch_size=100)
    for step in range(5):
        sink.on_event(session(), event(step=step))

    assert sink.dropped == 2
    sink.flush()
    steps = [wire["step"] for wire in client.batches[0]["events"]]
    assert steps == [2, 3, 4]
    assert client.batches[0]["dropped"] == 2


def test_the_drop_count_is_cumulative_across_batches(wall):
    client = FakeClient()
    sink = exporter(client, maxlen=1, batch_size=100)
    for step in range(3):
        sink.on_event(session(), event(step=step))
    sink.flush()
    assert client.batches[0]["dropped"] == 2

    for step in range(3):
        sink.on_event(session(), event(step=step))
    sink.flush()
    assert client.batches[1]["dropped"] == 4


def test_each_lane_is_bounded_on_its_own(wall):
    client = FakeClient()
    sink = exporter(client, maxlen=2, batch_size=100)
    for seq in range(4):
        sink.on_exit(delta(seq=seq))
    sink.flush()

    assert [wire["seq"] for wire in client.batches[0]["exits"]] == [2, 3]
    assert sink.dropped == 2


# --- an invalid key --------------------------------------------------------


def test_an_invalid_key_drops_silently_without_touching_the_network(wall):
    client = FakeClient()
    client.key_state = "invalid"
    sink = exporter(client)

    sink.on_event(session(), event())
    sink.on_anomaly(session(), anomaly(), "warn")
    sink.on_exit(delta())
    sink.on_circuit("openai", "open", 1, 5.0)
    sink.flush()

    assert client.batches == []
    assert sink.dropped == 4
    assert sink.pending == 0


def test_a_key_rejected_after_the_queue_filled_stops_the_flush(wall):
    client = FakeClient()
    sink = exporter(client)
    sink.on_event(session(), event())
    client.key_state = "invalid"
    sink.flush()
    assert client.batches == []


# --- fail-open -------------------------------------------------------------


def test_no_observer_method_raises_when_the_client_explodes(wall, caplog):
    client = FakeClient(raises=True)
    sink = exporter(client)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        sink.on_event(session(), event())
        sink.on_anomaly(session(), anomaly(), "raise")
        sink.on_exit(delta())
        sink.on_circuit("openai", "open", 1, 5.0)
        sink.flush()
    assert sink.pending == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.on_event(object(), object()),
        lambda s: s.on_anomaly(object(), object(), "raise"),
        lambda s: s.on_exit(object()),
        lambda s: s.on_circuit(object(), object(), "nope", None),
    ],
)
def test_garbage_handed_to_an_observer_is_logged_not_raised(call, wall, caplog):
    sink = exporter(FakeClient())
    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert call(sink) is None


def test_the_warning_about_bad_observer_input_is_rate_limited(wall, caplog):
    clock = MovableClock()
    sink = exporter(FakeClient(), now=clock)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        for _ in range(5):
            sink.on_event(object(), object())
    assert len(warnings_matching(caplog, "export")) == 1


# --- concurrency -----------------------------------------------------------


def test_eight_producers_lose_nothing_under_the_queue_bound(wall):
    client = FakeClient()
    sink = exporter(client, maxlen=10_000, batch_size=500)
    errors: list[BaseException] = []

    def produce() -> None:
        try:
            state = session()
            for step in range(200):
                sink.on_event(state, event(step=step))
        except BaseException as exc:  # pragma: no cover - the assertion below reports it
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


# --- the flusher thread ----------------------------------------------------


def test_the_flusher_posts_on_its_own_schedule(wall):
    client = FakeClient()
    sink = Exporter(client, maxlen=100, flush_every_s=0.001, batch_size=200)
    sink.start()
    try:
        sink.on_event(session(), event())
        assert client.posted.wait(timeout=5)
    finally:
        sink.stop()

    assert sum(len(batch["events"]) for batch in client.batches) == 1
    assert sink.pending == 0


def test_a_full_batch_wakes_the_flusher_early(wall):
    client = FakeClient()
    sink = Exporter(client, maxlen=100, flush_every_s=30.0, batch_size=2)
    sink.start()
    try:
        sink.on_event(session(), event(step=1))
        sink.on_event(session(), event(step=2))
        assert client.posted.wait(timeout=5)
    finally:
        sink.stop()

    assert len(client.batches[0]["events"]) == 2


def test_stop_drains_what_is_left(wall):
    client = FakeClient()
    sink = Exporter(client, maxlen=100, flush_every_s=30.0, batch_size=200)
    sink.start()
    sink.on_event(session(), event())
    sink.stop()

    assert sink.pending == 0
    assert sum(len(batch["events"]) for batch in client.batches) == 1


def test_starting_twice_runs_one_flusher(wall):
    client = FakeClient()
    sink = Exporter(client, maxlen=100, flush_every_s=30.0, batch_size=200)
    sink.start()
    first = sink._thread
    sink.start()
    try:
        assert sink._thread is first
    finally:
        sink.stop()


def test_the_flusher_thread_is_tracked_for_the_alert_drain(wall):
    client = FakeClient()
    sink = Exporter(client, maxlen=100, flush_every_s=30.0, batch_size=200)
    sink.start()
    try:
        with alerts._THREADS_LOCK:
            tracked = list(alerts._ALERT_THREADS)
        assert sink._thread in tracked
    finally:
        sink.stop()


def test_stopping_a_flusher_that_never_started_is_harmless(wall):
    sink = Exporter(FakeClient(), maxlen=100, flush_every_s=30.0, batch_size=200)
    sink.stop()
    assert sink.pending == 0


# --- the exit hook ---------------------------------------------------------


def test_the_exit_hook_drains_the_queue_and_never_raises(wall):
    client = FakeClient()
    sink = exporter(client)
    sink.on_event(session(), event())

    assert sink._drain_at_exit() is None
    assert sum(len(batch["events"]) for batch in client.batches) == 1
    assert sink.pending == 0
    assert sink._drain_at_exit() is None


def test_the_exit_hook_survives_a_client_that_explodes(wall):
    client = FakeClient(raises=True)
    sink = exporter(client)
    sink.on_event(session(), event())
    assert sink._drain_at_exit() is None


def test_the_exit_hook_stops_the_flusher_within_the_budget(wall):
    client = FakeClient()
    sink = Exporter(client, maxlen=100, flush_every_s=30.0, batch_size=200)
    sink.start()
    sink.on_event(session(), event())

    started = time.monotonic()
    sink._drain_at_exit()
    assert time.monotonic() - started < 3.5
    assert not sink._thread.is_alive()
    assert sum(len(batch["events"]) for batch in client.batches) == 1


def test_flush_stops_posting_once_its_budget_is_spent(wall):
    class SlowClient(FakeClient):
        def events(self, batch: dict) -> bool:
            time.sleep(0.05)
            return super().events(batch)

    client = SlowClient()
    sink = exporter(client, batch_size=1)
    for step in range(4):
        sink.on_event(session(), event(step=step))

    sink.flush(timeout=0.01)

    assert len(client.batches) == 1
    assert sink.pending == 3
