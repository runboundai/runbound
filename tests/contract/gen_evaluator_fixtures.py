"""Writes tests/contract/fixtures/evaluator_*.json. Run once to capture; the tests only load and compare.

The expected values are authored here by hand from the TPD (Draft 5.3 §4b tables and matrix, register K1);
they are NOT computed by the evaluator. ``python tests/contract/gen_evaluator_fixtures.py`` rewrites the files.
"""

from __future__ import annotations

import itertools
import json
import pathlib

OUT = pathlib.Path(__file__).parent / "fixtures"
T, F, U = True, False, None

#: TPD §4b, verbatim: (a, b) -> (ALL OF, ANY OF), for the unordered pair
TABLE = {
    (T, T): (T, T), (T, F): (F, T), (T, U): (U, T),
    (F, U): (F, U), (F, F): (F, F), (U, U): (U, U),
}


def pair(a, b):
    return TABLE.get((a, b)) or TABLE[(b, a)]


def lit(value):
    return {"lit": value}


def fold(kind, values):
    """Left fold of the two-input table: the K2 reference for groups of three."""
    index = 0 if kind == "all" else 1
    acc = values[0]
    for value in values[1:]:
        acc = pair(acc, value)[index]
    return acc


def truth_cases():
    cases, n = [], 0

    def add(note, when, unless, expect):
        nonlocal n
        n += 1
        cases.append({"id": f"O-TT-{n}", "note": note, "when": when, "unless": unless, "expect_block": expect})

    for kind in ("all", "any"):
        index = 0 if kind == "all" else 1
        for a, b in itertools.product((T, F, U), repeat=2):  # both orders of every mixed row
            add(f"{kind.upper()} OF({a},{b})", {kind: [lit(a), lit(b)]}, None, pair(a, b)[index])
    add("ANY OF(true, unknown) matches", {"any": [lit(T), lit(U)]}, None, T)
    for c in (T, F, U):
        add(f"UNLESS {c} over an unconditional rule", None, lit(c), {T: F, F: T, U: U}[c])
    for c, d in itertools.product((T, F, U), repeat=2):
        add(f"ALL OF(true) UNLESS {d} with when={c}", lit(c), lit(d), pair(c, {T: F, F: T, U: U}[d])[0])
    nested = [
        ("ALL OF(true, ANY OF(false, unknown))", {"all": [lit(T), {"any": [lit(F), lit(U)]}]}, U),
        ("ANY OF(false, ALL OF(true, unknown))", {"any": [lit(F), {"all": [lit(T), lit(U)]}]}, U),
        ("ALL OF(ANY OF(true, unknown), ANY OF(false, false))", {"all": [{"any": [lit(T), lit(U)]}, {"any": [lit(F), lit(F)]}]}, F),
        ("ANY OF(ALL OF(true, true), ALL OF(unknown, unknown))", {"any": [{"all": [lit(T), lit(T)]}, {"all": [lit(U), lit(U)]}]}, T),
    ]
    for note, when, expect in nested:
        add(note, when, None, expect)
    for kind in ("all", "any"):
        for triple in itertools.product((T, F, U), repeat=3):
            add(f"{kind.upper()} OF{triple} = the pairwise fold", {kind: [lit(v) for v in triple]}, None, fold(kind, list(triple)))
    return cases


def rule(id, decision, when=T, unless=None, limit=None):
    out = {"id": id, "decision": decision, "when": lit(when)}
    if unless is not None:
        out["unless"] = lit(unless)
    if limit is not None:
        out["limit"] = {"source": f"limit:{id}", "exhausted": limit == "exhausted"}
    return out


def cond(name, op, value, source="arg"):
    return {"cond": {"name": name, "op": op, "value": value, "source": source}}


