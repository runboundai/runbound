"""Tests for the threshold detectors (the spike detector has its own file).

Time is hand-set (monotonic-style floats) everywhere: detectors must never
depend on the wall clock or on the tests sleeping.
"""

from runbound.config import GuardrailConfig
from runbound.detectors import (
    DEFAULT_DETECTORS,
    BudgetDetector,
    ErrorStormDetector,
    LoopDetector,
    SpikeDetector,
    StepDetector,
    TimeoutDetector,
    VelocityDetector,
)
from runbound.events import Event
from runbound.state import SessionState


def tool_event(
    step: int,
    ts: float = 0.0,
    tool_name: str = "search",
    args_hash: str | None = "h1",
) -> Event:
    return Event(
        kind="tool_call",
        ts=ts,
        step=step,
        tool_name=tool_name,
        args_hash=args_hash,
    )


def llm_event(
    step: int,
    ts: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
    args_hash: str | None = None,
) -> Event:
    return Event(
        kind="llm_call",
        ts=ts,
        step=step,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        model="gpt-4o",
        args_hash=args_hash,
    )


def replay(events: list[Event], session_id: str = "s1", loop_window: int = 20) -> SessionState:
    """Build a SessionState that has already recorded ``events``."""
    state = SessionState(session_id, loop_window=loop_window)
    for event in events:
        state.record(event)
    return state


# --------------------------------------------------------------------------
# LoopDetector
# --------------------------------------------------------------------------


def test_loop_fires_at_exactly_threshold():
    config = GuardrailConfig(loop_threshold=3, loop_window=20)
    events = [tool_event(step=i, ts=float(i)) for i in range(1, 4)]
    state = replay(events)

    anomaly = LoopDetector().check(state, events[-1], config)

    assert anomaly is not None
    assert anomaly.detector == "loop"
    assert anomaly.severity == "critical"
    assert "search" in anomaly.message
    assert "3" in anomaly.message
    assert anomaly.details["count"] == 3
    assert anomaly.details["threshold"] == 3
    assert anomaly.details["session_id"] == "s1"
    assert anomaly.details["tool_name"] == "search"


def test_loop_silent_below_threshold():
    config = GuardrailConfig(loop_threshold=3)
    events = [tool_event(step=1, ts=1.0), tool_event(step=2, ts=2.0)]
    state = replay(events)

    assert LoopDetector().check(state, events[-1], config) is None


def test_loop_ignores_distinct_hashes():
    config = GuardrailConfig(loop_threshold=3)
    events = [
        tool_event(step=1, ts=1.0, args_hash="a"),
        tool_event(step=2, ts=2.0, args_hash="b"),
        tool_event(step=3, ts=3.0, args_hash="c"),
    ]
    state = replay(events)

    assert LoopDetector().check(state, events[-1], config) is None


def test_loop_ignores_llm_call_events():
    """llm_call events never trip the loop detector, even on a looping state."""
    config = GuardrailConfig(loop_threshold=3)
    events = [tool_event(step=i, ts=float(i)) for i in range(1, 4)]
    state = replay(events)
    probe = llm_event(step=4, ts=4.0, args_hash="h1")

    assert state.recent_hashes.count("h1") == 3  # the state IS in a loop
    assert LoopDetector().check(state, probe, config) is None


def test_loop_llm_calls_do_not_contribute_hashes():
    config = GuardrailConfig(loop_threshold=3)
    events = [llm_event(step=i, ts=float(i), args_hash="h1") for i in range(1, 4)]
    events.append(tool_event(step=4, ts=4.0))
    state = replay(events)

    assert LoopDetector().check(state, events[-1], config) is None


def test_loop_ignores_event_without_hash():
    config = GuardrailConfig(loop_threshold=3)
    events = [tool_event(step=i, ts=float(i)) for i in range(1, 4)]
    state = replay(events)
    probe = tool_event(step=4, ts=4.0, args_hash=None)

    assert LoopDetector().check(state, probe, config) is None


def test_loop_only_counts_within_window():
    """Occurrences that slid out of the window no longer count."""
    config = GuardrailConfig(loop_threshold=3, loop_window=3)
    events = [
        tool_event(step=1, ts=1.0, args_hash="h1"),
        tool_event(step=2, ts=2.0, args_hash="h1"),
        tool_event(step=3, ts=3.0, args_hash="other"),
        tool_event(step=4, ts=4.0, args_hash="h1"),
    ]
    state = replay(events, loop_window=3)

    assert list(state.recent_hashes) == ["h1", "other", "h1"]
    assert LoopDetector().check(state, events[-1], config) is None


