"""Tests for the two "somebody stated a limit" checks added in Wave 14:
the wall-clock session timeout and the per-call dollar cap.

Neither measures real time or spends real money: ``started_at`` is set by hand
and durations and costs are values carried on the events.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.detectors import (
    DEFAULT_DETECTORS,
    SpikeDetector,
    TimeoutDetector,
)
from runbound.engine import Engine
from runbound.events import Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

HOUR = 3600.0


def state_at(started_at: float = 0.0, key: str | None = None, tags: dict | None = None):
    """A session whose run clock (and lifetime clock) started at ``started_at``.

    ``run_started_at`` is set alongside ``started_at`` (T133 split them, and
    ``max_session_seconds`` now reads the former) so every test below that was
    written against the single old clock keeps exercising the same scenario:
    a session that has been running, uninterrupted, since ``started_at``.
    """
    state = SessionState("s1", key=key, tags=tags)
    state.started_at = started_at
    state.run_started_at = started_at
    return state


def event(kind: str = "llm_call", ts: float = 0.0, step: int = 1, **fields) -> Event:
    return Event(kind=kind, ts=ts, step=step, **fields)


def llm_event(step: int = 1, cost: float = 0.0, duration: float = 1.0) -> Event:
    return Event(
        kind="llm_call",
        ts=float(step),
        step=step,
        cost_usd=cost,
        duration_s=duration,
        tokens_out=10,
        model="gpt-4o",
    )


def feed(detector, state: SessionState, evt: Event, config: GuardrailConfig):
    """Record an event the way the engine does, then ask the detector."""
    state.record(evt)
    return detector.check(state, evt, config)


# --- the timeout detector ---------------------------------------------------


def test_a_session_past_its_wall_clock_limit_is_critical():
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_seconds=HOUR)

    anomaly = detector.check(state, event(ts=3601.0), config)

    assert anomaly is not None
    assert anomaly.detector == "timeout"
    assert anomaly.severity == "critical"
    assert anomaly.details["elapsed_s"] == pytest.approx(3601.0)
    assert anomaly.details["limit"] == pytest.approx(HOUR)
    assert anomaly.details["session_id"] == "s1"
    assert "3601s" in anomaly.message and "3600s" in anomaly.message


def test_a_session_exactly_at_the_limit_is_not_timed_out():
    """Boundary: the limit is the number a session may reach and keep running."""
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_seconds=HOUR)

    assert detector.check(state, event(ts=HOUR), config) is None


def test_no_limit_means_no_timeout():
    detector, state = TimeoutDetector(), state_at(0.0)

    assert detector.check(state, event(ts=1e9), GuardrailConfig()) is None


def test_the_timeout_names_the_keyed_session_it_stopped():
    detector = TimeoutDetector()
    state = state_at(0.0, key="user:8842", tags={"plan": "free"})
    config = GuardrailConfig(max_session_seconds=HOUR)

    anomaly = detector.check(state, event(ts=4000.0), config)

    assert "user:8842" in anomaly.message
    assert anomaly.details["key"] == "user:8842"
    assert anomaly.details["tags"] == {"plan": "free"}


def test_the_timeout_fires_once_per_session():
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_seconds=HOUR)

    assert detector.check(state, event(ts=3601.0), config) is not None
    assert detector.check(state, event(ts=3602.0, step=2), config) is None


def test_another_session_is_still_timed_out():
    detector = TimeoutDetector()
    config = GuardrailConfig(max_session_seconds=HOUR)

    assert detector.check(state_at(0.0), event(ts=3601.0), config) is not None
    other = SessionState("s2", key="user:2")
    other.started_at = 0.0
    other.run_started_at = 0.0
    assert detector.check(other, event(ts=3601.0), config) is not None


@pytest.mark.parametrize("kind", ["llm_call", "tool_call", "llm_error", "unknown"])
def test_any_event_kind_can_time_a_session_out(kind):
    """A stuck run is stuck whatever kind of work it is doing."""
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_seconds=HOUR)

    assert detector.check(state, event(kind=kind, ts=3601.0), config) is not None


def test_a_malformed_session_is_silent_rather_than_raising():
    """Fail-open: the detector never takes down the host it is watching.

    ``run_started_at`` (not ``started_at``) is what the run-scoped clock now
    reads (T133), so that is the attribute this test corrupts.
    """

    class Odd:
        session_id = "x"
        run_started_at = "not a number"

    config = GuardrailConfig(max_session_seconds=HOUR)

    assert TimeoutDetector().check(Odd(), event(ts=3601.0), config) is None


def test_timeout_is_one_of_the_default_detectors():
    assert TimeoutDetector in DEFAULT_DETECTORS


# --- the lifetime clock (T133: max_session_lifetime_seconds) ---------------


def test_no_lifetime_limit_means_no_lifetime_timeout():
    detector, state = TimeoutDetector(), state_at(0.0)

    assert detector.check(state, event(ts=1e9), GuardrailConfig()) is None


def test_a_session_past_its_lifetime_limit_trips_with_lifetime_scope():
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_lifetime_seconds=HOUR)

    anomaly = detector.check(state, event(ts=3601.0), config)

    assert anomaly is not None
    assert anomaly.detector == "timeout"
    assert anomaly.details["scope"] == "lifetime"
    assert anomaly.details["elapsed_s"] == pytest.approx(3601.0)


def test_a_session_exactly_at_the_lifetime_limit_is_silent():
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_lifetime_seconds=HOUR)

    assert detector.check(state, event(ts=HOUR), config) is None


def test_the_run_scope_anomaly_says_scope_run():
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_seconds=HOUR)

    anomaly = detector.check(state, event(ts=3601.0), config)

    assert anomaly.details["scope"] == "run"


def test_run_and_lifetime_walls_trip_independently():
    """A run reset well inside its lifetime can still trip the lifetime wall,
    and a session under its lifetime cap can still trip on one long run —
    each wall fires once, on its own clock, regardless of the other."""
    detector = TimeoutDetector()
    state = state_at(0.0)
    # The run clock resets (as api.session() would do on re-entry), but the
    # lifetime clock — since creation — has run past its cap.
    state.run_started_at = 10_000.0
    config = GuardrailConfig(max_session_seconds=HOUR, max_session_lifetime_seconds=HOUR)

    anomaly = detector.check(state, event(ts=10_050.0), config)

    assert anomaly is not None
    assert anomaly.details["scope"] == "lifetime"
    assert anomaly.details["elapsed_s"] == pytest.approx(10_050.0)

    # The run wall is still armed (it never fired) and trips on its own turn.
    second = detector.check(state, event(ts=10_050.0 + HOUR + 1.0), config)
    assert second is not None
    assert second.details["scope"] == "run"


def test_lifetime_timeout_fires_once_per_session():
    detector, state = TimeoutDetector(), state_at(0.0)
    config = GuardrailConfig(max_session_lifetime_seconds=HOUR)

    assert detector.check(state, event(ts=3601.0), config) is not None
    assert detector.check(state, event(ts=3602.0, step=2), config) is None


# --- the timeout through the engine -----------------------------------------


def test_a_timeout_raises_and_latches_under_on_anomaly_raise():
    config = GuardrailConfig(max_session_seconds=HOUR, on_anomaly="raise")
    engine, state = Engine(config), state_at(0.0)

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, event(ts=3601.0))

    assert excinfo.value.anomaly.detector == "timeout"
    assert state.tripped_by is not None
    assert state.tripped_by.detector == "timeout"


def test_a_timeout_under_on_trip_once_stops_the_call_but_not_the_session():
    config = GuardrailConfig(max_session_seconds=HOUR, on_anomaly="raise", on_trip="once")
    engine, state = Engine(config), state_at(0.0)

    with pytest.raises(GuardrailTripped):
        engine.process(state, event(ts=3601.0))

    assert state.tripped_by is None


# --- the per-call dollar cap ------------------------------------------------


def test_a_call_over_the_dollar_cap_is_critical_on_the_very_first_call():
    detector, state = SpikeDetector(), SessionState("s1")
    config = GuardrailConfig(max_cost_per_call_usd=0.5)

    anomaly = feed(detector, state, llm_event(1, cost=1.2), config)

    assert anomaly is not None
    assert anomaly.severity == "critical"
    assert anomaly.details["metric"] == "cost_usd"
    assert anomaly.details["value"] == pytest.approx(1.2)
    assert anomaly.details["cap"] == pytest.approx(0.5)
    assert anomaly.details["confirmed"] is True
    assert "call cost $1.2000" in anomaly.message
    assert "cap $0.5000" in anomaly.message


def test_a_call_exactly_at_the_dollar_cap_does_not_trip_it():
    detector, state = SpikeDetector(), SessionState("s1")
    config = GuardrailConfig(max_cost_per_call_usd=0.5)

    assert feed(detector, state, llm_event(1, cost=0.5), config) is None


def test_no_dollar_cap_means_no_breach():
    detector, state = SpikeDetector(), SessionState("s1")

    assert feed(detector, state, llm_event(1, cost=1000.0), GuardrailConfig()) is None


def test_a_dollar_cap_breach_is_reported_once_per_session():
    detector, config = SpikeDetector(), GuardrailConfig(max_cost_per_call_usd=0.5)
    state = SessionState("s1")

    assert feed(detector, state, llm_event(1, cost=1.2), config) is not None
    assert feed(detector, state, llm_event(2, cost=9.0), config) is None


def test_a_dollar_cap_breach_in_another_session_is_still_reported():
    detector, config = SpikeDetector(), GuardrailConfig(max_cost_per_call_usd=0.5)

    assert feed(detector, SessionState("s1"), llm_event(1, cost=1.2), config) is not None
    assert feed(detector, SessionState("s2"), llm_event(1, cost=1.2), config) is not None


def test_a_dollar_cap_breach_raises_on_the_first_call_under_on_anomaly_raise():
    config = GuardrailConfig(on_anomaly="raise", max_cost_per_call_usd=0.5)
    engine = Engine(config)

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(SessionState("s1"), llm_event(1, cost=1.2))

    assert excinfo.value.anomaly.detector == "spike"
    assert excinfo.value.anomaly.details["metric"] == "cost_usd"


def test_a_duration_cap_still_wins_when_a_call_breaks_both():
    detector, state = SpikeDetector(), SessionState("s1")
    config = GuardrailConfig(max_call_seconds=30.0, max_cost_per_call_usd=0.5)

    anomaly = feed(detector, state, llm_event(1, cost=1.2, duration=60.0), config)

    assert anomaly.details["metric"] == "duration"


# --- configuration ----------------------------------------------------------


def test_the_new_limits_default_to_none():
    cfg = GuardrailConfig()

    assert cfg.max_session_seconds is None
    assert cfg.max_cost_per_call_usd is None
    assert cfg.max_active_sessions is None
    assert cfg.max_session_depth is None
    assert cfg.max_child_sessions is None


@pytest.mark.parametrize(
    "name",
    [
        "max_session_seconds",
        "max_cost_per_call_usd",
        "max_active_sessions",
        "max_session_depth",
        "max_child_sessions",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_limit_is_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        GuardrailConfig(**{name: value}).validate()


@pytest.mark.parametrize(
    "name",
    [
        "max_session_seconds",
        "max_cost_per_call_usd",
        "max_active_sessions",
        "max_session_depth",
        "max_child_sessions",
    ],
)
def test_a_positive_limit_validates(name):
    GuardrailConfig(**{name: 1}).validate()
