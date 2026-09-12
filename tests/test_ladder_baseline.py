"""Tests for the held baseline — abnormal calls must not become "normal".

The spike detector learns what a session's calls look like from that session's
own recent calls. Left alone, a sustained spike train walks into that history
and drags the median up until the abuse reads as ordinary: the ladder stalls
and an abuser is quietly granted a new normal.

These tests pin the fix. While the session's trailing window holds an abnormal
call the baseline is *held* at the snapshot taken before it, so the tenth 40s
call is still measured against the two-second session it interrupted. A window
of nothing but ordinary calls hands tracking back to the live median.

Nothing here measures real time: durations and token counts are values fed to
the detector.
"""

import statistics

import pytest

from runbound.config import GuardrailConfig
from runbound.detectors import SpikeDetector
from runbound.events import Anomaly, Event
from runbound.state import SessionState

NORMAL_SECONDS = 2.0
NORMAL_TOKENS = 100
SPIKE_SECONDS = 40.0  # twenty times this session's normal
WARMUP_CALLS = 4
KEY = "user:8842"


def ladder_config(**overrides) -> GuardrailConfig:
    """The abuse ladder on a *four* call warmup — no padded history."""
    overrides.setdefault("spike_warmup_calls", WARMUP_CALLS)
    config = GuardrailConfig(on_spike="limit", **overrides)
    config.validate()
    return config


class Run:
    """A detector, a session and a step counter, fed one call at a time."""

    def __init__(self, config: GuardrailConfig, key: str | None = None) -> None:
        self.config = config
        self.detector = SpikeDetector()
        self.state = SessionState("s1", key=key, tags={"plan": "free"})
        self.step = 0

    def call(
        self, duration: float = NORMAL_SECONDS, tokens_out: int = NORMAL_TOKENS
    ) -> Anomaly | None:
        """Record one model call the way the engine does, then ask the detector."""
        self.step += 1
        event = Event(
            kind="llm_call",
            ts=float(self.step),
            step=self.step,
            tokens_out=tokens_out,
            duration_s=duration,
            model="gpt-4o",
        )
        self.state.record(event)
        return self.detector.check(self.state, event, self.config)

    def spike(self) -> Anomaly | None:
        return self.call(SPIKE_SECONDS)

    def warm(self, calls: int = WARMUP_CALLS) -> "Run":
        for _ in range(calls):
            assert self.call() is None
        return self

    @property
    def baseline(self) -> tuple[float, float] | None:
        return self.state.spike_baseline

    @property
    def live_median(self) -> float:
        """What an unheld baseline would call this session's normal duration."""
        return statistics.median([call[0] for call in self.state.recent_calls])


NORMAL_BASELINE = (NORMAL_SECONDS, float(NORMAL_TOKENS))


# --- the snapshot -----------------------------------------------------------


def test_a_new_session_carries_no_baseline_snapshot():
    assert SessionState("s").spike_baseline is None


def test_a_warmed_session_stores_the_medians_it_judged_the_call_against():
    run = Run(GuardrailConfig()).warm(calls=WARMUP_CALLS + 1)

    assert run.baseline == pytest.approx(NORMAL_BASELINE)


# --- a sustained train is judged against the session it interrupted ---------


def test_a_sustained_spike_train_is_judged_against_the_pre_spike_median():
    """Ten 40s calls in a row: every one of them is still 20x normal."""
    run = Run(ladder_config(), key=KEY).warm()

    seen = []
    for _ in range(10):
        seen.append(run.spike())
        assert run.baseline == pytest.approx(NORMAL_BASELINE)

    reported = [(a.details["level"], a.details["median"]) for a in seen if a is not None]
    assert reported == [
        (1, pytest.approx(NORMAL_SECONDS)),
        (2, pytest.approx(NORMAL_SECONDS)),
        (3, pytest.approx(NORMAL_SECONDS)),
    ]
    assert run.live_median > NORMAL_SECONDS  # a live baseline had long since drifted


