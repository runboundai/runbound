"""Tests for the tuned spike verdict and for deterministic keyed session ids.

Three production-review findings meet here:

* **M5** — a warmup of 10 calls per key that a five-message support-chat
  session never reached, so the flagship detector never armed for the persona
  it was built for. The default is now 4.
* **M6** — a verdict made of a ratio alone, which called a 1.2s answer after
  0.1s lookups a spike. A call must now also rise by an absolute amount
  (``spike_min_duration_s`` / ``spike_min_output_tokens``) before the ratio
  means anything. The per-call hard caps are somebody's stated limit and
  ignore the floors.
* **B2 (part)** — random per-process session ids, which made PagerDuty's dedup
  key different on every worker: one incident, up to eight pages. A keyed
  session's id is now derived from the key, so every worker agrees on it,
  while :func:`runbound.clear` still yields a brand-new session.

Nothing here measures real time: durations are values fed to the detector.
"""

import hashlib

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.detectors import SpikeDetector
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

NORMAL_SECONDS = 2.0
NORMAL_TOKENS = 100
KEY = "user:8842"


def llm_event(
    step: int,
    duration: float = NORMAL_SECONDS,
    tokens_out: int = NORMAL_TOKENS,
) -> Event:
    return Event(
        kind="llm_call",
        ts=float(step),
        step=step,
        tokens_out=tokens_out,
        duration_s=duration,
        model="gpt-4o",
    )


def feed(
    detector: SpikeDetector, state: SessionState, event: Event, config: GuardrailConfig
) -> Anomaly | None:
    """Record an event the way the engine does, then ask the detector."""
    state.record(event)
    return detector.check(state, event, config)


def warm(
    detector: SpikeDetector,
    state: SessionState,
    config: GuardrailConfig,
    duration: float = NORMAL_SECONDS,
    tokens_out: int = NORMAL_TOKENS,
) -> int:
    """Feed exactly enough unremarkable calls to arm the baseline."""
    for step in range(1, config.spike_warmup_calls + 1):
        assert feed(detector, state, llm_event(step, duration, tokens_out), config) is None
    return config.spike_warmup_calls + 1


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def chat(duration: float = NORMAL_SECONDS, key: str = KEY) -> None:
    """One chatbot turn: enter the end-user's session, make one model call."""
    with runbound.session(key):
        api._record_llm_call("gpt-4o", 500, NORMAL_TOKENS, duration_s=duration)


# --- warmup (M5) ------------------------------------------------------------


def test_the_default_warmup_is_four():
    assert GuardrailConfig().spike_warmup_calls == 4


def test_a_short_chatbot_session_arms_on_its_fifth_call():
    """The wedge persona: five messages is now enough to be protected."""
    seen: list[Anomaly] = []
    runbound.init(
        on_anomaly="callback", callback=seen.append, on_spike="trip", spike_confirm=1
    )

    for _ in range(3):
        chat()
    chat(74.0)  # call 4: three calls of history, still warming up
    assert seen == []

    chat(74.0)  # call 5: the baseline is armed

    assert [(a.detector, a.severity) for a in seen] == [("spike", "critical")]
    assert seen[0].details["key"] == KEY


# --- absolute floors (M6) ---------------------------------------------------


def test_a_twelve_times_median_call_that_barely_rises_is_silent():
    """A 1.2s answer after 0.1s lookups is a longer question, not an incident."""
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, duration=0.1)

    assert feed(detector, state, llm_event(step, duration=1.2), config) is None


def test_a_call_that_clears_both_the_factor_and_the_floor_fires():
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, duration=0.1)

    anomaly = feed(detector, state, llm_event(step, duration=8.0), config)

    assert anomaly is not None
    assert anomaly.details["metric"] == "duration"
    assert anomaly.severity == "warn"


def test_a_rise_exactly_at_the_duration_floor_is_not_a_spike():
    """Boundary: the rise must be strictly greater than the floor."""
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, duration=0.125)  # exact in binary

    at_floor = 0.125 + config.spike_min_duration_s
    assert feed(detector, state, llm_event(step, duration=at_floor), config) is None
    assert feed(detector, state, llm_event(step + 1, duration=at_floor + 0.125), config)


def test_a_huge_ratio_in_output_tokens_that_barely_rises_is_silent():
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, tokens_out=10)

    assert feed(detector, state, llm_event(step, tokens_out=120), config) is None


def test_an_output_rise_past_the_floor_fires():
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, tokens_out=100)

    anomaly = feed(detector, state, llm_event(step, tokens_out=1200), config)

    assert anomaly is not None
    assert anomaly.details["metric"] == "output_tokens"


def test_a_rise_exactly_at_the_output_floor_is_not_a_spike():
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, tokens_out=10)

    at_floor = 10 + config.spike_min_output_tokens
    assert feed(detector, state, llm_event(step, tokens_out=at_floor), config) is None
    assert feed(detector, state, llm_event(step + 1, tokens_out=at_floor + 1), config)


def test_a_big_rise_below_the_factor_is_still_not_a_spike():
    """Both conditions are required: an 8s rise at 5x median stays quiet."""
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config)  # median 2.0s

    assert feed(detector, state, llm_event(step, duration=10.0), config) is None


