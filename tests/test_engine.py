"""Tests for the Engine — the piece that turns one attempted tool call into an
allow or a refusal.

Fakes, never mocks: an observer is anything with ``on_event(session, event)``
and ``on_anomaly(session, anomaly, reacted)``, so the doubles here are the real
interfaces. Delivery is not the engine's concern; an observer — telemetry
export among them — is how the engine reports a refusal.
"""

import logging

import pytest

from runbound.config import RunboundConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Decision, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation, CapabilityRefused
from runbound.policy import ToolCall, ToolPolicy
from runbound.shared import LocalState
from runbound.state import SessionState

CRITICAL = Anomaly("policy", "critical", "wire_transfer is denied", {"tool": "wire_transfer"})


class RecordingObserver:
    """Records the anomalies it is told about, into a shared call log."""

    def __init__(self, log: list, label: str = "obs") -> None:
        self.log = log
        self.label = label

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.log.append((self.label, anomaly.detector, reacted))


class HaltedShared(LocalState):
    def __init__(self, mode: str = "stop") -> None:
        self.mode = mode

    def halted(self) -> bool:
        return True

    def halt_mode(self):
        return self.mode


def tool_event(step: int, name: str = "search", args_hash: str = "h1") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name=name, args_hash=args_hash)


def session() -> SessionState:
    return SessionState("s1")


def call(name: str = "wire_transfer") -> ToolCall:
    return ToolCall(name=name, args=(), kwargs={}, session_key=None, tags={})


# --- record, then notify -----------------------------------------------------


def test_process_records_the_attempt_into_the_session_counters():
    engine = Engine(RunboundConfig())
    state = session()

    engine.process(state, tool_event(1))
    engine.process(state, tool_event(2, name="lookup"))
    engine.process(state, tool_event(3))

    assert state.tool_calls == {"search": 2, "lookup": 1}
    assert state.event_count == 3


def test_process_raises_the_stored_anomaly_for_a_latched_session():
    engine = Engine(RunboundConfig())
    state = session()
    state.tripped_by = CRITICAL

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, tool_event(1))

    assert excinfo.value.anomaly is CRITICAL
    # The attempt was still recorded: it was made.
    assert state.tool_calls == {"search": 1}


# --- refusing ---------------------------------------------------------------


def test_refuse_stamps_the_decision_tells_the_observers_and_raises():
    log: list = []
    engine = Engine(RunboundConfig(), observers=[RecordingObserver(log)])
    decision = Decision(verdict="deny", kind="action", boundary="policy", detector="policy", reason="no")

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.refuse(session(), CRITICAL, decision, reacted="blocked")

    exc = excinfo.value
    assert exc.decision.boundary == "policy"
    assert exc.reason == "policy"
    assert str(exc) == CRITICAL.message
    assert exc.anomaly.anomaly_id == CRITICAL.anomaly_id
    assert log == [("obs", "policy", "blocked")]


def test_refuse_with_latch_stops_the_session_and_the_first_latch_wins():
    engine = Engine(RunboundConfig())
    state = session()
    decision = Decision(verdict="deny", kind="action", boundary="policy")
    second = Anomaly("policy", "critical", "second", {"tool": "other"})

    with pytest.raises(GuardrailTripped):
        engine.refuse(state, CRITICAL, decision, latch=True)
    with pytest.raises(GuardrailTripped):
        engine.refuse(state, second, decision, latch=True)

    assert state.tripped_by is not None
    assert state.tripped_by.message == CRITICAL.message
    assert state.tripped_at is not None


def test_refuse_without_latch_leaves_the_session_running():
    engine = Engine(RunboundConfig())
    state = session()

    with pytest.raises(GuardrailTripped):
        engine.refuse(state, CRITICAL, Decision(verdict="deny", kind="action"))

    assert state.tripped_by is None


