"""Every refusal is a record: the once-per-session dedup no longer drops them.

``Engine._alert`` used to remember one notification per ``(session, detector,
severity, ...)`` and drop everything after it, so the second refusal in a
session, even of a different tool, never reached ``runbound.events()``,
``runbound.decisions()`` or the export. Now a refusal is recorded and exported
each time, up to ``REFUSAL_RECORD_CAP`` per ``(session, detector, rule, tool)``,
and the rest are counted and summarised when the session block exits.
"""

import logging

import pytest

import runbound
from runbound import api, local_events
from runbound import engine as engine_module
from runbound import export as export_module
from runbound.exceptions import GuardrailTripped

from test_export import FakeClient, anomaly, event, exporter, session as export_session


class Recorder:
    """An engine observer that keeps what it is told, in order."""

    def __init__(self):
        self.rows = []

    def on_anomaly(self, session, anomaly, reacted):
        self.rows.append((anomaly.detector, anomaly.details.get("tool"), anomaly.details.get("suppressed_count"), reacted))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _declare_tools():
    """Declared after init(), on every test: another test module resetting the
    registry must not take these away (the tool rules live in it)."""

    def issue_refund():
        return "ran"

    def transfer_call():
        return "ran"

    def delete_account():
        return "ran"

    globals()["issue_refund"] = runbound.tool(effects={"financial"})(issue_refund)
    globals()["transfer_call"] = runbound.tool(effects={"external"})(transfer_call)
    globals()["delete_account"] = runbound.tool(effects={"write"}, blocked=True)(delete_account)


@pytest.fixture
def world():
    logging.disable(logging.CRITICAL)
    local_events.clear_for_tests()
    runbound.init(on_anomaly="raise", tool_policy={"on_violation": "block"}, loop_threshold=1000, loop_window=1000)
    _declare_tools()
    recorder = Recorder()
    api._ENGINE.observers.append(recorder)
    yield recorder
    runbound.exit_safe_mode()
    runbound.reset()
    local_events.clear_for_tests()
    logging.disable(logging.NOTSET)


def refused(fn) -> bool:
    try:
        fn()
        return False
    except GuardrailTripped:
        return True


def restricted_call(key="call:1"):
    runbound.enter_safe_mode("test")  # process posture: restricted
    return runbound.session(key)


# --- R1: a refusal is its own record ------------------------------------------------------------


def test_a_refused_transfer_after_a_refused_refund_is_its_own_decision(world):
    with restricted_call():
        assert refused(issue_refund) and refused(transfer_call)

    tools = [tool for _detector, tool, _count, _reacted in world.rows]
    assert tools == ["issue_refund", "transfer_call"]
    assert len(runbound.decisions()) == 2  # the transfer's own Decision, not a dropped one


def test_every_refusal_under_the_cap_is_recorded_in_events_and_decisions(world):
    with restricted_call():
        for _ in range(5):
            assert refused(issue_refund)

    anomalies = [e for e in runbound.events() if e.get("kind") == "anomaly"]
    assert len(anomalies) == 5 and len(world.rows) == 5
    assert len(runbound.decisions()) == 5


def test_150_identical_refusals_are_100_rows_and_one_summary_of_50(world):
    with restricted_call():
        for _ in range(150):
            assert refused(issue_refund)

    plain = [row for row in world.rows if row[2] is None]
    summaries = [row for row in world.rows if row[2] is not None]
    assert len(plain) == engine_module.REFUSAL_RECORD_CAP == 100
    assert summaries == [("safe_mode", "issue_refund", 50, "blocked")]
    assert len(runbound.decisions()) == 100  # a summary carries no Decision of its own


def test_two_tools_storming_do_not_hide_each_other(world):
    with restricted_call():
        for _ in range(120):
            refused(issue_refund)
            refused(transfer_call)

    summaries = sorted((tool, count) for _d, tool, count, _r in world.rows if count is not None)
    assert summaries == [("issue_refund", 20), ("transfer_call", 20)]
    per_tool = {tool: sum(1 for _d, t, c, _r in world.rows if t == tool and c is None) for tool in ("issue_refund", "transfer_call")}
    assert per_tool == {"issue_refund": 100, "transfer_call": 100}


