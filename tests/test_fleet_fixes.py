"""Two things fleet mode owed the plane, tested end to end.

Both are about *state* rather than telemetry, and both were invisible from the
plane's side before:

* a block refused at the door because its key is latched — locally or by
  another worker — is a door refusal, and the plane's ledger has a column for
  it;
* ``export_events=False`` turns telemetry off, not the fleet. A worker that
  stops reporting its exits stops contributing to the shared spend, which is
  the one thing fleet mode exists to get right.

The plane is the same in-memory :class:`FakePlane` the rest of the fleet tests
use, wired in where the api builds its client.
"""

import threading
import time

import pytest

import runbound
from runbound import api, shared as shared_module
from runbound.exceptions import GuardrailTripped
from runbound.export import Exporter
from runbound.plane_types import EntryDecision, ExitDelta, key_hash
from runbound.state import SessionState
from test_shared_state import FakePlane

PLANE_URL = "https://plane.example"
KEY = "user-9"


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
    """A fake control plane, wired in wherever the api builds a client."""
    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url = url
        fake.token = token
        fake.service = service
        fake.worker_id = worker_id
        fake.timeout_s = timeout_s
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    return fake


def start(**kwargs) -> None:
    """init() in fleet mode, with the poller effectively switched off."""
    fields = {
        "control_plane_url": PLANE_URL,
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
        "control_plane_poll_s": 3600.0,
        "export_events": False,
        "auto_wrap": False,
    }
    fields.update(kwargs)
    runbound.init(**fields)


def latch_payload(**fields) -> dict:
    payload = {
        "detector": "budget",
        "severity": "critical",
        "message": "Budget exceeded on another worker",
        "details": {"limit_hit": "budget_usd", "total_cost_usd": 5.4},
        "ttl_remaining_s": 30.0,
    }
    payload.update(fields)
    return payload


def refuse(key: str = KEY) -> GuardrailTripped:
    """Enter a block that must be refused at the door, and return why."""
    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session(key):
            raise AssertionError("the refused block ran")
    return caught.value


def door_trips(plane: FakePlane) -> list:
    """Every trip report the plane took that says "refused at the door"."""
    return [report for report in plane.payloads("trip") if report.refused_at_door]


def batches(plane: FakePlane) -> list[dict]:
    """Everything the exporter has posted, flushed on this thread first."""
    api._SHARED._exporter.flush(1.0)
    return plane.payloads("events")


def lane(plane: FakePlane, name: str) -> list:
    """Every record posted on one lane of the export batches."""
    return [record for batch in batches(plane) for record in batch.get(name, [])]


# --- gap 1: a refusal at the door is a door refusal --------------------------