def matrix_cases():
    c = []

    def add(id, note, rules, fallback, expect, **extra):
        c.append({"id": id, "note": note, "rules": rules, "fallback": fallback, "expect": expect, **extra})

    add("O-MAT-1", "matching deny, an unknown ask beside it: deny", [rule("m", "deny"), rule("u", "ask", U)], "allow",
        {"decision": "deny", "primary_reason": "rule", "matched": ["m"], "unknown": ["u"]})
    add("O-MAT-1b", "matching deny, an unknown deny beside it: deny", [rule("m", "deny"), rule("u", "deny", U)], "allow",
        {"decision": "deny", "matched": ["m"], "unknown": ["u"]})
    add("O-MAT-2", "matching ask, an unknown deny: refuse", [rule("m", "ask"), rule("u", "deny", U)], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable", "matched": ["m"], "unknown": ["u"]})
    add("O-MAT-3", "matching ask, an unknown allow (could neither ask nor deny): ask", [rule("m", "ask"), rule("u", "allow", U)], "allow",
        {"decision": "ask", "primary_reason": "rule", "matched": ["m"], "unknown": ["u"]})
    add("O-MAT-3b", "K1: matching ask, an unknown ask (a second requirement): refuse", [rule("m", "ask"), rule("u", "ask", U)], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable"})
    add("O-MAT-4", "matching allow, an unknown ask: refuse", [rule("m", "allow"), rule("u", "ask", U)], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable"})
    add("O-MAT-4b", "matching allow, an unknown deny: refuse", [rule("m", "allow"), rule("u", "deny", U)], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable"})
    add("O-MAT-5", "matching allow, nothing unknown: allow", [rule("m", "allow")], "ask",
        {"decision": "allow", "primary_reason": "rule", "matched": ["m"], "unknown": []})
    add("O-MAT-6", "no match, an unknown deny, fallback allow: refuse", [rule("n", "allow", F), rule("u", "deny", U)], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable", "matched": [], "unknown": ["u"]})
    add("O-MAT-6a", "no match, an unknown ask, fallback allow: refuse", [rule("u", "ask", U)], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable"})
    add("O-MAT-6b", "K1: no match, fallback ask, an unknown ask: refuse", [rule("n", "allow", F), rule("u", "ask", U)], "ask",
        {"decision": "refuse", "primary_reason": "condition_unevaluable"})
    add("O-MAT-6c", "no match, fallback deny, an unknown ask: deny (nothing can be stricter)", [rule("u", "ask", U)], "deny",
        {"decision": "deny", "primary_reason": "fallback"})
    add("O-MAT-7", "no match, nothing unknown: the service fallback", [rule("n", "deny", F)], "ask",
        {"decision": "ask", "primary_reason": "fallback", "matched": [], "unknown": []})
    add("O-MAT-7a", "no rules at all: the service fallback allow", [], "allow", {"decision": "allow", "primary_reason": "fallback"})
    add("O-MAT-7b", "no rules, fallback deny", [], "deny", {"decision": "deny", "primary_reason": "fallback"})
    add("O-MAT-8", "K1: an unknown allow with an exhausted limit: refuse", [rule("u", "allow", U, limit="exhausted")], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable"})
    add("O-MAT-8a", "an unknown allow whose limit is not exhausted never refuses", [rule("u", "allow", U, limit="ok")], "allow",
        {"decision": "allow", "primary_reason": "fallback"})
    add("O-MAT-8b", "an unknown deny-baseline stays deny whatever the unknown limit says", [rule("m", "deny"), rule("u", "allow", U, limit="exhausted")], "allow",
        {"decision": "deny"})
    # limits (step 5)
    add("O-LIM-1", "a matching allow whose own limit is exhausted: deny, reason limit", [rule("m", "allow", limit="exhausted")], "allow",
        {"decision": "deny", "primary_reason": "limit", "contributors_include": ["limit"]})
    add("O-LIM-2", "an ask over its limit is refused, not queued: deny, reason limit", [rule("m", "ask", limit="exhausted")], "allow",
        {"decision": "deny", "primary_reason": "limit"})
    add("O-LIM-3", "a service or code limit that is exhausted denies a fallback allow", [], "allow",
        {"decision": "deny", "primary_reason": "limit"}, limits=[{"source": "code.max_calls", "exhausted": True}])
    add("O-LIM-4", "a limit that is not exhausted changes nothing", [rule("m", "allow", limit="ok")], "allow",
        {"decision": "allow"}, limits=[{"source": "code.max_calls", "exhausted": False}])
    add("O-LIM-5", "a limit on a rule that does not match is not checked", [rule("n", "allow", F, limit="exhausted")], "allow",
        {"decision": "allow", "primary_reason": "fallback"})
    # AC5: unknown inputs
    gt = lambda **kw: {"when": cond("row_count", ">", 10000), **kw}
    add("O-UNK-1", "IF row_count > 10000 -> deny, row_count=None: refuse", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "refuse", "primary_reason": "condition_unevaluable", "unknown": ["big"]}, args={"row_count": None})
    add("O-UNK-2", "the argument is missing: refuse", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "refuse"}, args={})
    add("O-UNK-3", "the argument has the wrong type (a string): refuse", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "refuse"}, args={"row_count": "20000"})
    add("O-UNK-3a", "a bool is not a number: refuse", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "refuse"}, args={"row_count": True})
    add("O-UNK-3b", "NaN is not a number: refuse", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "refuse"}, args={"row_count": float("nan")})
    add("O-UNK-4", "row_count above the threshold: deny", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "deny", "matched": ["big"]}, args={"row_count": 10001})
    add("O-UNK-4a", "row_count at the threshold: no match, the fallback", [{"id": "big", "decision": "deny", **gt()}], "allow",
        {"decision": "allow", "primary_reason": "fallback", "matched": []}, args={"row_count": 10000})
    add("O-UNK-5", "IF environment equals staging -> allow, environment=None, fallback ask: does not allow",
        [{"id": "stg", "decision": "allow", "when": cond("environment", "equals", "staging", "context")}], "ask",
        {"decision": "ask", "primary_reason": "fallback", "unknown": ["stg"]}, context={"environment": None})
    add("O-UNK-5a", "environment missing from the context: does not allow", [{"id": "stg", "decision": "allow", "when": cond("environment", "equals", "staging", "context")}], "ask",
        {"decision": "ask"}, context={})
    add("O-UNK-5b", "environment equals staging: the allow rule matches", [{"id": "stg", "decision": "allow", "when": cond("environment", "equals", "staging", "context")}], "ask",
        {"decision": "allow", "matched": ["stg"]}, context={"environment": "staging"})
    add("O-UNK-5c", "equals is case-sensitive: 'Staging' does not match", [{"id": "stg", "decision": "allow", "when": cond("environment", "equals", "staging", "context")}], "ask",
        {"decision": "ask", "matched": []}, context={"environment": "Staging"})
    add("O-UNK-6", "an operator outside the closed list is unknown: refuse, never allow", [{"id": "x", "decision": "deny", "when": cond("n", "regex", [1, 2])}], "allow",
        {"decision": "refuse"}, args={"n": 1})
    add("O-UNK-7", "ANY OF(true, unknown) inside a rule matches, so the rule decides (deny)",
        [{"id": "any", "decision": "deny", "when": {"any": [cond("a", "equals", "x"), cond("b", ">", 1)]}}], "allow",
        {"decision": "deny", "matched": ["any"]}, args={"a": "x"})
    # AC6: the ceiling
    add("O-CEIL-1", "ask and deny both matching: deny", [rule("a", "ask"), rule("d", "deny")], "allow",
        {"decision": "deny", "matched": ["a", "d"]})
    add("O-CEIL-2", "no match: the fallback", [rule("n", "deny", F)], "ask", {"decision": "ask", "primary_reason": "fallback"})
    add("O-CEIL-3", "code blocked, a console allow: deny", [rule("a", "allow")], "allow",
        {"decision": "deny", "primary_reason": "code_blocked", "matched": ["a"], "contributors_include": ["code_blocked", "rule"]}, ceiling={"blocked": True})
    add("O-CEIL-4", "a code constraint refused, rules 1-3 all allow: deny", [rule("a", "allow")], "allow",
        {"decision": "deny", "primary_reason": "code_constraint"}, ceiling={"constraint_refused": True})
    add("O-CEIL-4a", "a code constraint refused beats an unknown rule: deny, not refuse", [rule("u", "deny", U)], "allow",
        {"decision": "deny", "primary_reason": "code_constraint"}, ceiling={"constraint_refused": True})
    add("O-CEIL-5", "code approval with an explicit tool allow: ask, carrying the code requirement", [rule("a", "allow")], "allow",
        {"decision": "ask", "primary_reason": "code_approval", "matched": ["a"], "contributors_include": ["code_approval", "rule"]}, ceiling={"approval": True})
    add("O-CEIL-5a", "code approval with no rules, fallback allow: ask", [], "allow",
        {"decision": "ask", "primary_reason": "code_approval"}, ceiling={"approval": True})
    add("O-CEIL-5b", "code approval, a matching deny: deny", [rule("d", "deny")], "allow", {"decision": "deny"}, ceiling={"approval": True})
    add("O-CEIL-6", "the effect set alone would allow more than code does: the code ceiling stands",
        [rule("a", "allow")], "allow", {"decision": "ask", "primary_reason": "code_approval"},
        ceiling={"approval": True, "effects": ["read"]}, same_for_effects=[[], ["read"], ["write"], ["destructive"], ["read", "financial"]])
    return c


if __name__ == "__main__":
    (OUT / "evaluator_truth.json").write_text(json.dumps(truth_cases(), indent=1) + "\n")
    cases = matrix_cases()
    for case in cases:
        args = case.get("args") or {}
        for key, value in list(args.items()):
            if isinstance(value, float) and value != value:
                args[key] = {"$float": "nan"}  # JSON has no NaN; the loader turns this marker back
    (OUT / "evaluator_matrix.json").write_text(json.dumps(cases, indent=1) + "\n")
    print(len(truth_cases()), "truth cases,", len(cases), "matrix cases")
