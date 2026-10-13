"""Approval requirements as a set; each satisfied separately (TPD §4b step 6, §12 item 12). Ids O-APR-*."""

import pytest

from contract_support import load
from runbound._rules.approvals import Decision, Requirement, after_decisions, authorised, requirements, states, wait_s
from runbound._rules.evaluator import Outcome

pytestmark = [pytest.mark.contract(item="§12.12")]

REQS = load("approvals_requirements.json")
WAITS = load("approvals_waits.json")
FLOW = load("approvals_flow.json")


def req(spec):
    return Requirement(spec["source"], frozenset(spec.get("eligible", ())), spec.get("wait_s"))


@pytest.mark.parametrize("case", REQS, ids=[c["id"] for c in REQS])
def test_which_requirements_a_call_needs(case):
    outcome = Outcome(case["outcome"]["decision"], case["outcome"]["primary_reason"])
    got = requirements(
        outcome,
        req(case["code_approval"]) if case.get("code_approval") else None,
        [req(s) for s in case.get("matching_ask", ())],
        case.get("fallback_operative", False),
        [req(s) for s in case.get("service", ())],
        [req(s) for s in case.get("tags", ())],
    )
    assert [r.source for r in got] == case["expect"]
    for source, eligible in case.get("expect_eligible", {}).items():
        assert {r.source: r.eligible for r in got}[source] == frozenset(eligible)


@pytest.mark.parametrize("case", WAITS, ids=[c["id"] for c in WAITS])
def test_the_wait_is_the_shortest(case):
    assert wait_s([req(s) for s in case["reqs"]]) == case["expect_wait"]


@pytest.mark.parametrize("case", FLOW, ids=[c["id"] for c in FLOW])
def test_requirements_are_satisfied_one_by_one(case):
    reqs = [req(s) for s in case["reqs"]]
    decisions = [Decision(d["approver"], d["source"], d.get("verdict", "approve")) for d in case["decisions"]]
    got = states(reqs, decisions, one_decider_satisfies_all=case.get("shared", False))
    assert got == case["expect_states"]
    assert authorised(reqs, got) is case["expect_authorised"]
    assert after_decisions(reqs, got) == case["expect_after"]


def test_two_requirements_one_satisfied_and_one_pending_never_executes():
    """§12 item 12, named."""
    a, b = Requirement("a", frozenset({"x"})), Requirement("b", frozenset({"y"}))
    got = states([a, b], [Decision("x", "a")])
    assert not authorised([a, b], got) and after_decisions([a, b], got) == "wait"
    got = states([a, b], [Decision("x", "a"), Decision("y", "b")])
    assert authorised([a, b], got)
