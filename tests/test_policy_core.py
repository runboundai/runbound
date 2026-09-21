"""Tests for the action policy core: rules, evaluation, and enforcement.

Policy is the customer's statement about what their agent may do. These tests
pin the two things that make it trustworthy: the evaluation ORDER (a denied
tool is denied whatever else is configured) and the one deliberate exception
to fail-open — a permission gate that raises refuses the call.
"""

import logging

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolCall, ToolPolicy, Violation, coerce, evaluate
from runbound.state import SessionState

SECRET = "sk-do-not-log-me-42"


def call(
    name: str = "send_email",
    args: tuple = (),
    kwargs: dict | None = None,
    session_key: str | None = None,
    tags: dict | None = None,
) -> ToolCall:
    return ToolCall(name, args, dict(kwargs or {}), session_key, dict(tags or {}))


def session(key: str | None = None, tags: dict | None = None) -> SessionState:
    return SessionState("s1", key=key, tags=tags)


def tool_event(name: str = "send_email", step: int = 1) -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name=name, args_hash="h")


class RecordingObserver:
    """Remembers every anomaly it was told about."""

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


def engine(policy=None, observers=None, **config_kwargs) -> Engine:
    config = GuardrailConfig(tool_policy=policy, **config_kwargs)
    return Engine(config, detectors=[], observers=observers if observers is not None else [])


# --- validation -------------------------------------------------------------


def test_empty_policy_is_valid_and_permits_everything():
    policy = ToolPolicy()
    policy.validate()
    assert evaluate(policy, call("anything"), 1) is None


def test_unknown_on_violation_is_rejected():
    with pytest.raises(ValueError, match="on_violation"):
        ToolPolicy(on_violation="explode").validate()


@pytest.mark.parametrize("mode", ["block", "block_and_latch", "dry_run"])
def test_every_documented_on_violation_mode_is_accepted(mode):
    ToolPolicy(on_violation=mode).validate()


def test_require_approval_without_a_callback_is_rejected():
    with pytest.raises(ValueError, match="approval_callback"):
        ToolPolicy(require_approval=["refund"]).validate()


def test_require_approval_with_a_callback_is_accepted():
    ToolPolicy(require_approval=["refund"], approval_callback=lambda c: True).validate()


def test_approval_callback_without_require_approval_is_accepted():
    # Harmless: the gate simply never fires.
    ToolPolicy(approval_callback=lambda c: True).validate()


@pytest.mark.parametrize("limit", [0, -1, "3", None])
def test_max_calls_values_must_be_positive_ints(limit):
    with pytest.raises(ValueError, match="max_calls"):
        ToolPolicy(max_calls={"refund": limit}).validate()


def test_max_calls_limit_of_one_is_valid():
    ToolPolicy(max_calls={"refund": 1}).validate()


def test_a_tool_in_both_deny_and_allow_is_rejected():
    with pytest.raises(ValueError, match="both deny and allow"):
        ToolPolicy(deny=["shell"], allow=["shell", "search"]).validate()


@pytest.mark.parametrize("field_name", ["deny", "allow", "require_approval"])
def test_tool_names_must_be_strings(field_name):
    with pytest.raises(ValueError, match=field_name):
        ToolPolicy(**{field_name: [object()]}, approval_callback=lambda c: True).validate()


# --- coercion ---------------------------------------------------------------


def test_coerce_builds_a_policy_from_a_dict():
    policy = coerce({"deny": ["shell"], "on_violation": "dry_run"})
    assert isinstance(policy, ToolPolicy)
    assert policy.deny == ["shell"]
    assert policy.on_violation == "dry_run"


def test_coerce_passes_through_a_policy_and_none():
    policy = ToolPolicy(deny=["shell"])
    assert coerce(policy) is policy
    assert coerce(None) is None


def test_coerce_rejects_an_unknown_key_with_a_clear_message():
    with pytest.raises(ValueError) as excinfo:
        coerce({"denny": ["shell"]})
    message = str(excinfo.value)
    assert "denny" in message
    assert "deny" in message  # the valid field names are listed


def test_coerce_rejects_something_that_is_not_a_policy():
    with pytest.raises(ValueError, match="ToolPolicy"):
        coerce(["deny", "shell"])


def test_config_defaults_to_no_policy():
    assert GuardrailConfig().tool_policy is None
    GuardrailConfig().validate()


def test_config_validate_coerces_a_dict_policy_in_place():
    config = GuardrailConfig(tool_policy={"deny": ["shell"]})
    config.validate()
    assert isinstance(config.tool_policy, ToolPolicy)
    assert config.tool_policy.deny == ["shell"]


