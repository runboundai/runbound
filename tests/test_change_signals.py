"""The runtime says what changed.

One local record kind joins ``runbound.events()``: ``"runtime_change"`` — a
value this process runs on (the policy version the plane delivered, ...)
differs from the last one seen. Never on first sight (there is nothing to
change *from*), never again while it stays the same. The exporter carries
these on their own ``changes`` lane. None of it holds a prompt, a reply or a
tool argument.
"""

import pytest

import runbound
from runbound import api, local_events
from runbound.config import RunboundConfig
from runbound.export import Exporter
from runbound.shared import RemoteState


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def _changes(what: str | None = None) -> list:
    return [
        e
        for e in runbound.events(n=-1)
        if e["kind"] == "runtime_change" and (what is None or e["what"] == what)
    ]


# --- runtime_change ------------------------------------------------------------


def test_note_runtime_value_never_records_on_first_sight():
    assert local_events.note_runtime_value("sdk_version", "0.4.0") is False
    assert _changes() == []


def test_note_runtime_value_records_only_a_real_change():
    local_events.note_runtime_value("policy_version", 3)
    assert local_events.note_runtime_value("policy_version", 3) is False
    assert local_events.note_runtime_value("policy_version", 4) is True

    assert [(c["from"], c["to"]) for c in _changes("policy_version")] == [(3, 4)]


def test_a_change_record_has_exactly_its_documented_shape():
    local_events.note_runtime_value("policy_version", 1)
    local_events.note_runtime_value("policy_version", 2)

    (change,) = _changes()
    assert isinstance(change["at"], float)
    assert set(change) == {"kind", "at", "what", "from", "to"}


def test_none_is_neither_recorded_nor_remembered():
    local_events.note_runtime_value("policy_version", 1)
    assert local_events.note_runtime_value("policy_version", None) is False
    assert local_events.note_runtime_value("policy_version", 1) is False
    assert _changes() == []


def test_clear_for_tests_forgets_every_last_value():
    local_events.note_runtime_value("policy_version", 1)
    local_events.clear_for_tests()

    assert local_events.note_runtime_value("policy_version", 2) is False
    assert _changes() == []


class _PlaneClient:
    """Just enough of a plane client for the policy fetch."""

    def __init__(self) -> None:
        self.policy_body: dict = {}

    def policy(self, service):
        return self.policy_body


def test_a_new_policy_version_is_a_runtime_change():
    client = _PlaneClient()
    remote = RemoteState(client, None, RunboundConfig())

    client.policy_body = {"version": 4, "policy": {"deny": ["wire"]}}
    remote._fetch_policy(4)
    client.policy_body = {"version": 5, "policy": {"deny": ["wire", "refund"]}}
    remote._fetch_policy(5)
    remote._fetch_policy(5)

    assert [(c["from"], c["to"]) for c in _changes("policy_version")] == [(4, 5)]


# --- sinks ---------------------------------------------------------------------


def test_every_sink_sees_every_record_after_the_on_event_callback():
    order = []
    runbound.init(on_event=lambda r: order.append(("callback", r["kind"])))
    local_events.add_sink(lambda r: order.append(("sink", r["kind"])))

    local_events.record_runtime_change("policy_version", 1, 2)

    assert order == [("callback", "runtime_change"), ("sink", "runtime_change")]


def test_a_sink_that_raises_never_breaks_the_caller_or_the_next_sink():
    seen = []

    def broken(record):
        raise RuntimeError("sink bug")

    runbound.init()
    local_events.add_sink(broken)
    local_events.add_sink(seen.append)

    local_events.record_runtime_change("policy_version", 1, 2)  # does not raise

    assert [r["kind"] for r in seen] == ["runtime_change"]


def test_a_removed_sink_hears_nothing_more():
    seen = []
    local_events.add_sink(seen.append)
    local_events.remove_sink(seen.append)
    local_events.remove_sink(seen.append)  # removing twice is harmless

    local_events.record_runtime_change("policy_version", 1, 2)

    assert seen == []


def test_records_carry_no_tool_argument():
    runbound.init()

    @runbound.tool
    def refund(user: str, amount: float):
        return "ok"

    refund(user="SECRET-ARG", amount=1.0)
    local_events.note_runtime_value("policy_version", 1)
    local_events.note_runtime_value("policy_version", 2)

    blob = repr(runbound.events(n=-1))
    assert "SECRET" not in blob
    assert _changes("policy_version")


# --- the exporter's changes lane ------------------------------------------------


class _Spy:
    service = "checkout"
    worker_id = "host-1:1"
    key_state = "ok"

    def __init__(self) -> None:
        self.batches: list = []

    def events(self, batch: dict) -> bool:
        self.batches.append(batch)
        return True


def _exporter(**kwargs):
    client = _Spy()
    return Exporter(client, **kwargs), client


def test_the_exporter_posts_changes_on_their_own_lane():
    exporter, client = _exporter()
    exporter.on_change(
        {"kind": "runtime_change", "at": 1_790_000_000.0, "what": "policy_version", "from": 1, "to": 2}
    )

    exporter.flush()
    (batch,) = client.batches
    (runtime,) = batch["changes"]
    assert runtime == {
        "ts_wall": runtime["ts_wall"],
        "kind": "runtime_change",
        "what": "policy_version",
        "from": 1,
        "to": 2,
    }
    assert runtime["ts_wall"].startswith("2026-")
    assert batch["events"] == [] and batch["anomalies"] == []


def test_absent_fields_are_omitted_not_sent_as_null():
    exporter, client = _exporter()
    exporter.on_change(
        {"kind": "runtime_change", "at": 1_790_000_000.0, "what": "policy_version",
         "from": None, "to": 2, "reason": None}
    )

    exporter.flush()
    (change,) = client.batches[0]["changes"]
    assert set(change) == {"ts_wall", "kind", "what", "to"}


def test_the_exporter_ignores_every_other_record_kind():
    exporter, client = _exporter()
    for kind in ("anomaly", "refusal", "decision", "posture"):
        exporter.on_change({"kind": kind, "at": 1.0})

    assert exporter.pending == 0


def test_a_runtime_change_is_telemetry_and_export_events_off_silences_it():
    exporter, client = _exporter(include_events=False)
    exporter.on_change({"kind": "runtime_change", "at": 1.0, "what": "policy_version", "from": 1, "to": 2})

    assert exporter.pending == 0


def test_a_refused_changes_batch_is_resent_whole():
    exporter, client = _exporter()
    exporter.on_change({"kind": "runtime_change", "at": 1.0, "what": "policy_version", "from": 1, "to": 2})
    taken = exporter._take()
    batch, priority, anomalies, events = taken
    exporter._requeue(batch["batch_id"], priority, anomalies, events)

    again, *_ = exporter._take()
    assert again["batch_id"] == batch["batch_id"]
    assert again["changes"] == batch["changes"]


def test_start_listens_for_changes_and_stop_stops_listening():
    exporter, client = _exporter(flush_every_s=3600.0)
    exporter.start()
    try:
        local_events.record_runtime_change("policy_version", 1, 2)
        assert exporter.pending == 1
    finally:
        exporter.stop(0.5)
    local_events.record_runtime_change("policy_version", 2, 3)
    assert exporter.pending == 0
    assert [c["to"] for b in client.batches for c in b["changes"]] == [2]


def test_a_broken_change_record_is_counted_dropped_never_raised():
    exporter, client = _exporter()
    exporter.on_change({"kind": "runtime_change", "at": "not a time"})

    assert exporter.dropped == 1
