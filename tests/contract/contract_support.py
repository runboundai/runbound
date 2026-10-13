"""Shared loader for the R0 contract fixtures: JSON data to the pure evaluator's types.

A fixture node is ``{"lit": true|false|null}``, ``{"cond": {"name", "op", "value", "source"?}}``,
``{"all": [node, ...]}`` or ``{"any": [node, ...]}``. A case is
``{"id", "ceiling"?, "rules"?, "fallback", "limits"?, "args"?, "context"?, "expect"}``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from runbound._rules.conditions import Cond, Group, Lit
from runbound._rules.evaluator import Ceiling, LimitState, Rule, evaluate

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    with open(FIXTURES / name) as fh:
        return json.load(fh)


def node(spec: Any) -> Any:
    if spec is None:
        return None
    if "lit" in spec:
        return Lit(spec["lit"])
    if "cond" in spec:
        return Cond(**spec["cond"])
    if "all" in spec:
        return Group("all", tuple(node(item) for item in spec["all"]))
    if "any" in spec:
        return Group("any", tuple(node(item) for item in spec["any"]))
    raise ValueError(f"unknown fixture node {spec!r}")


def limit(spec: Any) -> "LimitState | None":
    return None if spec is None else LimitState(spec["source"], spec.get("exhausted", False))


def rule(spec: dict) -> Rule:
    return Rule(spec["id"], spec["decision"], node(spec.get("when")), node(spec.get("unless")), limit(spec.get("limit")))


def _revive(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$float"}:
        return float(value["$float"])
    if isinstance(value, dict):
        return {key: _revive(item) for key, item in value.items()}
    return value


def run(case: dict, effects: Any = None) -> Any:
    ceiling = dict(case.get("ceiling", {}))
    if effects is not None:
        ceiling["effects"] = effects
    return evaluate(
        Ceiling(ceiling.get("blocked", False), ceiling.get("approval", False), ceiling.get("constraint_refused", False),
                frozenset(ceiling.get("effects", ()))),
        [rule(spec) for spec in case.get("rules", ())],
        case["fallback"],
        [limit(spec) for spec in case.get("limits", ())],
        args=_revive(case.get("args")),
        context=_revive(case.get("context")),
    )


def expected_matches(outcome: Any, expect: dict) -> None:
    assert outcome.decision == expect["decision"], (outcome, expect)
    if "primary_reason" in expect:
        assert outcome.primary_reason == expect["primary_reason"], (outcome, expect)
    if "matched" in expect:
        assert list(outcome.matched_rule_ids) == expect["matched"], (outcome, expect)
    if "unknown" in expect:
        assert list(outcome.unknown_rule_ids) == expect["unknown"], (outcome, expect)
    for kind in expect.get("contributors_include", ()):
        assert kind in [c.kind for c in outcome.contributors], (kind, outcome)

from engine_support import Clock, FakeClient, engine_on_link, outcome_of  # noqa: E402,F401  (re-exported for the contract tests)