def test_a_near_zero_baseline_can_no_longer_produce_a_degenerate_alert():
    """The "0.0s vs 0.0s" page: a 50x ratio on a 0.01s baseline says nothing."""
    detector, state, config = SpikeDetector(), SessionState("s1"), GuardrailConfig()
    step = warm(detector, state, config, duration=0.01, tokens_out=1)

    assert feed(detector, state, llm_event(step, duration=0.5, tokens_out=2), config) is None


def test_the_floors_are_configurable():
    detector, state = SpikeDetector(), SessionState("s1")
    config = GuardrailConfig(spike_min_duration_s=0.5)
    step = warm(detector, state, config, duration=0.1)

    assert feed(detector, state, llm_event(step, duration=1.2), config) is not None


# --- caps are somebody's stated limit and ignore the floors ------------------


def test_a_duration_cap_is_critical_even_when_the_rise_is_tiny():
    detector, state = SpikeDetector(), SessionState("s1")
    config = GuardrailConfig(max_call_seconds=1.0)

    anomaly = feed(detector, state, llm_event(1, duration=1.5), config)

    assert anomaly is not None
    assert anomaly.severity == "critical"
    assert anomaly.details["cap"] == pytest.approx(1.0)


def test_an_output_cap_is_critical_even_when_the_rise_is_tiny():
    detector, state = SpikeDetector(), SessionState("s1")
    config = GuardrailConfig(max_tokens_out_per_call=100)

    anomaly = feed(detector, state, llm_event(1, tokens_out=150), config)

    assert anomaly is not None
    assert anomaly.severity == "critical"
    assert anomaly.details["metric"] == "output_tokens"


# --- config -----------------------------------------------------------------


def test_the_floor_defaults():
    cfg = GuardrailConfig()

    assert cfg.spike_min_duration_s == pytest.approx(2.0)
    assert cfg.spike_min_output_tokens == 500


@pytest.mark.parametrize("field", ["spike_min_duration_s", "spike_min_output_tokens"])
@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_a_non_positive_floor_raises(field, value):
    with pytest.raises(ValueError):
        GuardrailConfig(**{field: value}).validate()


@pytest.mark.parametrize("field", ["spike_min_duration_s", "spike_min_output_tokens"])
def test_a_tiny_positive_floor_is_valid(field):
    GuardrailConfig(**{field: 0.001}).validate()


# --- deterministic keyed session ids (B2) -----------------------------------


def derived_id(key: str, generation: int = 0) -> str:
    """The id every worker must derive for ``key`` at ``generation``."""
    return f"{hashlib.sha256(key.encode()).hexdigest()[:10]}-{generation}"


def test_a_keyed_session_id_is_derived_from_the_key():
    runbound.init()

    with runbound.session(KEY) as state:
        assert state.session_id == derived_id(KEY)


def test_two_workers_derive_the_same_id_for_the_same_key():
    """The dedup fix: one incident, one PagerDuty page, however many workers."""
    runbound.init()
    with runbound.session(KEY) as first:
        first_id = first.session_id

    api._teardown_for_tests()  # a second worker, freshly started
    runbound.init()
    with runbound.session(KEY) as second:
        second_id = second.session_id

    assert second_id == first_id


def test_different_keys_get_different_ids():
    runbound.init()

    with runbound.session("user:1") as a:
        pass
    with runbound.session("user:2") as b:
        pass

    assert a.session_id != b.session_id


def test_the_default_session_keeps_a_random_id():
    """Only keyed sessions are derived; the default one stays a uuid."""
    runbound.init()
    first = runbound.current_session().session_id

    runbound.reset()

    assert runbound.current_session().session_id != first
    assert "-" not in first


def test_clear_starts_the_next_generation_of_a_key():
    runbound.init()
    with runbound.session(KEY) as first:
        pass

    runbound.clear(KEY)

    with runbound.session(KEY) as second:
        assert second.session_id == derived_id(KEY, 1)
    assert second.session_id != first.session_id


def test_clear_still_re_arms_the_detectors_for_the_key():
    """The new generation is a new session, so fire-once memos let go."""
    runbound.init(budget_usd=0.005, on_anomaly="raise")
    assert _trips(5) == 3  # $0.00225 a call: calls 3, 4 and 5 are over budget

    runbound.clear(KEY)

    assert _trips(5) == 3  # a brand-new latch, from the same breach point
    assert runbound.is_tripped(KEY).detector == "budget"


def _trips(messages: int) -> int:
    """Send ``messages`` chat turns; how many were stopped."""
    stopped = 0
    for _ in range(messages):
        try:
            chat()
        except GuardrailTripped:
            stopped += 1
    return stopped


def test_init_forgets_the_generations():
    """A restarted process starts every key at generation 0 again."""
    runbound.init()
    with runbound.session(KEY):
        pass
    runbound.clear(KEY)

    runbound.init()

    with runbound.session(KEY) as state:
        assert state.session_id == derived_id(KEY)


def test_reset_forgets_the_generations():
    runbound.init()
    with runbound.session(KEY):
        pass
    runbound.clear(KEY)

    runbound.reset()

    with runbound.session(KEY) as state:
        assert state.session_id == derived_id(KEY)


def test_an_unencodable_key_still_gets_a_session():
    """Fail-open: a key that is not valid UTF-8 costs nothing."""
    runbound.init()

    with runbound.session("user:\ud800") as state:
        assert state is not None
        assert state.session_id.endswith("-0")
