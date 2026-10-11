"""Tests for the control-plane wire contract.

These types are the only shape the SDK ever puts on the network, so the
tests care about two things above all: that a round trip preserves what it
should, and that nothing the customer never agreed to send (raw keys, error
text, arbitrary objects) can reach the wire through them.
"""

import dataclasses

import pytest

from runbound.events import Anomaly, Event
from runbound.plane_types import (
    DETAIL_KEYS_MAX,
    DETAIL_STRING_MAX,
    TAG_KEYS_MAX,
    TAG_VALUE_MAX,
    HelloReply,
    PlaneStatus,
    WireAnomaly,
    WireEvent,
    anomaly_to_wire,
    event_to_wire,
    from_wire,
    key_hash,
    scrub_details,
    scrub_tags,
    to_wire,
)

TS = "2026-09-05T12:00:00Z"


def llm_event(**overrides) -> Event:
    fields = dict(
        kind="llm_call",
        ts=1.0,
        step=3,
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.5,
        model="gpt-4o",
        duration_s=1.5,
        tokens_reasoning=4,
    )
    fields.update(overrides)
    return Event(**fields)


# --- key_hash ---------------------------------------------------------------


def test_key_hash_is_deterministic():
    assert key_hash("user-42") == key_hash("user-42")


def test_key_hash_is_sha256_hex():
    digest = key_hash("user-42")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_key_hash_distinguishes_keys():
    assert key_hash("user-42") != key_hash("user-43")


def test_key_hash_never_contains_the_key():
    assert "user-42" not in key_hash("user-42")


def test_key_hash_handles_unicode():
    assert key_hash("ключ-☃") == key_hash("ключ-☃")
    assert len(key_hash("ключ-☃")) == 64


def test_key_hash_replaces_unencodable_characters():
    # A lone surrogate cannot be utf-8 encoded; "replace" keeps us from
    # raising inside a hot path over a key we did not choose.
    assert len(key_hash("bad-\ud800-key")) == 64


def test_key_hash_of_empty_string():
    assert len(key_hash("")) == 64


# --- to_wire / from_wire ----------------------------------------------------


ROUND_TRIPS = [
    WireEvent(
        ts_wall=TS,
        kind="tool_call",
        key_hash="abc",
        step=2,
        tool_name=None,
        args_hash=None,
        duration_s=1.5,
        error_class=None,
    ),
    WireAnomaly(
        ts_wall=TS,
        key_hash=None,
        detector="budget",
        severity="critical",
        message="Budget exceeded",
        details={"total_cost_usd": 1.25},
        reacted="raise",
    ),
    HelloReply(
        org_id="org_1",
        plan="pro",
        entitlements={"fleet": True},
        halt=False,
        policy_version=5,
        poll_s=10.0,
        notice="upgrade soon",
    ),
    PlaneStatus(
        mode="degraded",
        last_contact_age_s=12.5,
        consecutive_failures=3,
        notice=None,
        halt_stale_s=12.5,
        entries_window={"plane": 2, "cache": 5, "local": 13},
        entries_local_share=0.65,
        reason="entry timeouts",
    ),
]


@pytest.mark.parametrize("obj", ROUND_TRIPS, ids=lambda o: type(o).__name__)
def test_round_trip_preserves_every_field(obj):
    assert from_wire(type(obj), to_wire(obj)) == obj


@pytest.mark.parametrize("obj", ROUND_TRIPS, ids=lambda o: type(o).__name__)
def test_to_wire_returns_a_plain_dict(obj):
    wire = to_wire(obj)
    assert isinstance(wire, dict)
    assert set(wire) == {f.name for f in dataclasses.fields(obj)}


@pytest.mark.parametrize("cls", [type(o) for o in ROUND_TRIPS], ids=lambda c: c.__name__)
def test_wire_types_are_frozen(cls):
    obj = cls()
    with pytest.raises(dataclasses.FrozenInstanceError):
        obj.__setattr__(dataclasses.fields(cls)[0].name, "x")


def test_from_wire_fills_missing_keys_with_defaults():
    reply = from_wire(HelloReply, {"poll_s": 2.0})

    assert reply.poll_s == 2.0
    assert reply.halt is False
    assert reply.notice is None
    assert reply.policy_version == 0
    assert reply.tools_known is True


