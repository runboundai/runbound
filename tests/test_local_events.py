"""Local telemetry is free.

``runbound.events(n=100)`` and ``runbound.decisions(n=100)`` read an
in-memory ring of this process's own anomalies, refusals, posture
transitions and Decisions -- no account, no plane, nothing written to disk,
nothing sent anywhere. An optional ``on_event=`` callback on ``init()`` is
told about every record as it is appended, and a callback that raises must
never break the host call (fail-open, the golden rule).
"""

import pytest

import runbound
from runbound import api
from runbound.exceptions import CircuitOpen, GuardrailTripped
from runbound.state import SessionState


class _Failure(Exception):
    def __init__(self, status_code=None):
        super().__init__(f"status {status_code}")
        if status_code is not None:
            self.status_code = status_code


def setup_function(_fn):
    api._teardown_for_tests()


def teardown_function(_fn):
    api._teardown_for_tests()


def test_events_and_decisions_are_empty_before_init():
    assert runbound.events() == []
    assert runbound.decisions() == []


def test_a_refusal_is_recorded_as_an_event_and_a_decision():
    """max_actions_per_run's own door stage always goes through
    Engine.refuse(), which stamps a Decision onto the anomaly -- a
    reliable way to exercise both the "refusal" event and the decisions()
    ring in one call, unlike a post-call wall trip (StepDetector and
    friends), which has no Decision to stamp at all."""
    runbound.init(on_anomaly="raise", max_actions_per_run=1)

    @runbound.tool
    def ping():
        return "pong"

    ping()
    try:
        ping()
    except GuardrailTripped:
        pass

    events = runbound.events()
    assert any(e["kind"] == "refusal" for e in events)
    refusal = [e for e in events if e["kind"] == "refusal"][0]
    assert refusal["detector"] == "fanout"

    decisions = runbound.decisions()
    assert len(decisions) >= 1
    assert decisions[-1]["verdict"] in ("deny", "restrict")


def test_a_circuit_open_refusal_is_recorded_with_its_decision():
    """Engine._admit_circuit stays outside Engine.refuse() on purpose (see
    its own docstring: routing it through refuse() would not add a new
    alert, since _alert already dedupes a circuit anomaly on
    (detector, provider) against the one _announce_circuit sent when the
    circuit opened) -- but that is a reason to skip a *second* alert to
    external observers, never a reason for the refusal itself to be
    missing from this process's own local ring. Money, posture and fanout
    denials all land in both events() and decisions(); circuit denials
    must too."""
    runbound.init(circuit_failure_threshold=2, on_provider_failure="open")
    state = SessionState("s1")
    for _ in range(2):
        api._ENGINE.record_llm_error(state, "gpt-4o", _Failure(503), 0.1, "openai")

    with pytest.raises(CircuitOpen):
        api._ENGINE.admit(state, provider="openai")

    events = runbound.events()
    refusals = [e for e in events if e["kind"] == "refusal"]
    assert any(e["detector"] == "circuit" for e in refusals)

    decisions = runbound.decisions()
    assert len(decisions) >= 1
    assert decisions[-1]["boundary"] == "circuit"
    assert decisions[-1]["verdict"] == "deny"


def test_records_never_carry_call_arguments_prompts_or_replies():
    """Invariant 1, content independence: nothing here is content."""
    runbound.init(on_anomaly="raise", max_actions_per_run=1)
    secret = "this-exact-prompt-text-must-never-leak-into-a-record"

    @runbound.tool
    def ping(text):
        return "pong: " + text

    ping(secret)
    try:
        ping(secret)
    except GuardrailTripped:
        pass

    blob = repr(runbound.events()) + repr(runbound.decisions())
    assert secret not in blob


def test_manual_safe_mode_is_recorded_as_a_posture_transition():
    runbound.init()
    runbound.enter_safe_mode(reason="manual test", posture="restricted")
    runbound.exit_safe_mode()

    postures = [e for e in runbound.events() if e["kind"] == "posture"]
    assert [p["posture"] for p in postures] == ["restricted", "full"]
    assert postures[0]["source"] == "manual"


def test_n_limits_how_many_records_come_back_most_recent_last():
    runbound.init()
    for i in range(5):
        runbound.enter_safe_mode(reason=f"r{i}", posture="restricted")
        runbound.exit_safe_mode()

    postures = [e for e in runbound.events(n=2) if e["kind"] == "posture"]
    assert len(runbound.events(n=2)) == 2
    assert postures[-1]["posture"] == "full"  # exit_safe_mode's own transition
    assert postures[-1]["reason"] == "exit_safe_mode"


def test_on_event_callback_is_told_about_every_record():
    seen = []
    runbound.init(on_event=lambda record: seen.append(record["kind"]))

    runbound.enter_safe_mode(posture="restricted")

    assert "posture" in seen


def test_a_raising_on_event_callback_never_breaks_the_host_call():
    def boom(_record):
        raise RuntimeError("a broken observer")

    runbound.init(max_steps=1, on_anomaly="raise", on_event=boom)
    with runbound.session("s3"):
        api._record_llm_call("gpt-4o", 10, 10)
        try:
            api._record_llm_call("gpt-4o", 10, 10)
        except GuardrailTripped:
            pass  # the refusal itself must still work -- on_event did not eat it

    # And a plain call must keep succeeding too.
    runbound.enter_safe_mode(posture="restricted")
    runbound.exit_safe_mode()


def test_on_event_must_be_callable_or_none():
    with pytest.raises(ValueError):
        runbound.init(on_event="not callable")
