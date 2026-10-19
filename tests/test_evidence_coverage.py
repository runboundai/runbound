"""PS-AAI-03m (SDK half): the hello says where this worker's evidence can go. ``coverage_digest`` rides every hello (a hash of the three STATE fields only); ``evidence_coverage``
(the state plus the telemetry counters) rides the first hello, a change of state, and at most hourly. Local state only: no new call, no decision.
"""

import json

import pytest

from runbound import delivery, spans
from runbound.export import Exporter
from runbound.plane_types import HelloReply
from test_shared_state import FakePlane, MovableClock, remote


class Client:
    service, worker_id, key_state = "s", "w", "unknown"

    def events(self, batch):
        return True


class Spans:
    """Stands in for the OTLP exporter's counters."""

    sent, dropped, rejected, pending = 40, 3, 2, 5


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    delivery._reset_for_tests()
    monkeypatch.setattr(spans, "_provider", lambda: None)
    monkeypatch.setattr(spans, "exporter", lambda: None)
    yield
    delivery._reset_for_tests()


def world(include_events=True, **cfg):
    clock = MovableClock()
    exporter = Exporter(Client(), include_events=include_events)
    return remote(FakePlane(), clock, exporter=exporter, **cfg), clock, exporter


def hello_of(shared):
    return shared._hello_payload()


def answer(shared):
    shared.apply_hello(HelloReply(policy_version=0))


def test_no_endpoint_no_provider_and_an_open_lane():
    shared, *_ = world()
    body = hello_of(shared)["evidence_coverage"]
    assert (body["otel_endpoint_set"], body["tracer_provider_detected"], body["events_lane_state"]) == (False, False, "open")
    assert body["telemetry_stats"] == {"sent": 0, "dropped": 0, "rejected": 0, "pending": 0, "events_dropped": 0}


def test_an_endpoint_a_provider_and_the_counters_are_reported(monkeypatch):
    shared, *_ = world(otel_endpoint="http://collector:4318")
    monkeypatch.setattr(spans, "_provider", lambda: object())
    monkeypatch.setattr(spans, "exporter", lambda: Spans)
    delivery.dropped("events", "http_400", 4)
    delivery.dropped("events", "too_large", 1)
    body = hello_of(shared)["evidence_coverage"]
    assert body["otel_endpoint_set"] is True and body["tracer_provider_detected"] is True
    assert body["telemetry_stats"] == {"sent": 40, "dropped": 3, "rejected": 2, "pending": 5, "events_dropped": 5}


def test_a_tracer_provider_installed_after_init_is_seen_at_the_next_hello(monkeypatch):
    shared, *_ = world()
    assert hello_of(shared)["evidence_coverage"]["tracer_provider_detected"] is False
    answer(shared)
    first = hello_of(shared)["coverage_digest"]
    monkeypatch.setattr(spans, "_provider", lambda: object())
    after = hello_of(shared)
    assert after["evidence_coverage"]["tracer_provider_detected"] is True and after["coverage_digest"] != first


def test_the_events_lane_is_stopped_by_a_rejected_key_or_the_plan_and_disabled_by_the_customer():
    shared, _, exporter = world()
    delivery.lane_stopped("events", "key_rejected")  # a 401/403
    assert hello_of(shared)["evidence_coverage"]["events_lane_state"] == "stopped"
    delivery.lane_open("events")
    assert hello_of(shared)["evidence_coverage"]["events_lane_state"] == "open"
    exporter.include_events = False  # the plan cap: the plane switched it off, the customer did not
    assert hello_of(shared)["evidence_coverage"]["events_lane_state"] == "stopped"
    off, *_ = world(include_events=False)  # export_events=False
    assert hello_of(off)["evidence_coverage"]["events_lane_state"] == "disabled"
    nothing = remote(FakePlane(), MovableClock())  # no exporter at all
    assert hello_of(nothing)["evidence_coverage"]["events_lane_state"] == "disabled"


def test_the_digest_covers_the_state_only_so_counters_never_move_it():
    shared, *_ = world()
    first = hello_of(shared)
    answer(shared)
    delivery.dropped("events", "http_400", 9)
    second = hello_of(shared)
    assert first["coverage_digest"] == second["coverage_digest"]
    import hashlib

    state = {k: first["evidence_coverage"][k] for k in ("otel_endpoint_set", "tracer_provider_detected", "events_lane_state")}
    assert first["coverage_digest"] == hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_the_body_rides_the_first_hello_a_change_and_the_hour_and_only_the_digest_otherwise():
    shared, clock, _ = world()
    assert "evidence_coverage" in hello_of(shared)
    assert "evidence_coverage" in hello_of(shared), "not answered yet: the plane may not have it, so it is sent again"
    answer(shared)
    quiet = hello_of(shared)
    assert "evidence_coverage" not in quiet and "coverage_digest" in quiet
    delivery.lane_stopped("events", "key_rejected")
    assert "evidence_coverage" in hello_of(shared)  # the state changed
    answer(shared)
    assert "evidence_coverage" not in hello_of(shared)
    clock.advance(3599)
    assert "evidence_coverage" not in hello_of(shared)
    clock.advance(2)
    assert "evidence_coverage" in hello_of(shared), "the counters ride at most hourly"


def test_the_hello_grows_by_at_most_300_bytes_and_makes_no_call():
    shared, _, _ = world(otel_endpoint="http://collector:4318")
    plane = shared._client
    before = len(plane.calls)
    payload = hello_of(shared)
    added = {k: payload[k] for k in ("evidence_coverage", "coverage_digest")}
    assert len(json.dumps(added, separators=(",", ":"))) <= 300
    assert len(plane.calls) == before, "building the hello reads local state only"
    answer(shared)
    assert len(json.dumps({"coverage_digest": hello_of(shared)["coverage_digest"]}, separators=(",", ":"))) <= 100


def test_a_failure_building_it_never_breaks_the_hello(monkeypatch):
    shared, *_ = world()
    monkeypatch.setattr(type(shared), "_evidence_state", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    payload = hello_of(shared)
    assert "coverage_digest" not in payload and payload["service"] == "checkout"
