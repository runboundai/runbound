"""admission.py's pure stage functions, table-driven and mock-free.

Every stage is plain arithmetic — numbers in, a :class:`~runbound.events.Decision`
out — so each is tested here with bare values, no ``Engine``, no
``SessionState``, no lock. The shared shape every table below checks:
exactly-at-the-limit allows, one over denies, ``None`` (the limit is off)
always allows, and the zero/empty edge is explicit rather than assumed.
"""

import pytest

from runbound import admission
from runbound.events import ALLOW, Decision

# --- circuit -----------------------------------------------------------------


def test_circuit_allows_when_the_breaker_says_so():
    assert admission.circuit(True, "openai", "closed", 30.0) is ALLOW


def test_circuit_denies_when_the_breaker_is_open():
    decision = admission.circuit(False, "openai", "open", 30.0)
    assert decision.verdict == "deny"
    assert decision.kind == "model_call"
    assert decision.boundary == "circuit"
    assert decision.detector == "circuit"
    assert decision.evaluation == {"cooldown_s": 30.0}


# --- unpriced ------------------------------------------------------------


def test_unpriced_allows_a_known_price():
    assert admission.unpriced(True, "gpt-4o") is ALLOW


def test_unpriced_denies_an_unknown_price():
    decision = admission.unpriced(False, "mystery-model")
    assert decision.verdict == "deny"
    assert decision.boundary == "money"
    assert decision.detector == "budget"
    assert decision.evaluation == {"model": "mystery-model"}


# --- steps -----------------------------------------------------------------


@pytest.mark.parametrize(
    "turns, max_steps, allow",
    [
        (0, None, True),  # None always allows
        (0, 5, True),  # zero case
        (4, 5, True),  # exactly at the limit (this call would be step 5) allows
        (5, 5, False),  # one over: this call would be step 6
        (6, 5, False),  # already well past
    ],
)
def test_steps_table(turns, max_steps, allow):
    decision = admission.steps(turns, max_steps)
    if allow:
        assert decision is ALLOW
    else:
        assert decision.verdict == "deny"
        assert decision.kind == "model_call"
        assert decision.boundary == "steps"
        assert decision.detector == "steps"
        # The number the wall would have reported: turns + 1, not turns.
        assert decision.evaluation == {"limit": max_steps, "used": turns + 1}


# --- run_time ----------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed_s, max_seconds, allow",
    [
        (0.0, None, True),
        (0.0, 60.0, True),  # zero case
        (60.0, 60.0, True),  # exactly at the limit allows
        (60.001, 60.0, False),  # one over denies
        (120.0, 60.0, False),
    ],
)
def test_run_time_table(elapsed_s, max_seconds, allow):
    decision = admission.run_time(elapsed_s, max_seconds)
    if allow:
        assert decision is ALLOW
    else:
        assert decision.verdict == "deny"
        assert decision.boundary == "time"
        assert decision.detector == "timeout"
        assert decision.evaluation == {"limit": max_seconds, "used": elapsed_s}


# --- tokens ------------------------------------------------------------------


@pytest.mark.parametrize(
    "total_tokens, stated_cap, max_total_tokens, allow",
    [
        (0, None, 1000, True),  # uncapped call: not checked at all
        (0, 100, None, True),  # no wall configured
        (0, 0, 1000, True),  # zero cap, zero case
        (900, 100, 1000, True),  # projects to exactly the limit: allows
        (900, 101, 1000, False),  # one token over projects one over
        (1000, 1, 1000, False),
    ],
)
def test_tokens_table(total_tokens, stated_cap, max_total_tokens, allow):
    decision = admission.tokens(total_tokens, stated_cap, max_total_tokens)
    if allow:
        assert decision is ALLOW
    else:
        assert decision.verdict == "deny"
        assert decision.boundary == "tokens"
        assert decision.detector == "budget"
        assert decision.evaluation == {
            "limit": max_total_tokens,
            "used": total_tokens,
            "estimate": stated_cap,
        }


# --- money ---------------------------------------------------------------


