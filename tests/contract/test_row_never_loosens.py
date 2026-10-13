"""A row never loosens what the code declares (TPD §4c.1; register C15). Passes since the C15 fix.

``fails_closed`` is the engine's own question (hooks.py): a closed tool is refused when the plane is gone and when a
user hook fails. A row's ``risk_class`` may raise a tool's class; it must never turn a closed tool open. The test
walks every declared set of effects (all subsets of the six classes, plus an unknown class, plus none at all) against
every ``risk_class`` and both row states.
"""

import itertools

import pytest

from runbound import policy_rows
from runbound.hooks import fails_closed
from runbound.policy import CAPABILITIES

pytestmark = [pytest.mark.contract(item="§4c.1")]

CLASSES = tuple(CAPABILITIES) + ("legacy_unknown",)
DECLARED = [frozenset(c) for n in range(len(CLASSES) + 1) for c in itertools.combinations(CLASSES, n)]


def _row(risk_class, state="enforced"):
    return policy_rows.parse([{"tool": "t", "state": state, "risk_class": risk_class}])["t"]


@pytest.mark.parametrize("risk_class", list(CAPABILITIES))
def test_a_row_never_turns_a_closed_tool_open_for_any_declared_set(risk_class):
    for declared in DECLARED:
        for shape in (declared, None if not declared else declared):
            after = policy_rows.effective_effects(_row(risk_class), shape)
            if fails_closed(shape):
                assert fails_closed(after), (sorted(declared), risk_class, after)


@pytest.mark.parametrize("risk_class", list(CAPABILITIES))
def test_a_row_never_removes_a_declared_class(risk_class):
    for declared in DECLARED:
        after = policy_rows.effective_effects(_row(risk_class), declared)
        assert frozenset(declared) <= frozenset(after or ()), (sorted(declared), risk_class, after)


@pytest.mark.parametrize("risk_class", ["read", "write", "external"])
@pytest.mark.parametrize("declared", [None, frozenset()])
def test_an_unclassified_tool_stays_unclassified_under_a_row_of_an_open_class(risk_class, declared):
    assert policy_rows.effective_effects(_row(risk_class), declared) == declared  # O-LEG-ROW-125, flipped


@pytest.mark.parametrize("risk_class", ["financial", "destructive", "privileged"])
def test_a_consequential_row_class_still_raises_an_unclassified_tool(risk_class):
    assert policy_rows.effective_effects(_row(risk_class), None) == frozenset({risk_class})


@pytest.mark.parametrize("declared", [frozenset({"write"}), frozenset({"read", "write"})])
@pytest.mark.parametrize("risk_class", ["read", "write"])
def test_a_declared_bare_write_is_unchanged_by_a_lower_or_equal_class(declared, risk_class):
    assert policy_rows.effective_effects(_row(risk_class), declared) == declared


def test_a_dry_run_row_changes_nothing():
    for risk_class in CAPABILITIES:
        for declared in (None, frozenset({"read"}), frozenset({"financial"})):
            assert policy_rows.effective_effects(_row(risk_class, "dry_run"), declared) == declared


# --- the capability verdict, which is the other thing a row could loosen ---------------------------------------------

_VERDICT_RANK = {"allow": 0, "approve": 1, "deny": 2}


def _capability_sets():
    sets = [{}]  # nothing narrowed
    for name in CAPABILITIES:
        for verdict in ("deny", "approve"):
            sets.append({name: verdict})
    sets.append({"financial": "deny", "destructive": "deny", "privileged": "deny"})
    sets.append({name: "deny" for name in CAPABILITIES})
    return sets


@pytest.mark.parametrize("risk_class", list(CAPABILITIES))
def test_a_row_never_turns_a_capability_refusal_into_a_pass_for_any_declared_set(risk_class):
    """``init(capabilities=...)`` judges a tool with no declared class strictly (it is refused whenever anything is
    narrowed). A row naming a class must not hand it, or any declared tool, a more lenient verdict than the code's own
    declaration got: the verdict through the engine's own judge, with and without the row, never gets lighter."""
    from runbound.config import RunboundConfig
    from runbound.engine import Engine

    for capabilities in _capability_sets():
        engine = Engine(RunboundConfig(capabilities=capabilities))
        for declared in DECLARED + [None]:
            before = _VERDICT_RANK[engine.judge_action(frozenset(declared or ()))["verdict"]]
            raised = policy_rows.effective_effects(_row(risk_class), declared)
            after = _VERDICT_RANK[engine.judge_action(frozenset(raised or ()), unclassified=not declared)["verdict"]]
            assert after >= before, (capabilities, sorted(declared or ()), risk_class)


def test_an_unclassified_tool_stays_refused_by_a_narrowed_capability_whatever_its_row_says(tmp_path):
    import runbound
    from engine_support import Clock, engine_on_link, outcome_of

    from runbound.plane_types import HelloReply

    for risk_class in ("read", "write", "external"):
        shared = engine_on_link(tmp_path / risk_class, clock=Clock(), capabilities={"destructive": "deny"})
        shared.apply_hello(HelloReply())
        shared._set_rows([{"tool": "send", "state": "enforced", "risk_class": risk_class}], "h")

        @runbound.tool(reviewed=True)  # declares no effects
        def send():
            return "ran"

        assert outcome_of(send) == "refused:capability", risk_class


def test_o_cap_tight_1_the_deliberate_tightening_an_undeclared_tool_under_a_consequential_row_is_judged_as_unclassified_too():
    """Register K11 (amended): an undeclared tool whose row names a consequential class used to be judged ONLY as that
    class; with ``capabilities={"financial": "approve"}`` that was "needs approval". It is now the stricter of that and
    "unclassified", which is "deny" whenever anything is narrowed: approve -> deny. This is intentional, so it is pinned."""
    from runbound.config import RunboundConfig
    from runbound.engine import Engine

    engine = Engine(RunboundConfig(capabilities={"financial": "approve"}))
    raised = policy_rows.effective_effects(_row("financial"), None)
    assert engine.judge_action(raised)["verdict"] == "approve"  # what it was
    now = engine.judge_action(raised, unclassified=True)
    assert (now["verdict"], now["denied_class"]) == ("deny", "unclassified")  # what it is
    declared = engine.judge_action(frozenset({"financial"}), unclassified=False)
    assert declared["verdict"] == "approve"  # a tool the code DECLARES financial is unchanged