def test_loop_fires_once_per_session():
    config = GuardrailConfig(loop_threshold=3)
    detector = LoopDetector()
    events = [tool_event(step=i, ts=float(i)) for i in range(1, 4)]
    state = replay(events)

    assert detector.check(state, events[-1], config) is not None

    extra = tool_event(step=4, ts=4.0)
    state.record(extra)
    assert detector.check(state, extra, config) is None


def test_loop_sessions_fire_independently():
    config = GuardrailConfig(loop_threshold=3)
    detector = LoopDetector()
    events_a = [tool_event(step=i, ts=float(i)) for i in range(1, 4)]
    events_b = [tool_event(step=i, ts=float(i)) for i in range(1, 4)]
    state_a = replay(events_a, session_id="a")
    state_b = replay(events_b, session_id="b")

    assert detector.check(state_a, events_a[-1], config) is not None
    assert detector.check(state_b, events_b[-1], config) is not None


# --------------------------------------------------------------------------
# BudgetDetector
# --------------------------------------------------------------------------


def test_budget_disabled_when_both_knobs_none():
    config = GuardrailConfig()
    event = llm_event(step=1, ts=1.0, tokens_in=10_000, cost=99.0)
    state = replay([event])

    assert BudgetDetector().check(state, event, config) is None


def test_budget_silent_when_cost_exactly_at_limit():
    config = GuardrailConfig(budget_usd=1.0)
    event = llm_event(step=1, ts=1.0, cost=1.0)
    state = replay([event])

    assert state.total_cost_usd == 1.0
    assert BudgetDetector().check(state, event, config) is None


def test_budget_fires_when_cost_strictly_exceeds_limit():
    config = GuardrailConfig(budget_usd=1.0)
    events = [llm_event(step=1, ts=1.0, cost=0.75), llm_event(step=2, ts=2.0, cost=0.5)]
    state = replay(events)

    anomaly = BudgetDetector().check(state, events[-1], config)

    assert anomaly is not None
    assert anomaly.detector == "budget"
    assert anomaly.severity == "critical"
    assert "1.25" in anomaly.message
    assert anomaly.details["total_cost_usd"] == 1.25
    assert anomaly.details["budget_usd"] == 1.0
    assert anomaly.details["session_id"] == "s1"


def test_budget_silent_when_tokens_exactly_at_limit():
    config = GuardrailConfig(max_total_tokens=100)
    event = llm_event(step=1, ts=1.0, tokens_in=60, tokens_out=40)
    state = replay([event])

    assert state.total_tokens == 100
    assert BudgetDetector().check(state, event, config) is None


def test_budget_fires_when_tokens_strictly_exceed_limit():
    config = GuardrailConfig(max_total_tokens=100)
    event = llm_event(step=1, ts=1.0, tokens_in=60, tokens_out=41)
    state = replay([event])

    anomaly = BudgetDetector().check(state, event, config)

    assert anomaly is not None
    assert anomaly.detector == "budget"
    assert "101" in anomaly.message
    assert anomaly.details["total_tokens"] == 101
    assert anomaly.details["max_total_tokens"] == 100


def test_budget_cost_limit_ignored_when_only_tokens_configured():
    config = GuardrailConfig(max_total_tokens=1000)
    event = llm_event(step=1, ts=1.0, tokens_in=10, cost=500.0)
    state = replay([event])

    assert BudgetDetector().check(state, event, config) is None


def test_budget_fires_once_per_session():
    config = GuardrailConfig(budget_usd=1.0)
    detector = BudgetDetector()
    first = llm_event(step=1, ts=1.0, cost=2.0)
    state = replay([first])

    assert detector.check(state, first, config) is not None

    second = llm_event(step=2, ts=2.0, cost=2.0)
    state.record(second)
    assert detector.check(state, second, config) is None


def test_budget_sessions_fire_independently():
    config = GuardrailConfig(budget_usd=1.0)
    detector = BudgetDetector()
    event = llm_event(step=1, ts=1.0, cost=2.0)
    state_a = replay([event], session_id="a")
    state_b = replay([event], session_id="b")

    assert detector.check(state_a, event, config) is not None
    assert detector.check(state_b, event, config) is not None


# --------------------------------------------------------------------------
# VelocityDetector
# --------------------------------------------------------------------------


def test_velocity_disabled_when_knob_none():
    config = GuardrailConfig()
    event = llm_event(step=1, ts=1.0, tokens_in=1_000_000)
    state = replay([event])

    assert VelocityDetector().check(state, event, config) is None


def test_velocity_silent_when_exactly_at_limit():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    events = [
        llm_event(step=1, ts=10.0, tokens_in=50),
        llm_event(step=2, ts=20.0, tokens_in=50),
    ]
    state = replay(events)

    assert VelocityDetector().check(state, events[-1], config) is None


