"""Tests for the retry-storm half of Wave 13.

Three layers, in the order an error travels through them: the session state
that counts failures, the detector that calls a run of them a storm, and the
engine/api pair that turns a provider's failures into an open circuit.

Nothing here waits on real time: event timestamps are values, and the
breaker's clock is a fake moved by hand.
"""

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.detectors import DEFAULT_DETECTORS, ErrorStormDetector
from runbound.engine import CIRCUIT_DETECTOR, Engine
from runbound.events import Event
from runbound.exceptions import CircuitOpen, GuardrailTripped
from runbound.state import SessionState


class FakeClock:
    """A monotonic clock moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingObserver:
    """Records the anomalies the engine reports (Wave 31: observers, not
    alerters, are how the engine reports one now)."""

    def __init__(self) -> None:
        self.sent: list = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


class Failure(Exception):
    """An SDK-shaped error carrying an HTTP status."""

    def __init__(self, status_code=None):
        super().__init__(f"status {status_code}")
        if status_code is not None:
            self.status_code = status_code


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def error_event(ts: float, step: int = 1, kind: str = "llm_error") -> Event:
    return Event(kind=kind, ts=ts, step=step, error="boom")


# --- state ------------------------------------------------------------------


def test_failures_are_timestamped_and_counted():
    state = SessionState("s1")

    state.record(error_event(10.0, 1))
    state.record(error_event(11.0, 2, kind="tool_error"))

    assert list(state.error_timestamps) == [10.0, 11.0]
    assert state.consecutive_errors == 2


def test_failures_older_than_a_minute_are_pruned_on_append():
    state = SessionState("s1")
    state.record(error_event(10.0, 1))
    state.record(error_event(20.0, 2))

    state.record(error_event(75.0, 3))

    assert list(state.error_timestamps) == [20.0, 75.0]


def test_a_successful_model_call_resets_the_consecutive_count():
    state = SessionState("s1")
    state.record(error_event(10.0, 1))
    state.record(error_event(11.0, 2))

    state.record(Event(kind="llm_call", ts=12.0, step=3, tokens_out=5))

    assert state.consecutive_errors == 0
    assert list(state.error_timestamps) == [10.0, 11.0]  # the window is untouched


def test_a_model_request_for_a_tool_joins_the_loop_window():
    state = SessionState("s1")

    state.record(Event(kind="tool_request", ts=1.0, step=1, tool_name="search", args_hash="req:a"))
    state.record(Event(kind="tool_call", ts=2.0, step=2, tool_name="search", args_hash="a"))

    assert list(state.recent_hashes) == ["req:a", "a"]
    assert state.tool_calls == {"search": 1}  # only the executed call is a call


# --- the detector -----------------------------------------------------------


def storm(count: int, limit: int | None = 3, start: float = 100.0):
    """Feed ``count`` failures to a fresh detector; return every verdict."""
    detector = ErrorStormDetector()
    config = GuardrailConfig(error_storm_limit=limit)
    state = SessionState("s1", key="user:1")
    verdicts = []
    for index in range(count):
        event = error_event(start + index, index + 1)
        state.record(event)
        verdicts.append(detector.check(state, event, config))
    return verdicts


def test_the_storm_detector_is_silent_at_the_limit():
    assert storm(3, limit=3) == [None, None, None]


def test_it_fires_one_past_the_limit():
    verdicts = storm(4, limit=3)
    anomaly = verdicts[-1]

    assert anomaly is not None
    assert anomaly.detector == "error_storm"
    assert anomaly.severity == "critical"
    assert "4" in anomaly.message and "60s" in anomaly.message
    assert anomaly.details["errors_last_60s"] == 4
    assert anomaly.details["limit"] == 3
    assert anomaly.details["key"] == "user:1"
    assert anomaly.details["session_id"] == "s1"
    assert anomaly.details["tags"] == {}


def test_it_fires_once_per_session():
    verdicts = storm(6, limit=3)

    assert [v is not None for v in verdicts] == [False, False, False, True, False, False]


def test_no_limit_disables_it():
    assert storm(20, limit=None) == [None] * 20


def test_failures_spread_over_more_than_a_minute_are_not_a_storm():
    detector = ErrorStormDetector()
    config = GuardrailConfig(error_storm_limit=3)
    state = SessionState("s1")
    verdicts = []
    for index in range(6):
        event = error_event(100.0 + index * 30.0, index + 1)
        state.record(event)
        verdicts.append(detector.check(state, event, config))

    assert verdicts == [None] * 6


def test_it_says_nothing_about_a_session_that_has_not_failed():
    detector = ErrorStormDetector()
    state = SessionState("s1")
    event = Event(kind="llm_call", ts=1.0, step=1)
    state.record(event)

    assert detector.check(state, event, GuardrailConfig(error_storm_limit=1)) is None


def test_it_is_one_of_the_default_detectors():
    assert ErrorStormDetector in DEFAULT_DETECTORS


# --- engine: the llm_error event --------------------------------------------


def test_record_llm_error_emits_an_event_and_numbers_the_step():
    engine = Engine(GuardrailConfig())
    state = SessionState("s1")

    engine.record_llm_error(state, "gpt-4o", Failure(503), 1.5, "openai")
    engine.record_llm_error(state, "gpt-4o", Failure(503), 2.5, "openai")

    assert state.step_count == 2
    assert state.consecutive_errors == 2
    assert len(state.error_timestamps) == 2


def test_a_storm_trips_the_session_under_raise():
    engine = Engine(GuardrailConfig(on_anomaly="raise", error_storm_limit=2))
    state = SessionState("s1")

    engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    with pytest.raises(GuardrailTripped) as excinfo:
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    assert excinfo.value.anomaly.detector == "error_storm"


def test_the_circuit_still_counts_a_failure_that_tripped_the_session():
    """A latched session must not stop the breaker from seeing the outage."""
    engine = Engine(
        GuardrailConfig(
            on_anomaly="raise",
            error_storm_limit=1,
            on_provider_failure="open",
            circuit_failure_threshold=2,
        ),
    )
    state = SessionState("s1")

    engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")  # at the limit
    for _ in range(2):  # past it: the session is latched and every call raises
        with pytest.raises(GuardrailTripped):
            engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    assert engine.circuit_allows("openai") is False  # ...and the outage was still counted


def test_an_error_that_cannot_be_stringified_is_still_recorded():
    class Nasty(Exception):
        def __str__(self):
            raise RuntimeError("no string for you")

    engine = Engine(GuardrailConfig())
    state = SessionState("s1")

    engine.record_llm_error(state, "gpt-4o", Nasty(), 0.0, "openai")

    assert state.consecutive_errors == 1


# --- engine: the circuit ----------------------------------------------------


def open_engine(mode: str, observer: RecordingObserver, clock=None) -> Engine:
    engine = Engine(
        GuardrailConfig(on_provider_failure=mode, circuit_failure_threshold=2),
        observers=[observer],
    )
    if clock is not None:
        engine.circuit._now = clock
    return engine


@pytest.mark.parametrize("mode", ["open", "notify"])
def test_the_open_circuit_is_alerted_once_in_either_mode(mode):
    observer = RecordingObserver()
    engine = open_engine(mode, observer)
    state = SessionState("s1")

    for _ in range(5):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    circuit_alerts = [a for a in observer.sent if a.detector == CIRCUIT_DETECTOR]
    assert len(circuit_alerts) == 1
    assert circuit_alerts[0].severity == "critical"
    assert "openai" in circuit_alerts[0].message
    assert circuit_alerts[0].details["provider"] == "openai"
    assert circuit_alerts[0].details["failures"] == 2
    assert circuit_alerts[0].details["cooldown_seconds"] == 30.0


def test_the_open_mode_message_says_calls_fail_fast():
    observer = RecordingObserver()
    engine = open_engine("open", observer)
    state = SessionState("s1")

    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    assert "fail fast" in observer.sent[-1].message


def test_the_notify_mode_message_says_calls_continue():
    observer = RecordingObserver()
    engine = open_engine("notify", observer)
    state = SessionState("s1")

    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    assert "notify only" in observer.sent[-1].message


def test_notify_never_blocks_a_call():
    engine = open_engine("notify", RecordingObserver())
    state = SessionState("s1")

    for _ in range(5):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    assert engine.circuit.state("openai") == "open"  # counted...
    assert engine.circuit_allows("openai") is True  # ...but never enforced


def test_open_blocks_and_heals_on_a_good_probe():
    clock = FakeClock()
    engine = open_engine("open", RecordingObserver(), clock)
    state = SessionState("s1")

    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    assert engine.circuit_allows("openai") is False

    clock.advance(31.0)
    assert engine.circuit_allows("openai") is True  # the probe
    assert engine.circuit_allows("openai") is False  # everyone else waits

    engine.record_llm_success("openai")
    assert engine.circuit_allows("openai") is True


def test_a_client_side_error_never_opens_the_circuit():
    observer = RecordingObserver()
    engine = open_engine("open", observer)
    state = SessionState("s1")

    for _ in range(5):
        engine.record_llm_error(state, "gpt-4o", Failure(400), 0.1, "openai")

    assert engine.circuit_allows("openai") is True
    assert [a for a in observer.sent if a.detector == CIRCUIT_DETECTOR] == []


def test_a_broken_breaker_never_blocks_the_host():
    """Fail-open: the circuit's own bugs cost nothing but the circuit."""

    class Broken:
        def record_failure(self, key):
            raise RuntimeError("boom")

        def allow(self, key):
            raise RuntimeError("boom")

        def record_success(self, key):
            raise RuntimeError("boom")

        def state(self, key):
            raise RuntimeError("boom")

    engine = Engine(GuardrailConfig(on_provider_failure="open"))
    engine.circuit = Broken()
    state = SessionState("s1")

    engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    engine.record_llm_success("openai")

    assert engine.circuit_allows("openai") is True


