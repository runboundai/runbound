"""A refusal *from the plane* is refused at the door, end to end.

Two answers arrive as ``EntryDecision(allow=False, refusal=...)``: the org's
daily budget is spent (detector ``budget``), and, under
``on_plane_loss="refuse"``, the plane could not be asked (detector ``plane``).
Before Wave 24 the api read only offsets, strikes and the latch off a decision
and never ``allow``, so the plane's "no" was silently overruled. These tests
drive the refusal through ``runbound.session()`` itself, which no earlier
test did.
"""

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import EntryDecision

from test_session_sync import plane, start  # noqa: F401  (fixture + helper)


ORG_BUDGET = {
    "detector": "budget",
    "severity": "critical",
    "message": "Org daily budget spent: $12.40 of $10.00",
    "details": {"reason": "org_budget", "spend_usd": 12.4, "limit_usd": 10.0},
}


def test_an_org_budget_refusal_from_the_plane_is_refused_at_the_door(plane):
    plane.decision = EntryDecision(allow=False, refusal=dict(ORG_BUDGET))
    start(on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user:1"):
            pytest.fail("the block must not run")

    anomaly = caught.value.anomaly
    assert anomaly.detector == "budget"
    assert anomaly.details["reason"] == "org_budget"
    assert anomaly.details["origin"] == "plane"
    assert "12.40" in str(caught.value)


def test_a_plane_refusal_latches_nothing_and_earns_no_strike(plane):
    plane.decision = EntryDecision(allow=False, refusal=dict(ORG_BUDGET))
    start(on_anomaly="raise")

    with pytest.raises(GuardrailTripped):
        with runbound.session("user:1"):
            pass

    assert runbound.is_tripped("user:1") is None
    status = runbound.session_status("user:1")
    assert status is None or status["strikes"] == 0
    assert api._STRIKES.get("user:1", 0) == 0


def test_the_next_entry_asks_the_plane_again_and_is_admitted_when_it_says_yes(plane):
    plane.decision = EntryDecision(allow=False, refusal=dict(ORG_BUDGET))
    start(on_anomaly="raise")
    with pytest.raises(GuardrailTripped):
        with runbound.session("user:1"):
            pass
    asked_before = sum(1 for name, _p, _t in plane.calls if name == "enter")

    plane.decision = EntryDecision(allow=True)
    api._SHARED._cache.clear()
    ran = False
    with runbound.session("user:1"):
        ran = True

    asked_after = sum(1 for name, _p, _t in plane.calls if name == "enter")
    assert ran
    assert asked_after == asked_before + 1


def test_a_refusal_is_enforced_whatever_on_anomaly_says(plane):
    """Like a halt: the plane's no is a decision, not a detector opinion."""
    plane.decision = EntryDecision(allow=False, refusal=dict(ORG_BUDGET))
    start(on_anomaly="warn")

    with pytest.raises(GuardrailTripped):
        with runbound.session("user:1"):
            pass


def test_plane_loss_refuse_refuses_at_the_door_when_the_plane_is_down(plane):
    plane.ok = False
    start(on_anomaly="raise", on_plane_loss="refuse")

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user:2"):
            pytest.fail("the block must not run")

    assert caught.value.anomaly.detector == "plane"
    assert caught.value.anomaly.details["reason"] == "plane_unreachable"
    assert runbound.is_tripped("user:2") is None


def test_plane_loss_guard_locally_admits_when_the_plane_is_down(plane):
    plane.ok = False
    start(on_anomaly="raise")

    ran = False
    with runbound.session("user:2"):
        ran = True
    assert ran


def test_an_allowed_decision_still_applies_fleet_facts(plane):
    """The patch must not have broken the ordinary path."""
    plane.decision = EntryDecision(allow=True, fleet_spend_usd=4.0, strikes=2, generation=1)
    start(on_anomaly="raise", budget_usd=5.0)

    with runbound.session("user:3") as state:
        assert state.spend_offset_usd == pytest.approx(4.0)
        assert state.strikes == 2


def test_a_refusal_that_carries_a_fleet_latch_still_adopts_the_latch_and_strikes(plane):
    """The plane refuses a latched key with the latch attached: that path must
    keep behaving exactly as before Wave 24 — strikes travel, the latch is
    adopted locally with origin 'fleet', and the block is refused."""
    plane.decision = EntryDecision(
        allow=False,
        refusal={"detector": "budget", "severity": "critical",
                 "message": "latched elsewhere", "details": {}},
        latch={"detector": "budget", "severity": "critical",
               "message": "Budget exceeded on worker A", "details": {}, "ttl_s": 60.0},
        strikes=1,
        generation=1,
    )
    start(on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user:4"):
            pytest.fail("the block must not run")

    assert caught.value.anomaly.details.get("origin") == "fleet"
    assert runbound.is_tripped("user:4") is not None
    assert api._STRIKES.get("user:4") == 1


@pytest.mark.parametrize("latch", [
    {"detector": "budget", "severity": "critical", "message": "expired",
     "details": {}, "ttl_remaining_s": 0.0},
    "not-a-dict",
])
def test_a_refusal_whose_latch_cannot_be_adopted_is_still_refused(plane, latch):
    """Review finding: an expired or unreadable latch must not turn the plane's
    'no' into an admission."""
    plane.decision = EntryDecision(
        allow=False,
        refusal=dict(ORG_BUDGET),
        latch=latch,
    )
    start(on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user:5"):
            pytest.fail("the block must not run")

    assert caught.value.anomaly.details.get("origin") == "plane"
