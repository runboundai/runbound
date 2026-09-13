"""T166 -- one anomaly, one identity, on both channels.

An anomaly that stops a session is reported to the control plane twice: the
synchronous trip (``POST /v1/trip``, so the rest of the fleet learns the
latch now) and the telemetry export of the same anomaly (``POST /v1/events``,
whenever the exporter next flushes). Before this the plane had to guess that
the two reports were one fact. Now the anomaly says so itself: ``Anomaly``
stamps an ``anomaly_id`` on itself at construction and every wire form
carries it unchanged.

Privacy: the id is ``uuid4().hex`` -- random, never derived from the session
key, the org or the message -- so a customer's key cannot be recovered from
a ledger row, and two runs of the same code never produce the same id.
"""

import dataclasses
import re

from runbound.events import Anomaly
from runbound.plane_types import (
    TripReport,
    WireAnomaly,
    anomaly_to_wire,
    from_wire,
    key_hash,
    to_wire,
)

TS = "2026-09-13T12:00:00Z"


def an_anomaly(**overrides) -> Anomaly:
    fields = dict(
        detector="budget",
        severity="critical",
        message="spend 12.50 over budget 10.00",
        details={"spend_usd": 12.5},
    )
    fields.update(overrides)
    return Anomaly(**fields)


# --- the id itself ---------------------------------------------------------


def test_every_anomaly_is_stamped_with_an_id():
    assert re.fullmatch(r"[0-9a-f]{32}", an_anomaly().anomaly_id)


def test_two_anomalies_built_in_the_same_microsecond_have_different_ids():
    # Nothing sleeps here on purpose: the ids must differ because they are
    # random, not because time passed between the two constructions.
    ids = {an_anomaly().anomaly_id for _ in range(1000)}

    assert len(ids) == 1000


def test_the_id_is_not_derived_from_anything_about_the_session():
    # The same detector, the same message, the same key -- and two ids, so
    # no ledger row can be turned back into the key it is about.
    first = an_anomaly(details={"key": "user:9"})
    second = an_anomaly(details={"key": "user:9"})

    assert first.anomaly_id != second.anomaly_id
    assert key_hash("user:9") not in (first.anomaly_id, second.anomaly_id)


def test_the_id_is_the_last_field_so_positional_construction_still_works():
    anomaly = Anomaly("loop", "critical", "looping", {"count": 9})

    assert (anomaly.detector, anomaly.message) == ("loop", "looping")
    assert anomaly.anomaly_id


def test_an_id_can_be_stated_when_a_caller_has_one():
    assert an_anomaly(anomaly_id="fixed").anomaly_id == "fixed"


def test_the_id_survives_a_frozen_replace():
    anomaly = an_anomaly()

    assert dataclasses.replace(anomaly, severity="warn").anomaly_id == anomaly.anomaly_id


# --- the id on the wire ----------------------------------------------------


def test_the_wire_anomaly_carries_the_id():
    anomaly = an_anomaly()

    wire = anomaly_to_wire(anomaly, "raise", None, TS)

    assert wire.anomaly_id == anomaly.anomaly_id


def test_the_id_survives_redaction_and_truncation():
    anomaly = an_anomaly(message="over budget for user:9" * 40, details={"key": "user:9"})

    wire = anomaly_to_wire(anomaly, "raise", key_hash("user:9"), TS, key="user:9")

    assert wire.anomaly_id == anomaly.anomaly_id
    assert "user:9" not in wire.message


def test_an_anomaly_like_object_without_an_id_sends_an_empty_one():
    # A test double, or an anomaly built by code older than this release:
    # the wire form is still valid and the plane falls back to its
    # pre-0.3.0 dedupe.
    class Double:
        detector = "budget"
        severity = "critical"
        message = "over"
        details: dict = {}

    assert anomaly_to_wire(Double(), "raise", None, TS).anomaly_id == ""


def test_a_non_string_id_never_reaches_the_wire():
    class Double:
        detector = "budget"
        severity = "critical"
        message = "over"
        details: dict = {}
        anomaly_id = 17

    assert anomaly_to_wire(Double(), "raise", None, TS).anomaly_id == ""


def test_the_wire_anomaly_round_trips_its_id():
    wire = WireAnomaly(detector="budget", anomaly_id="abc123")

    assert from_wire(WireAnomaly, to_wire(wire)).anomaly_id == "abc123"


def test_a_payload_from_an_older_sdk_decodes_to_an_empty_id():
    old = {"ts_wall": TS, "detector": "budget", "severity": "critical", "reacted": "raise"}

    assert from_wire(WireAnomaly, old).anomaly_id == ""


# --- the id on a trip report ----------------------------------------------


def test_a_trip_report_can_carry_the_id_and_the_worker():
    report = TripReport(key_hash="a" * 64, anomaly_id="abc123", worker_id="host-1:42")

    assert from_wire(TripReport, to_wire(report)).anomaly_id == "abc123"
    assert from_wire(TripReport, to_wire(report)).worker_id == "host-1:42"


def test_a_trip_report_from_an_older_sdk_names_neither():
    old = {"key_hash": "a" * 64, "anomaly": {}, "strikes": 1}

    report = from_wire(TripReport, old)

    assert (report.anomaly_id, report.worker_id) == ("", "")


# --- one anomaly, two channels, one id ------------------------------------


def test_the_trip_and_the_telemetry_export_of_one_anomaly_agree_on_its_id():
    # The whole point of the id: this is the pair the plane has to file as
    # one refusal. Both channels are driven here from the same `Anomaly`
    # object, exactly as `engine._latch` drives them.
    from runbound.export import Exporter
    from runbound.state import SessionState

    class Client:
        service = "checkout"
        worker_id = "host-1:42"
        key_state = "unknown"

        def __init__(self) -> None:
            self.batches: list[dict] = []

        def events(self, batch: dict) -> bool:
            self.batches.append(batch)
            return True

    class Plane(Client):
        def __init__(self) -> None:
            super().__init__()
            self.reports: list[TripReport] = []

        def trip(self, report: TripReport) -> object:
            self.reports.append(report)
            return {"ack": True}

    from runbound.config import GuardrailConfig
    from runbound.shared import RemoteState

    anomaly = an_anomaly()
    session = SessionState("sess-1", key="user-9")
    plane = Plane()
    sink = Exporter(plane, maxlen=100, flush_every_s=60.0, batch_size=200)
    shared = RemoteState(plane, sink, GuardrailConfig(control_plane_url="http://plane"))

    shared.trip("user-9", session, anomaly, 60.0, False)
    sink.on_anomaly(session, anomaly, "raise")
    sink.flush()

    exported = plane.batches[-1]["anomalies"][0]
    assert plane.reports[0].anomaly_id == anomaly.anomaly_id
    assert plane.reports[0].anomaly["anomaly_id"] == anomaly.anomaly_id
    assert exported["anomaly_id"] == anomaly.anomaly_id