def test_config_validate_rejects_an_invalid_policy():
    config = GuardrailConfig(tool_policy={"require_approval": ["refund"]})
    with pytest.raises(ValueError, match="approval_callback"):
        config.validate()


def test_config_validate_rejects_an_unknown_policy_key():
    with pytest.raises(ValueError, match="tool_policy"):
        GuardrailConfig(tool_policy={"nope": 1}).validate()


# --- evaluation order -------------------------------------------------------


def _every_rule_policy() -> ToolPolicy:
    """A policy under which one tool trips every rule at once."""
    return ToolPolicy(
        deny=["refund"],
        allow=["search"],
        max_calls={"refund": 1},
        constraints={"refund": lambda c: False},
        require_approval=["refund"],
        approval_callback=lambda c: False,
    )


def test_deny_beats_every_other_rule():
    violation = evaluate(_every_rule_policy(), call("refund"), 9)
    assert violation.rule == "deny"
    assert violation.tool == "refund"


def test_allow_beats_max_calls_and_below():
    policy = _every_rule_policy()
    policy.deny = []
    assert evaluate(policy, call("refund"), 9).rule == "allow"


def test_max_calls_beats_constraint_and_approval():
    policy = _every_rule_policy()
    policy.deny = []
    policy.allow = None
    assert evaluate(policy, call("refund"), 9).rule == "max_calls"


def test_constraint_beats_approval():
    policy = _every_rule_policy()
    policy.deny = []
    policy.allow = None
    policy.max_calls = {}
    assert evaluate(policy, call("refund"), 9).rule == "constraint"


def test_approval_is_the_last_gate():
    policy = _every_rule_policy()
    policy.deny = []
    policy.allow = None
    policy.max_calls = {}
    policy.constraints = {}
    assert evaluate(policy, call("refund"), 9).rule == "approval"


# --- individual rules -------------------------------------------------------


def test_deny_reports_the_tool_and_reason():
    violation = evaluate(ToolPolicy(deny=["shell"]), call("shell"), 1)
    assert isinstance(violation, Violation)
    assert violation.rule == "deny"
    assert "shell" in violation.reason
    assert violation.details["tool"] == "shell"
    assert violation.details["rule"] == "deny"


def test_allow_none_permits_everything_not_denied():
    policy = ToolPolicy(deny=["shell"])
    assert policy.allow is None
    assert evaluate(policy, call("anything_at_all"), 3) is None
    assert evaluate(policy, call("shell"), 1).rule == "deny"


def test_allow_list_blocks_a_tool_that_is_not_listed():
    policy = ToolPolicy(allow=["search"])
    assert evaluate(policy, call("search"), 1) is None
    violation = evaluate(policy, call("shell"), 1)
    assert violation.rule == "allow"
    assert "shell" in violation.reason


def test_max_calls_boundary_allows_exactly_the_limit():
    policy = ToolPolicy(max_calls={"refund": 1})
    assert evaluate(policy, call("refund"), 1) is None


def test_max_calls_violation_carries_count_and_limit():
    policy = ToolPolicy(max_calls={"refund": 1})
    violation = evaluate(policy, call("refund"), 2)
    assert violation.rule == "max_calls"
    assert violation.details["count"] == 2
    assert violation.details["limit"] == 1
    assert "1" in violation.reason


def test_max_calls_ignores_tools_with_no_limit():
    assert evaluate(ToolPolicy(max_calls={"refund": 1}), call("search"), 99) is None


def test_constraint_true_allows_and_false_blocks():
    allowed = ToolPolicy(constraints={"refund": lambda c: True})
    blocked = ToolPolicy(constraints={"refund": lambda c: False})
    assert evaluate(allowed, call("refund"), 1) is None
    assert evaluate(blocked, call("refund"), 1).rule == "constraint"


def test_a_constraint_that_raises_blocks_the_call_fail_closed():
    def boom(c: ToolCall) -> bool:
        raise ValueError(f"cannot read {SECRET}")

    violation = evaluate(ToolPolicy(constraints={"refund": boom}), call("refund"), 1)
    assert violation.rule == "constraint"
    assert "ValueError" in violation.reason
    assert SECRET not in violation.reason
    assert SECRET not in repr(violation.details)


def test_approval_true_allows_false_blocks():
    yes = ToolPolicy(require_approval=["refund"], approval_callback=lambda c: True)
    no = ToolPolicy(require_approval=["refund"], approval_callback=lambda c: False)
    assert evaluate(yes, call("refund"), 1) is None
    assert evaluate(no, call("refund"), 1).rule == "approval"


