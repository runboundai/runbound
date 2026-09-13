"""Tests for the spike detector — the zero-configuration behavior watch.

Sessions are built by hand with explicit durations and token counts: nothing
here measures real time, so a "74 second call" costs the suite nothing. The
detector is fed through ``SessionState.record`` (as the engine does), because
its whole input is the recorded call window.
"""

import logging
import threading

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.detectors import DEFAULT_DETECTORS, SpikeDetector
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

NORMAL_SECONDS = 2.0
NORMAL_TOKENS = 100
WARMUP_CALLS = GuardrailConfig().spike_warmup_calls


def llm_event(
    step: int,
    duration: float = NORMAL_SECONDS,
    tokens_out: int = NORMAL_TOKENS,
    tokens_reasoning: int = 0,
    cost: float = 0.0,
) -> Event:
    return Event(
        kind="llm_call",
        ts=float(step),
        step=step,
        tokens_out=tokens_out,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost,
        duration_s=duration,
        model="gpt-4o",
    )


def session(key: str | None = None, tags: dict | None = None) -> SessionState:
    return SessionState("s1", key=key, tags=tags)


def feed(detector: SpikeDetector, state: SessionState, event: Event, config) -> Anomaly | None:
    """Record an event the way the engine does, then ask the detector."""
    state.record(event)
    return detector.check(state, event, config)


def warm(
    detector: SpikeDetector,
    state: SessionState,
    config: GuardrailConfig,
    calls: int = WARMUP_CALLS,
    duration: float = NORMAL_SECONDS,
    tokens_out: int = NORMAL_TOKENS,
) -> int:
    """Feed ``calls`` unremarkable model calls; returns the next step number."""
    for step in range(1, calls + 1):
        assert feed(detector, state, llm_event(step, duration, tokens_out), config) is None
    return calls + 1


class RecordingObserver:
    """An observer that just remembers the anomalies it was told about.

    Delivery left the SDK in Wave 31; observers (telemetry export among
    them) are how the engine reports an anomaly now, so this is the spy
    these tests watch instead of an alerter.
    """

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []

    def on_event(self, session, event):
        pass

    def on_anomaly(self, session, anomaly, reacted):
        self.sent.append(anomaly)


def engine_warmup(
    engine: Engine, state: SessionState, calls: int = WARMUP_CALLS, cost: float = 0.0
) -> int:
    for step in range(1, calls + 1):
        engine.process(state, llm_event(step, cost=cost))
    return calls + 1


# --- warmup -----------------------------------------------------------------


def test_a_spike_after_warmup_is_a_warning():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    step = warm(detector, state, config)

    anomaly = feed(detector, state, llm_event(step, duration=40.0), config)

    assert anomaly is not None
    assert (anomaly.detector, anomaly.severity) == ("spike", "warn")
    assert anomaly.details["metric"] == "duration"
    assert anomaly.details["confirmed"] is False


def test_no_verdict_while_the_baseline_is_still_warming_up():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    step = warm(detector, state, config, calls=WARMUP_CALLS - 1)  # one call short

    assert feed(detector, state, llm_event(step, duration=1000.0), config) is None


def test_the_warmup_boundary_is_exactly_spike_warmup_calls():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig(spike_warmup_calls=3)
    step = warm(detector, state, config, calls=3)

    assert feed(detector, state, llm_event(step, duration=40.0), config) is not None


def test_a_call_exactly_at_the_factor_is_not_a_spike():
    """Boundary: abnormal is strictly greater than factor x median."""
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    step = warm(detector, state, config)

    at_threshold = NORMAL_SECONDS * config.spike_factor
    assert feed(detector, state, llm_event(step, duration=at_threshold), config) is None


def test_a_zero_median_never_arms_the_baseline():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    step = warm(detector, state, config, duration=0.0, tokens_out=0)

    assert feed(detector, state, llm_event(step, duration=90.0, tokens_out=9000), config) is None


# --- caps -------------------------------------------------------------------


