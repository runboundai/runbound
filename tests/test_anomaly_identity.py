"""One anomaly, one identity, on both channels.

An anomaly reaches the caller (as ``exc.anomaly``) and the control plane (the
telemetry export) as one fact. ``Anomaly`` stamps an ``anomaly_id`` on itself
at construction and every form of it carries that id unchanged.

Privacy: the id is ``uuid4().hex`` -- random, never derived from the session
key, the org or the message -- so a customer's key cannot be recovered from
a ledger row, and two runs of the same code never produce the same id.
"""

import dataclasses
import re

from runbound.events import Anomaly
from runbound.plane_types import (
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


# --- one refusal, both channels, one id ------------------------------------


def test_the_refusal_and_the_telemetry_export_agree_on_one_id():
    # The whole point of the id: the exception the caller catches, the local
    # event record and the exported anomaly are one refusal, not three.
    import pytest

    from runbound import api
    from runbound.config import RunboundConfig
    from runbound.engine import Engine
    from runbound.events import Decision
    from runbound.export import Exporter
    from runbound.exceptions import GuardrailTripped
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

    api._teardown_for_tests()
    try:
        anomaly = an_anomaly(detector="policy")
        session = SessionState("sess-1", key="user-9")
        plane = Client()
        sink = Exporter(plane, maxlen=100, flush_every_s=60.0, batch_size=200)
        engine = Engine(RunboundConfig(), observers=[sink])

        with pytest.raises(GuardrailTripped) as excinfo:
            engine.refuse(session, anomaly, Decision(verdict="deny", kind="action"), reacted="blocked")
        sink.flush()

        exported = plane.batches[-1]["anomalies"][0]
        assert excinfo.value.anomaly.anomaly_id == anomaly.anomaly_id
        assert exported["anomaly_id"] == anomaly.anomaly_id
    finally:
        api._teardown_for_tests()
