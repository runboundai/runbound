"""The §4b decision matrix, K1's rows, limits, unknown inputs and the code ceiling. Ids O-MAT-*, O-LIM-*, O-UNK-*, O-CEIL-*."""

import pytest

from contract_support import expected_matches, load, run
from runbound._rules.evaluator import Rule, evaluate, Ceiling

pytestmark = [pytest.mark.contract(item="§12.3/§12.9")]

CASES = load("evaluator_matrix.json")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_case(case):
    expected_matches(run(case), case["expect"])


@pytest.mark.parametrize("case", [c for c in CASES if "same_for_effects" in c], ids=lambda c: c["id"])
def test_the_effect_set_selects_defaults_and_is_never_a_ceiling(case):
    outcomes = {run(case, effects=effects) for effects in case["same_for_effects"]}
    assert len(outcomes) == 1  # whatever the declared effects, the code ceiling alone decides


def test_the_seven_matrix_rows_are_all_present():
    ids = {c["id"] for c in CASES}
    assert {f"O-MAT-{n}" for n in range(1, 9)} | {"O-MAT-3b", "O-MAT-6b"} <= ids


def test_a_malformed_rule_set_raises_rather_than_allowing():
    with pytest.raises(ValueError):
        evaluate(Ceiling(), [Rule("a", "maybe")], "allow")
    with pytest.raises(ValueError):
        evaluate(Ceiling(), [], "perhaps")
    with pytest.raises(ValueError):
        evaluate(Ceiling(), [Rule("a", "allow"), Rule("a", "deny")], "allow")


def test_an_evaluator_that_raises_inside_a_condition_is_unknown():
    class Boom(dict):
        def __contains__(self, key):
            raise RuntimeError("boom")

    from runbound._rules.conditions import Cond

    outcome = evaluate(Ceiling(), [Rule("r", "deny", Cond("x", ">", 1))], "allow", args=Boom())
    assert (outcome.decision, outcome.primary_reason) == ("refuse", "condition_unevaluable")


def test_the_outcome_is_deterministic():
    case = next(c for c in CASES if c["id"] == "O-CEIL-1")
    assert run(case) == run(case)