def test_from_wire_ignores_extra_keys():
    status = from_wire(
        PlaneStatus, {"mode": "connected", "future_field": {"nested": 1}}
    )

    assert status.mode == "connected"
    assert not hasattr(status, "future_field")


def test_from_wire_falls_back_to_defaults_on_wrong_types():
    reply = from_wire(
        HelloReply,
        {
            "halt": "yes",  # not a bool
            "poll_s": "lots",  # not a number
            "policy_version": 1.5,  # not an int
            "entitlements": [1, 2],  # not a dict
            "plan": "pro",  # good, survives
        },
    )

    assert reply.halt is False  # the default, not "yes"
    assert reply.poll_s == 0.0
    assert reply.policy_version == 0
    assert reply.entitlements == {}
    assert reply.plan == "pro"


def test_from_wire_accepts_an_int_for_a_float_field():
    reply = from_wire(HelloReply, {"poll_s": 3})

    assert reply.poll_s == 3.0
    assert isinstance(reply.poll_s, float)


def test_from_wire_rejects_a_bool_for_an_int_field():
    reply = from_wire(HelloReply, {"policy_version": True})

    assert reply.policy_version == 0


def test_from_wire_accepts_none_for_optional_fields():
    reply = from_wire(HelloReply, {"notice": None, "org_id": None})

    assert reply.notice is None
    assert reply.org_id == ""  # org_id is not optional; wrong type -> default


def test_a_plane_that_never_heard_of_tool_reports_is_assumed_to_have_ours():
    """``tools_known`` defaults to True, and the default is load-bearing.

    A plane too old to know the field omits it. Defaulting to False there would
    have every worker resend its whole tool report on every heartbeat, forever,
    to a plane that was never going to store it.
    """
    assert from_wire(HelloReply, {"org_id": "acme"}).tools_known is True
    assert from_wire(HelloReply, {"tools_known": False}).tools_known is False
    assert from_wire(HelloReply, {"tools_known": "nope"}).tools_known is True


def test_from_wire_tolerates_a_non_dict_payload():
    assert from_wire(PlaneStatus, None) == PlaneStatus()
    assert from_wire(PlaneStatus, ["mode"]) == PlaneStatus()


def test_from_wire_copies_dicts_so_the_payload_cannot_be_mutated_later():
    payload = {"details": {"a": 1}}
    anomaly = from_wire(WireAnomaly, payload)
    payload["details"]["a"] = 2

    assert anomaly.details == {"a": 1}


# --- event_to_wire ----------------------------------------------------------


def test_event_to_wire_copies_the_numbers():
    wire = event_to_wire(llm_event(), key_hash="kh", ts_wall=TS)

    assert wire.ts_wall == TS
    assert wire.kind == "llm_call"
    assert wire.key_hash == "kh"
    assert wire.step == 3
    assert wire.duration_s == 1.5


def test_event_to_wire_keeps_tool_name_and_args_hash():
    event = Event(kind="tool_call", ts=1.0, step=1, tool_name="search", args_hash="h1")
    wire = event_to_wire(event, key_hash=None, ts_wall=TS)

    assert wire.tool_name == "search"
    assert wire.args_hash == "h1"
    assert wire.key_hash is None


def test_event_to_wire_has_no_error_text_field_at_all():
    names = {f.name for f in dataclasses.fields(WireEvent)}

    assert "error" not in names


def test_event_to_wire_drops_the_error_message():
    event = Event(
        kind="llm_error",
        ts=1.0,
        step=1,
        error="RateLimitError: key sk-secret for tenant acme is over quota",
    )
    wire = event_to_wire(event, key_hash=None, ts_wall=TS)

    assert wire.error_class == "RateLimitError"
    assert "acme" not in str(to_wire(wire))
    assert "sk-secret" not in str(to_wire(wire))


def test_event_to_wire_takes_the_last_segment_of_a_dotted_class():
    event = Event(
        kind="llm_error", ts=1.0, step=1, error="openai.APITimeoutError: timed out"
    )

    assert event_to_wire(event, None, TS).error_class == "APITimeoutError"


def test_event_to_wire_recognizes_a_bare_exception_class_name():
    # engine._error_text falls back to type(exc).__name__ when str() raises.
    event = Event(kind="llm_error", ts=1.0, step=1, error="TimeoutError")

    assert event_to_wire(event, None, TS).error_class == "TimeoutError"


