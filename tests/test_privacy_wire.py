"""The raw session key must not survive the wire boundary.

Detectors name the end user they are talking about — ``details["key"]`` and
``"… for session 'user:leaky-9999': …"`` — and that is deliberate: a local log
with the key in it is the useful one. This file is about the other side of that
decision: every place a record leaves the process, the key is replaced by a
short digest of itself unless the customer opted into ``send_session_keys``.

The redaction lives in :mod:`runbound.plane_types` and is applied by
``anomaly_to_wire``, the exporter and ``RemoteState.trip``, so the detectors
themselves are never touched. The generic webhook alerter that used to apply
it too left the SDK in Wave 31 — delivery, and the redaction its body needs,
is the control plane's job now, and its adapter is byte-compatible with what
this module used to send. Nothing here opens a socket: bodies are inspected
as the JSON they would have been.
"""

import json
import threading
import time

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import _policy_anomaly
from runbound.events import Anomaly
from runbound.export import Exporter
from runbound.plane_types import (
    DETAIL_STRING_MAX,
    REDACTED_ELLIPSIS,
    REDACTED_HASH_CHARS,
    anomaly_to_wire,
    key_hash,
    redact_key,
    redacted_key,
    to_wire,
)
from runbound.policy import Violation
from runbound.shared import RemoteState
from runbound.state import SessionState

KEY = "user:leaky-9999"
DIGEST = key_hash(KEY)
STAND_IN = DIGEST[:REDACTED_HASH_CHARS] + REDACTED_ELLIPSIS

#: Pieces of the key that must not appear anywhere in an outbound record. The
#: whole key, the part that identifies the human, and the bare account number.
# Every distinctive piece of KEY, and nothing that could occur by chance.
# The bare "9999" used to be here and had to go: a wire record carries
# timestamps, costs and token counts, so those four digits can turn up in a
# payload that leaked nothing at all -- and a privacy assertion that cries
# wolf is worse than no assertion, because the next person re-runs it
# instead of investigating. "leaky-9999" still pins the numeric tail; it
# just cannot be produced by an epoch or a float.
FRAGMENTS = (KEY, "user:leaky", "leaky-9999", "leaky")


def session(key: str = KEY, **tags) -> SessionState:
    return SessionState("sess-1", key=key, tags=tags or {"tier": "free"})


def policy_anomaly(state: SessionState) -> Anomaly:
    """The real anomaly a blocked tool call produces, key and all."""
    violation = Violation(
        tool="wire_transfer",
        rule="ban",
        reason="tool is banned",
        details={"limit": 0},
    )
    return _policy_anomaly(state, violation, dry_run=False)


def assert_clean(blob: str) -> None:
    for fragment in FRAGMENTS:
        assert fragment not in blob, f"{fragment!r} survived onto the wire"


# --- the anomaly the detectors actually build --------------------------------


def test_the_detector_really_does_put_the_raw_key_in_the_anomaly():
    """The leak this file exists to close: the input side is unchanged."""
    anomaly = policy_anomaly(session())

    assert anomaly.details["key"] == KEY
    assert repr(KEY) in anomaly.message


def test_the_wire_anomaly_carries_the_hash_and_not_the_key():
    wire = anomaly_to_wire(policy_anomaly(session()), "raise", DIGEST, "TS", key=KEY)

    blob = json.dumps(to_wire(wire))
    assert_clean(blob)
    assert wire.details["key"] == STAND_IN
    assert STAND_IN in wire.message
    assert wire.key_hash == DIGEST


def test_the_stand_in_is_twelve_hex_characters_and_an_ellipsis():
    assert redacted_key(KEY) == STAND_IN
    assert len(STAND_IN) == REDACTED_HASH_CHARS + 1
    assert STAND_IN[:-1] == DIGEST[:12]
    assert STAND_IN.endswith("…")


def test_opting_in_leaves_the_anomaly_exactly_as_the_detector_wrote_it():
    anomaly = policy_anomaly(session())
    wire = anomaly_to_wire(
        anomaly, "raise", DIGEST, "TS", send_session_keys=True, key=KEY
    )

    assert wire.details["key"] == KEY
    assert repr(KEY) in wire.message


