"""Local telemetry is free.

``runbound.events(n=100)`` and ``runbound.decisions(n=100)`` read an
in-memory ring of this process's own anomalies, refusals, runtime changes and
Decisions -- no account, no plane, nothing written to disk, nothing sent
anywhere. An optional ``on_event=`` callback on ``init()`` is told about every
record as it is appended, and a callback that raises must never break the host
call (fail-open, the golden rule).
"""

import pytest

import runbound
from runbound import api, local_events
from runbound.exceptions import ExecutionRefused


def setup_function(_fn):
    api._teardown_for_tests()


def teardown_function(_fn):
    api._teardown_for_tests()


def _over_the_limit(secret: str | None = None) -> None:
    """Run one tool twice under ``max_actions_per_run=1``; the second is refused."""

    @runbound.tool
    def ping(text=None):
        return "pong"

    ping(secret)
    try:
        ping(secret)
    except ExecutionRefused:
        pass


def test_events_and_decisions_are_empty_before_init():
    assert runbound.events() == []
    assert runbound.decisions() == []


def test_a_refusal_is_recorded_as_an_event_and_a_decision():
    """max_actions_per_run's own door stage always goes through
    Engine.refuse(), which stamps a Decision onto the anomaly -- a
    reliable way to exercise both the "refusal" event and the decisions()
    ring in one call."""
    runbound.init(max_actions_per_run=1)

    _over_the_limit()

    events = runbound.events()
    assert any(e["kind"] == "refusal" for e in events)
    refusal = [e for e in events if e["kind"] == "refusal"][0]
    assert refusal["detector"] == "fanout"

    decisions = runbound.decisions()
    assert len(decisions) >= 1
    assert decisions[-1]["verdict"] in ("deny", "restrict")


def test_a_policy_block_is_recorded_with_its_decision():
    runbound.init(tool_policy={"on_violation": "block"})

    @runbound.tool(blocked=True)
    def wire_transfer():
        return "sent"

    with pytest.raises(ExecutionRefused):
        wire_transfer()

    anomalies = [e for e in runbound.events() if e["kind"] == "anomaly"]
    assert any(e["detector"] == "policy" and e["reacted"] == "blocked" for e in anomalies)
    assert runbound.decisions()[-1]["boundary"] == "policy"
    assert runbound.decisions()[-1]["verdict"] == "deny"


def test_records_never_carry_call_arguments_prompts_or_replies():
    """Invariant 1, content independence: nothing here is content."""
    runbound.init(max_actions_per_run=1)
    secret = "this-exact-prompt-text-must-never-leak-into-a-record"

    _over_the_limit(secret)

    blob = repr(runbound.events()) + repr(runbound.decisions())
    assert secret not in blob


def test_n_limits_how_many_records_come_back_most_recent_last():
    runbound.init()
    for i in range(5):
        local_events.record_runtime_change("policy_version", i, i + 1)

    recent = runbound.events(n=2)
    assert len(recent) == 2
    assert [e["to"] for e in recent] == [4, 5]


def test_on_event_callback_is_told_about_every_record():
    seen = []
    runbound.init(on_event=lambda record: seen.append(record["kind"]), max_actions_per_run=1)

    _over_the_limit()

    assert "anomaly" in seen
    assert "refusal" in seen


def test_a_raising_on_event_callback_never_breaks_the_host_call():
    def boom(_record):
        raise RuntimeError("a broken observer")

    runbound.init(max_actions_per_run=1, on_event=boom)

    @runbound.tool
    def ping():
        return "pong"

    assert ping() == "pong"
    with pytest.raises(ExecutionRefused):  # the refusal itself must still work
        ping()
    assert any(e["kind"] == "refusal" for e in runbound.events())


def test_on_event_must_be_callable_or_none():
    with pytest.raises(ValueError):
        runbound.init(on_event="not callable")
