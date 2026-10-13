"""Today's plane-loss behaviour per capability class (R0 AC11). Ids O-LEG-STA-*. Captured, never edited to go green."""

import json
import pathlib

import pytest

from . import legacy_states_script as script

pytestmark = pytest.mark.legacy

with open(pathlib.Path(__file__).parent / "fixtures" / "legacy_states.json") as _fh:
    CASES = json.load(_fh)["cases"]


@pytest.mark.parametrize("case_id", sorted(CASES, key=lambda c: int(c.rsplit("-", 1)[1])))
def test_the_class_in_the_state_does_what_it_did(case_id):
    case = CASES[case_id]
    assert script.run_case(case["state"], case["class"]) == {k: v for k, v in case.items() if k not in ("state", "class")}


def test_every_class_in_every_state_is_captured():
    assert sorted(CASES) == sorted(script.case_ids())
    assert {(c["state"], c["class"]) for c in CASES.values()} == {(s, n) for _i, s, n in script.cases()}