def test_details_other_than_the_key_are_untouched():
    anomaly = Anomaly(
        detector="policy",
        severity="critical",
        message="blocked",
        details={"key": KEY, "tool": "wire_transfer", "count": 3, "ok": True},
    )
    wire = anomaly_to_wire(anomaly, "raise", DIGEST, "TS", key=KEY)

    assert wire.details == {
        "key": STAND_IN,
        "tool": "wire_transfer",
        "count": 3,
        "ok": True,
    }


def test_tags_are_customer_labels_and_are_left_alone():
    anomaly = Anomaly(
        detector="policy",
        severity="critical",
        message="blocked",
        details={"key": KEY, "tags": {"tier": "free", "region": "eu"}},
    )
    wire = anomaly_to_wire(anomaly, "raise", DIGEST, "TS", key=KEY)

    assert wire.details["tags"] == {"tier": "free", "region": "eu"}


def test_the_key_is_redacted_everywhere_it_appears_in_the_message():
    anomaly = Anomaly(
        detector="loop",
        severity="critical",
        message=f"session {KEY!r} looped; clear it with clear({KEY!r})",
        details={},
    )
    wire = anomaly_to_wire(anomaly, "raise", DIGEST, "TS", key=KEY)

    assert_clean(wire.message)
    assert wire.message.count(STAND_IN) == 2


def test_the_key_is_redacted_inside_nested_string_details():
    anomaly = Anomaly(
        detector="loop",
        severity="critical",
        message="looped",
        details={"who": {"key": KEY}, "seen": [f"first {KEY}", 7]},
    )
    wire = anomaly_to_wire(anomaly, "raise", DIGEST, "TS", key=KEY)

    assert_clean(json.dumps(wire.details))
    assert wire.details["who"]["key"] == STAND_IN
    assert wire.details["seen"] == [f"first {STAND_IN}", 7]


@pytest.mark.parametrize(
    "key",
    [
        "user:a.b*c(d)[e]+f?",
        "^anchor$",
        "back\\slash",
        "a|b",
        "{braces}",
    ],
)
def test_a_key_full_of_regex_metacharacters_is_still_redacted(key):
    """Redaction is ``str.replace``, never a pattern: a key is not a regex."""
    digest = key_hash(key)
    anomaly = Anomaly(
        detector="policy",
        severity="critical",
        message=f"blocked for session {key!r}",
        details={"key": key},
    )
    wire = anomaly_to_wire(anomaly, "raise", digest, "TS", key=key)

    assert key not in wire.message
    assert wire.details["key"] == redacted_key(key, digest)


def test_a_key_is_redacted_before_the_message_is_truncated():
    """Otherwise the truncation could leave half a key behind."""
    padding = "x" * (DETAIL_STRING_MAX - 4)
    anomaly = Anomaly(
        detector="loop",
        severity="critical",
        message=f"{padding} {KEY} tail",
        details={},
    )
    wire = anomaly_to_wire(anomaly, "raise", DIGEST, "TS", key=KEY)

    assert len(wire.message) == DETAIL_STRING_MAX
    assert_clean(wire.message)


def test_an_unkeyed_anomaly_is_unchanged():
    anomaly = Anomaly(detector="budget", severity="critical", message="over", details={})
    wire = anomaly_to_wire(anomaly, "raise", None, "TS", key=None)

    assert wire.message == "over"
    assert wire.details == {}


def test_the_anomaly_itself_is_never_mutated():
    anomaly = policy_anomaly(session())
    details = anomaly.details

    anomaly_to_wire(anomaly, "raise", DIGEST, "TS", key=KEY)

    assert anomaly.details is details
    assert details["key"] == KEY
    assert repr(KEY) in anomaly.message


# --- redact_key on its own ---------------------------------------------------


def test_redact_key_returns_other_types_untouched():
    marker = object()

    assert redact_key(marker, KEY) is marker
    assert redact_key(7, KEY) == 7
    assert redact_key(None, KEY) is None