def test_a_duration_cap_is_critical_on_the_very_first_call():
    detector, state = SpikeDetector(), session()
    config = GuardrailConfig(max_call_seconds=30.0)

    anomaly = feed(detector, state, llm_event(1, duration=60.0), config)

    assert anomaly is not None
    assert anomaly.severity == "critical"
    assert anomaly.details["metric"] == "duration"
    assert anomaly.details["cap"] == pytest.approx(30.0)
    assert anomaly.details["confirmed"] is True
    assert "cap" in anomaly.message


def test_an_output_cap_is_critical_on_the_very_first_call():
    detector, state = SpikeDetector(), session()
    config = GuardrailConfig(max_tokens_out_per_call=1000)

    # A thinking model's completion count already includes its reasoning
    # tokens (the provider's semantics), so the cap reads tokens_out as-is.
    anomaly = feed(
        detector, state, llm_event(1, tokens_out=1400, tokens_reasoning=900), config
    )

    assert anomaly is not None
    assert anomaly.severity == "critical"
    assert anomaly.details["metric"] == "output_tokens"
    assert anomaly.details["value"] == pytest.approx(1400)


def test_a_call_exactly_at_the_cap_does_not_trip_it():
    detector, state = SpikeDetector(), session()
    config = GuardrailConfig(max_call_seconds=30.0)

    assert feed(detector, state, llm_event(1, duration=30.0), config) is None


def test_a_cap_breach_is_reported_once_per_session():
    detector, config = SpikeDetector(), GuardrailConfig(max_call_seconds=30.0)
    state = session()

    assert feed(detector, state, llm_event(1, duration=60.0), config) is not None
    assert feed(detector, state, llm_event(2, duration=90.0), config) is None
    assert feed(detector, state, llm_event(3, duration=99.0), config) is None


def test_a_cap_breach_in_another_session_is_still_reported():
    detector, config = SpikeDetector(), GuardrailConfig(max_call_seconds=30.0)

    assert feed(detector, session(), llm_event(1, duration=60.0), config) is not None
    other = SessionState("s2", key="user:2")
    assert feed(detector, other, llm_event(1, duration=60.0), config) is not None


# --- confirmation -----------------------------------------------------------


def test_a_second_abnormal_call_confirms_the_spike_then_the_detector_goes_quiet():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()  # confirm=2
    step = warm(detector, state, config)

    warning = feed(detector, state, llm_event(step, duration=40.0), config)
    confirmed = feed(detector, state, llm_event(step + 1, duration=45.0), config)

    assert warning.severity == "warn"
    assert confirmed.severity == "critical"
    assert confirmed.details["confirmed"] is True
    assert feed(detector, state, llm_event(step + 2, duration=50.0), config) is None
    assert feed(detector, state, llm_event(step + 3, duration=55.0), config) is None


def test_rearm_lets_a_still_spiking_session_report_again():
    """A healed latch must not leave a still-spiking session silent (T137).

    ``rearm`` clears the fire-once memos (``_warned``/``_confirmed``) but not
    the trailing abnormality window (``_flags``): a session whose calls are
    *still* abnormal reports again on its very next one, without needing to
    warm the window back up from nothing.
    """
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()  # confirm=2
    step = warm(detector, state, config)

    feed(detector, state, llm_event(step, duration=40.0), config)  # warn
    confirmed = feed(detector, state, llm_event(step + 1, duration=45.0), config)
    assert confirmed.severity == "critical"
    assert feed(detector, state, llm_event(step + 2, duration=50.0), config) is None  # quiet

    detector.rearm(state.session_id)

    reconfirmed = feed(detector, state, llm_event(step + 3, duration=55.0), config)

    assert reconfirmed is not None
    assert reconfirmed.severity == "critical"
    assert reconfirmed.details["confirmed"] is True


