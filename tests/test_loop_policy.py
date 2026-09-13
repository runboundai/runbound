"""Tests for the loop reaction policies (``on_loop``).

Nothing here sleeps for real: the engine's ``time`` module reference is
replaced with a fake that records the delays it was asked for.
"""

import logging

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState


class FakeClock:
    """Stands in for the ``time`` module inside the engine."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


class RecordingObserver:
    """An observer that just remembers the anomalies it was told about."""

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


class StubDetector:
    """Returns a fixed anomaly every time it is checked."""

    def __init__(self, anomaly: Anomaly | None, name: str = "stub") -> None:
        self.anomaly = anomaly
        self.name = name

    def check(self, state, event, config):
        return self.anomaly


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(
        kind="tool_call",
        ts=float(step),
        step=step,
        tool_name="search",
        args_hash=args_hash,
    )


def llm_event(step: int, tokens: int = 0, cost: float = 0.0) -> Event:
    return Event(kind="llm_call", ts=float(step), step=step, tokens_in=tokens, cost_usd=cost)


def session(loop_window: int = 20) -> SessionState:
    return SessionState("s1", loop_window=loop_window)


def loop_repeats(engine: Engine, state: SessionState, count: int, start: int = 1) -> None:
    """Push ``count`` identical tool calls through the engine."""
    for step in range(start, start + count):
        engine.process(state, tool_event(step))


# --- config validation ------------------------------------------------------


def test_loop_policy_defaults():
    cfg = GuardrailConfig()

    assert cfg.on_loop is None
    assert cfg.loop_hard_threshold is None
    assert cfg.throttle_base_seconds == pytest.approx(2.0)
    assert cfg.throttle_max_seconds == pytest.approx(30.0)


@pytest.mark.parametrize("policy", [None, "break", "throttle", "escalate"])
def test_valid_on_loop_policies_are_accepted(policy):
    GuardrailConfig(on_loop=policy).validate()


@pytest.mark.parametrize("policy", ["pause", "BREAK", "", "raise"])
def test_unknown_on_loop_policy_is_rejected(policy):
    with pytest.raises(ValueError, match="on_loop"):
        GuardrailConfig(on_loop=policy).validate()


def test_loop_hard_threshold_must_exceed_loop_threshold():
    GuardrailConfig(loop_threshold=3, loop_hard_threshold=4).validate()

    for bad in (3, 2, 0, -1):
        with pytest.raises(ValueError, match="loop_hard_threshold"):
            GuardrailConfig(loop_threshold=3, loop_hard_threshold=bad).validate()


def test_loop_hard_threshold_must_be_an_int():
    with pytest.raises(ValueError, match="loop_hard_threshold"):
        GuardrailConfig(loop_threshold=3, loop_hard_threshold=4.5).validate()


def test_throttle_seconds_must_be_positive():
    for base in (0, -1.0):
        with pytest.raises(ValueError, match="throttle_base_seconds"):
            GuardrailConfig(throttle_base_seconds=base).validate()
    for maximum in (0, -1.0):
        with pytest.raises(ValueError, match="throttle_max_seconds"):
            GuardrailConfig(throttle_max_seconds=maximum).validate()


def test_throttle_max_must_not_be_below_throttle_base():
    GuardrailConfig(throttle_base_seconds=5.0, throttle_max_seconds=5.0).validate()

    with pytest.raises(ValueError, match="throttle_max_seconds"):
        GuardrailConfig(throttle_base_seconds=5.0, throttle_max_seconds=4.0).validate()


def test_init_rejects_a_bad_loop_policy():
    with pytest.raises(ValueError):
        runbound.init(on_loop="pause")

    assert runbound.current_session() is None


# --- default (None) keeps today's behavior ----------------------------------


def test_default_policy_inherits_on_anomaly():
    engine = Engine(GuardrailConfig(loop_threshold=3, on_anomaly="raise"))
    state = session()

    with pytest.raises(GuardrailTripped) as excinfo:
        loop_repeats(engine, state, 3)

    assert excinfo.value.anomaly.detector == "loop"
    assert excinfo.value.anomaly.severity == "critical"


def test_default_policy_keeps_the_loop_detector_firing_once():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(loop_threshold=3, on_anomaly="warn"), observers=[observer]
    )

    loop_repeats(engine, session(), 6)

    assert len(observer.sent) == 1


# --- break ------------------------------------------------------------------


def test_break_raises_even_though_the_global_reaction_is_warn():
    engine = Engine(
        GuardrailConfig(loop_threshold=3, on_anomaly="warn", on_loop="break")
    )

    with pytest.raises(GuardrailTripped) as excinfo:
        loop_repeats(engine, session(), 3)

    assert excinfo.value.anomaly.detector == "loop"


def test_break_leaves_the_global_warn_reaction_alone_for_other_detectors(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(budget_usd=1.0, on_anomaly="warn", on_loop="break")
    engine = Engine(config)

    engine.process(session(), llm_event(1, cost=2.0))  # budget anomaly: warns, no raise

    assert "Budget exceeded" in caplog.text


def test_break_still_fires_once_per_session():
    config = GuardrailConfig(loop_threshold=3, on_loop="break")
    engine = Engine(config)
    state = session()

    with pytest.raises(GuardrailTripped):
        loop_repeats(engine, state, 3)

    engine.process(state, tool_event(4))  # already fired: silent from here on


# --- throttle ---------------------------------------------------------------


def test_throttle_delays_grow_exponentially_and_cap(clock):
    config = GuardrailConfig(
        loop_threshold=3, on_loop="throttle", throttle_base_seconds=2.0, throttle_max_seconds=30.0
    )
    engine = Engine(config)

    loop_repeats(engine, session(), 8)  # counts 3..8 trip; 1 and 2 do not

    assert clock.delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_throttle_never_raises_and_lets_the_tool_run(clock):
    config = GuardrailConfig(loop_threshold=2, on_loop="throttle", on_anomaly="raise")
    engine = Engine(config)

    loop_repeats(engine, session(), 5)  # on_anomaly="raise" must not apply to the loop

    assert len(clock.delays) == 4


def test_throttle_alerts_once_but_reacts_every_repeat(clock):
    observer = RecordingObserver()
    config = GuardrailConfig(loop_threshold=3, on_loop="throttle")
    engine = Engine(config, observers=[observer])

    loop_repeats(engine, session(), 10)

    assert len(observer.sent) == 1
    assert len(clock.delays) == 8


def test_throttle_logs_the_delay_and_the_tool_name(clock, caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    engine = Engine(GuardrailConfig(loop_threshold=2, on_loop="throttle"))

    loop_repeats(engine, session(), 2)

    assert "search" in caplog.text
    assert "2.0" in caplog.text


def test_throttle_falls_back_to_the_base_delay_when_count_is_missing(clock):
    anomaly = Anomaly("loop", "critical", "looping", {})
    config = GuardrailConfig(on_loop="throttle", throttle_base_seconds=3.0)
    engine = Engine(config, detectors=[StubDetector(anomaly, "loop")])

    engine.process(session(), tool_event(1))

    assert clock.delays == [3.0]


def test_throttle_survives_an_anomaly_with_unusable_details(clock):
    anomaly = Anomaly("loop", "critical", "looping", {"count": "lots"})
    config = GuardrailConfig(on_loop="throttle", throttle_base_seconds=1.5)
    engine = Engine(config, detectors=[StubDetector(anomaly, "loop")])

    engine.process(session(), tool_event(1))  # host survives a malformed anomaly

    assert clock.delays == [1.5]


def test_throttle_dedup_is_per_session(clock):
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(loop_threshold=2, on_loop="throttle"), observers=[observer])

    loop_repeats(engine, session(), 3)
    loop_repeats(engine, SessionState("s2", loop_window=20), 3)

    assert len(observer.sent) == 2  # one per session


# --- escalate ---------------------------------------------------------------


def test_escalate_warns_below_the_hard_threshold_then_raises(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(loop_threshold=3, on_loop="escalate")  # hard defaults to 6
    engine = Engine(config)
    state = session()

    loop_repeats(engine, state, 5)  # counts 3, 4, 5 => warn phase
    assert "Loop detected" in caplog.text

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, tool_event(6))

    assert excinfo.value.anomaly.severity == "critical"
    assert excinfo.value.anomaly.details["count"] == 6


def test_escalate_warn_phase_anomalies_are_warn_severity():
    observer = RecordingObserver()
    config = GuardrailConfig(loop_threshold=3, on_loop="escalate")
    engine = Engine(config, observers=[observer])

    loop_repeats(engine, session(), 4)

    assert [a.severity for a in observer.sent] == ["warn"]


def test_escalate_honors_an_explicit_hard_threshold():
    config = GuardrailConfig(loop_threshold=2, loop_hard_threshold=4, on_loop="escalate")
    engine = Engine(config)
    state = session()

    loop_repeats(engine, state, 3)  # counts 2 and 3: warn phase, no raise

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, tool_event(4))

    assert excinfo.value.anomaly.details["count"] == 4


def test_escalate_overrides_the_callback_for_loops_but_not_for_other_detectors():
    seen: list[Anomaly] = []
    config = GuardrailConfig(
        loop_threshold=3,
        # Steps are model turns (T134): loop_repeats below pushes tool_call
        # events, which no longer count toward max_steps, so the single
        # llm_call turn after it must trip the threshold on its own.
        max_steps=0,
        on_loop="escalate",
        on_anomaly="callback",
        callback=seen.append,
    )
    engine = Engine(config)
    state = session()

    loop_repeats(engine, state, 3)  # loop warn phase: policy handles it
    assert seen == []

    engine.process(state, llm_event(4))  # steps anomaly: global callback still fires

    assert [a.detector for a in seen] == ["steps"]


# --- the other detectors are untouched --------------------------------------


def test_budget_velocity_and_steps_are_unchanged_when_on_loop_is_set(clock):
    config = GuardrailConfig(
        budget_usd=1.0,
        max_steps=5,
        tokens_per_minute_limit=10,
        on_anomaly="raise",
        on_loop="throttle",
    )
    engine = Engine(config)

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(session(), llm_event(1, tokens=100, cost=2.0))

    assert excinfo.value.anomaly.detector == "budget"
    assert clock.delays == []


def test_steps_still_raise_while_a_loop_is_being_throttled(clock):
    config = GuardrailConfig(max_steps=2, on_anomaly="raise", on_loop="throttle")
    engine = Engine(config)
    state = session()

    engine.process(state, llm_event(1))
    engine.process(state, llm_event(2))

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, llm_event(3))

    assert excinfo.value.anomaly.detector == "steps"


# --- end to end through the public API --------------------------------------


def test_end_to_end_throttled_tool_keeps_running(clock):
    runbound.init(loop_threshold=3, on_loop="throttle", throttle_base_seconds=1.0)
    calls: list[int] = []

    @runbound.tool
    def search(query: str) -> str:
        calls.append(1)
        return "result"

    for _ in range(5):
        assert search("same") == "result"

    assert len(calls) == 5
    assert clock.delays == [1.0, 2.0, 4.0]


def test_end_to_end_break_stops_the_tool_before_it_runs(clock):
    runbound.init(loop_threshold=3, on_anomaly="warn", on_loop="break")
    calls: list[int] = []

    @runbound.tool
    def search(query: str) -> str:
        calls.append(1)
        return "result"

    search("same")
    search("same")

    with pytest.raises(GuardrailTripped):
        search("same")

    assert len(calls) == 2  # the third call never executed


class TestLoopPolicyNeverShadowsOtherCriticals:
    """A co-firing non-loop critical must win over the loop policy.

    Fire-once detectors get no second chance: if the budget's raise were
    shadowed by a throttle sleep, the overspend would continue forever,
    merely slowed. Regression test for the EM-found edge case.
    """

    def test_nonloop_critical_beats_throttle(self, clock):
        from runbound.detectors import LoopDetector

        config = GuardrailConfig(on_anomaly="raise", on_loop="throttle", loop_threshold=3)
        config.validate()
        stub = StubDetector(None, name="budget")
        engine = Engine(
            config, detectors=[LoopDetector(), stub], observers=[RecordingObserver()]
        )
        state = session()
        # Two harmless repeats, then arm the stub so the 3rd repeat co-fires
        # loop (critical, throttle policy) and a non-loop critical.
        engine.process(state, tool_event(1))
        engine.process(state, tool_event(2))
        stub.anomaly = Anomaly(
            detector="budget", severity="critical", message="over budget", details={}
        )
        with pytest.raises(GuardrailTripped) as excinfo:
            engine.process(state, tool_event(3))
        assert excinfo.value.anomaly.detector == "budget"
        assert clock.delays == []  # the loop policy never got to sleep

    def test_loop_policy_still_applies_without_other_criticals(self, clock):
        config = GuardrailConfig(on_loop="throttle", loop_threshold=3)
        config.validate()
        engine = Engine(config, observers=[RecordingObserver()])
        state = session()
        loop_repeats(engine, state, 3)
        assert clock.delays == [pytest.approx(2.0)]