def test_velocity_fires_when_strictly_above_limit():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    events = [
        llm_event(step=1, ts=10.0, tokens_in=50),
        llm_event(step=2, ts=20.0, tokens_in=51),
    ]
    state = replay(events)

    anomaly = VelocityDetector().check(state, events[-1], config)

    assert anomaly is not None
    assert anomaly.detector == "velocity"
    assert anomaly.severity == "warn"
    assert "101" in anomaly.message
    assert anomaly.details["tokens_last_60s"] == 101
    assert anomaly.details["tokens_per_minute_limit"] == 100
    assert anomaly.details["session_id"] == "s1"


def test_velocity_fires_on_single_event_that_alone_exceeds_limit():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    event = llm_event(step=1, ts=5.0, tokens_in=101)
    state = replay([event])

    assert VelocityDetector().check(state, event, config) is not None


def test_velocity_does_not_fire_on_single_event_within_limit():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    event = llm_event(step=1, ts=5.0, tokens_in=100)
    state = replay([event])

    assert VelocityDetector().check(state, event, config) is None


def test_velocity_expired_tokens_do_not_count_and_are_pruned():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    events = [
        llm_event(step=1, ts=0.0, tokens_in=90),  # 200s before the last event
        llm_event(step=2, ts=100.0, tokens_in=30),  # 100s before, also expired
        llm_event(step=3, ts=190.0, tokens_in=40),
        llm_event(step=4, ts=200.0, tokens_in=40),
    ]
    state = replay(events)
    assert len(state.token_timestamps) == 4

    assert VelocityDetector().check(state, events[-1], config) is None
    assert list(state.token_timestamps) == [(190.0, 40), (200.0, 40)]


def test_velocity_counts_token_recorded_exactly_60s_ago():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    events = [
        llm_event(step=1, ts=40.0, tokens_in=60),
        llm_event(step=2, ts=100.0, tokens_in=41),
    ]
    state = replay(events)

    anomaly = VelocityDetector().check(state, events[-1], config)

    assert anomaly is not None
    assert anomaly.details["tokens_last_60s"] == 101
    assert len(state.token_timestamps) == 2


def test_velocity_fires_once_per_session():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    detector = VelocityDetector()
    first = llm_event(step=1, ts=1.0, tokens_in=200)
    state = replay([first])

    assert detector.check(state, first, config) is not None

    second = llm_event(step=2, ts=2.0, tokens_in=200)
    state.record(second)
    assert detector.check(state, second, config) is None


def test_velocity_sessions_fire_independently():
    config = GuardrailConfig(tokens_per_minute_limit=100)
    detector = VelocityDetector()
    event = llm_event(step=1, ts=1.0, tokens_in=200)
    state_a = replay([event], session_id="a")
    state_b = replay([event], session_id="b")

    assert detector.check(state_a, event, config) is not None
    assert detector.check(state_b, event, config) is not None


# --------------------------------------------------------------------------
# StepDetector
# --------------------------------------------------------------------------


def test_steps_disabled_when_knob_none():
    config = GuardrailConfig()
    event = tool_event(step=9999, ts=1.0)
    state = replay([event])

    assert StepDetector().check(state, event, config) is None


def test_steps_silent_when_exactly_at_limit():
    config = GuardrailConfig(max_steps=5)
    event = tool_event(step=5, ts=1.0)
    state = replay([event])

    assert state.step_count == 5
    assert StepDetector().check(state, event, config) is None


def test_steps_fires_when_strictly_above_limit():
    config = GuardrailConfig(max_steps=5)
    event = tool_event(step=6, ts=1.0)
    state = replay([event])

    anomaly = StepDetector().check(state, event, config)

    assert anomaly is not None
    assert anomaly.detector == "steps"
    assert anomaly.severity == "critical"
    assert "6" in anomaly.message
    assert anomaly.details["step_count"] == 6
    assert anomaly.details["max_steps"] == 5
    assert anomaly.details["session_id"] == "s1"


def test_steps_fires_once_per_session():
    config = GuardrailConfig(max_steps=5)
    detector = StepDetector()
    first = tool_event(step=6, ts=1.0)
    state = replay([first])

    assert detector.check(state, first, config) is not None

    second = tool_event(step=7, ts=2.0)
    state.record(second)
    assert detector.check(state, second, config) is None


def test_steps_sessions_fire_independently():
    config = GuardrailConfig(max_steps=5)
    detector = StepDetector()
    event = tool_event(step=6, ts=1.0)
    state_a = replay([event], session_id="a")
    state_b = replay([event], session_id="b")

    assert detector.check(state_a, event, config) is not None
    assert detector.check(state_b, event, config) is not None


