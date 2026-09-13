"""Tests for the auto-expiring latch (``latch_ttl_seconds``).

A latch must not be *necessarily* permanent. A false-positive spike trip
should not need a restart or a manual :func:`runbound.clear`. Without a ttl
the latch behaves exactly as it always has: permanent until cleared.

Healing is re-admission, not a clean slate (T137): the engine rearms every
detector for this session id, but no counter is reset — a session still over
budget when the ttl elapses is over budget on its very next event, and
re-trips immediately, with the same detector. ``latch_ttl_seconds`` is not a
windowed budget; only :func:`runbound.clear` resets the counters themselves.
"""

import logging

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound.config import GuardrailConfig
from runbound.detectors import BudgetDetector, LoopDetector
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

TTL = 60.0
BUDGET = 0.02
CRITICAL = Anomaly("budget", "critical", "out of money", {})


class FakeClock:
    """Stands in for the engine's ``time`` module; moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class OnceDetector:
    """Fires its anomaly the first time it is asked, like the real ones."""

    name = "stub"

    def __init__(self, anomaly: Anomaly | None = CRITICAL) -> None:
        self.anomaly = anomaly
        self.checked = 0

    def check(self, state, event, config):
        self.checked += 1
        return self.anomaly if self.checked == 1 else None


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
        kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash
    )


def engine_for(ttl: float | None, detector: OnceDetector) -> Engine:
    config = GuardrailConfig(on_anomaly="raise", latch_ttl_seconds=ttl)
    config.validate()
    return Engine(config, detectors=[detector])


def chat(key: str = "user:abuser") -> None:
    """One chatbot turn: enter the user's session, make one model call."""
    with runbound.session(key):
        api._record_llm_call("gpt-4o", 2000, 500)


def latch_the_key(key: str = "user:abuser", turns: int = 3) -> None:
    """Spend past the budget so ``key`` ends up latched."""
    for _ in range(turns):
        try:
            chat(key)
        except GuardrailTripped:
            pass


# --- no ttl: the latch is permanent, as before -------------------------------


def test_the_default_is_no_ttl():
    assert GuardrailConfig().latch_ttl_seconds is None


def test_without_a_ttl_the_latch_never_expires(clock):
    detector = OnceDetector()
    engine = engine_for(None, detector)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    clock.advance(365 * 24 * 3600.0)  # a year later

    for step in range(2, 52):
        with pytest.raises(GuardrailTripped):
            engine.process(session, tool_event(step))

    assert detector.checked == 1  # never detected again
    assert session.tripped_by is CRITICAL


def test_without_a_ttl_entry_still_refuses_however_long_it_has_been(clock):
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    latch_the_key()
    clock.advance(365 * 24 * 3600.0)

    with pytest.raises(GuardrailTripped):
        with runbound.session("user:abuser"):
            pass

    assert runbound.is_tripped("user:abuser").detector == "budget"


# --- inside the window: nothing changes --------------------------------------


def test_inside_the_window_the_latch_holds(clock):
    engine = engine_for(TTL, OnceDetector())
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    clock.advance(TTL - 1)

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(2))

    assert session.tripped_by is CRITICAL


def test_exactly_at_the_ttl_the_latch_still_holds(clock):
    """The boundary: the latch expires *after* the window, not at it."""
    engine = engine_for(TTL, OnceDetector())
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    clock.advance(TTL)

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(2))

    assert session.tripped_by is CRITICAL


def test_entry_still_refuses_inside_the_window(clock):
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    clock.advance(TTL - 1)

    with pytest.raises(GuardrailTripped):
        with runbound.session("user:abuser"):
            pass


# --- past the window: the session heals --------------------------------------


def test_an_event_past_the_window_heals_the_session(clock):
    detector = OnceDetector()
    engine = engine_for(TTL, detector)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    clock.advance(TTL + 1)

    engine.process(session, tool_event(2))  # no raise: healed

    assert session.tripped_by is None
    assert session.tripped_at is None
    assert detector.checked == 2  # detection runs again


