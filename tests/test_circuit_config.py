"""The circuit-rate knobs, real ``init()`` fields.

``circuit_mode``, ``circuit_failure_rate``, ``circuit_min_calls``,
``circuit_slow_call_seconds``, ``circuit_slow_rate``,
``circuit_half_open_calls``, ``circuit_fleet`` and ``circuit_posture`` are
real ``GuardrailConfig`` fields again, validated loudly at ``init()`` time
(see ``tests/test_plane_only_controls.py`` for the acceptance-level triple:
code only, plane only, code tightened by plane). This file is the pure-
function acceptance test for :func:`runbound.controls_merge.
effective_circuit_rate` and :func:`runbound.controls_merge.
effective_circuit_posture` -- the plane's own raw payload is still
untrusted wire data (fail open to whatever this worker's own code already
configured, never a crash), it is just no longer the *only* input any
more.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.controls_merge import (
    _parse_circuit_rate,
    effective_circuit_posture,
    effective_circuit_rate,
)


def test_the_knobs_are_real_local_fields_again():
    cfg = GuardrailConfig()
    assert cfg.circuit_mode == "count"
    assert cfg.circuit_failure_rate == 0.5
    assert cfg.circuit_min_calls == 5
    assert cfg.circuit_slow_call_seconds is None
    assert cfg.circuit_slow_rate == 0.5
    assert cfg.circuit_half_open_calls == 1
    assert cfg.circuit_fleet is True
    assert cfg.circuit_posture is False


# --- _parse_circuit_rate: a plane's raw payload, defended against garbage --


def test_absent_or_non_mapping_is_none():
    for value in (None, "rate", 5, [], True):
        assert _parse_circuit_rate(value) is None


def test_an_empty_dict_from_the_plane_states_rate_mode_with_every_default():
    result = _parse_circuit_rate({})
    assert result == {
        "mode": "rate",
        "min_calls": 5,
        "failure_rate": 0.5,
        "slow_call_seconds": None,
        "slow_rate": 0.5,
        "half_open_calls": 1,
    }


def test_a_fully_stated_bundle_round_trips():
    result = _parse_circuit_rate(
        {
            "min_calls": 10,
            "failure_rate": 0.3,
            "slow_call_seconds": 5.0,
            "slow_rate": 0.2,
            "half_open_calls": 3,
        }
    )
    assert result == {
        "mode": "rate",
        "min_calls": 10,
        "failure_rate": 0.3,
        "slow_call_seconds": 5.0,
        "slow_rate": 0.2,
        "half_open_calls": 3,
    }


@pytest.mark.parametrize("value", [0, -0.1, 1.1, 2])
def test_failure_rate_out_of_range_falls_back_to_none(value):
    assert _parse_circuit_rate({"failure_rate": value}) is None


@pytest.mark.parametrize("value", [0, -0.1, 1.1, 2])
def test_slow_rate_out_of_range_falls_back_to_none(value):
    assert _parse_circuit_rate({"slow_rate": value}) is None


def test_failure_rate_of_exactly_one_is_valid():
    assert _parse_circuit_rate({"failure_rate": 1.0}) is not None


@pytest.mark.parametrize("value", [0, -1, -5])
def test_non_positive_min_calls_falls_back_to_none(value):
    assert _parse_circuit_rate({"min_calls": value}) is None


def test_min_calls_of_one_is_valid():
    assert _parse_circuit_rate({"min_calls": 1}) is not None


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_half_open_calls_falls_back_to_none(value):
    assert _parse_circuit_rate({"half_open_calls": value}) is None


@pytest.mark.parametrize("value", [0, -1.0])
def test_non_positive_slow_call_seconds_falls_back_to_none(value):
    assert _parse_circuit_rate({"slow_call_seconds": value}) is None


def test_slow_call_seconds_of_none_is_valid_off():
    result = _parse_circuit_rate({"slow_call_seconds": None})
    assert result is not None and result["slow_call_seconds"] is None


def test_min_calls_that_is_not_a_number_falls_back_to_none():
    with_bad_type = _parse_circuit_rate({"min_calls": "many"})
    assert with_bad_type is None


def test_min_calls_bool_falls_back_to_none_even_though_bool_is_an_int():
    assert _parse_circuit_rate({"min_calls": True}) is None


# --- effective_circuit_rate: code tightened by an optional plane -----------


def test_no_code_and_no_plane_is_count_mode():
    effective, violations = effective_circuit_rate(None, None)
    assert effective is None
    assert violations == []


def test_no_code_and_a_valid_plane_turns_rate_mode_on_from_nothing():
    effective, violations = effective_circuit_rate(None, {"min_calls": 3})
    assert effective["mode"] == "rate"
    assert effective["min_calls"] == 3
    assert violations == []


def test_code_rate_mode_stands_when_the_plane_says_nothing():
    code = {
        "mode": "rate", "min_calls": 3, "failure_rate": 0.4,
        "slow_call_seconds": 1.0, "slow_rate": 0.4, "half_open_calls": 2,
    }
    effective, violations = effective_circuit_rate(code, None)
    assert effective == code
    assert violations == []


def test_a_looser_plane_field_is_refused_and_code_stands():
    code = {
        "mode": "rate", "min_calls": 3, "failure_rate": 0.3,
        "slow_call_seconds": 1.0, "slow_rate": 0.3, "half_open_calls": 2,
    }
    effective, violations = effective_circuit_rate(code, {"min_calls": 10})

    assert effective["min_calls"] == 3  # code's own stricter value wins
    assert len(violations) == 1
    assert violations[0].path == "circuit_rate.min_calls"


def test_a_stricter_plane_field_tightens_code():
    code = {
        "mode": "rate", "min_calls": 10, "failure_rate": 0.5,
        "slow_call_seconds": None, "slow_rate": 0.5, "half_open_calls": 3,
    }
    effective, violations = effective_circuit_rate(code, {"min_calls": 3})

    assert effective["min_calls"] == 3
    assert violations == []


def test_a_field_the_plane_never_mentions_leaves_codes_value_alone():
    code = {
        "mode": "rate", "min_calls": 3, "failure_rate": 0.3,
        "slow_call_seconds": 1.0, "slow_rate": 0.3, "half_open_calls": 2,
    }
    effective, violations = effective_circuit_rate(code, {"min_calls": 1})

    assert effective["failure_rate"] == 0.3  # untouched, plane never said
    assert violations == []


# --- effective_circuit_posture: an OR gate, independent of rate mode -------


@pytest.mark.parametrize("code", [True, False])
@pytest.mark.parametrize("plane_value", [True, False, None, 1, "yes", {}])
def test_true_can_only_be_tightened_in(code, plane_value):
    effective, _violation = effective_circuit_posture(code, plane_value, stated=True)
    assert effective == (code or plane_value is True)


def test_an_unstated_plane_value_is_never_a_violation():
    effective, violation = effective_circuit_posture(True, None, stated=False)
    assert effective is True
    assert violation is None


def test_an_explicit_false_from_the_plane_is_refused_and_visible():
    effective, violation = effective_circuit_posture(True, False, stated=True)
    assert effective is True
    assert violation is not None
    assert violation.path == "circuit_posture"


def test_circuit_posture_does_not_require_rate_mode():
    """Independent of circuit_mode -- a count-mode breaker narrows
    the posture exactly like a rate-mode one. Enabling it never implies
    rate mode."""
    effective, _violations = effective_circuit_posture(False, True, stated=True)
    assert effective is True
    assert effective_circuit_rate(None, None)[0] is None