def test_providers_have_their_own_circuits():
    engine = open_engine("open", RecordingObserver())
    state = SessionState("s1")

    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    assert engine.circuit_allows("openai") is False
    assert engine.circuit_allows("anthropic") is True


# --- the api surface --------------------------------------------------------


def test_circuit_state_reads_closed_before_init():
    assert api.circuit_state() == "closed"
    assert api.circuit_state("anthropic") == "closed"


def test_circuit_state_follows_the_engine():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)

    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")
    assert api.circuit_state("openai") == "closed"
    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")

    assert api.circuit_state("openai") == "open"
    assert api.circuit_state("anthropic") == "closed"


def test_the_before_hook_raises_circuit_open_once_the_circuit_is_open():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    api._HOOKS.before("openai")  # closed: nothing happens

    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")

    with pytest.raises(CircuitOpen) as excinfo:
        api._HOOKS.before("openai")
    assert isinstance(excinfo.value, GuardrailTripped)
    assert excinfo.value.provider == "openai"
    assert excinfo.value.anomaly.detector == CIRCUIT_DETECTOR
    assert "openai" in str(excinfo.value)
    api._HOOKS.before("anthropic")  # a different provider is unaffected


def test_the_before_hook_never_blocks_under_notify():
    runbound.init(circuit_failure_threshold=1)  # on_provider_failure defaults to notify

    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")

    api._HOOKS.before("openai")
    assert api.circuit_state("openai") == "open"


def test_the_hooks_are_inert_before_init():
    api._HOOKS.before("openai")
    api._HOOKS.success("openai")
    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")
    api._HOOKS.tool_request("search", "req:abc")


def test_the_error_hook_propagates_a_storm_trip():
    runbound.init(on_anomaly="raise", error_storm_limit=1)

    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")
    with pytest.raises(GuardrailTripped) as excinfo:
        api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")

    assert excinfo.value.anomaly.detector == "error_storm"


def test_the_success_hook_closes_the_circuit():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    api._HOOKS.error("gpt-4o", Failure(503), 0.1, "openai")

    api._HOOKS.success("openai")

    assert api.circuit_state("openai") == "closed"
    api._HOOKS.before("openai")


def test_circuit_open_and_circuit_state_are_exported():
    assert runbound.CircuitOpen is CircuitOpen
    assert runbound.circuit_state is api.circuit_state
