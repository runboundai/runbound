"""Tests for the auto-expiring latch (``latch_ttl_seconds``).

A latch must not be *necessarily* permanent. A per-hour or per-day budget
should heal itself once the window has passed, and a false-positive spike trip
should not need a restart or a manual :func:`runbound.clear`. Without a ttl
the latch behaves exactly as it always has: permanent until cleared.

Healing gives the session another window, not a clean slate: detectors fire
once per session, so a healed session resumes but will not re-alert on the same
condition. :func:`runbound.clear` remains the way to re-arm them.
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


def test_entering_a_healed_session_no_longer_raises(clock):
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    spend_at_breach = api._REGISTRY["user:abuser"].total_cost_usd
    clock.advance(TTL + 1)

    body_ran = False
    with runbound.session("user:abuser"):  # entry-driven healing
        body_ran = True
        api._record_llm_call("gpt-4o", 2000, 500)

    assert body_ran is True
    assert api._REGISTRY["user:abuser"].total_cost_usd > spend_at_breach


def test_healing_happens_on_the_event_path_too_in_callback_mode(clock):
    """Only ``"raise"`` checks the latch at the door; every mode heals on an event."""
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
    chat()

    assert len(seen) == 2  # healed: no further re-application
    assert runbound.is_tripped("user:abuser") is None


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


def test_a_healed_session_keeps_its_counters_and_its_memoized_detectors(clock):
    """Another window, not a clean slate.

    The budget detector already fired for this session id, so it stays quiet
    even though the session is still over budget: the ttl buys the user a
    fresh window of service, and :func:`runbound.clear` is what re-arms the
    detectors so they can stop the same user again on their own merits.
    """
    runbound.init(budget_usd=BUDGET, on_anomaly="raise", latch_ttl_seconds=TTL)
    latch_the_key()
    tripped = api._REGISTRY["user:abuser"]
    over_budget = tripped.total_cost_usd
    assert over_budget > BUDGET

    clock.advance(TTL + 1)
    for _ in range(5):
        chat()  # none of these raise

    healed = api._REGISTRY["user:abuser"]
    assert healed is tripped  # the same session, not a new one
    assert healed.total_cost_usd > over_budget  # counters were never reset
    assert runbound.is_tripped("user:abuser") is None


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