def test_healing_is_logged_at_info(clock, caplog):
    caplog.set_level(logging.INFO, logger="runbound")
    engine = engine_for(TTL, OnceDetector())
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    clock.advance(TTL + 1)
    engine.process(session, tool_event(2))

    assert "latch expired" in caplog.text
    assert "s1" in caplog.text


def test_entering_a_healed_session_no_longer_raises_but_its_own_event_does(clock):
    """The door heals silently (T137): a still-over-budget session re-trips
    on its own first event afterward, not at entry — entry has no event of
    its own to judge, only the body does.
    """
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    spend_at_breach = api._REGISTRY["user:abuser"].total_cost_usd
    clock.advance(TTL + 1)

    entered = False
    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("user:abuser"):  # entry-driven healing: no raise here
            entered = True
            api._record_llm_call("gpt-4o", 2000, 500)  # still over budget: re-trips

    assert entered is True
    assert excinfo.value.anomaly.detector == "budget"
    assert api._REGISTRY["user:abuser"].total_cost_usd > spend_at_breach


def test_healing_happens_on_the_event_path_too_in_callback_mode(clock):
    """Every mode heals on an event (not just ``"raise"`` at the door), and a
    session still over budget re-trips immediately once healed (T137).
    """
    seen: list[Anomaly] = []
    runbound.init(
        budget_usd=BUDGET,
        on_anomaly="callback",
        callback=seen.append,
        latch_ttl_seconds=TTL,
    )
    for _ in range(4):
        chat()  # callback mode never raises
    assert len(seen) == 2  # the breaching call and one re-application

    clock.advance(TTL + 1)
    chat()  # healed, then re-trips on this same call: still over budget

    assert len(seen) == 3  # the healed session's own new trip
    assert runbound.is_tripped("user:abuser").detector == "budget"


def test_is_tripped_reports_a_healed_session_as_running(clock):
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    assert runbound.is_tripped("user:abuser").detector == "budget"

    clock.advance(TTL + 1)

    assert runbound.is_tripped("user:abuser") is None


def test_is_tripped_without_a_key_heals_the_default_session(clock):
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    api._record_llm_call("gpt-4o", 2000, 500)
    api._record_llm_call("gpt-4o", 2000, 500)
    with pytest.raises(GuardrailTripped):
        api._record_llm_call("gpt-4o", 2000, 500)

    clock.advance(TTL + 1)

    assert runbound.is_tripped() is None


# --- what healing does and does not undo -------------------------------------


def test_a_healed_session_keeps_its_counters_but_rearms_its_detectors(clock):
    """Another window, not a clean slate (T137).

    A heal never resets a counter — only :func:`runbound.clear` does that —
    but it does rearm every detector, so a session still over budget re-trips
    on its very next event, with the same detector: the wall
    ``latch_ttl_seconds`` promises, not a session that runs free until
    someone calls ``clear()``.
    """
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    tripped = api._REGISTRY["user:abuser"]
    over_budget = tripped.total_cost_usd
    assert over_budget > BUDGET

    clock.advance(TTL + 1)
    with pytest.raises(GuardrailTripped) as excinfo:
        chat()  # healed, then re-trips on this same call: still over budget

    assert excinfo.value.anomaly.detector == "budget"
    healed = api._REGISTRY["user:abuser"]
    assert healed is tripped  # the same session, not a new one
    assert healed.total_cost_usd > over_budget  # counters were never reset
    assert runbound.is_tripped("user:abuser").detector == "budget"  # re-latched


def test_clear_still_works_independently_of_the_ttl(clock):
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    tripped = api._REGISTRY["user:abuser"]

    runbound.clear("user:abuser")  # inside the window: forgiven anyway

    assert runbound.is_tripped("user:abuser") is None
    with runbound.session("user:abuser") as fresh:
        api._record_llm_call("gpt-4o", 2000, 500)

    assert fresh is not tripped
    assert fresh.total_cost_usd == pytest.approx(0.01)