@pytest.mark.parametrize(
    "error",
    [
        None,
        "",
        "connection reset by peer",
        "user bob@example.com: not found",
        "https://api.example.com: unreachable",
        "429 too many requests",
    ],
)
def test_event_to_wire_reports_no_class_when_none_is_derivable(error):
    event = Event(kind="llm_error", ts=1.0, step=1, error=error)

    assert event_to_wire(event, None, TS).error_class is None


def test_a_wire_event_has_exactly_the_tool_call_fields():
    """The plane reads these and nothing else; model-call fields (tokens, cost, model, provider) are gone."""
    assert {f.name for f in dataclasses.fields(WireEvent)} == {
        "ts_wall", "kind", "key_hash", "step", "tool_name", "args_hash", "duration_s", "error_class",
    }


def test_wire_event_never_carries_loop_exempt():
    """loop_exempt is local bookkeeping for the loop window; it never ships."""
    names = {f.name for f in dataclasses.fields(WireEvent)}

    assert "loop_exempt" not in names


def test_event_to_wire_round_trips():
    wire = event_to_wire(llm_event(), key_hash="kh", ts_wall=TS)

    assert from_wire(WireEvent, to_wire(wire)) == wire


# --- anomaly_to_wire --------------------------------------------------------


def test_anomaly_to_wire_carries_the_verdict():
    anomaly = Anomaly(
        detector="budget",
        severity="critical",
        message="Budget exceeded: $1.25 spent",
        details={"total_cost_usd": 1.25, "limit_hit": "budget_usd"},
    )
    wire = anomaly_to_wire(anomaly, reacted="raise", key_hash="kh", ts_wall=TS)

    assert wire.detector == "budget"
    assert wire.severity == "critical"
    assert wire.message == "Budget exceeded: $1.25 spent"
    assert wire.reacted == "raise"
    assert wire.key_hash == "kh"
    assert wire.ts_wall == TS
    assert wire.details == {"total_cost_usd": 1.25, "limit_hit": "budget_usd"}


def test_wire_anomaly_has_no_error_field_at_all():
    names = {f.name for f in dataclasses.fields(WireAnomaly)}

    assert "error" not in names
    assert "error_class" not in names


def test_anomaly_to_wire_drops_callables_from_details():
    anomaly = Anomaly(
        detector="loop",
        severity="warn",
        message="loop",
        details={"callback": len, "repeats": 3},
    )
    wire = anomaly_to_wire(anomaly, "warn", None, TS)

    assert wire.details == {"repeats": 3}


def test_anomaly_to_wire_drops_arbitrary_objects_from_details():
    class Session:
        pass

    anomaly = Anomaly(
        detector="loop",
        severity="warn",
        message="loop",
        details={"session": Session(), "repeats": 3},
    )
    wire = anomaly_to_wire(anomaly, "warn", None, TS)

    assert wire.details == {"repeats": 3}


def test_anomaly_to_wire_truncates_long_strings():
    anomaly = Anomaly(
        detector="loop", severity="warn", message="loop", details={"blob": "x" * 5000}
    )
    wire = anomaly_to_wire(anomaly, "warn", None, TS)

    assert len(wire.details["blob"]) == DETAIL_STRING_MAX


def test_anomaly_to_wire_caps_the_number_of_detail_keys():
    details = {f"k{i}": i for i in range(500)}
    wire = anomaly_to_wire(
        Anomaly(detector="loop", severity="warn", message="loop", details=details),
        "warn",
        None,
        TS,
    )

    assert len(wire.details) == DETAIL_KEYS_MAX


def test_anomaly_to_wire_keeps_nested_scalars_and_lists():
    details = {
        "nested": {"a": 1, "b": [1, 2.5, "three", True, None]},
        "empty": {},
    }
    wire = anomaly_to_wire(
        Anomaly(detector="loop", severity="warn", message="loop", details=details),
        "warn",
        None,
        TS,
    )

    assert wire.details == details


def test_anomaly_to_wire_keeps_a_url_that_is_already_a_string():
    details = {"webhook": "https://hooks.slack.com/services/TDEMO1/BDEMO1/xyz"}
    wire = anomaly_to_wire(
        Anomaly(detector="loop", severity="warn", message="loop", details=details),
        "warn",
        None,
        TS,
    )

    assert wire.details["webhook"] == details["webhook"]


def test_anomaly_to_wire_drops_objects_nested_in_lists():
    details = {"items": [1, object(), "two"]}
    wire = anomaly_to_wire(
        Anomaly(detector="loop", severity="warn", message="loop", details=details),
        "warn",
        None,
        TS,
    )

    assert wire.details["items"] == [1, "two"]