def test_rearm_does_not_reopen_a_ladder_closed_session():
    """The abuse ladder's close is a separate state machine (T137, on_spike="limit").

    ``latch_ttl_seconds`` heals a plain latch; a session the *ladder* closed
    is retired by its own rollover, on its own cooldown — ``rearm`` must not
    accidentally resurrect it.
    """
    detector = SpikeDetector()
    detector._closed.add("s1")

    state = session()
    step = warm(detector, state, GuardrailConfig(on_spike="limit"))
    config = GuardrailConfig(on_spike="limit")

    assert feed(detector, state, llm_event(step, duration=40.0), config) is None

    detector.rearm("s1")

    assert feed(detector, state, llm_event(step + 1, duration=45.0), config) is None


def test_a_lone_spike_that_returns_to_normal_says_nothing_more():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    step = warm(detector, state, config)

    assert feed(detector, state, llm_event(step, duration=40.0), config).severity == "warn"

    for extra in range(1, 6):
        assert feed(detector, state, llm_event(step + extra), config) is None


def test_spike_confirm_of_one_skips_the_warning_phase():
    detector, state = SpikeDetector(), session()
    config = GuardrailConfig(spike_confirm=1)
    step = warm(detector, state, config)

    anomaly = feed(detector, state, llm_event(step, duration=40.0), config)

    assert anomaly.severity == "critical"


def test_abnormal_calls_beyond_the_trailing_window_do_not_confirm():
    """Two spikes six calls apart are two incidents, not one confirmation."""
    detector, state = SpikeDetector(), session()
    config = GuardrailConfig(spike_confirm=2)
    step = warm(detector, state, config)

    assert feed(detector, state, llm_event(step, duration=40.0), config).severity == "warn"
    for offset in range(1, 6):  # five normal calls push the flag out of the window
        assert feed(detector, state, llm_event(step + offset), config) is None

    assert feed(detector, state, llm_event(step + 6, duration=40.0), config) is None


# --- output work ------------------------------------------------------------


def test_reasoning_tokens_alone_can_trip_the_output_baseline():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    step = warm(detector, state, config)

    # When a model starts thinking, its completion count balloons (reasoning
    # is included in tokens_out by the provider) — that is what spikes.
    anomaly = feed(
        detector,
        state,
        llm_event(step, tokens_out=1600, tokens_reasoning=1500),
        config,
    )

    assert anomaly is not None
    assert anomaly.details["metric"] == "output_tokens"
    assert anomaly.details["value"] == pytest.approx(1600)
    assert anomaly.details["median"] == pytest.approx(NORMAL_TOKENS)


# --- robustness -------------------------------------------------------------


def test_one_huge_call_in_the_history_does_not_move_the_baseline():
    """A mean would be dragged upwards by the outlier; the median stays 2.0s."""
    detector, state, config = SpikeDetector(), session(), GuardrailConfig()
    last_warmup = WARMUP_CALLS - 1
    warm(detector, state, config, calls=last_warmup)
    outlier = last_warmup + 1
    assert feed(detector, state, llm_event(outlier, duration=100.0), config) is None  # warming

    warning = feed(detector, state, llm_event(outlier + 1, duration=30.0), config)

    assert warning.severity == "warn"
    assert warning.details["median"] == pytest.approx(NORMAL_SECONDS)


def test_disabled_spike_detection_never_says_anything():
    detector, state = SpikeDetector(), session()
    config = GuardrailConfig(spike_detection=False, max_call_seconds=1.0)

    for step in range(1, 21):
        assert feed(detector, state, llm_event(step, duration=float(step) * 50), config) is None


def test_non_llm_events_are_ignored():
    detector, state, config = SpikeDetector(), session(), GuardrailConfig(max_call_seconds=1.0)
    warm(detector, state, config, duration=0.5, tokens_out=1)

    tool_call = Event(kind="tool_call", ts=11.0, step=11, tool_name="search", args_hash="h")
    assert feed(detector, state, tool_call, config) is None


def test_an_empty_call_window_is_silent():
    detector, config = SpikeDetector(), GuardrailConfig(max_call_seconds=1.0)

    # An llm_call the session never recorded: nothing to compare against.
    assert detector.check(session(), llm_event(1, duration=99.0), config) is None