def test_two_rules_storming_do_not_hide_each_other(world):
    """A posture refusal (rule from the posture) and a policy block (rule "blocked")
    on different tools are counted apart, and so are their summaries."""
    with restricted_call():
        for _ in range(110):
            refused(issue_refund)  # posture
            refused(delete_account)  # blocked=True: a tool rule

    summaries = {(tool, count) for _d, tool, count, _r in world.rows if count is not None}
    assert summaries == {("issue_refund", 10), ("delete_account", 10)}


def test_a_summary_is_reported_once_and_then_only_for_new_refusals(world):
    with restricted_call():
        for _ in range(103):
            refused(issue_refund)
    with runbound.session("call:1"):
        for _ in range(4):
            refused(issue_refund)

    counts = [count for _d, _t, count, _r in world.rows if count is not None]
    assert counts == [3, 4]  # they add up to what was suppressed, never repeat


def test_the_cap_is_per_session_not_shared_between_sessions(world):
    runbound.enter_safe_mode("test")
    with runbound.session("call:a"):
        for _ in range(100):
            refused(issue_refund)
    with runbound.session("call:b"):
        for _ in range(100):
            refused(issue_refund)

    assert [c for _d, _t, c, _r in world.rows if c is not None] == []  # neither passed the cap
    assert len(world.rows) == 200


def test_the_summary_is_queued_before_the_sessions_exit_record(world, monkeypatch):
    order = []
    monkeypatch.setattr(api, "_sync_exit", lambda key, state: order.append("exit"))
    world.on_anomaly = lambda s, a, r: order.append("summary" if a.details.get("suppressed_count") else "refusal")
    with restricted_call():
        for _ in range(101):
            refused(issue_refund)

    assert order[-2:] == ["summary", "exit"]


def test_the_loop_detector_still_notifies_once_per_key_under_throttle(world):
    """The dedup that stays: a throttled loop is one notification, not a storm."""
    runbound.init(
        on_anomaly="raise", on_loop="throttle", loop_threshold=3,
        throttle_base_seconds=0.001, throttle_max_seconds=0.001,
    )
    recorder = Recorder()
    api._ENGINE.observers.append(recorder)
    world = recorder

    @runbound.tool(effects={"read"}, reviewed=True)
    def lookup():
        return "ran"

    with runbound.session("call:loop"):
        for _ in range(12):
            lookup()

    loops = [row for row in world.rows if row[0] == "loop"]
    assert len(loops) == 1


# --- R2(b): a refusal still exports over the telemetry cap ---------------------------------------------


def wire_kinds(client):
    batch = client.batches[0]
    return len(batch["events"]), [a["detector"] for a in batch["anomalies"]]


@pytest.mark.parametrize("reacted", ["raise", "blocked", "door"])
def test_over_the_cap_a_refusal_still_exports_and_a_plain_call_does_not(reacted):
    client = FakeClient()
    sink = exporter(client)
    sink.include_events = False  # what the plane's events_over_cap does

    sink.on_event(export_session(), event())
    sink.on_anomaly(export_session(), anomaly("safe_mode"), reacted)
    sink.flush()

    assert wire_kinds(client) == (0, ["safe_mode"])


@pytest.mark.parametrize("reacted", ["warn", "callback", "dry_run"])
def test_over_the_cap_an_observation_is_still_telemetry_and_stays_silent(reacted):
    client = FakeClient()
    sink = exporter(client)
    sink.include_events = False

    sink.on_anomaly(export_session(), anomaly("spike"), reacted)
    sink.flush()

    assert client.batches == []


def test_the_customers_own_export_events_off_is_honoured_for_refusals():
    client = FakeClient()
    sink = exporter(client, include_events=False)  # the customer's choice

    sink.on_anomaly(export_session(), anomaly("safe_mode"), "blocked")
    sink.flush()

    assert client.batches == []


def test_the_plane_lifting_the_cap_does_not_override_the_customers_choice():
    sink = exporter(FakeClient(), include_events=False)
    sink.include_events = True  # what shared._set_include_events would (wrongly) do alone
    assert sink.customer_events is False
