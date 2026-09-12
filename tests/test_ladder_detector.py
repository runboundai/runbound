"""Tests for the abuse ladder — ``on_spike="limit"``.

A moderate spike must not slam the door. Under ``on_spike="limit"`` a keyed
session climbs a ladder instead: a notice (level 1), a session *limit* that
stops nothing (level 2), and only an exhausted limit closes the session and
hands it to the api for rollover (level 3, ``action="rollover"``).

Nothing here measures real time: durations and token counts are values fed to
the detector, and the one latch-expiry test drives a fake clock by hand.
"""

import logging

import pytest

from runbound.config import GuardrailConfig
from runbound import engine as engine_module
from runbound.detectors import SpikeDetector
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

NORMAL_SECONDS = 2.0
NORMAL_TOKENS = 100
SPIKE_SECONDS = 40.0
KEY = "user:8842"

#: Normal calls fed before every ladder test. Comfortably past
#: ``spike_warmup_calls`` on purpose: the ladder counts abnormal calls, and a
#: session's median only stays put while its history outnumbers the spikes —
#: a chatbot user who has been chatting normally before they start abusing.
BASELINE_CALLS = 15


def limit_config(**overrides) -> GuardrailConfig:
    config = GuardrailConfig(on_spike="limit", **overrides)
    config.validate()
    return config


def llm_event(step: int, duration: float = NORMAL_SECONDS, tokens_out: int = NORMAL_TOKENS):
    return Event(
        kind="llm_call",
        ts=float(step),
        step=step,
        tokens_out=tokens_out,
        duration_s=duration,
        model="gpt-4o",
    )


def keyed(key: str | None = KEY, **fields) -> SessionState:
    return SessionState("s1", key=key, tags={"plan": "free"}, **fields)


def feed(detector, state, event, config) -> Anomaly | None:
    """Record an event the way the engine does, then ask the detector."""
    state.record(event)
    return detector.check(state, event, config)


class Ladder:
    """A detector, a keyed session and a step counter, fed call by call."""

    def __init__(self, config: GuardrailConfig, state: SessionState | None = None) -> None:
        self.config = config
        self.detector = SpikeDetector()
        self.state = state if state is not None else keyed()
        self.step = 0

    def call(self, duration: float = NORMAL_SECONDS) -> Anomaly | None:
        self.step += 1
        return feed(self.detector, self.state, llm_event(self.step, duration), self.config)

    def spike(self) -> Anomaly | None:
        return self.call(SPIKE_SECONDS)

    def warm(self) -> "Ladder":
        for _ in range(BASELINE_CALLS):
            assert self.call() is None
        return self

    def to_level_2(self) -> Anomaly:
        """Warm up, then spike until the session is limited."""
        self.warm()
        anomaly = None
        for _ in range(self.config.spike_confirm):
            anomaly = self.spike()
        assert anomaly is not None and anomaly.details["level"] == 2
        return anomaly


class RecordingObserver:
    def __init__(self) -> None:
        self.sent: list[Anomaly] = []
        self.reactions: list[str] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)
        self.reactions.append(reacted)