def test_anomaly_to_wire_drops_non_string_dict_keys():
    wire = anomaly_to_wire(
        Anomaly(
            detector="loop", severity="warn", message="loop", details={1: "a", "b": 2}
        ),
        "warn",
        None,
        TS,
    )

    assert wire.details == {"b": 2}


def test_anomaly_to_wire_survives_a_self_referencing_details_dict():
    details = {"n": 1}
    details["self"] = details

    wire = anomaly_to_wire(
        Anomaly(detector="loop", severity="warn", message="loop", details=details),
        "warn",
        None,
        TS,
    )

    assert wire.details["n"] == 1


def test_anomaly_to_wire_tolerates_details_that_is_not_a_dict():
    wire = anomaly_to_wire(
        Anomaly(detector="loop", severity="warn", message="loop", details=None),
        "warn",
        None,
        TS,
    )

    assert wire.details == {}


def test_anomaly_to_wire_does_not_mutate_the_anomaly():
    details = {"repeats": 3, "callback": len}
    anomaly = Anomaly(
        detector="loop", severity="warn", message="loop", details=details
    )
    anomaly_to_wire(anomaly, "warn", None, TS)

    assert anomaly.details == details


def test_anomaly_to_wire_truncates_a_long_message():
    anomaly = Anomaly(
        detector="loop", severity="warn", message="m" * 10_000, details={}
    )

    assert len(anomaly_to_wire(anomaly, "warn", None, TS).message) == DETAIL_STRING_MAX


def test_anomaly_to_wire_round_trips():
    wire = anomaly_to_wire(
        Anomaly(detector="budget", severity="critical", message="m", details={"a": 1}),
        "raise",
        "kh",
        TS,
    )

    assert from_wire(WireAnomaly, to_wire(wire)) == wire


# --- scrub_tags -------------------------------------------------------------


def test_scrub_tags_caps_the_number_of_keys():
    tags = {f"t{i}": "v" for i in range(200)}

    assert len(scrub_tags(tags)) == TAG_KEYS_MAX


def test_scrub_tags_truncates_values():
    scrubbed = scrub_tags({"tenant": "z" * 500})

    assert len(scrubbed["tenant"]) == TAG_VALUE_MAX


def test_scrub_tags_stringifies_non_string_values():
    assert scrub_tags({"n": 5, "flag": True}) == {"n": "5", "flag": "True"}


def test_scrub_tags_drops_non_string_keys():
    assert scrub_tags({1: "a", "b": "c"}) == {"b": "c"}


def test_scrub_tags_truncates_long_keys():
    scrubbed = scrub_tags({"k" * 500: "v"})

    assert list(scrubbed) == ["k" * TAG_VALUE_MAX]


def test_scrub_tags_of_empty_and_none():
    assert scrub_tags(None) == {}
    assert scrub_tags({}) == {}
    assert scrub_tags("not a dict") == {}


def test_scrub_tags_drops_a_value_that_cannot_be_stringified():
    class Rude:
        def __str__(self):
            raise RuntimeError("no")

    assert scrub_tags({"a": Rude(), "b": "ok"}) == {"b": "ok"}


# --- scrub_details ----------------------------------------------------------


def test_scrub_details_is_json_serializable():
    import json

    details = {
        "n": 1,
        "f": 1.5,
        "s": "x",
        "b": True,
        "none": None,
        "list": [1, "a"],
        "dict": {"k": "v"},
        "bad": object(),
        "fn": print,
    }

    json.dumps(scrub_details(details))  # must not raise


def test_scrub_details_caps_list_length():
    scrubbed = scrub_details({"items": list(range(500))})

    assert len(scrubbed["items"]) == DETAIL_KEYS_MAX


def test_scrub_details_converts_a_tuple_to_a_list():
    assert scrub_details({"pair": (1, 2)}) == {"pair": [1, 2]}


def test_a_hello_reply_has_no_circuits_field_and_ignores_one_on_the_wire():
    from runbound.plane_types import from_wire

    assert "circuits" not in {f.name for f in dataclasses.fields(HelloReply)}
    got = from_wire(HelloReply, {"policy_version": 4, "circuits": {"openai": "open"}})
    assert got.policy_version == 4 and not hasattr(got, "circuits")