def test_an_approval_callback_that_raises_blocks_the_call_fail_closed():
    def boom(c: ToolCall) -> bool:
        raise TimeoutError("approval service is down")

    policy = ToolPolicy(require_approval=["refund"], approval_callback=boom)
    violation = evaluate(policy, call("refund"), 1)
    assert violation.rule == "approval"
    assert "TimeoutError" in violation.reason


def test_approval_required_with_no_callback_blocks_rather_than_permits():
    # An unvalidated policy still fails closed: the customer asked for a gate.
    policy = ToolPolicy(require_approval=["refund"])
    assert evaluate(policy, call("refund"), 1).rule == "approval"


def test_approval_only_gates_the_listed_tools():
    policy = ToolPolicy(require_approval=["refund"], approval_callback=lambda c: False)
    assert evaluate(policy, call("search"), 1) is None


def test_predicates_see_the_session_key_and_tags():
    seen: list[ToolCall] = []

    def remember(c: ToolCall) -> bool:
        seen.append(c)
        return True

    policy = ToolPolicy(
        constraints={"refund": remember},
        require_approval=["refund"],
        approval_callback=remember,
    )
    given = call("refund", args=(1,), kwargs={"to": SECRET}, session_key="u-7", tags={"plan": "free"})
    assert evaluate(policy, given, 1) is None
    assert [c.session_key for c in seen] == ["u-7", "u-7"]
    assert [c.tags for c in seen] == [{"plan": "free"}, {"plan": "free"}]
    assert seen[0].kwargs == {"to": SECRET}


# --- state counters ---------------------------------------------------------


def test_new_session_has_no_tool_calls():
    assert session().tool_calls == {}


def test_record_counts_tool_calls_per_name():
    state = session()
    state.record(tool_event("send_email", 1))
    state.record(tool_event("send_email", 2))
    state.record(tool_event("search", 3))
    assert state.tool_calls == {"send_email": 2, "search": 1}


def test_record_ignores_events_that_are_not_named_tool_calls():
    state = session()
    state.record(Event(kind="llm_call", ts=1.0, step=1, model="gpt-4o"))
    state.record(Event(kind="tool_call", ts=2.0, step=2, tool_name=None))
    assert state.tool_calls == {}


# --- engine enforcement -----------------------------------------------------


def test_enforce_policy_without_a_policy_is_a_no_op():
    assert engine().enforce_policy(session(), call("shell")) is None


def test_a_denied_tool_raises_policy_violation():
    eng = engine(ToolPolicy(deny=["shell"]))
    state = session()
    with pytest.raises(PolicyViolation) as excinfo:
        eng.enforce_policy(state, call("shell"))
    exc = excinfo.value
    assert isinstance(exc, GuardrailTripped)
    assert exc.violation.rule == "deny"
    assert exc.anomaly.detector == "policy"
    assert exc.anomaly.severity == "critical"
    assert str(exc) == exc.anomaly.message
    assert "shell" in exc.anomaly.message


def test_a_permitted_tool_passes_through():
    eng = engine(ToolPolicy(deny=["shell"]))
    assert eng.enforce_policy(session(), call("search")) is None


def test_policy_anomaly_carries_session_identity_and_the_rule():
    eng = engine(ToolPolicy(deny=["shell"]))
    state = session(key="user-9", tags={"plan": "free"})
    with pytest.raises(PolicyViolation) as excinfo:
        eng.enforce_policy(state, call("shell", session_key="user-9"))
    details = excinfo.value.anomaly.details
    assert details["session_id"] == "s1"
    assert details["key"] == "user-9"
    assert details["tags"] == {"plan": "free"}
    assert details["tool"] == "shell"
    assert details["rule"] == "deny"
    assert "user-9" in excinfo.value.anomaly.message


def test_max_calls_is_enforced_from_the_recorded_session_counters():
    eng = engine(ToolPolicy(max_calls={"refund": 1}))
    state = session()

    state.record(tool_event("refund", 1))
    eng.enforce_policy(state, call("refund"))  # first attempt: at the limit

    state.record(tool_event("refund", 2))
    with pytest.raises(PolicyViolation) as excinfo:
        eng.enforce_policy(state, call("refund"))
    assert excinfo.value.violation.details == {
        "tool": "refund",
        "rule": "max_calls",
        "count": 2,
        "limit": 1,
    }