def test_redact_key_with_no_key_to_look_for_changes_nothing():
    text = f"session {KEY}"

    assert redact_key(text, None) == text
    assert redact_key(text, "") == text
    assert redact_key(text, 42) == text


def test_redact_key_survives_a_self_referencing_details_dict():
    details: dict = {"key": KEY}
    details["self"] = details

    redacted = redact_key(details, KEY, DIGEST)

    assert redacted["key"] == STAND_IN


# --- the exporter ------------------------------------------------------------


class Spy:
    """A plane client that keeps the batches it was handed."""

    service = "checkout"
    worker_id = "host-1:42"
    key_state = "ok"

    def __init__(self) -> None:
        self.batches: list[dict] = []

    def events(self, batch: dict) -> bool:
        self.batches.append(batch)
        return True


def drained(exporter: Exporter, client: Spy) -> str:
    exporter.flush()
    assert client.batches, "nothing was posted"
    return json.dumps(client.batches, ensure_ascii=False)


def test_an_exported_anomaly_carries_no_raw_key():
    client = Spy()
    exporter = Exporter(client)
    exporter.on_anomaly(session(), policy_anomaly(session()), "raise")

    blob = drained(exporter, client)
    assert_clean(blob)
    assert STAND_IN in blob


def test_an_exported_trip_carries_no_raw_key():
    client = Spy()
    exporter = Exporter(client)
    exporter.on_trip(session(), policy_anomaly(session()), "door")

    assert_clean(drained(exporter, client))


def test_the_exporter_sends_raw_keys_only_when_told_to():
    client = Spy()
    exporter = Exporter(client, send_session_keys=True)
    exporter.on_anomaly(session(), policy_anomaly(session()), "raise")

    assert KEY in drained(exporter, client)


def test_build_gives_the_exporter_the_customers_setting():
    from runbound.shared import build

    default = build(
        GuardrailConfig(control_plane_url="https://plane.test", token="k")
    )
    opted_in = build(
        GuardrailConfig(
            control_plane_url="https://plane.test", token="k", send_session_keys=True
        )
    )

    assert default._exporter.send_session_keys is False
    assert opted_in._exporter.send_session_keys is True


# --- the synchronous trip report ---------------------------------------------


class TripSpy:
    """Just enough of a plane client for ``RemoteState.trip``."""

    key_state = "ok"
    consecutive_failures = 0

    def __init__(self) -> None:
        self.reports: list = []

    def trip(self, report) -> bool:
        self.reports.append(report)
        return True


def config(**kwargs) -> GuardrailConfig:
    fields = {"control_plane_url": "https://plane.test", "token": "k"}
    fields.update(kwargs)
    return GuardrailConfig(**fields)


def test_the_trip_report_carries_no_raw_key():
    client = TripSpy()
    state = session()
    shared = RemoteState(client, None, config())

    shared.trip(KEY, state, policy_anomaly(state), None, door=False)

    assert_clean(json.dumps(to_wire(client.reports[0])))
    assert client.reports[0].key_hash == DIGEST


def test_the_trip_report_carries_the_key_when_the_customer_opted_in():
    client = TripSpy()
    state = session()
    shared = RemoteState(client, None, config(send_session_keys=True))

    shared.trip(KEY, state, policy_anomaly(state), None, door=False)

    assert client.reports[0].anomaly["details"]["key"] == KEY


def test_a_broken_details_dict_does_not_cost_the_export():
    client = Spy()
    exporter = Exporter(client)
    anomaly = Anomaly(
        detector="policy",
        severity="critical",
        message="blocked",
        details=_Exploding({"key": KEY}),
    )

    assert exporter.on_anomaly(session(), anomaly, "raise") is None
    assert exporter.dropped == 1
    assert exporter.pending == 0


class _Exploding(dict):
    """A details dict that raises when it is walked."""

    def items(self):
        raise RuntimeError("no")


def teardown_module(module) -> None:
    """No exporter thread may outlive this file."""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if "runbound" in t.name]:
            return
        time.sleep(0.01)