def test_a_remote_latch_reports_one_door_trip_per_refused_entry(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")

    for _ in range(3):
        refuse()

    reports = door_trips(plane)
    assert len(reports) == 3
    assert all(report.key_hash == key_hash(KEY) for report in reports)


def test_a_door_trip_keeps_the_detector_that_latched_the_key(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")

    refused = refuse()

    report = door_trips(plane)[0]
    assert refused.anomaly.detector == "budget"
    assert report.anomaly["detector"] == "budget"
    assert report.anomaly["message"] == "Budget exceeded on another worker"
    assert report.anomaly["reacted"] == "door"
    assert report.latch_ttl_s is None


def test_a_locally_latched_key_reports_door_trips_on_later_entries(plane):
    plane.decision = EntryDecision()
    start(budget_usd=0.10, on_anomaly="raise", custom_prices={"test": (0.0, 300.0)})

    with pytest.raises(GuardrailTripped):
        with runbound.session(KEY):
            runbound.record_call("test", tokens_in=0, tokens_out=1000)
    refuse()
    refuse()

    reports = plane.payloads("trip")
    # The trip itself, then one door refusal for each block turned away.
    assert [report.refused_at_door for report in reports] == [False, True, True]
    assert door_trips(plane)[0].anomaly["detector"] == "budget"


def test_a_door_refusal_is_reported_once_per_entry_not_once_per_event(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")

    refuse()

    assert len(door_trips(plane)) == 1


def test_the_fleet_status_counts_the_blocks_this_worker_refused(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")

    for _ in range(3):
        refuse()

    assert runbound.fleet_status(KEY)["door_refusals"] == 3


def test_a_key_nobody_refused_has_no_door_refusals(plane):
    plane.decision = EntryDecision()
    start(on_anomaly="raise")

    with runbound.session(KEY):
        pass

    assert runbound.fleet_status(KEY)["door_refusals"] == 0


def test_clearing_a_key_forgives_its_door_refusals(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")
    refuse()

    runbound.clear(KEY)

    assert runbound.session_status(KEY) is None
    assert KEY not in api._DOOR_REFUSALS


def test_the_door_refusal_reaches_the_observers_as_a_door_reaction(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise", export_events=True)

    refuse()

    reactions = [record["reacted"] for record in lane(plane, "anomalies")]
    assert "door" in reactions


def test_without_a_plane_a_refused_entry_reports_nothing_and_still_raises(
    plane, caplog
):
    runbound.init(budget_usd=0.10, on_anomaly="raise", auto_wrap=False,
                    custom_prices={"test": (0.0, 300.0)})

    with caplog.at_level("WARNING", logger="runbound"):
        with pytest.raises(GuardrailTripped):
            with runbound.session(KEY):
                runbound.record_call("test", tokens_in=0, tokens_out=1000)
        refused = refuse()

    assert refused.anomaly.detector == "budget"
    assert plane.calls == []  # LocalState: nothing was even built
    assert "could not" not in caplog.text


def test_a_door_refusal_survives_a_plane_that_explodes(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")
    refuse()  # the latch is this worker's now
    plane.explodes = True

    refused = refuse()

    assert refused.anomaly.detector == "budget"
    assert runbound.fleet_status(KEY)["door_refusals"] == 2


# --- gap 2: exits are fleet state, not telemetry -----------------------------


def test_exits_reach_the_plane_with_events_switched_off(plane):
    plane.decision = EntryDecision()
    start(export_events=False, custom_prices={"test": (0.0, 300.0)})

    with runbound.session(KEY):
        runbound.record_call("test", tokens_in=0, tokens_out=1000)

    exits = lane(plane, "exits")
    assert [delta["key_hash"] for delta in exits] == [key_hash(KEY)]
    assert exits[0]["spend_delta_usd"] == pytest.approx(0.30)
    assert lane(plane, "events") == []
    assert lane(plane, "anomalies") == []


def test_both_lanes_flow_with_events_switched_on(plane):
    plane.decision = EntryDecision()
    start(export_events=True, custom_prices={"test": (0.0, 300.0)})

    with runbound.session(KEY):
        runbound.record_call("test", tokens_in=0, tokens_out=1000)

    assert lane(plane, "exits") != []
    assert lane(plane, "events") != []


def test_a_circuit_change_reaches_the_plane_with_events_switched_off(plane):
    plane.decision = EntryDecision()
    start(export_events=False)

    api._SHARED.circuit("openai@api", "open", 5, 30.0)

    assert [record["label"] for record in lane(plane, "circuits")] == ["openai@api"]


def test_a_trip_still_goes_straight_to_the_plane_with_events_switched_off(plane):
    plane.decision = EntryDecision()
    start(budget_usd=0.10, on_anomaly="raise", custom_prices={"test": (0.0, 300.0)})

    with pytest.raises(GuardrailTripped):
        with runbound.session(KEY):
            runbound.record_call("test", tokens_in=0, tokens_out=1000)

    assert [report.refused_at_door for report in plane.payloads("trip")] == [False]


def test_an_exporter_that_drops_events_is_not_an_engine_observer(plane):
    start(export_events=False)

    assert api._SHARED._exporter is not None
    assert api._SHARED.observers() == []


# --- gap 2, at the exporter --------------------------------------------------


class Client:
    """The client half of the plane: records batches, answers True."""

    def __init__(self) -> None:
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.key_state = "unknown"
        self.batches: list[dict] = []

    def events(self, batch: dict) -> bool:
        self.batches.append(batch)
        return True


def test_the_exporter_drops_events_and_anomalies_when_told_to():
    client = Client()
    sink = Exporter(client, flush_every_s=30.0, include_events=False)
    session = SessionState("sess-1", key=KEY)

    sink.on_event(session, _event())
    sink.on_anomaly(session, _anomaly(), "raise")

    assert sink.pending == 0
    assert sink.dropped == 0  # configured off, not lost


def test_the_exporter_keeps_exits_and_circuits_when_events_are_off():
    client = Client()
    sink = Exporter(client, flush_every_s=30.0, include_events=False)

    sink.on_exit(ExitDelta(key_hash=key_hash(KEY), seq=1, spend_delta_usd=0.3))
    sink.on_circuit("openai@api", "open", 5, 30.0)
    sink.flush(1.0)

    batch = client.batches[0]
    assert [delta["seq"] for delta in batch["exits"]] == [1]
    assert [record["label"] for record in batch["circuits"]] == ["openai@api"]
    assert batch["events"] == []


def test_the_exporter_keeps_a_trip_the_plane_refused_when_events_are_off():
    client = Client()
    sink = Exporter(client, flush_every_s=30.0, include_events=False)

    sink.on_trip(SessionState("sess-1", key=KEY), _anomaly(), "door")
    sink.flush(1.0)

    anomalies = client.batches[0]["anomalies"]
    assert [record["reacted"] for record in anomalies] == ["door"]


def _event():
    from runbound.events import Event

    return Event(ts=time.monotonic(), step=1, kind="model_call", tokens_in=1)


def _anomaly():
    from runbound.events import Anomaly

    return Anomaly("budget", "critical", "over", {})


def test_no_fleet_thread_outlives_a_teardown_with_events_off(plane):
    plane.decision = EntryDecision()
    start(export_events=False)

    with runbound.session(KEY):
        pass
    api._teardown_for_tests()

    deadline = time.monotonic() + 2.0
    names = ("runbound-exporter", "runbound-plane-poller")
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name in names]:
            break
        time.sleep(0.01)

    assert [t.name for t in threading.enumerate() if t.name in names] == []