class FakeClock:
    """Stands in for the engine's ``time`` module; moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:  # pragma: no cover - never slept
        pass

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- configuration ----------------------------------------------------------


def test_limit_is_a_third_on_spike_mode():
    GuardrailConfig(on_spike="limit").validate()
    with pytest.raises(ValueError, match="on_spike"):
        GuardrailConfig(on_spike="break").validate()


def test_limit_requires_the_latch():
    with pytest.raises(ValueError) as excinfo:
        GuardrailConfig(on_spike="limit", on_trip="once").validate()

    message = str(excinfo.value)
    assert 'on_spike="limit"' in message
    assert "latch" in message
    assert "cooldown" in message


def test_notify_and_trip_do_not_need_the_latch():
    GuardrailConfig(on_spike="notify", on_trip="once").validate()
    GuardrailConfig(on_spike="trip", on_trip="once").validate()


def test_the_ladder_defaults():
    config = GuardrailConfig()

    assert config.spike_limit_calls == 5
    assert config.spike_cooldown_seconds == pytest.approx(300.0)
    assert config.spike_max_strikes == 3


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_limit_is_rejected(value):
    with pytest.raises(ValueError, match="spike_limit_calls"):
        GuardrailConfig(spike_limit_calls=value).validate()


@pytest.mark.parametrize("value", [0, 0.0, -0.5])
def test_a_non_positive_cooldown_is_rejected(value):
    with pytest.raises(ValueError, match="spike_cooldown_seconds"):
        GuardrailConfig(spike_cooldown_seconds=value).validate()


@pytest.mark.parametrize("value", [0, -2])
def test_a_non_positive_strike_count_is_rejected(value):
    with pytest.raises(ValueError, match="spike_max_strikes"):
        GuardrailConfig(spike_max_strikes=value).validate()


def test_the_smallest_usable_ladder_is_valid():
    GuardrailConfig(spike_limit_calls=1, spike_cooldown_seconds=0.001, spike_max_strikes=1).validate()


# --- state ------------------------------------------------------------------


def test_the_new_session_fields_default_to_an_unclimbed_ladder():
    state = SessionState("s")

    assert state.spike_level == 0
    assert state.spike_allowance is None
    assert state.spike_allowance_base is None
    assert state.strikes == 0
    assert state.latch_ttl_override is None


def test_the_new_session_fields_are_constructor_arguments():
    state = SessionState("s", strikes=2, spike_allowance_base=3, latch_ttl_override=30.0)

    assert (state.strikes, state.spike_allowance_base, state.latch_ttl_override) == (2, 3, 30.0)


# --- the ladder is off unless the session is keyed --------------------------


def test_an_unkeyed_session_in_limit_mode_behaves_like_trip():
    """The default session guards a process: there is nobody to roll over."""
    ladder = Ladder(limit_config(), state=SessionState("s1"))
    ladder.warm()

    warning = ladder.spike()
    confirmed = ladder.spike()

    assert warning.severity == "warn"
    assert confirmed.severity == "critical"
    assert "level" not in warning.details
    assert "level" not in confirmed.details


def test_a_keyed_session_under_notify_is_untouched_by_the_ladder():
    ladder = Ladder(GuardrailConfig())  # on_spike defaults to "notify"
    ladder.warm()

    ladder.spike()
    confirmed = ladder.spike()

    assert confirmed.severity == "critical"
    assert "level" not in confirmed.details
    assert ladder.state.spike_level == 0


# --- level 1: the watching notice -------------------------------------------


def test_the_first_abnormal_call_is_a_level_one_notice():
    ladder = Ladder(limit_config(spike_confirm=3))
    ladder.warm()

    anomaly = ladder.spike()

    assert anomaly.severity == "warn"
    assert anomaly.details["level"] == 1
    assert anomaly.details["confirmed"] is False
    assert anomaly.details["key"] == KEY
    assert ladder.state.spike_level == 1


def test_the_level_one_notice_is_sent_once_per_session():
    ladder = Ladder(limit_config(spike_confirm=3))
    ladder.warm()

    assert ladder.spike().details["level"] == 1
    assert ladder.spike() is None  # second abnormal call: still watching


# --- level 2: the session limit ---------------------------------------------


def test_confirmation_limits_the_session_instead_of_stopping_it():
    ladder = Ladder(limit_config())

    anomaly = ladder.to_level_2()

    assert anomaly.severity == "warn"
    assert anomaly.details["level"] == 2
    assert anomaly.details["action"] == "limit"
    assert anomaly.details["allowance"] == 5
    assert anomaly.details["confirmed"] is True
    assert ladder.state.spike_level == 2
    assert ladder.state.spike_allowance == 5
    assert KEY in anomaly.message
    assert "limited" in anomaly.message


def test_further_abnormal_calls_burn_the_allowance_silently():
    ladder = Ladder(limit_config())
    ladder.to_level_2()

    for left in (4, 3, 2, 1):
        assert ladder.spike() is None
        assert ladder.state.spike_allowance == left


# --- level 3: rollover ------------------------------------------------------


def test_an_exhausted_allowance_closes_the_session():
    ladder = Ladder(limit_config())
    ladder.to_level_2()
    for _ in range(4):
        assert ladder.spike() is None

    anomaly = ladder.spike()

    assert anomaly.severity == "critical"
    assert anomaly.details["level"] == 3
    assert anomaly.details["action"] == "rollover"
    assert anomaly.details["strikes"] == 1
    assert anomaly.details["allowance"] == 5
    assert anomaly.details["cooldown_seconds"] == pytest.approx(300.0)
    assert anomaly.details["max_strikes"] == 3
    assert anomaly.details["confirmed"] is True
    assert anomaly.details["key"] == KEY
    assert anomaly.details["tags"] == {"plan": "free"}
    assert ladder.state.spike_level == 3


def test_the_rollover_message_names_the_key_the_cooldown_and_the_strike():
    ladder = Ladder(limit_config())
    ladder.to_level_2()
    for _ in range(4):
        ladder.spike()

    message = ladder.spike().message

    assert KEY in message
    assert "5 abnormal calls" in message
    assert "300" in message
    assert "strike 1 of 3" in message


def test_the_rollover_counts_from_the_strikes_the_session_already_carries():
    ladder = Ladder(limit_config(), state=keyed(strikes=1))
    ladder.to_level_2()
    for _ in range(4):
        ladder.spike()

    assert ladder.spike().details["strikes"] == 2


def test_the_detector_goes_silent_once_the_session_is_closed():
    ladder = Ladder(limit_config())
    ladder.to_level_2()
    for _ in range(4):
        ladder.spike()
    assert ladder.spike().details["level"] == 3

    assert ladder.spike() is None
    assert ladder.call() is None
    assert ladder.spike() is None


# --- healing ----------------------------------------------------------------


def heal(ladder: Ladder) -> None:
    """Normal calls until the trailing window no longer confirms a spike."""
    for _ in range(4):
        assert ladder.call() is None


def test_a_quiet_window_takes_the_session_back_down_to_watching():
    ladder = Ladder(limit_config())
    ladder.to_level_2()

    heal(ladder)

    assert ladder.state.spike_level == 1
    assert ladder.state.spike_allowance is None


def test_a_healed_session_is_limited_again_on_a_new_confirmation():
    """Re-confirmation is new information, so it is reported again."""
    ladder = Ladder(limit_config())
    ladder.to_level_2()
    heal(ladder)

    assert ladder.spike() is None  # one flag: not yet confirmed
    again = ladder.spike()

    assert again.details["level"] == 2
    assert again.details["action"] == "limit"
    assert ladder.state.spike_allowance == 5


def test_healing_restores_the_full_allowance():
    ladder = Ladder(limit_config())
    ladder.to_level_2()
    ladder.spike()  # allowance 4
    heal(ladder)
    ladder.spike()
    ladder.spike()  # limited again

    assert ladder.state.spike_allowance == 5


# --- a session that arrives with tighter terms ------------------------------


def test_the_session_allowance_base_overrides_the_configured_limit():
    """What the api hands a post-rollover session: a halved allowance."""
    ladder = Ladder(limit_config(), state=keyed(spike_allowance_base=2))

    limited = ladder.to_level_2()
    assert limited.details["allowance"] == 2
    assert ladder.state.spike_allowance == 2

    assert ladder.spike() is None  # allowance 1
    closed = ladder.spike()

    assert closed.details["level"] == 3
    assert closed.details["allowance"] == 2


# --- caps and fail-open -----------------------------------------------------


def test_a_cap_is_still_critical_on_the_first_call_in_limit_mode():
    ladder = Ladder(limit_config(max_call_seconds=30.0))

    anomaly = ladder.call(duration=60.0)

    assert anomaly.severity == "critical"
    assert anomaly.details["cap"] == pytest.approx(30.0)
    assert "level" not in anomaly.details


def test_a_malformed_keyed_session_is_tolerated():
    """Fail-open: an unreadable session makes the ladder silent, not loud."""
    broken = type("Broken", (), {"session_id": "x", "key": KEY})()

    assert SpikeDetector().check(broken, llm_event(1, duration=99.0), limit_config()) is None


class SealedSession(SessionState):
    """A session that refuses to have its ladder position written."""

    def seal(self) -> "SealedSession":
        self._sealed = True
        return self

    def __setattr__(self, name, value):
        if name == "spike_level" and getattr(self, "_sealed", False):
            raise RuntimeError("this session cannot be written to")
        super().__setattr__(name, value)


def test_a_session_that_cannot_be_written_to_never_reaches_the_host(caplog):
    """Fail-open: a broken ladder write is logged and skipped, not raised."""
    caplog.set_level(logging.WARNING, logger="runbound")
    engine, _ = engine_ladder()
    state = SealedSession("s-sealed", key=KEY).seal()

    warmup_then(engine, state, [SPIKE_SECONDS, SPIKE_SECONDS])  # the host runs on

    assert "detector" in caplog.text
    assert state.tripped_by is None


# --- engine routing ---------------------------------------------------------


def engine_ladder(observer=None, **overrides) -> tuple[Engine, SessionState]:
    config = limit_config(on_anomaly="raise", **overrides)
    engine = Engine(config, observers=[observer] if observer is not None else [])
    return engine, keyed()


def run(engine: Engine, state: SessionState, durations) -> None:
    start = state.step_count
    for offset, duration in enumerate(durations, start=1):
        engine.process(state, llm_event(start + offset, duration))


def warmup_then(engine, state, durations) -> None:
    run(engine, state, [NORMAL_SECONDS] * BASELINE_CALLS + list(durations))


def test_a_limited_session_never_raises_and_says_so(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    engine, state = engine_ladder()

    warmup_then(engine, state, [SPIKE_SECONDS, SPIKE_SECONDS])  # level 1, then level 2

    assert state.spike_level == 2
    assert state.tripped_by is None
    assert "limited" in caplog.text
    assert KEY in caplog.text


def test_each_rung_of_the_ladder_alerts_exactly_once():
    observer = RecordingObserver()
    engine, state = engine_ladder(observer)

    warmup_then(engine, state, [SPIKE_SECONDS] * 6)  # level 1, level 2, allowance 4..1

    assert [(a.severity, a.details["level"]) for a in observer.sent] == [
        ("warn", 1),
        ("warn", 2),
    ]


def test_a_re_confirmation_after_healing_alerts_again():
    observer = RecordingObserver()
    engine, state = engine_ladder(observer)

    warmup_then(engine, state, [SPIKE_SECONDS, SPIKE_SECONDS])  # limited
    run(engine, state, [NORMAL_SECONDS] * 4)  # healed
    run(engine, state, [SPIKE_SECONDS, SPIKE_SECONDS])  # limited again

    assert [(a.severity, a.details["level"]) for a in observer.sent] == [
        ("warn", 1),
        ("warn", 2),
        ("warn", 2),
    ]


def test_the_rollover_raises_latches_and_alerts_once():
    observer = RecordingObserver()
    engine, state = engine_ladder(observer)

    with pytest.raises(GuardrailTripped) as excinfo:
        warmup_then(engine, state, [SPIKE_SECONDS] * 7)

    anomaly = excinfo.value.anomaly
    assert anomaly.details["action"] == "rollover"
    assert state.tripped_by is anomaly
    assert state.tripped_by.details["action"] == "rollover"
    assert state.tripped_at is not None
    assert [a.details["level"] for a in observer.sent] == [1, 2, 3]


def test_a_latched_session_stays_refused_without_re_alerting():
    observer = RecordingObserver()
    engine, state = engine_ladder(observer)

    with pytest.raises(GuardrailTripped):
        warmup_then(engine, state, [SPIKE_SECONDS] * 7)
    with pytest.raises(GuardrailTripped):
        run(engine, state, [NORMAL_SECONDS])

    # The three ladder rungs are each their own alert-worthy anomaly; the
    # retry against an already-latched session is not a fourth one — it is
    # the reapply's single "blocked" notice (Engine._reapply), never a fresh
    # escalation.
    assert [a.details["level"] for a in observer.sent[:3]] == [1, 2, 3]
    assert "blocked" not in observer.reactions[:3]
    assert len(observer.sent) == 4
    assert observer.reactions[3] == "blocked"
    assert observer.sent[3] is state.tripped_by


def test_an_unkeyed_confirmation_in_limit_mode_still_stops_the_process():
    engine, _ = engine_ladder()
    state = SessionState("s-default")

    with pytest.raises(GuardrailTripped) as excinfo:
        warmup_then(engine, state, [SPIKE_SECONDS, SPIKE_SECONDS])

    assert excinfo.value.anomaly.details["confirmed"] is True


# --- the per-session latch ttl (the cooldown the api sets) ------------------


CRITICAL = Anomaly("budget", "critical", "out of money", {})


class OnceDetector:
    """Fires its anomaly the first time it is asked, like the real ones."""

    name = "stub"

    def __init__(self) -> None:
        self.checked = 0

    def check(self, state, event, config):
        self.checked += 1
        return CRITICAL if self.checked == 1 else None


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


def latch_engine() -> Engine:
    config = GuardrailConfig(on_anomaly="raise", latch_ttl_seconds=1_000.0)
    config.validate()
    return Engine(config, detectors=[OnceDetector()])


def tool_event(step: int) -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name="t", args_hash="h")


def test_a_session_ttl_override_beats_the_configured_ttl(clock):
    """The cooldown: this session heals in 10s, whatever the config says."""
    engine = latch_engine()
    state = SessionState("s1", latch_ttl_override=10.0)

    with pytest.raises(GuardrailTripped):
        engine.process(state, tool_event(1))
    clock.advance(11.0)

    engine.process(state, tool_event(2))  # healed

    assert state.tripped_by is None


def test_without_an_override_the_configured_ttl_still_rules(clock):
    engine = latch_engine()
    state = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(state, tool_event(1))
    clock.advance(11.0)

    with pytest.raises(GuardrailTripped):
        engine.process(state, tool_event(2))

    assert state.tripped_by is CRITICAL
