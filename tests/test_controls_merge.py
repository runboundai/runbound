"""``runbound.controls_merge`` — the shared fixture, plus the SDK's own
tighten-against-the-code helpers.

``merge``/``combine`` are read against ``tests/fixtures/controls_cases.json``
exactly as the control plane's own merge module and the dashboard's TS
preview mirror are — the one table three suites agree on (see the
fixture's own ``_comment`` and this module's docstring in
``runbound/controls_merge.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runbound.controls_merge import (
    Violation,
    combine,
    effective_capabilities,
    effective_detector,
    effective_envelope,
    effective_limits,
    fold_levels,
    malformed_limits,
    merge,
    LIMIT_FIELDS,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "controls_cases.json"


def _load() -> dict:
    with open(FIXTURE_PATH) as f:
        return json.load(f)


DATA = _load()


@pytest.mark.parametrize("case", DATA["merge"], ids=lambda c: c["name"])
def test_merge_case(case: dict) -> None:
    result = merge(case["base"], case["candidate"])

    assert result.accepted == case["accepted"], case["name"]
    expected = tuple(Violation(v["path"], v["base"], v["candidate"]) for v in case["violations"])
    assert result.violations == expected, case["name"]


@pytest.mark.parametrize("case", DATA["combine"], ids=lambda c: c["name"])
def test_combine_case(case: dict) -> None:
    assert combine(case["a"], case["b"]) == case["combined"], case["name"]


def test_the_shared_fixture_is_not_empty() -> None:
    """Same guard as the plane's and the dashboard's own copies of this
    test: an empty ``parametrize`` list would report a false "0 failed"."""
    assert len(DATA["merge"]) >= 10
    assert len(DATA["combine"]) >= 8


# --- fold_levels --------------------------------------------------------------


def test_fold_levels_takes_the_strictest_of_org_service_and_run():
    folded = fold_levels(
        {
            "org": {"budget_usd": 5.0},
            "service": {"budget_usd": 50.0},
            "run": {"budget_usd": 20.0},
        }
    )

    assert folded == {"budget_usd": 5.0}


def test_fold_levels_ignores_agent_key_and_action():
    """These three levels are carried but not enforced yet -- see
    ENFORCEABLE_LEVELS. A stricter value at one of them must never leak
    into this worker's effective number."""
    folded = fold_levels({"agent": {"budget_usd": 1.0}, "key": {"budget_usd": 1.0}, "action": {"max_steps": 1}})

    assert folded == {}


def test_fold_levels_of_nothing_is_empty():
    assert fold_levels(None) == {}
    assert fold_levels({}) == {}


# --- effective_limits ---------------------------------------------------------


def test_effective_limits_the_plane_can_tighten_the_code():
    effective, violations = effective_limits(
        {"budget_usd": 100.0}, {"org": {"budget_usd": 10.0}}
    )

    assert effective["budget_usd"] == 10.0
    assert violations == []


def test_effective_limits_the_plane_cannot_loosen_the_code():
    effective, violations = effective_limits(
        {"budget_usd": 10.0}, {"org": {"budget_usd": 100.0}}
    )

    assert effective["budget_usd"] == 10.0
    assert violations == [Violation("limits.budget_usd", 10.0, 100.0)]


def test_effective_limits_the_plane_saying_nothing_is_never_a_violation():
    """The overwhelmingly common case: the plane's Controls body mentions a
    handful of fields, not every one the code cares about. Silence on a
    field must never be read as the plane trying to loosen it."""
    effective, violations = effective_limits(
        {"budget_usd": 10.0, "max_steps": 20}, {"org": {"max_steps": 5}}
    )

    assert effective["budget_usd"] == 10.0
    assert effective["max_steps"] == 5
    assert violations == []


def test_effective_limits_the_plane_introducing_a_limit_tightens():
    effective, violations = effective_limits({"budget_usd": None}, {"org": {"budget_usd": 5.0}})

    assert effective["budget_usd"] == 5.0
    assert violations == []


def test_effective_limits_a_plane_token_budget_is_merged_like_a_dollar_budget():
    effective, violations = effective_limits({}, {"org": {"budget_tokens": 500_000}, "service": {"budget_tokens": 200_000}})

    assert effective["budget_tokens"] == 200_000  # org and service compete: the lower cap
    assert violations == []


def test_effective_limits_the_plane_cannot_loosen_a_token_budget_the_code_holds():
    effective, violations = effective_limits({"budget_tokens": 100_000}, {"org": {"budget_tokens": 900_000}})

    assert effective["budget_tokens"] == 100_000
    assert violations == [Violation("limits.budget_tokens", 100_000, 900_000)]


def test_effective_limits_with_no_plane_body_at_all_is_the_code_unchanged():
    effective, violations = effective_limits({"budget_usd": 10.0, "max_steps": 20}, {})

    assert effective["budget_usd"] == 10.0
    assert effective["max_steps"] == 20
    assert violations == []


# --- effective_capabilities ---------------------------------------------------


def test_effective_capabilities_the_plane_tightens():
    effective, violations = effective_capabilities({}, {"financial": "deny"})

    assert effective == {"financial": "deny"}
    assert violations == []


def test_effective_capabilities_the_plane_cannot_loosen():
    effective, violations = effective_capabilities({"financial": "deny"}, {"financial": "allow"})

    assert effective == {"financial": "deny"}
    assert violations == [Violation("capabilities.financial", "deny", "allow")]


def test_effective_capabilities_unmentioned_class_is_never_a_violation():
    effective, violations = effective_capabilities({"financial": "deny"}, {"destructive": "deny"})

    assert effective == {"financial": "deny", "destructive": "deny"}
    assert violations == []


# --- effective_envelope --------------------------------------------------------


