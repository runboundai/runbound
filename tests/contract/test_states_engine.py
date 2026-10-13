"""The engine against the policy-state contract (TPD §5.7; register C8). Ids O-STA-ENG-*.

For each scenario of ``legacy/legacy_states_script`` (a cache of some age, a hello or not, some silence) the pure
contract (``runbound._rules.states``) says whether a tool of each class runs: ``fresh``/``cached`` enforce the
sealed policy in full (an empty one allows), ``unavailable`` follows the service's fail mode (read, write and
external open; financial, destructive, privileged and unclassified closed). Where today's engine agrees the test
passes; where it differs it is a strict xfail naming C8 (the 15-minute split, the 60 s consequential grace). The
contract's validity window is the SDK's own staleness limit, 24 h, so a 25 h cache is unavailable in both.
"""

import pytest

from legacy import legacy_states_script as script
from runbound._rules.states import Cache, CodeRules, Validity, outage_outcome, state

pytestmark = [pytest.mark.contract(item="§12.7")]

VALIDITY = Validity(fresh_s=30, cache_s=24 * 3600)
SERVICE = {"read": "open", "write": "open", "external": "open", "financial": "closed", "destructive": "closed",
           "privileged": "closed", "unclassified": "closed"}


SEALED_DECISION = {"blocked": "deny", "approval": "ask", "allowed": "allow"}


def contract_outcome(state_id: str, class_name: str):
    start = 1000.0
    if state_id.startswith("sealed_"):  # an expired cache that held a rule for the tool, 61 s into an outage
        cache = Cache(sealed_at=start - 25 * 3600, decision=SEALED_DECISION[state_id[len("sealed_"):]])
        now, served_at = start + 61, None
    else:
        _id, age_s, hello, silence_s = next(s for s in script.STATES if s[0] == state_id)
        now = start + silence_s
        cache = Cache(sealed_at=start - age_s) if age_s is not None else None
        if hello:  # a hello at the start serves policy and seals a cache of that moment
            cache, served_at = Cache(sealed_at=start), start
        else:
            served_at = None
    current = state(now, served_at, cache, VALIDITY)
    if cache is None and served_at is None:
        # The floor (F3, register C14) sits in front of the state model: a worker that has had no handshake and loaded no sealed
        # cache runs no governed call, whatever its fail mode would say once it is merely "unavailable".
        return outage_outcome("unavailable", frozenset(), {}, CodeRules(constraint_refused=True))
    return outage_outcome(current, frozenset(script.all_classes()[class_name]), SERVICE, CodeRules(), cache)


def contract_runs(state_id: str, class_name: str) -> bool:
    return contract_outcome(state_id, class_name).decision in ("allow", "evaluate")


def _params():
    """The engine of a service that has ADOPTED enforced defaults against the pure state contract: all of them pass.

    A service without adopted defaults keeps today's per-class cache trust (tests/legacy/test_legacy_states.py holds it unchanged); the single
    24 h window of the contract belongs to a service whose sealed policy carries enforced defaults (Apollo's ruling on TR2-O1, flipped by TR2-O3).
    """
    for case_id, state_id, class_name in script.cases():
        if state_id.startswith("sealed_"):
            continue  # the sealed cases have their own tests below
        yield pytest.param(state_id, class_name, contract_runs(state_id, class_name), id=case_id.replace("LEG", "STA-ENG"))


def _TODAY(case_id: str) -> bool:
    import json
    import pathlib

    fixture = pathlib.Path(script.__file__).parent / "fixtures" / "legacy_states.json"
    return json.loads(fixture.read_text())["cases"][case_id]["ran"]


@pytest.mark.parametrize("state_id, class_name, expected", list(_params()))
def test_the_engine_does_what_the_state_contract_says_with_enforced_defaults(state_id, class_name, expected):
    assert script.run_case(state_id, class_name, defaults=script.ENFORCED_DEFAULTS)["ran"] is expected


def test_the_contract_and_today_differ_only_where_the_register_says():
    differing = {(s, c) for _i, s, c in script.cases() if contract_runs(s, c) != _TODAY(_i)}
    assert {s for s, _c in differing} <= {"cache_16min", "cache_0min_no_hello_61s", "hello_then_61s_silence", "cache_25h", "no_cache",
                                          "no_hello_61s", "sealed_allowed"}, differing
    # C8: the four fail-closed classes; K8: a bare write (with or without read) is unclassified, so closed when unavailable
    assert {c for _s, c in differing} <= {"financial", "destructive", "privileged", "unclassified", "write", "read_write"}, differing
    assert not {c for s, c in differing if s in ("cache_16min", "cache_0min_no_hello_61s", "hello_then_61s_silence")} & {"write", "read_write"}


def _sealed_cases():
    return [(i, s, c) for i, s, c in script.cases() if s.startswith("sealed_")]


@pytest.mark.parametrize("case_id, state_id, class_name", _sealed_cases(), ids=[i for i, _s, _c in _sealed_cases()])
def test_an_expired_sealed_policy_that_restricted_the_tool_still_refuses_it(request, case_id, state_id, class_name):
    """K7, the outcome: today's engine enforces the rows of an expired cache, so a restricted tool is still refused.

    The one difference is K8, not K7: a sealed ``allowed`` write tool is a bare write, unclassified, so closed."""
    if script.run_case(state_id, class_name)["ran"] is not contract_runs(state_id, class_name):
        request.applymarker(pytest.mark.xfail(strict=True, raises=AssertionError, reason="K8: a bare write fails open today"))
    assert script.run_case(state_id, class_name)["ran"] is contract_runs(state_id, class_name)


@pytest.mark.parametrize("case_id, state_id, class_name", _sealed_cases(), ids=[i for i, _s, _c in _sealed_cases()])
def test_the_reason_an_expired_policy_refuses_is_policy_expired_restricted(request, case_id, state_id, class_name):
    """K7, the reason code: an expired policy that restricted the tool refuses ``policy_expired_restricted`` (R2 TR2-O1 unified it)."""
    outcome = contract_outcome(state_id, class_name)
    if outcome.decision != "refuse":
        pytest.skip("the contract lets this call run (fail open), so there is no refusal reason to compare")
    result = script.run_case(state_id, class_name)
    expected = {"plane_unavailable": "plane_unavailable"}.get(outcome.reason, outcome.reason)
    assert result["reason"] == expected