@pytest.mark.parametrize(
    "state",
    [
        type("NoWindow", (), {"session_id": "x", "lock": threading.RLock()})(),
        type(
            "JunkWindow",
            (),
            {"session_id": "x", "lock": threading.RLock(), "recent_calls": ["nonsense"]},
        )(),
        type(
            "ShortTuples",
            (),
            {"session_id": "x", "lock": threading.RLock(), "recent_calls": [(1.0,)]},
        )(),
    ],
)
def test_a_malformed_session_is_tolerated(state):
    """Fail-open: an unreadable window makes the detector silent, not loud."""
    config = GuardrailConfig(max_call_seconds=1.0)

    assert SpikeDetector().check(state, llm_event(1, duration=99.0), config) is None


# --- details ----------------------------------------------------------------


def test_details_carry_the_session_identity_and_the_numbers():
    detector = SpikeDetector()
    state = session(key="user:8842", tags={"plan": "free"})
    config = GuardrailConfig()
    step = warm(detector, state, config)

    anomaly = feed(detector, state, llm_event(step, duration=74.0), config)

    assert anomaly.details == {
        "session_id": "s1",
        "key": "user:8842",
        "tags": {"plan": "free"},
        "metric": "duration",
        "value": pytest.approx(74.0),
        "median": pytest.approx(2.0),
        "factor": pytest.approx(10.0),
        "confirmed": False,
    }
    assert "user:8842" in anomaly.message
    assert "74.0s" in anomaly.message
    assert "2.0s" in anomaly.message


def test_details_tags_are_a_copy_of_the_session_tags():
    detector, config = SpikeDetector(), GuardrailConfig(max_call_seconds=1.0)
    state = session(key="k", tags={"plan": "free"})

    anomaly = feed(detector, state, llm_event(1, duration=9.0), config)
    anomaly.details["tags"]["plan"] = "mutated"

    assert state.tags == {"plan": "free"}


def test_the_default_session_reports_no_key():
    detector, config = SpikeDetector(), GuardrailConfig(max_call_seconds=1.0)

    anomaly = feed(detector, session(), llm_event(1, duration=9.0), config)

    assert anomaly.details["key"] is None
    assert "s1" in anomaly.message


# --- the detector set -------------------------------------------------------


def test_spike_is_one_of_the_default_detectors():
    assert [cls.name for cls in DEFAULT_DETECTORS] == [
        "loop",
        "budget",
        "velocity",
        "steps",
        "events",
        "spike",
        "error_storm",
        "timeout",
    ]


# --- engine routing ---------------------------------------------------------


def test_a_warn_spike_never_raises_even_under_on_anomaly_raise(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(on_anomaly="raise"), observers=[observer])
    state = session(key="user:8842")
    step = engine_warmup(engine, state)

    engine.process(state, llm_event(step, duration=40.0))  # host keeps running

    assert [(a.detector, a.severity) for a in observer.sent] == [("spike", "warn")]
    assert "user:8842" in caplog.text


def test_a_confirmed_spike_notifies_but_never_raises_by_default(caplog):
    """Thinking mode alone must not break the session: on_spike defaults
    to "notify" — the escalation is logged and alerted, the host runs on."""
    caplog.set_level(logging.WARNING, logger="runbound")
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(on_anomaly="raise"), observers=[observer])
    state = session()
    step = engine_warmup(engine, state)

    engine.process(state, llm_event(step, duration=40.0))      # warn phase
    engine.process(state, llm_event(step + 1, duration=45.0))  # confirmed — no raise

    # dedup is per (session, detector, severity): the watch notice AND the
    # escalation both reach on-call, each exactly once.
    assert [a.severity for a in observer.sent] == ["warn", "critical"]
    assert "notifying only" in caplog.text