def test_effective_envelope_the_plane_can_turn_it_on():
    effective, violation = effective_envelope(False, True, stated=True)

    assert effective is True
    assert violation is None


def test_effective_envelope_the_plane_cannot_turn_it_off():
    effective, violation = effective_envelope(True, False, stated=True)

    assert effective is True
    assert violation == Violation("envelope", True, False)


def test_effective_envelope_unstated_is_never_a_violation_even_when_code_is_true():
    """The plane's Controls body simply not mentioning envelope (the common
    case, since the SDK's own default is already True) must never be
    reported as a refused loosening."""
    effective, violation = effective_envelope(True, None, stated=False)

    assert effective is True
    assert violation is None


def test_effective_envelope_stated_false_against_code_false_is_a_no_op():
    effective, violation = effective_envelope(False, False, stated=True)

    assert effective is False
    assert violation is None


# --- effective_detector --------------------------------------------------------


def test_effective_detector_nothing_stated_is_no_override():
    spec, violations = effective_detector("loop", "notify", None)

    assert spec is None
    assert violations == []


def test_effective_detector_the_plane_can_tighten_notify_to_stop():
    """The merge itself always tightens (this is what makes it a real
    control); whether a "notify" -> "stop" tightening can actually be
    *applied* is Engine._merge_detectors's own can_stop gate, which this
    pure function knows nothing about."""
    spec, violations = effective_detector("loop", "notify", {"action": "stop", "mode": "enforce"})

    assert spec == {"action": "stop", "mode": "enforce"}
    assert violations == []


def test_effective_detector_the_plane_cannot_loosen_stop_to_notify():
    spec, violations = effective_detector("loop", "stop", {"action": "notify", "mode": "enforce"})

    assert spec == {"action": "stop", "mode": "enforce"}
    assert violations == [Violation("detectors.loop.action", "stop", "notify")]


def test_effective_detector_shadow_mode_is_adopted_freely_with_a_notify_baseline():
    """Nothing local to protect when the baseline itself is "notify": no
    stop was ever going to happen, so a preview of one is not a loosening."""
    spec, violations = effective_detector("budget", "notify", {"action": "stop", "mode": "shadow"})

    assert spec == {"action": "stop", "mode": "shadow"}
    assert violations == []


def test_effective_detector_shadow_cannot_loosen_a_real_stop_into_a_preview():
    """The code already stops on this detector for real (local_action ==
    "stop"); a plane "shadow" would turn that into a preview that stops
    nothing, which is a loosening -- refused, on the mode field."""
    spec, violations = effective_detector("budget", "stop", {"action": "stop", "mode": "shadow"})

    assert spec == {"action": "stop", "mode": "enforce"}
    assert violations == [Violation("detectors.budget.mode", "enforce", "shadow")]


def test_effective_detector_action_and_mode_can_both_be_refused_at_once():
    """A plane {"action": "notify", "mode": "shadow"} against a "stop"
    baseline loosens on both fields at once -- both are named."""
    spec, violations = effective_detector("steps", "stop", {"action": "notify", "mode": "shadow"})

    assert spec == {"action": "stop", "mode": "enforce"}
    assert violations == [
        Violation("detectors.steps.action", "stop", "notify"),
        Violation("detectors.steps.mode", "enforce", "shadow"),
    ]


def test_effective_detector_mode_defaults_to_enforce_when_unstated():
    spec, violations = effective_detector("budget", "stop", {"action": "stop"})

    assert spec == {"action": "stop", "mode": "enforce"}
    assert violations == []


def test_effective_detector_mode_is_free_when_unstated_and_baseline_is_notify():
    spec, violations = effective_detector("budget", "notify", {"action": "notify"})

    assert spec == {"action": "notify", "mode": "enforce"}
    assert violations == []


# --- a malformed plane value costs its own limit and nothing else -----------------------------

MALFORMED = ("lots", True, [1], {"n": 1}, float("nan"))


def _other(field: str) -> str:
    return "max_events" if field == "max_steps" else "max_steps"


@pytest.mark.parametrize("bad", MALFORMED, ids=repr)
@pytest.mark.parametrize("field", LIMIT_FIELDS)
def test_a_malformed_plane_value_is_ignored_for_that_field_alone(field: str, bad) -> None:
    other = _other(field)
    plane = {"org": {field: bad, other: 5}}

    without_cap, violations = effective_limits({other: 20}, plane)
    assert without_cap[field] is None and without_cap[other] == 5 and violations == []

    with_cap, violations = effective_limits({field: 100, other: 20}, plane)
    assert with_cap[field] == 100 and with_cap[other] == 5  # the code's own cap stands; the valid tightening applies
    assert violations == []  # a value that was never a limit is not a loosening either


@pytest.mark.parametrize("field", LIMIT_FIELDS)
def test_a_malformed_value_at_one_level_leaves_a_valid_one_at_another(field: str) -> None:
    folded = fold_levels({"org": {field: "lots"}, "service": {field: 7}})

    assert folded[field] == 7


@pytest.mark.parametrize("field", LIMIT_FIELDS)
def test_none_is_still_a_limit_not_a_malformed_one(field: str) -> None:
    assert malformed_limits({"org": {field: None}}) == ()
    assert fold_levels({"org": {field: None}}) == {field: None}


def test_malformed_limits_names_each_one_on_an_enforceable_level_and_no_others() -> None:
    found = malformed_limits({
        "org": {"budget_usd": "x", "max_steps": 3},
        "service": {"budget_tokens": True},
        "agent": {"budget_usd": "ignored: this worker does not read the agent level"},
    })

    assert found == ("limits.org.budget_usd", "limits.service.budget_tokens")
    assert malformed_limits(None) == () and malformed_limits({"org": "nope"}) == ()