def test_a_healed_session_can_latch_again_on_a_new_anomaly(clock):
    """Healing arms the latch itself again, even if detectors stay memoized."""
    detector = OnceDetector()
    engine = engine_for(TTL, detector)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    clock.advance(TTL + 1)
    engine.process(session, tool_event(2))  # healed
    later = Anomaly("steps", "critical", "too many steps", {})
    detector.checked = 0
    detector.anomaly = later

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(3))

    assert session.tripped_by is later
    assert session.tripped_at == pytest.approx(clock.now)


# --- state -------------------------------------------------------------------


def test_tripped_at_defaults_to_none():
    assert SessionState("s").tripped_at is None


def test_latching_stamps_the_monotonic_time(clock):
    engine = engine_for(TTL, OnceDetector())
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))

    assert session.tripped_at == pytest.approx(clock.now)


# --- fail-open ---------------------------------------------------------------


def test_a_clock_that_cannot_be_read_leaves_the_latch_permanent(monkeypatch):
    """The host survives a broken clock; the latch simply never expires."""
    monkeypatch.setattr(engine_module, "time", _BrokenClock())
    engine = engine_for(TTL, OnceDetector())
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))
    assert session.tripped_at is None

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(2))

    assert session.tripped_by is CRITICAL


class _BrokenClock:
    def monotonic(self):
        raise RuntimeError("no clock here")

    def sleep(self, seconds: float) -> None:
        pass


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_a_non_positive_ttl_is_rejected(value):
    with pytest.raises(ValueError, match="latch_ttl_seconds"):
        GuardrailConfig(latch_ttl_seconds=value).validate()


@pytest.mark.parametrize("value", [None, 0.5, 3600.0])
def test_a_positive_ttl_or_none_is_accepted(value):
    GuardrailConfig(latch_ttl_seconds=value).validate()  # must not raise


def test_init_rejects_a_non_positive_ttl():
    with pytest.raises(ValueError, match="latch_ttl_seconds"):
        runbound.init(latch_ttl_seconds=0)


# --- T137 acceptance, stated with the task's own numbers ---------------------


def test_an_over_budget_session_with_ttl_1_retrips_on_its_next_event(clock):
    """``latch_ttl_seconds=1``: an over-budget session re-trips, same detector."""
    config = GuardrailConfig(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=1.0)
    config.validate()
    engine = Engine(config, detectors=[BudgetDetector()])
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped) as first:
        engine.process(session, llm_call_event(1, cost=BUDGET + 0.01))
    assert first.value.anomaly.detector == "budget"

    clock.advance(1.01)

    with pytest.raises(GuardrailTripped) as second:
        engine.process(session, llm_call_event(2, ts=2.0, cost=0.0))

    assert second.value.anomaly.detector == "budget"  # the same detector
    assert session.total_cost_usd == pytest.approx(BUDGET + 0.01)  # never reset


def test_a_loop_latch_heals_and_a_new_repeat_retrips(clock):
    """A loop latch heals (T137) and a *new* repeat of the same call re-trips."""
    config = GuardrailConfig(on_anomaly="raise", loop_threshold=3, latch_ttl_seconds=1.0)
    config.validate()
    engine = Engine(config, detectors=[LoopDetector()])
    session = SessionState("s1")

    for step in (1, 2):
        engine.process(session, tool_event(step))
    with pytest.raises(GuardrailTripped) as first:
        engine.process(session, tool_event(3))  # the third repeat trips it
    assert first.value.anomaly.detector == "loop"

    clock.advance(1.01)

    with pytest.raises(GuardrailTripped) as second:
        engine.process(session, tool_event(4))  # one more repeat: re-trips

    assert second.value.anomaly.detector == "loop"
    assert session.tripped_by is second.value.anomaly


def llm_call_event(step: int, ts: float | None = None, cost: float = 0.0) -> Event:
    return Event(
        kind="llm_call", ts=float(ts if ts is not None else step), step=step, cost_usd=cost
    )