def test_a_broken_observer_never_loses_the_refusal(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")

    class Boom:
        def on_event(self, session, event):
            raise RuntimeError("broken")

        def on_anomaly(self, session, anomaly, reacted):
            raise RuntimeError("broken")

    log: list = []
    engine = Engine(RunboundConfig(), observers=[Boom(), RecordingObserver(log)])

    with pytest.raises(GuardrailTripped):
        engine.refuse(session(), CRITICAL, Decision(verdict="deny", kind="action"))

    assert log == [("obs", "policy", "warn")]
    assert "observer" in caplog.text.lower()


def test_observers_hear_in_order_and_before_the_exception_leaves():
    log: list = []
    engine = Engine(
        RunboundConfig(),
        observers=[RecordingObserver(log, "one"), RecordingObserver(log, "two")],
    )

    with pytest.raises(GuardrailTripped):
        engine.refuse(session(), CRITICAL, Decision(verdict="deny", kind="action"), reacted="door")

    assert log == [("one", "policy", "door"), ("two", "policy", "door")]


# --- enforce_policy ---------------------------------------------------------


def test_enforce_policy_blocks_a_denied_tool_with_policy_violation():
    engine = Engine(RunboundConfig(tool_policy=ToolPolicy(deny=["wire_transfer"])))
    state = session()

    with pytest.raises(PolicyViolation) as excinfo:
        engine.enforce_policy(state, call("wire_transfer"))

    assert excinfo.value.violation.rule == "deny"
    assert excinfo.value.reason == "tool_blocked"
    assert state.tripped_by is None  # block, not block_and_latch


def test_enforce_policy_is_a_no_op_without_a_policy():
    Engine(RunboundConfig()).enforce_policy(session(), call())


def test_enforce_policy_allows_a_tool_the_policy_does_not_name():
    engine = Engine(RunboundConfig(tool_policy=ToolPolicy(deny=["wire_transfer"])))

    engine.enforce_policy(session(), call("lookup"))


def test_enforce_policy_fails_closed_when_evaluation_breaks(monkeypatch, caplog):
    """A built-in that raises refuses every class (RS-3 F1)."""
    caplog.set_level(logging.WARNING, logger="runbound")
    import runbound.engine as engine_module

    def boom(*args, **kwargs):
        raise RuntimeError("evaluator bug")

    monkeypatch.setattr(engine_module, "evaluate", boom)
    engine = Engine(RunboundConfig(tool_policy=ToolPolicy(deny=["wire_transfer"])))

    with pytest.raises(GuardrailTripped) as caught:
        engine.enforce_policy(session(), call("wire_transfer"))

    assert caught.value.reason == "hook_error"
    assert "evaluator bug" not in str(caught.value)
    assert "could not evaluate" in caplog.text


# --- admit_action: halt, capability class, action cap -------------------------


def test_the_action_cap_refuses_the_call_after_the_limit():
    engine = Engine(RunboundConfig(max_actions_per_run=2))
    state = session()
    for _ in range(2):
        engine.admit_action(state, "search")
        state.mark_action_admitted()

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.admit_action(state, "search")

    assert excinfo.value.reason == "blast_radius"
    assert excinfo.value.anomaly.detector == "fanout"
    assert excinfo.value.anomaly.details["limit"] == 2


def test_a_refused_attempt_does_not_count_against_the_action_cap():
    engine = Engine(RunboundConfig(max_actions_per_run=1))
    state = session()
    engine.admit_action(state, "search")
    state.mark_action_admitted()

    for _ in range(3):
        with pytest.raises(GuardrailTripped):
            engine.admit_action(state, "search")
        state.mark_action_refused()

    assert (state.executed_actions, state.refused_actions) == (1, 3)


def test_a_capability_class_rule_refuses_a_decorated_tool():
    engine = Engine(RunboundConfig(capabilities={"financial": "deny"}))

    with pytest.raises(CapabilityRefused) as excinfo:
        engine.admit_action(session(), "refund", effects=frozenset({"financial"}), decorated=True)

    assert excinfo.value.reason == "capability"
    assert excinfo.value.violation.rule == "capability"
    assert excinfo.value.anomaly.details["denied_class"] == "financial"


def test_an_undecorated_call_skips_the_class_rules():
    engine = Engine(RunboundConfig(capabilities={"financial": "deny"}))

    engine.admit_action(session(), "refund", effects=frozenset({"financial"}), decorated=False)


def test_a_halted_fleet_refuses_every_tool_under_raise():
    engine = Engine(RunboundConfig(on_halt="raise"), shared=HaltedShared())

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.admit_call(session(), ToolCall("search", (), {}, None, {}))

    assert excinfo.value.reason == "halt"
    assert excinfo.value.scope["level"] == "fleet"


def test_a_halted_fleet_only_warns_under_warn(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    engine = Engine(RunboundConfig(on_halt="warn"), shared=HaltedShared())

    engine.admit_call(session(), ToolCall("search", (), {}, None, {}))
    engine.admit_call(session(), ToolCall("search", (), {}, None, {}))

    assert caplog.text.count("Fleet halted") == 1  # at most once a minute


# --- construction -----------------------------------------------------------


def test_local_state_is_the_default_shared_state_and_no_observers_the_default():
    engine = Engine(RunboundConfig())

    assert isinstance(engine.shared, LocalState)
    assert engine.observers == []
