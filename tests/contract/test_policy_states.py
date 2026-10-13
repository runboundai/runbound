"""Fresh / cached / unavailable and the outage rule (TPD §5.7, §12 items 7 and 10). Ids O-STA-*."""

import pytest

from contract_support import load
from runbound._rules.states import Cache, CodeRules, Validity, outage_outcome, state

pytestmark = [pytest.mark.contract(item="§12.7")]

STATES = load("states_state.json")
OUTAGE = load("states_outage.json")


@pytest.mark.parametrize("case", STATES, ids=[c["id"] for c in STATES])
def test_the_state_at_a_moment(case):
    sealed = None if case["sealed_at"] is None else Cache(case["sealed_at"])
    assert state(case["now"], case["served_at"], sealed, Validity(**case["validity"])) == case["expect"], case["note"]


@pytest.mark.parametrize("case", OUTAGE, ids=[c["id"] for c in OUTAGE])
def test_what_a_call_does_in_each_state(case):
    cached = None if case["cached"] is None else Cache(0, case["cached"])
    got = outage_outcome(case["state"], frozenset(case["effects"]), case["fail_mode_by_tag"], CodeRules(**case["code"]), cached)
    for key, value in case["expect"].items():
        assert getattr(got, key) == value, (case["note"], got)
    assert got.state == case["state"]


def test_the_state_is_a_function_of_now_alone():
    validity = Validity(30, 100)
    sealed = Cache(0)
    assert [state(t, None, sealed, validity) for t in (50, 100, 101, 50)] == ["cached", "cached", "unavailable", "cached"]
