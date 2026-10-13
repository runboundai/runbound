"""Three-valued truth tables (TPD §4b, §12 item 11; register K2). Ids O-TT-*."""

import itertools

import pytest

from contract_support import load, node, run  # noqa: F401
from runbound._rules.conditions import block
from runbound._rules.tri import all_of, and3, any_of, not3, or3

pytestmark = [pytest.mark.contract(item="§12.11")]

CASES = load("evaluator_truth.json")
T, F, U = True, False, None


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_block_of_every_truth_table_case(case):
    assert block(node(case["when"]), node(case["unless"])) is case["expect_block"], case["note"]


def test_there_is_a_case_for_every_row_of_both_tables_in_both_orders():
    notes = {c["note"] for c in CASES}
    for kind in ("ALL", "ANY"):
        for a, b in itertools.product((T, F, U), repeat=2):
            assert f"{kind} OF({a},{b})" in notes
    assert "ANY OF(true, unknown) matches" in notes  # the named one


def test_any_of_true_and_unknown_matches():
    assert any_of([T, U]) is True and any_of([U, T]) is True


@pytest.mark.parametrize("values", list(itertools.product((T, F, U), repeat=3)))
def test_a_group_of_three_equals_the_pairwise_fold_in_every_order(values):
    for order in itertools.permutations(values):
        assert all_of(order) is and3(and3(order[0], order[1]), order[2])
        assert any_of(order) is or3(or3(order[0], order[1]), order[2])
        assert all_of(order) is all_of(values) and any_of(order) is any_of(values)


@pytest.mark.parametrize("value, expect", [(T, F), (F, T), (U, U)])
def test_unless_is_three_valued_negation(value, expect):
    assert not3(value) is expect