def test_dry_run_logs_and_never_raises(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    observer = RecordingObserver()
    eng = engine(ToolPolicy(deny=["shell"], on_violation="dry_run"), observers=[observer])
    assert eng.enforce_policy(session(), call("shell")) is None
    assert "would block" in caplog.text
    assert "shell" in caplog.text
    assert observer.sent[0].severity == "warn"


def test_block_does_not_latch_the_session():
    eng = engine(ToolPolicy(deny=["shell"]))
    state = session()
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(state, call("shell"))
    assert state.tripped_by is None


def test_block_and_latch_latches_the_session():
    eng = engine(ToolPolicy(deny=["shell"], on_violation="block_and_latch"))
    state = session()
    with pytest.raises(PolicyViolation) as excinfo:
        eng.enforce_policy(state, call("shell"))
    assert state.tripped_by is excinfo.value.anomaly
    assert state.tripped_at is not None


def test_block_and_latch_honors_on_trip_once():
    eng = engine(
        ToolPolicy(deny=["shell"], on_violation="block_and_latch"), on_trip="once"
    )
    state = session()
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(state, call("shell"))
    assert state.tripped_by is None


def test_dry_run_never_latches():
    eng = engine(
        ToolPolicy(deny=["shell"], on_violation="dry_run"),
        on_trip="latch",
    )
    state = session()
    eng.enforce_policy(state, call("shell"))
    assert state.tripped_by is None


# --- alerts -----------------------------------------------------------------


def test_the_same_violation_alerts_once_per_session_rule_and_tool():
    observer = RecordingObserver()
    eng = engine(ToolPolicy(deny=["shell"]), observers=[observer])
    state = session()
    for _ in range(3):
        with pytest.raises(PolicyViolation):
            eng.enforce_policy(state, call("shell"))
    assert len(observer.sent) == 1


def test_a_different_tool_alerts_again():
    observer = RecordingObserver()
    eng = engine(ToolPolicy(deny=["shell", "wire_money"]), observers=[observer])
    state = session()
    for tool in ("shell", "wire_money"):
        with pytest.raises(PolicyViolation):
            eng.enforce_policy(state, call(tool))
    assert [a.details["tool"] for a in observer.sent] == ["shell", "wire_money"]


def test_a_different_rule_on_the_same_tool_alerts_again():
    observer = RecordingObserver()
    policy = ToolPolicy(max_calls={"refund": 1}, constraints={"refund": lambda c: False})
    eng = engine(policy, observers=[observer])
    state = session()
    state.record(tool_event("refund", 1))
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(state, call("refund"))  # constraint: 1 is within the limit
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(state, call("refund"))  # still the constraint: deduped
    assert len(observer.sent) == 1

    state.record(tool_event("refund", 2))
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(state, call("refund"))  # now over the limit
    assert [a.details["rule"] for a in observer.sent] == ["constraint", "max_calls"]


def test_detector_alerts_are_deduped_exactly_as_before():
    observer = RecordingObserver()
    eng = engine(observers=[observer])
    state = session()
    anomaly = Anomaly("budget", "critical", "out of money", {"session_id": "s1"})
    eng._alert(anomaly, state)
    eng._alert(anomaly, state)
    assert len(observer.sent) == 1


# --- reaction independence and privacy --------------------------------------


def test_policy_is_enforced_regardless_of_on_anomaly():
    eng = engine(ToolPolicy(deny=["shell"]), on_anomaly="warn")
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(session(), call("shell"))


def test_raw_arguments_never_reach_the_anomaly():
    def boom(c: ToolCall) -> bool:
        raise RuntimeError(f"bad recipient {c.kwargs['to']}")

    eng = engine(ToolPolicy(constraints={"send_email": boom}))
    with pytest.raises(PolicyViolation) as excinfo:
        eng.enforce_policy(
            session(), call("send_email", args=(SECRET,), kwargs={"to": SECRET})
        )
    anomaly = excinfo.value.anomaly
    assert SECRET not in repr(anomaly)
    assert SECRET not in anomaly.message


# --- fail-open on our own bugs ---------------------------------------------


def test_a_policy_the_engine_cannot_evaluate_lets_the_call_through(caplog):
    class Hostile:
        def __contains__(self, item):
            raise RuntimeError("policy is broken")

    caplog.set_level(logging.WARNING, logger="runbound")
    policy = ToolPolicy()
    policy.deny = Hostile()
    eng = engine(policy)
    assert eng.enforce_policy(session(), call("shell")) is None
    assert "policy" in caplog.text


def test_a_dict_policy_reaching_the_engine_is_still_enforced():
    eng = engine({"deny": ["shell"]})
    with pytest.raises(PolicyViolation):
        eng.enforce_policy(session(), call("shell"))


def test_an_unusable_policy_object_never_breaks_the_host(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    eng = engine(42)
    assert eng.enforce_policy(session(), call("shell")) is None
    assert "tool_policy" in caplog.text