def test_a_confirmed_spike_raises_when_on_spike_is_trip():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(on_anomaly="raise", on_spike="trip"), observers=[observer]
    )
    state = session()
    step = engine_warmup(engine, state)

    engine.process(state, llm_event(step, duration=40.0))  # warn phase

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, llm_event(step + 1, duration=45.0))

    assert excinfo.value.anomaly.detector == "spike"
    assert excinfo.value.anomaly.severity == "critical"
    assert [a.severity for a in observer.sent] == ["warn", "critical"]


def test_on_spike_validation():
    with pytest.raises(ValueError):
        GuardrailConfig(on_spike="break").validate()
    GuardrailConfig(on_spike="trip").validate()
    GuardrailConfig(on_spike="notify").validate()


def test_a_cap_breach_raises_on_the_first_call_under_on_anomaly_raise():
    engine = Engine(GuardrailConfig(on_anomaly="raise", max_call_seconds=30.0))

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(session(), llm_event(1, duration=60.0))

    assert excinfo.value.anomaly.detector == "spike"


def test_a_warn_spike_does_not_shadow_a_co_firing_critical():
    engine = Engine(GuardrailConfig(budget_usd=1.0, on_anomaly="raise"))
    state = session()
    step = engine_warmup(engine, state)

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, llm_event(step, duration=40.0, cost=5.0))

    assert excinfo.value.anomaly.detector == "budget"


def test_a_warn_spike_does_not_invoke_the_callback_but_a_confirmed_one_does():
    seen: list[Anomaly] = []
    config = GuardrailConfig(
        on_anomaly="callback", callback=seen.append, on_spike="trip"
    )
    engine = Engine(config)
    state = session()
    step = engine_warmup(engine, state)

    engine.process(state, llm_event(step, duration=40.0))
    assert seen == []

    engine.process(state, llm_event(step + 1, duration=45.0))

    assert [(a.detector, a.severity) for a in seen] == [("spike", "critical")]


def test_a_quiet_session_is_never_touched_by_the_default_engine():
    """Zero config: steady traffic produces no anomalies at all."""
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(on_anomaly="raise"), observers=[observer])

    engine_warmup(engine, session(), calls=40)

    assert observer.sent == []


# --- end to end through the public API ---------------------------------------


@pytest.fixture
def _uninitialized():
    """A pristine SDK before and after, so no global state leaks."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_only_the_spiking_end_user_is_stopped(_uninitialized):
    """The wedge: one chatbot user goes wild, the others keep chatting."""
    runbound.init(on_anomaly="raise", spike_confirm=1, on_spike="trip")

    def chat(user: str, duration: float) -> None:
        with runbound.session(user, tags={"plan": "free"}):
            api._record_llm_call("gpt-4o", 500, NORMAL_TOKENS, duration_s=duration)

    for _ in range(WARMUP_CALLS):
        chat("user:1", NORMAL_SECONDS)
        chat("user:2", NORMAL_SECONDS)

    with pytest.raises(GuardrailTripped) as excinfo:
        chat("user:1", 74.0)

    assert excinfo.value.anomaly.details["key"] == "user:1"
    chat("user:2", NORMAL_SECONDS)  # the other end-user is untouched


def test_escalation_alerts_are_not_deduped_away():
    """The confirmed critical must reach the observers even after the warn did.

    Regression: dedup keyed only (session, detector) swallowed the critical,
    so on-call would only ever see the gentle "watching" notice.
    """
    config = GuardrailConfig(spike_warmup_calls=2, spike_confirm=2, on_anomaly="warn")
    config.validate()

    recorder = RecordingObserver()
    engine = Engine(config, observers=[recorder])
    state = SessionState("s-esc", spike_window=config.spike_window)
    step = 0

    def call(duration):
        nonlocal step
        step += 1
        engine.process(
            state,
            Event(kind="llm_call", ts=float(step), step=step,
                  tokens_in=100, tokens_out=150, duration_s=duration),
        )

    for _ in range(3):
        call(2.0)      # warmup
    call(74.0)         # first spike -> warn alert
    call(74.0)         # confirmed -> critical alert must ALSO go out
    assert [a.severity for a in recorder.sent] == ["warn", "critical"]
