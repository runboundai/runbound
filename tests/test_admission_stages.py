"""admission.py's pure stage functions, table-driven and mock-free.

Every stage is plain arithmetic — numbers in, a :class:`~runbound.events.Decision`
out — so each is tested here with bare values, no ``Engine``, no
``SessionState``, no lock. The shared shape every table below checks:
exactly-at-the-limit allows, one over denies, ``None`` (the limit is off)
always allows, and the zero/empty edge is explicit rather than assumed.
"""

import pytest

from runbound import admission
from runbound.events import ALLOW, Decision

# --- actions -----------------------------------------------------------------


@pytest.mark.parametrize(
    "executed, max_actions_per_run, allow",
    [
        (0, None, True),
        (0, 3, True),  # zero case
        (2, 3, True),  # one under the limit: this attempt would be the 3rd, allowed
        (3, 3, False),  # already executed the limit: the next would be one over
        (10, 3, False),
    ],
)
def test_actions_table(executed, max_actions_per_run, allow):
    # ``executed`` is the count *before* this attempt (an admission
    # refusal must never itself count as executed): "executed < max" allows.
    decision = admission.actions(executed, max_actions_per_run)
    if allow:
        assert decision is ALLOW
    else:
        assert decision.verdict == "deny"
        assert decision.kind == "action"
        assert decision.boundary == "blast_radius"
        assert decision.detector == "fanout"
        assert decision.evaluation == {"limit": max_actions_per_run, "used": executed + 1}


# --- capability --------------------------------------------------------------


def test_capability_allows():
    assert admission.capability("allow", "financial") is ALLOW


def test_capability_denies():
    decision = admission.capability("deny", "destructive")
    assert decision.verdict == "deny"
    assert decision.boundary == "capability"
    assert decision.detector == "capability"
    assert decision.level == "process"
    assert decision.evaluation == {"denied_class": "destructive"}


def test_capability_approve_restricts():
    decision = admission.capability("approve", "privileged")
    assert decision.verdict == "restrict"


# --- halted ------------------------------------------------------------------


def test_halted_allows_when_the_fleet_is_running():
    assert admission.halted(False, "ignored") is ALLOW


def test_halted_denies_at_fleet_level():
    decision = admission.halted(True, "halted by the control plane (mode: stop)")
    assert decision.verdict == "deny"
    assert decision.kind == "action"
    assert decision.boundary == "halt"
    assert decision.detector == "halt"
    assert decision.level == "fleet"
    assert decision.reason == "halted by the control plane (mode: stop)"


# --- Decision round-trip -------------------------------------------------


def test_decision_as_dict_from_dict_round_trips():
    decision = admission.actions(3, 3)
    data = decision.as_dict()
    restored = Decision.from_dict(data)
    assert restored == decision


def test_decision_from_dict_tolerates_garbage():
    assert Decision.from_dict(None) == Decision()
    assert Decision.from_dict({"verdict": "not-a-real-verdict"}) == Decision()
    assert Decision.from_dict({"policy_version": "not-an-int"}).policy_version is None
    assert Decision.from_dict({"evaluation": "not-a-dict"}).evaluation == {}
    assert Decision.from_dict({"boundary": 7}).boundary is None


def test_allow_constant_is_shared():
    """The allow path allocates nothing: every stage returns the same object."""
    assert admission.actions(0, 10) is admission.capability("allow", "read") is admission.halted(False, "") is ALLOW
