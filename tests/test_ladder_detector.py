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

from runbound import controls_merge
from runbound.config import GuardrailConfig
from runbound import engine as engine_module
from runbound.detectors import SpikeDetector
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState
from spike_test_helpers import spike_controls_body, spiking_config
from test_controls_engine import FakeControlsPlane

NORMAL_SECONDS = 2.0
NORMAL_TOKENS = 100
SPIKE_SECONDS = 40.0
KEY = "user:8842"

#: Normal calls fed before every ladder test. Comfortably past
#: ``spike_warmup_calls`` on purpose: the ladder counts abnormal calls, and a
#: session's median only stays put while its history outnumbers the spikes —
#: a chatbot user who has been chatting normally before they start abusing.
BASELINE_CALLS = 15


#: The nine tuning keywords spike_test_helpers' bundle understands --
#: pulled out of an overrides dict before it reaches GuardrailConfig(),
#: so they can be delivered through the plane's Controls instead (this
#: suite's own coverage of that path -- they are also real, local
#: GuardrailConfig fields; see test_spike_tuning.py).
_SPIKE_KWARGS = (
    "spike_limit_calls",
    "spike_cooldown_seconds",
    "spike_max_strikes",
    "spike_warmup_calls",
    "spike_min_duration_s",
    "spike_min_output_tokens",
    "spike_window",
    "spike_factor",
    "spike_confirm",
)


def limit_config(**overrides) -> GuardrailConfig:
    return spiking_config(on_spike="limit", **overrides)


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


# --- configuration -----------------------------------------------------
#
# on_spike/spike_limit_calls/spike_cooldown_seconds/spike_max_strikes are
# real, local GuardrailConfig fields too, validated loudly at init() time.
# The plane's own delivered values are untrusted wire data, though, so
# controls_merge._parse_spike fails open to the free default for anything
# malformed rather than raising -- these are its acceptance tests.


def test_limit_is_a_third_on_spike_mode():
    assert controls_merge._parse_spike({"mode": "limit"})["mode"] == "limit"
    assert controls_merge._parse_spike({"mode": "break"}) is None


def test_limit_requires_no_latch_check_any_more():
    """``GuardrailConfig.validate()`` ties on_spike="limit" to
    on_trip="latch" for a *local* config, but ``_parse_spike`` reads the
    plane's own, isolated wire payload, which carries no on_trip field to
    check against -- so it cannot and does not reject "limit" on that
    basis. Latching under a plane-stated "limit" still requires
    on_trip="latch" behaviorally (see the ladder's own use of the latch
    elsewhere in this file); this test only pins that _parse_spike itself
    does not reject the mode."""
    assert controls_merge._parse_spike({"mode": "limit"}) is not None


def test_notify_and_trip_parse_the_same_way():
    assert controls_merge._parse_spike({"mode": "notify"})["mode"] == "notify"
    assert controls_merge._parse_spike({"mode": "trip"})["mode"] == "trip"


def test_the_ladder_defaults():
    from spike_test_helpers import SPIKE_DEFAULTS

    assert SPIKE_DEFAULTS["limit_calls"] == 5
    assert SPIKE_DEFAULTS["cooldown_seconds"] == pytest.approx(300.0)
    assert SPIKE_DEFAULTS["max_strikes"] == 3
    result = controls_merge._parse_spike({})
    assert result["limit_calls"] == 5
    assert result["cooldown_seconds"] == pytest.approx(300.0)
    assert result["max_strikes"] == 3


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_limit_falls_back_to_none(value):
    assert controls_merge._parse_spike({"limit_calls": value}) is None


@pytest.mark.parametrize("value", [0, 0.0, -0.5])
def test_a_non_positive_cooldown_falls_back_to_none(value):
    assert controls_merge._parse_spike({"cooldown_seconds": value}) is None


@pytest.mark.parametrize("value", [0, -2])
def test_a_non_positive_strike_count_falls_back_to_none(value):
    assert controls_merge._parse_spike({"max_strikes": value}) is None


def test_the_smallest_usable_ladder_is_valid():
    result = controls_merge._parse_spike(
        {"limit_calls": 1, "cooldown_seconds": 0.001, "max_strikes": 1}
    )
    assert result is not None


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
    ladder = Ladder(spiking_config(on_spike="notify"))
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
    """An :class:`Engine` that actually goes through
    :meth:`Engine._effective_config` (unlike :class:`Ladder` above, which
    hands a config straight to the detector) — so this one needs a real,
    if fake, plane connection for ``spike_enabled``/``on_spike="limit"`` to
    survive that shim rather than being read back off as the free
    (disabled) default."""
    spike_kwargs = {name: overrides.pop(name) for name in list(overrides) if name in _SPIKE_KWARGS}
    config = GuardrailConfig(on_anomaly="raise", **overrides)
    config.validate()
    plane = FakeControlsPlane(spike_controls_body(on_spike="limit", **spike_kwargs))
    engine = Engine(config, observers=[observer] if observer is not None else [], shared=plane)
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