def test_the_ladder_burns_a_full_allowance_on_a_four_call_warmup():
    """The end-to-end climb, with no history padding to keep the median down."""
    run = Run(ladder_config(spike_limit_calls=5), key=KEY).warm()

    seen = [run.spike() for _ in range(7)]

    assert [None if a is None else a.details["level"] for a in seen] == [
        1,
        2,
        None,
        None,
        None,
        None,
        3,
    ]
    closed = seen[-1]
    assert closed.severity == "critical"
    assert closed.details["action"] == "rollover"
    assert closed.details["allowance"] == 5
    assert closed.details["strikes"] == 1
    assert run.state.spike_level == 3


def test_notify_mode_on_an_unkeyed_session_holds_the_baseline_too():
    """The correctness fix is not the ladder's: every mode gets it."""
    config = GuardrailConfig(spike_confirm=5)  # confirms only on five straight
    config.validate()
    run = Run(config).warm(calls=5)

    seen = [run.spike() for _ in range(5)]

    assert [None if a is None else a.severity for a in seen] == [
        "warn",
        None,
        None,
        None,
        "critical",
    ]
    assert seen[-1].details["median"] == pytest.approx(NORMAL_SECONDS)

    assert run.spike() is None  # a sixth spike: still held, still abnormal
    assert run.baseline == pytest.approx(NORMAL_BASELINE)
    assert run.live_median == pytest.approx(SPIKE_SECONDS)


def test_the_alert_reports_the_held_baseline_not_the_drifted_one():
    config = ladder_config(spike_warmup_calls=2, spike_confirm=5)
    run = Run(config, key=KEY).warm(calls=2)

    limited = [run.spike() for _ in range(5)][-1]

    assert limited.details["level"] == 2
    assert limited.details["median"] == pytest.approx(NORMAL_SECONDS)
    assert "this session's normal is 2.0s" in limited.message
    assert run.live_median == pytest.approx(SPIKE_SECONDS)  # what live would have said


# --- handing tracking back --------------------------------------------------


def test_a_cleared_window_lets_the_baseline_track_the_session_again():
    """A user who settles down at 3s is allowed a new normal."""
    run = Run(ladder_config(), key=KEY).warm()
    run.spike()
    run.spike()  # limited: level 2

    for _ in range(5):
        assert run.call(duration=3.0) is None

    assert run.state.spike_level == 1  # healed
    assert run.baseline == pytest.approx(NORMAL_BASELINE)  # a flag is still in the window

    assert run.call(duration=3.0) is None

    assert run.baseline == pytest.approx((3.0, float(NORMAL_TOKENS)))


# --- no snapshot to hold ----------------------------------------------------


def test_a_cap_flagged_call_before_any_baseline_falls_back_to_the_live_median():
    """A cap breach on call #1 flags a session that has never had a baseline."""
    config = GuardrailConfig(max_call_seconds=30.0)
    run = Run(config)

    breach = run.call(duration=60.0)

    assert breach.severity == "critical"
    assert breach.details["cap"] == pytest.approx(30.0)
    assert run.baseline is None

    for _ in range(5):  # the flag stays in the window; there is nothing to hold
        assert run.call() is None
    assert run.baseline is None

    assert run.call() is None  # the window is clear again

    assert run.baseline == pytest.approx(NORMAL_BASELINE)


@pytest.mark.parametrize("junk", ["nonsense", (1.0,), (1.0, 2.0, 3.0), ("a", "b"), 7])
def test_an_unreadable_snapshot_falls_back_to_the_live_median(junk):
    """Fail-open: a corrupted snapshot makes the detector guess, not raise."""
    run = Run(GuardrailConfig()).warm()
    assert run.spike().severity == "warn"
    run.state.spike_baseline = junk

    confirmed = run.spike()

    assert confirmed.severity == "critical"
    assert confirmed.details["median"] == pytest.approx(NORMAL_SECONDS)
