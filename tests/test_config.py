"""Tests for GuardrailConfig defaults and validation, plus the core models
(Event, Anomaly, GuardrailTripped) that have no test module of their own.
"""

import dataclasses

import pytest

import runbound
from runbound.config import GuardrailConfig
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped


def _noop(anomaly) -> None:
    return None


def test_defaults_match_spec():
    cfg = GuardrailConfig()
    assert cfg.budget_usd is None
    assert cfg.max_total_tokens is None
    assert cfg.max_steps is None
    assert cfg.max_events is None
    assert cfg.max_session_lifetime_seconds is None
    assert cfg.tokens_per_minute_limit is None
    assert cfg.loop_threshold == 3
    assert cfg.loop_window == 20
    assert cfg.on_anomaly == "warn"
    assert cfg.callback is None
    assert cfg.custom_prices == {}


def test_custom_prices_default_is_not_shared_between_instances():
    a = GuardrailConfig()
    b = GuardrailConfig()
    a.custom_prices["gpt-4o"] = (1.0, 2.0)
    assert b.custom_prices == {}


def test_default_config_validates():
    GuardrailConfig().validate()


@pytest.mark.parametrize("mode", ["warn", "raise"])
def test_valid_modes_without_callback(mode):
    GuardrailConfig(on_anomaly=mode).validate()


def test_callback_mode_with_callback_is_valid():
    GuardrailConfig(on_anomaly="callback", callback=_noop).validate()


def test_fully_populated_config_validates():
    cfg = GuardrailConfig(
        budget_usd=5.0,
        max_total_tokens=100_000,
        max_steps=50,
        tokens_per_minute_limit=10_000,
        loop_threshold=2,
        loop_window=2,
        on_anomaly="callback",
        callback=_noop,
        custom_prices={"local-model": (0.0, 0.0)},
    )
    cfg.validate()


@pytest.mark.parametrize("mode", ["", "RAISE", "log", "warning", None, 1])
def test_bad_on_anomaly_raises(mode):
    with pytest.raises(ValueError):
        GuardrailConfig(on_anomaly=mode).validate()


def test_callback_mode_without_callback_raises():
    with pytest.raises(ValueError):
        GuardrailConfig(on_anomaly="callback").validate()


@pytest.mark.parametrize("mode", ["warn", "raise"])
def test_callback_set_but_mode_not_callback_raises(mode):
    with pytest.raises(ValueError):
        GuardrailConfig(on_anomaly=mode, callback=_noop).validate()


@pytest.mark.parametrize(
    "field",
    [
        "budget_usd",
        "max_total_tokens",
        "max_steps",
        "max_events",
        "max_session_lifetime_seconds",
        "tokens_per_minute_limit",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_limits_raise(field, value):
    with pytest.raises(ValueError):
        GuardrailConfig(**{field: value}).validate()


@pytest.mark.parametrize(
    "field",
    [
        "budget_usd",
        "max_total_tokens",
        "max_steps",
        "max_events",
        "max_session_lifetime_seconds",
        "tokens_per_minute_limit",
    ],
)
def test_smallest_positive_limits_are_valid(field):
    GuardrailConfig(**{field: 1}).validate()


def test_tiny_positive_budget_is_valid():
    GuardrailConfig(budget_usd=0.0001).validate()


@pytest.mark.parametrize("threshold", [1, 0, -3])
def test_loop_threshold_below_two_raises(threshold):
    with pytest.raises(ValueError):
        GuardrailConfig(loop_threshold=threshold, loop_window=20).validate()


def test_loop_threshold_of_two_is_valid():
    GuardrailConfig(loop_threshold=2, loop_window=20).validate()


def test_loop_window_smaller_than_threshold_raises():
    with pytest.raises(ValueError):
        GuardrailConfig(loop_threshold=3, loop_window=2).validate()


def test_loop_window_equal_to_threshold_is_valid():
    GuardrailConfig(loop_threshold=3, loop_window=3).validate()


def test_validate_returns_none():
    assert GuardrailConfig().validate() is None


# --- core models -----------------------------------------------------------


def test_event_defaults_and_immutability():
    event = Event(kind="llm_call", ts=1.5, step=1)

    assert (event.tokens_in, event.tokens_out, event.cost_usd) == (0, 0, 0.0)
    assert event.model is None
    assert event.tool_name is None
    assert event.args_hash is None
    assert event.error is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.step = 2


def test_anomaly_is_frozen():
    anomaly = Anomaly(detector="loop", severity="critical", message="m", details={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        anomaly.severity = "warn"


def test_runbound_tripped_carries_anomaly_and_message():
    anomaly = Anomaly(
        detector="budget",
        severity="critical",
        message="budget exceeded: $5.10 > $5.00",
        details={"total_cost_usd": 5.10, "budget_usd": 5.0},
    )
    exc = GuardrailTripped(anomaly)

    assert isinstance(exc, Exception)
    assert exc.anomaly is anomaly
    assert str(exc) == anomaly.message


def test_runbound_tripped_is_catchable_as_raised():
    anomaly = Anomaly(detector="steps", severity="critical", message="too many", details={})
    with pytest.raises(GuardrailTripped) as caught:
        raise GuardrailTripped(anomaly)

    assert caught.value.anomaly.detector == "steps"


def test_package_exports_version_and_core_names():
    assert runbound.__version__ == "0.3.0"
    assert runbound.Event is Event
    assert runbound.Anomaly is Anomaly
    assert runbound.GuardrailConfig is GuardrailConfig
    assert runbound.GuardrailTripped is GuardrailTripped
    from runbound.state import SessionState

    assert runbound.SessionState is SessionState
