"""Exhaustive property test: the evaluator against a brute-force oracle written here, separately (AC4).

Every combination of 0-3 rules x {allow, ask, deny} x {true, false, unknown} x a limit {none, ok, exhausted} on
each rule, x a fallback {allow, ask, deny}, x a ceiling {none, code approval, code blocked, code constraint
refused}, x a service-wide limit {ok, exhausted}. The oracle enumerates every completion of the unknown rules to
true or false and computes ``(decision, approval requirement set)`` for each, without the evaluator's code.

* the evaluator refuses ``condition_unevaluable`` **iff** some completion is stricter than the baseline (all
  unknowns false) on ``(decision, requirement set)``: a higher decision, or an ``ask`` that needs a requirement
  the baseline does not (register K1);
* otherwise it returns the baseline;
* it never returns ``allow`` when any completion returns ``ask`` or ``deny`` (invariant 3).
"""

import itertools

import pytest

from runbound._rules.conditions import Lit
from runbound._rules.evaluator import Ceiling, LimitState, Rule, evaluate

pytestmark = [pytest.mark.contract(item="§12.3/invariant 3")]

RANK = {"allow": 0, "ask": 1, "deny": 2}
DECISIONS = ("allow", "ask", "deny")
STATES = (True, False, None)
LIMITS = (None, "ok", "exhausted")
CEILINGS = {
    "none": Ceiling(),
    "approval": Ceiling(approval=True),
    "blocked": Ceiling(blocked=True),
    "constraint": Ceiling(constraint_refused=True),
}


def oracle_one(ceiling, rules, fallback, global_exhausted, truth):
    """``(decision, requirement set)`` for one completion. ``truth[i]`` is True/False for every rule."""
    if ceiling in ("blocked", "constraint"):
        return ("deny", frozenset())
    matched = [i for i, r in enumerate(rules) if truth[i]]
    decisions = [rules[i][0] for i in matched] if matched else [fallback]
    if ceiling == "approval":
        decisions.append("ask")
    top = max(decisions, key=RANK.get)
    if top == "deny":
        return ("deny", frozenset())
    if global_exhausted or any(rules[i][2] == "exhausted" for i in matched):
        return ("deny", frozenset())
    if top == "allow":
        return ("allow", frozenset())
    reqs = {f"rule{i}" for i in matched if rules[i][0] == "ask"}
    if not matched and fallback == "ask":
        reqs.add("fallback")
    if ceiling == "approval":
        reqs.add("code")
    return ("ask", frozenset(reqs))


def stricter(candidate, baseline):
    if RANK[candidate[0]] != RANK[baseline[0]]:
        return RANK[candidate[0]] > RANK[baseline[0]]
    return candidate[0] == "ask" and not candidate[1] <= baseline[1]


def completions(rules):
    unknown = [i for i, r in enumerate(rules) if r[1] is None]
    for values in itertools.product((True, False), repeat=len(unknown)):
        truth = [r[1] for r in rules]
        for i, v in zip(unknown, values):
            truth[i] = v
        yield truth


def build(rules):
    return [Rule(f"rule{i}", d, Lit(s), None, None if lim is None else LimitState(f"l{i}", lim == "exhausted"))
            for i, (d, s, lim) in enumerate(rules)]


def scenarios():
    rule_choices = list(itertools.product(DECISIONS, STATES, LIMITS))
    for count in range(4):
        for rules in itertools.combinations_with_replacement(rule_choices, count):
            yield list(rules)


def test_the_evaluator_agrees_with_the_brute_force_oracle_everywhere():
    checked = refused = 0
    for rules in scenarios():
        built = build(rules)
        for fallback in DECISIONS:
            for name, ceiling in CEILINGS.items():
                for global_exhausted in (False, True):
                    limits = [LimitState("service", global_exhausted)]
                    out = evaluate(ceiling, built, fallback, limits)
                    base_truth = [bool(r[1]) for r in rules]  # unknown -> not matching
                    baseline = oracle_one(name, rules, fallback, global_exhausted, base_truth)
                    results = [oracle_one(name, rules, fallback, global_exhausted, t) for t in completions(rules)]
                    must_refuse = any(stricter(r, baseline) for r in results)
                    ctx = (rules, fallback, name, global_exhausted, out)
                    if must_refuse:
                        assert out.decision == "refuse" and out.primary_reason == "condition_unevaluable", ctx
                        refused += 1
                    else:
                        assert out.decision == baseline[0], ctx
                    if out.decision == "allow":  # invariant 3
                        assert all(r[0] == "allow" for r in results), ctx
                    checked += 1
    assert checked > 90_000 and refused > 1_000  # the space really was walked, and refusals really occur


def test_every_decision_lists_the_rules_that_matched_and_the_unknown_ones():
    rules = [("deny", True, None), ("ask", None, None), ("allow", False, None)]
    out = evaluate(Ceiling(), build(rules), "allow")
    assert out.matched_rule_ids == ("rule0",) and out.unknown_rule_ids == ("rule1",)