# --------------------------------------------------------------------------
# The set as a whole
# --------------------------------------------------------------------------


def test_default_detectors_are_the_seven_classes():
    assert DEFAULT_DETECTORS == [
        LoopDetector,
        BudgetDetector,
        VelocityDetector,
        StepDetector,
        SpikeDetector,
        ErrorStormDetector,
        TimeoutDetector,
    ]


def test_detectors_expose_their_names():
    assert [cls.name for cls in DEFAULT_DETECTORS] == [
        "loop",
        "budget",
        "velocity",
        "steps",
        "spike",
        "error_storm",
        "timeout",
    ]


def test_detectors_are_silent_on_an_empty_session():
    """Zero case: a fully-configured runbound says nothing before any work."""
    config = GuardrailConfig(
        budget_usd=1.0,
        max_total_tokens=100,
        max_steps=5,
        tokens_per_minute_limit=100,
    )
    state = SessionState("empty")
    event = Event(kind="llm_call", ts=0.0, step=0)

    assert all(cls().check(state, event, config) is None for cls in DEFAULT_DETECTORS)


def test_detectors_tolerate_events_with_missing_fields():
    """Detectors never raise on an odd event — the engine's fail-open depends on it."""
    config = GuardrailConfig(
        budget_usd=1.0,
        max_total_tokens=100,
        max_steps=5,
        tokens_per_minute_limit=100,
    )
    event = Event(kind="unknown", ts=0.0, step=1)
    state = replay([event])

    for cls in DEFAULT_DETECTORS:
        assert cls().check(state, event, config) is None


# --------------------------------------------------------------------------
# BudgetDetector — fleet offsets (Wave 18)
# --------------------------------------------------------------------------


def test_budget_offsets_default_to_zero():
    state = SessionState("s1")

    assert state.spend_offset_usd == 0.0
    assert state.tokens_offset == 0
    assert state.fleet_generation is None


def test_budget_trips_on_local_spend_plus_the_fleet_offset():
    config = GuardrailConfig(budget_usd=5.0)
    event = llm_event(step=1, ts=1.0, cost=0.30)
    state = replay([event])
    state.spend_offset_usd = 4.80

    anomaly = BudgetDetector().check(state, event, config)

    assert anomaly is not None
    assert anomaly.details["limit_hit"] == "budget_usd"
    assert abs(anomaly.details["total_cost_usd"] - 5.10) < 1e-9
    assert abs(anomaly.details["fleet_spend_offset_usd"] - 4.80) < 1e-9


def test_budget_silent_when_local_spend_plus_offset_is_exactly_at_the_limit():
    config = GuardrailConfig(budget_usd=5.0)
    event = llm_event(step=1, ts=1.0, cost=0.20)
    state = replay([event])
    state.spend_offset_usd = 4.80

    assert BudgetDetector().check(state, event, config) is None


def test_budget_trips_on_local_tokens_plus_the_fleet_offset():
    config = GuardrailConfig(max_total_tokens=1_000)
    event = llm_event(step=1, ts=1.0, tokens_in=100)
    state = replay([event])
    state.tokens_offset = 950

    anomaly = BudgetDetector().check(state, event, config)

    assert anomaly is not None
    assert anomaly.details["limit_hit"] == "max_total_tokens"
    assert anomaly.details["total_tokens"] == 1_050
    assert anomaly.details["fleet_tokens_offset"] == 950


def test_budget_details_omit_the_offsets_when_they_are_zero():
    config = GuardrailConfig(budget_usd=1.0)
    event = llm_event(step=1, ts=1.0, cost=1.5)
    state = replay([event])

    anomaly = BudgetDetector().check(state, event, config)

    assert anomaly is not None
    assert "fleet_spend_offset_usd" not in anomaly.details
    assert "fleet_tokens_offset" not in anomaly.details


def test_budget_offset_alone_can_trip_a_session_that_spent_nothing():
    config = GuardrailConfig(budget_usd=5.0)
    event = llm_event(step=1, ts=1.0)
    state = replay([event])
    state.spend_offset_usd = 5.01

    assert BudgetDetector().check(state, event, config) is not None


def test_budget_message_reports_the_fleet_total():
    config = GuardrailConfig(budget_usd=5.0)
    event = llm_event(step=1, ts=1.0, cost=0.30)
    state = replay([event])
    state.spend_offset_usd = 4.80

    anomaly = BudgetDetector().check(state, event, config)

    assert "5.10" in anomaly.message
