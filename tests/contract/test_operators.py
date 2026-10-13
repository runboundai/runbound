"""Every §5.2 operator, three-valued (TR3-O; WIRE-TR3 §2, §5). Ids O-OP-*.

True, false, or UNKNOWN: a missing input, ``None``, a value of the wrong type for the operator (a bool is not a number, a string is not a list, a
number is not a string) or an operator outside the closed list is unknown, never false. Strings compare exactly: no regex, no wildcard, no trimming.
"""

from decimal import Decimal

import pytest

from runbound._rules.conditions import Cond, Group, block, condition

pytestmark = [pytest.mark.contract(item="§5.2")]

T, F, U = True, False, None

CASES = [
    # number
    (">", 5, 3, T), (">", 3, 5, F), (">", 3, 3, F), (">", Decimal("5.5"), 5, T), (">", 5.5, 5, T), (">", True, 0, U), (">", "5", 3, U), (">", 5, "3", U),
    (">=", 3, 3, T), (">=", 2, 3, F), (">=", float("nan"), 3, U), ("<", 2, 3, T), ("<", 3, 3, F), ("<=", 3, 3, T), ("<=", 4, 3, F), ("<=", None, 3, U),
    ("between", 5, [1, 10], T), ("between", 1, [1, 10], T), ("between", 10, [1, 10], T), ("between", 11, [1, 10], F), ("between", 0, [1, 10], F),
    ("between", 5, [10, 1], U), ("between", 5, [1], U), ("between", 5, "1,10", U), ("between", "5", [1, 10], U), ("between", True, [0, 1], U),
    # string
    ("equals", "EU", "EU", T), ("equals", "eu", "EU", F), ("equals", " EU", "EU", F), ("equals", 5, "5", U), ("equals", "5", 5, U),
    ("starts_with", "refund_42", "refund", T), ("starts_with", "Refund_42", "refund", F), ("starts_with", "x", "", T), ("starts_with", 1, "1", U),
    ("ends_with", "a.csv", ".csv", T), ("ends_with", "a.CSV", ".csv", F), ("ends_with", ["a"], "a", U),
    ("is_one_of", "b", ["a", "b"], T), ("is_one_of", "B", ["a", "b"], F), ("is_one_of", "a", [], U), ("is_one_of", 1, ["1"], U), ("is_one_of", "a", "ab", U),
    ("is_one_of", "a", ["a", 1], U),
    # boolean
    ("is_true", True, None, T), ("is_true", False, None, F), ("is_false", False, None, T), ("is_false", True, None, F), ("is_true", 1, None, U),
    ("is_true", "true", None, U), ("is_false", 0, None, U),
    # list
    ("contains", ["a", "b"], "a", T), ("contains", ["a", "b"], "c", F), ("contains", ("a",), "a", T), ("contains", {"a"}, "a", T), ("contains", [1, 2], 2, T),
    ("contains", [True], 1, F), ("contains", [1], True, F), ("contains", ["1"], 1, F), ("contains", "abc", "a", U), ("contains", ["a"], None, U),
    ("contains", ["a"], ["a"], U), ("contains", [], "a", F),
    ("length_lt", [1, 2], 3, T), ("length_lt", [1, 2], 2, F), ("length_gt", [1, 2], 1, T), ("length_gt", [], 0, F), ("length_lt", "ab", 3, U),
    ("length_gt", [1], -1, U), ("length_gt", [1], True, U), ("length_lt", [1], 1.5, U),
    # outside the closed list
    ("matches", "abc", "a.c", U), ("regex", "abc", "a.*", U), ("is_between", 5, [1, 9], U), ("", 5, 5, U),
]


@pytest.mark.parametrize("op, actual, value, expected", CASES, ids=[f"O-OP-{i + 1}" for i in range(len(CASES))])
def test_the_operator_is_true_false_or_unknown(op, actual, value, expected):
    assert condition(Cond("x", op, value), {"x": actual}) is expected


def test_a_missing_argument_is_unknown_for_every_operator():
    for op in {c[0] for c in CASES if c[0]}:
        assert condition(Cond("x", op, 1), {}) is None and condition(Cond("x", op, 1), {"y": 1}) is None


def test_a_context_condition_reads_the_context_not_the_arguments():
    assert condition(Cond("environment", "equals", "prod", source="context"), {"environment": "dev"}, {"environment": "prod"}) is True
    assert condition(Cond("environment", "is_one_of", ["prod", "stage"], source="context"), {}, {"environment": "dev"}) is False
    assert condition(Cond("environment", "equals", "prod", source="context"), {}, {}) is None  # no context: unknown


def test_groups_and_unless_are_three_valued():
    yes, no = Cond("x", ">", 1), Cond("x", "<", 0)
    unknown = Cond("missing", ">", 1)
    assert condition(Group("all", (yes, unknown)), {"x": 2}) is None and condition(Group("all", (no, unknown)), {"x": 2}) is False
    assert condition(Group("any", (yes, unknown)), {"x": 2}) is True and condition(Group("any", (no, unknown)), {"x": 2}) is None
    nested = Group("all", (yes, Group("any", (no, Cond("x", "between", [1, 5])))))
    assert condition(nested, {"x": 2}) is True
    assert block(Group("all", (yes,)), Cond("x", ">", 100), {"x": 2}) is True  # unless is false: the rule applies
    assert block(Group("all", (yes,)), Cond("x", ">", 1), {"x": 2}) is False  # unless is true: it does not
    assert block(Group("all", (yes,)), unknown, {"x": 2}) is None  # unless is unknown: the block is unknown


def test_an_operator_that_raises_is_unknown_not_a_crash():
    class Boom:
        def __gt__(self, other):
            raise RuntimeError("boom")

    assert condition(Cond("x", ">", 1), {"x": Boom()}) is None


# --- the shared fixture the plane's vendored copy is held to -------------------------------------------------------------------


def _json_cases():
    out = []
    for index, (op, actual, value, expected) in enumerate(CASES):
        if isinstance(actual, (Decimal, set, tuple)) or (isinstance(actual, float) and actual != actual):
            continue  # not representable in JSON; the plane carries its own copy of the same table
        out.append({"id": f"O-OP-{index + 1}", "op": op, "actual": actual, "value": value, "expect": expected})
    return out


def test_the_operator_fixture_the_plane_parity_test_reads_is_this_table():
    import json
    import pathlib

    path = pathlib.Path(__file__).parent / "fixtures" / "operators.json"
    if not path.exists():
        path.write_text(json.dumps({"about": "Every §5.2 operator, three-valued (tests/contract/test_operators.py). expect is true, false or null (unknown).", "cases": _json_cases()},
                                   indent=1) + "\n")
    held = json.loads(path.read_text())["cases"]
    assert held == json.loads(json.dumps(_json_cases()))
    for case in held:  # and the evaluator gives each of them the answer it records
        assert condition(Cond("x", case["op"], case["value"]), {"x": case["actual"]}) is case["expect"], case["id"]
