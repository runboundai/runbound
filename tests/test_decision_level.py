"""``Decision.level`` says which scope decided a refusal.

A policy or action-cap refusal is the ``session``'s; an ``init(capabilities=...)``
class rule is the ``process``'s; a fleet halt (and a fleet-wide Narrow halt) is
the ``fleet``'s. Built with a bare engine, a bare session and a tiny fake plane
link.
"""

import pytest

from runbound import admission
from runbound.config import RunboundConfig
from runbound.engine import Engine
from runbound.events import DECISION_LEVELS
from runbound.exceptions import GuardrailTripped, PolicyViolation, CapabilityRefused
from runbound.policy import ToolCall, ToolPolicy
from runbound.state import SessionState

FINANCIAL = frozenset({"financial"})


class Plane:
    """The plane link's halt reads, settable."""

    fleet = True

    def __init__(self, halt_mode=None):
        self.mode = halt_mode

    def halted(self):
        return self.mode is not None

    def halt_mode(self):
        return self.mode

    def policy(self):
        return None

    @property
    def policy_version(self):
        return 0

    @property
    def policy_dry_run(self):
        return False


def engine(plane=None, **config) -> Engine:
    cfg = RunboundConfig(**config)
    cfg.validate()
    return Engine(cfg, shared=plane)


def refusal(eng: Engine, state: SessionState, effects=FINANCIAL, exc=GuardrailTripped):
    with pytest.raises(exc) as excinfo:
        eng.admit_call(state, ToolCall("t", (), {}, None, {}), effects=effects, decorated=True)
    return excinfo.value.decision


# --- the stage functions ---------------------------------------------------------------------------


def test_the_class_rule_decides_for_the_process():
    assert admission.capability("deny", "financial").level == "process"


def test_the_halt_decides_for_the_fleet():
    assert admission.halted(True, "why").level == "fleet"


def test_the_action_cap_decides_for_the_session():
    assert admission.actions(3, 3).level == "session"


# --- through the engine ----------------------------------------------------------------------------


def test_a_tool_refused_by_a_class_rule_is_a_process_decision():
    eng = engine(capabilities={"financial": "deny"})
    decision = refusal(eng, SessionState("s1"), exc=CapabilityRefused)
    assert (decision.boundary, decision.level) == ("capability", "process")


def test_an_approve_class_rule_restricts_at_the_process_level():
    eng = engine(capabilities={"financial": "approve"})
    decision = refusal(eng, SessionState("s1"), exc=CapabilityRefused)
    assert (decision.verdict, decision.level) == ("restrict", "process")


def test_a_narrow_halt_is_a_full_halt_and_a_fleet_decision():
    """Narrowing left the product: a halt of mode narrow refuses every tool, even a read."""
    eng = engine(Plane(halt_mode="narrow"))
    decision = refusal(eng, SessionState("s1"), effects=frozenset({"read", "write"}))
    assert (decision.boundary, decision.level) == ("halt", "fleet")


def test_a_full_halt_refuses_every_tool_as_a_fleet_decision():
    eng = engine(Plane(halt_mode="stop"))
    decision = refusal(eng, SessionState("s1"), effects=frozenset({"read"}))
    assert (decision.boundary, decision.level) == ("halt", "fleet")


def test_the_action_cap_refuses_for_the_session():
    eng = engine(max_actions_per_run=1)
    state = SessionState("s1")
    state.mark_action_admitted()
    decision = refusal(eng, state, effects=frozenset())
    assert (decision.boundary, decision.level) == ("blast_radius", "session")


def test_a_policy_refusal_is_a_session_decision():
    eng = engine(tool_policy=ToolPolicy(deny=["t"]))
    with pytest.raises(PolicyViolation) as excinfo:
        eng.enforce_policy(SessionState("s1"), ToolCall("t", (), {}, None, {}))
    assert excinfo.value.decision.level == "session"
    assert excinfo.value.scope["level"] == "session"


def test_every_level_the_engine_states_is_one_the_decision_knows():
    assert {"session", "process", "fleet"} <= set(DECISION_LEVELS)