@pytest.mark.parametrize(
    "estimate, remaining, allow",
    [
        (0.0, 0.0, True),  # zero case: a free call always fits
        (1.0, 1.0, True),  # exactly at the limit allows
        (1.01, 1.0, False),  # one cent over denies
        (5.0, 1.0, False),
    ],
)
def test_money_table(estimate, remaining, allow):
    decision = admission.money(estimate, remaining)
    if allow:
        assert decision is ALLOW
    else:
        assert decision.verdict == "deny"
        assert decision.boundary == "money"
        assert decision.detector == "budget"
        assert decision.evaluation == {"remaining": remaining, "estimate": estimate}


# --- actions -----------------------------------------------------------------


@pytest.mark.parametrize(
    "executed, max_actions_per_run, allow",
    [
        (0, None, True),
        (0, 3, True),  # zero case
        (2, 3, True),  # one under the limit: this attempt would be the 3rd, allowed
        (3, 3, False),  # already executed the limit: the next would be one over
        (10, 3, False),
    ],
)
def test_actions_table(executed, max_actions_per_run, allow):
    # ``executed`` is the count *before* this attempt (an admission
    # refusal must never itself count as executed), so the boundary moved
    # from "executed <= max allows" to "executed < max allows" — the same
    # "count already spent" shape ``steps`` above uses.
    decision = admission.actions(executed, max_actions_per_run)
    if allow:
        assert decision is ALLOW
    else:
        assert decision.verdict == "deny"
        assert decision.kind == "action"
        assert decision.boundary == "blast_radius"
        assert decision.detector == "fanout"
        assert decision.evaluation == {"limit": max_actions_per_run, "used": executed + 1}


# --- posture -----------------------------------------------------------------


def test_posture_allows():
    assert admission.posture("allow", "financial", "full", "posture", "full") is ALLOW


def test_posture_denies():
    decision = admission.posture(
        "deny", "financial", "restricted", "manual", "restricted denies financial"
    )
    assert decision.verdict == "deny"
    assert decision.kind == "action"
    assert decision.boundary == "posture"
    assert decision.detector == "safe_mode"
    assert decision.evaluation == {
        "posture": "restricted",
        "denied_class": "financial",
        "source": "manual",
    }


def test_posture_approve_with_no_queue_restricts_rather_than_denies():
    decision = admission.posture(
        "approve", "external", "custom", "class_rule", "needs approval"
    )
    assert decision.verdict == "restrict"


# --- capability --------------------------------------------------------------


def test_capability_allows():
    assert admission.capability("allow", "financial") is ALLOW


def test_capability_denies():
    decision = admission.capability("deny", "destructive")
    assert decision.verdict == "deny"
    assert decision.boundary == "capability"
    assert decision.detector == "safe_mode"
    assert decision.evaluation == {"denied_class": "destructive"}


def test_capability_approve_restricts():
    decision = admission.capability("approve", "privileged")
    assert decision.verdict == "restrict"


# --- stopped -----------------------------------------------------------------


def test_stopped_allows_when_the_posture_is_not_stopped():
    assert admission.stopped(False, "restricted", "manual", "restricted") is ALLOW


def test_stopped_denies_a_model_call_when_the_posture_is_stopped():
    decision = admission.stopped(True, "stopped", "manual", "manual: stopped")
    assert decision.verdict == "deny"
    assert decision.kind == "model_call"
    assert decision.boundary == "posture"
    assert decision.detector == "safe_mode"
    assert decision.evaluation == {"posture": "stopped", "source": "manual"}


def test_stopped_reports_the_source_that_set_it():
    decision = admission.stopped(True, "stopped", "ladder", "closed: allowance spent")
    assert decision.evaluation["source"] == "ladder"


# --- Decision round-trip -------------------------------------------------


def test_decision_as_dict_from_dict_round_trips():
    decision = admission.steps(5, 5)
    data = decision.as_dict()
    restored = Decision.from_dict(data)
    assert restored == decision


def test_decision_from_dict_tolerates_garbage():
    assert Decision.from_dict(None) == Decision()
    assert Decision.from_dict({"verdict": "not-a-real-verdict"}) == Decision()
    assert Decision.from_dict({"policy_version": "not-an-int"}).policy_version is None
    assert Decision.from_dict({"evaluation": "not-a-dict"}).evaluation == {}
    assert Decision.from_dict({"boundary": 7}).boundary is None


def test_allow_constant_is_shared():
    """The allow path allocates nothing: every stage returns the same object."""
    assert admission.steps(0, 10) is admission.run_time(0.0, 10.0) is ALLOW
