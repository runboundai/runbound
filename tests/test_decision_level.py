"""``Decision.level`` says which scope decided a posture or circuit refusal.

A session's own safe mode decides for that ``session``; ``runbound.enter_safe_mode`` for the
``process``; a posture the control plane states, or a fleet-wide Narrow halt, for the
``fleet``. A circuit breaker and an ``init(capabilities=...)`` class rule are the process's.
Built as ``tests/test_kill_switch.py`` builds its cases: a bare engine, a bare session, a
tiny fake plane link.
"""

import pytest

from runbound import admission
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import DECISION_LEVELS
from runbound.exceptions import GuardrailTripped, SafeModeViolation
from runbound.state import SessionState, make_posture_state

WRITE = frozenset({"write"})


class Plane:
    """The plane link's posture reads: a Controls-stated posture and a halt's, each settable."""

    fleet = True

    def __init__(self, posture=None, halt=None):
        self.posture, self.halt = posture, halt

    def posture_directive(self):
        return self.posture

    def halt_posture_directive(self):
        return self.halt

    def halt_mode(self):
        return None

    def controls_directive(self):
        return None

    def policy(self):
        return None

    @property
    def policy_version(self):
        return 0

    @property
    def policy_dry_run(self):
        return False


def engine(plane=None, **config) -> Engine:
    cfg = GuardrailConfig(on_anomaly="raise", **config)
    cfg.validate()
    return Engine(cfg, shared=plane)


def model_call_refusal(eng: Engine, state: SessionState):
    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(state)
    return excinfo.value.decision


def tool_refusal(eng: Engine, state: SessionState, effects=WRITE):
    with pytest.raises(SafeModeViolation) as excinfo:
        eng.admit(state, kind="action", tool="t", effects=effects, decorated=True)
    return excinfo.value.decision


def stopped(source: str):
    return make_posture_state("stopped", "because", source)


def restricted(source: str):
    return make_posture_state("restricted", "because", source)


# --- the stage functions ---------------------------------------------------------------------------


def test_the_circuit_decides_for_the_process():
    assert admission.circuit(False, "openai", "open", 30.0).level == "process"


def test_the_class_rule_decides_for_the_process():
    assert admission.capability("deny", "financial").level == "process"


@pytest.mark.parametrize("level", ["session", "process", "fleet"])
def test_stopped_and_posture_state_the_level_they_are_given(level):
    assert admission.stopped(True, "stopped", "manual", "why", level=level).level == level
    assert admission.posture("deny", "write", "restricted", "manual", "why", level=level).level == level


def test_stopped_and_posture_default_to_the_session():
    assert admission.stopped(True, "stopped", "manual", "why").level == "session"
    assert admission.posture("deny", "write", "restricted", "manual", "why").level == "session"


# --- a model call refused because the run is stopped ---------------------------------------------


def test_a_session_stopped_by_its_own_safe_mode_is_a_session_decision():
    eng, state = engine(), SessionState("s1")
    state.enter_safe_mode("because", "stopped")
    assert model_call_refusal(eng, state).level == "session"


def test_a_process_stopped_by_enter_safe_mode_is_a_process_decision():
    eng = engine()
    eng.enter_safe_mode("because", "stopped")
    assert model_call_refusal(eng, SessionState("s1")).level == "process"


def test_a_plane_stated_stop_is_a_fleet_decision():
    eng = engine(Plane(posture=stopped("plane")))
    assert model_call_refusal(eng, SessionState("s1")).level == "fleet"


def test_the_stopped_source_decides_not_the_first_narrowing():
    """A session merely restricted and a process stopped: the PROCESS stopped the call."""
    eng, state = engine(), SessionState("s1")
    state.enter_safe_mode("mild", "restricted")
    eng.enter_safe_mode("because", "stopped")
    decision = model_call_refusal(eng, state)
    assert decision.level == "process"
    assert decision.evaluation["source"] == "manual" and decision.reason == "because"


def test_two_stopped_sources_name_the_narrowest_scope():
    eng, state = engine(Plane(posture=stopped("plane"))), SessionState("s1")
    eng.enter_safe_mode("process says stop", "stopped")
    state.enter_safe_mode("session says stop", "stopped")
    assert model_call_refusal(eng, state).level == "session"


# --- a tool refused by a posture -------------------------------------------------------------------


def test_a_tool_refused_by_the_sessions_own_posture_is_a_session_decision():
    eng, state = engine(), SessionState("s1")
    state.enter_safe_mode("because", "restricted")
    decision = tool_refusal(eng, state, frozenset({"financial"}))
    assert (decision.boundary, decision.level) == ("posture", "session")


def test_a_tool_refused_by_the_process_posture_is_a_process_decision():
    eng = engine()
    eng.enter_safe_mode("because", "restricted")
    assert tool_refusal(eng, SessionState("s1"), frozenset({"financial"})).level == "process"


def test_a_tool_refused_by_a_plane_posture_is_a_fleet_decision():
    eng = engine(Plane(posture=restricted("plane")))
    assert tool_refusal(eng, SessionState("s1"), frozenset({"financial"})).level == "fleet"


def test_a_tool_refused_by_a_narrow_halt_is_a_fleet_decision():
    eng = engine(Plane(halt=restricted("halt")))
    assert tool_refusal(eng, SessionState("s1"), frozenset({"financial"})).level == "fleet"


def test_a_tool_refused_by_a_class_rule_is_a_process_decision():
    eng = engine(capabilities={"financial": "deny"})
    decision = tool_refusal(eng, SessionState("s1"), frozenset({"financial"}))
    assert (decision.boundary, decision.level) == ("capability", "process")


# --- the circuit through the engine ----------------------------------------------------------------


def test_an_open_circuit_refuses_for_the_process():
    eng = engine(on_provider_failure="open", circuit_failure_threshold=1, circuit_cooldown_seconds=30)
    eng.circuit.record_failure("openai")
    assert eng.circuit.state("openai") == "open"
    decision = model_call_refusal_for(eng, SessionState("s1"), provider="openai")
    assert (decision.boundary, decision.level) == ("circuit", "process")


def model_call_refusal_for(eng: Engine, state: SessionState, **kwargs):
    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(state, **kwargs)
    return excinfo.value.decision


def test_every_level_the_engine_states_is_one_the_decision_knows():
    assert {"session", "process", "fleet"} <= set(DECISION_LEVELS)
