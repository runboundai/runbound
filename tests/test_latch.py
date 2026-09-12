"""Tests for the latch: once a session trips critically, it stays tripped.

The bug this closes: every detector fires once per
session, so a chatbot that catches ``GuardrailTripped`` and keeps serving let
a "blocked" user run 10x over budget. A latched session re-applies its stored
reaction on every later event, and — under ``on_anomaly="raise"`` — refuses
entry to :func:`runbound.session` before the host spends anything at all.
"""

import logging

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.pricing import estimate_cost
from runbound.state import SessionState

BUDGET = 0.02
MESSAGES = 20
CRITICAL = Anomaly("budget", "critical", "out of money", {})
WARN = Anomaly("velocity", "warn", "going fast", {})


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class StubDetector:
    """Returns a fixed anomaly (or None), counting how often it was asked."""

    def __init__(self, anomaly: Anomaly | None, name: str = "stub") -> None:
        self.anomaly = anomaly
        self.name = name
        self.checked = 0

    def check(self, state, event, config):
        self.checked += 1
        return self.anomaly


class RecordingObserver:
    """Remembers every anomaly it was told about."""

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []
        self.reactions: list[str] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)
        self.reactions.append(reacted)


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(
        kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash
    )


def llm_event(step: int) -> Event:
    return Event(kind="llm_call", ts=float(step), step=step)


def state(loop_window: int = 20) -> SessionState:
    return SessionState("s1", loop_window=loop_window)


def chat(key: str = "user:abuser") -> None:
    """One chatbot turn: enter the user's session, make one model call."""
    with runbound.session(key):
        api._record_llm_call("gpt-4o", 2000, 500)


def calls_that_fit(budget: float = BUDGET) -> tuple[int, float]:
    """How many $0.01 calls stay inside ``budget``, and what they cost.

    Accumulated exactly the way ``SessionState.record`` accumulates them, so
    the expected breach point is derived rather than guessed.
    """
    per_call = estimate_cost("gpt-4o", 2000, 500)
    fitting, total = 0, 0.0
    while total + per_call <= budget:
        total += per_call
        fitting += 1
    return fitting, total + per_call


# --- the B1 probe -----------------------------------------------------------


def test_a_blocked_user_is_blocked_on_every_later_message():
    """The original bug report, now with the expected answer.

    Before the latch: 1 trip, $0.20 spent on a $0.02 budget. After it: every
    message from the breach on is stopped, and spend freezes at the breach.
    """
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    fitting, spend_at_breach = calls_that_fit()
    assert fitting == 2  # $0.01 a call, so calls 1 and 2 are inside $0.02

    trips = 0
    for _ in range(MESSAGES):
        try:
            chat()
        except GuardrailTripped:
            trips += 1

    assert trips == MESSAGES - fitting == 18
    assert api._REGISTRY["user:abuser"].total_cost_usd == pytest.approx(spend_at_breach)
    assert api._REGISTRY["user:abuser"].total_cost_usd == pytest.approx(0.03)


def test_the_latched_session_stopped_counting_steps_after_the_breach():
    """Entry raises before the body, so no model call is ever recorded."""
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")

    for _ in range(MESSAGES):
        try:
            chat()
        except GuardrailTripped:
            pass

    assert api._REGISTRY["user:abuser"].step_count == 3  # the breaching call is the last


# --- entry raises before the host spends anything ---------------------------


def test_entering_a_latched_session_raises_before_the_body_runs():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(3):
        try:
            chat()
        except GuardrailTripped:
            pass

    body_ran = False
    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user:abuser"):
            body_ran = True

    assert body_ran is False
    assert caught.value.anomaly.detector == "budget"


def test_entry_is_untouched_for_other_keys():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(3):
        try:
            chat()
        except GuardrailTripped:
            pass

    with runbound.session("user:innocent") as other:  # no raise
        api._record_llm_call("gpt-4o", 2000, 500)

    assert other.step_count == 1


def test_entry_does_not_raise_in_callback_mode():
    seen: list[Anomaly] = []
    runbound.init(budget_usd=BUDGET, on_anomaly="callback", callback=seen.append)

    for _ in range(4):
        chat()  # never raises in callback mode

    assert [a.detector for a in seen] == ["budget"] * 2  # calls 3 and 4


def test_entry_does_not_raise_in_warn_mode():
    runbound.init(budget_usd=BUDGET, on_anomaly="warn")

    for _ in range(MESSAGES):
        chat()

    assert api.is_tripped("user:abuser") is None


# --- re-applying the stored reaction ----------------------------------------


def test_callback_mode_re_invokes_the_callback_on_every_later_event():
    seen: list[Anomaly] = []
    config = GuardrailConfig(on_anomaly="callback", callback=seen.append)
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])
    session = state()

    for step in range(1, 12):
        engine.process(session, tool_event(step))

    assert seen == [CRITICAL] * 11
    assert session.tripped_by is CRITICAL


def test_a_callback_that_raises_during_re_application_is_swallowed(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")

    def callback(anomaly):
        raise RuntimeError("user callback is broken")

    config = GuardrailConfig(on_anomaly="callback", callback=callback)
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])
    session = state()

    engine.process(session, tool_event(1))
    engine.process(session, tool_event(2))  # fail-open: the host survives

    assert "callback" in caplog.text


def test_warn_mode_never_latches():
    config = GuardrailConfig(on_anomaly="warn")
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])
    session = state()

    for step in range(1, 6):
        engine.process(session, tool_event(step))

    assert session.tripped_by is None


def test_a_warn_severity_anomaly_never_latches():
    config = GuardrailConfig(on_anomaly="raise")
    engine = Engine(config, detectors=[StubDetector(WARN)])
    session = state()

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(1))

    assert session.tripped_by is None


def test_the_first_anomaly_is_the_one_that_is_kept():
    """The latch records why the session was stopped, not what came later."""
    later = Anomaly("steps", "critical", "too many steps", {})
    detector = StubDetector(CRITICAL)
    config = GuardrailConfig(on_anomaly="callback", callback=lambda a: None)
    engine = Engine(config, detectors=[detector])
    session = state()

    engine.process(session, tool_event(1))
    detector.anomaly = later
    engine.process(session, tool_event(2))

    assert session.tripped_by is CRITICAL


def test_a_latched_session_skips_detection_entirely():
    detector = StubDetector(CRITICAL)
    config = GuardrailConfig(on_anomaly="callback", callback=lambda a: None)
    engine = Engine(config, detectors=[detector])
    session = state()

    for step in range(1, 6):
        engine.process(session, tool_event(step))

    assert detector.checked == 1  # only the event that latched was ever detected


def test_a_latched_session_still_records_its_events():
    config = GuardrailConfig(on_anomaly="callback", callback=lambda a: None)
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])
    session = state()

    engine.process(session, tool_event(1))
    engine.process(session, tool_event(2))

    assert session.step_count == 2


def test_re_application_never_re_alerts():
    observer = RecordingObserver()
    config = GuardrailConfig(on_anomaly="callback", callback=lambda a: None)
    engine = Engine(config, detectors=[StubDetector(CRITICAL)], observers=[observer])
    session = state()

    for step in range(1, 12):
        engine.process(session, tool_event(step))

    # One page for the trip itself, one "blocked" notice for the wall the
    # rest of the retries are served (Engine._reapply dedupes that to a
    # single notification too) — never once per retry, and never eleven.
    assert observer.sent == [CRITICAL, CRITICAL]
    assert observer.reactions == ["callback", "blocked"]


# --- the loop and spike paths -----------------------------------------------


def test_loop_break_latches_and_keeps_breaking():
    config = GuardrailConfig(loop_threshold=3, on_loop="break", on_anomaly="raise")
    engine = Engine(config)
    session = state()

    for step in (1, 2):
        engine.process(session, tool_event(step))
    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(3))

    assert session.tripped_by is not None
    with pytest.raises(GuardrailTripped) as caught:
        engine.process(session, llm_event(4))  # a different action, still stopped

    assert caught.value.anomaly.detector == "loop"


def test_loop_throttle_never_latches(monkeypatch):
    monkeypatch.setattr("runbound.engine.time", _NoSleep())
    config = GuardrailConfig(loop_threshold=2, on_loop="throttle")
    engine = Engine(config)
    session = state()

    for step in range(1, 6):
        engine.process(session, tool_event(step))

    assert session.tripped_by is None


class _NoSleep:
    def sleep(self, seconds: float) -> None:
        pass


def test_escalate_latches_only_once_it_turns_critical():
    config = GuardrailConfig(loop_threshold=3, on_loop="escalate", on_anomaly="raise")
    engine = Engine(config)
    session = state()

    for step in range(1, 6):  # counts 3, 4, 5: the warn phase
        engine.process(session, tool_event(step))
    assert session.tripped_by is None

    with pytest.raises(GuardrailTripped):
        engine.process(session, tool_event(6))

    assert session.tripped_by.detector == "loop"


def test_a_warn_phase_spike_never_latches():
    config = GuardrailConfig(on_anomaly="raise", spike_warmup_calls=2, spike_window=3)
    engine = Engine(config)
    session = SessionState("s1", spike_window=3)

    for step in range(1, 4):
        engine.process(
            session, Event(kind="llm_call", ts=float(step), step=step, duration_s=1.0)
        )
    engine.process(session, Event(kind="llm_call", ts=4.0, step=4, duration_s=40.0))

    assert session.tripped_by is None


def test_a_tripping_spike_latches():
    config = GuardrailConfig(
        on_anomaly="raise", on_spike="trip", spike_warmup_calls=2, spike_window=3, spike_confirm=1
    )
    engine = Engine(config)
    session = SessionState("s1", spike_window=3)

    for step in range(1, 4):
        engine.process(
            session, Event(kind="llm_call", ts=float(step), step=step, duration_s=1.0)
        )
    with pytest.raises(GuardrailTripped):
        engine.process(session, Event(kind="llm_call", ts=4.0, step=4, duration_s=40.0))

    assert session.tripped_by.detector == "spike"


# --- is_tripped -------------------------------------------------------------


def test_is_tripped_reports_the_anomaly_for_a_latched_key():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(3):
        try:
            chat()
        except GuardrailTripped:
            pass

    anomaly = runbound.is_tripped("user:abuser")

    assert anomaly is not None
    assert anomaly.detector == "budget"
    assert anomaly.details["limit_hit"] == "budget_usd"


def test_is_tripped_is_none_for_a_healthy_key_and_creates_no_session():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    with runbound.session("user:known"):
        pass
    before = list(api._REGISTRY)

    assert runbound.is_tripped("user:known") is None
    assert runbound.is_tripped("user:never-seen") is None
    assert list(api._REGISTRY) == before


def test_is_tripped_without_a_key_reads_the_current_session():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(3):
        try:
            chat()
        except GuardrailTripped:
            pass

    assert runbound.is_tripped() is None  # outside a block: the default session

    with runbound.session("user:innocent"):
        assert runbound.is_tripped() is None


def test_is_tripped_without_a_key_reports_the_default_session():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")

    api._record_llm_call("gpt-4o", 2000, 500)
    api._record_llm_call("gpt-4o", 2000, 500)
    with pytest.raises(GuardrailTripped):
        api._record_llm_call("gpt-4o", 2000, 500)

    assert runbound.is_tripped().detector == "budget"


def test_is_tripped_is_inert_before_init():
    assert runbound.is_tripped() is None
    assert runbound.is_tripped("anything") is None


def test_is_tripped_survives_a_broken_registry(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()
    monkeypatch.setattr(api, "_REGISTRY", _BrokenRegistry())

    assert runbound.is_tripped("k") is None  # fail-open
    assert "runbound" in caplog.text


class _BrokenRegistry:
    """Answers ``get``/``pop`` — the calls these two tests exercise — with a
    blown-up registry. ``clear()`` is a real no-op rather than another raise:
    it exists only so the autouse ``_uninitialized`` fixture's own teardown
    (``_forget_sessions()``, unrelated to what is under test here) can finish
    once ``monkeypatch`` hands ``api._REGISTRY`` back at the end of the test,
    instead of the pre-existing fixture-ordering gap turning this test's own
    teardown into a failure that has nothing to do with is_tripped/clear.
    """

    def get(self, *args, **kwargs):
        raise RuntimeError("registry is broken")

    def pop(self, *args, **kwargs):
        raise RuntimeError("registry is broken")

    def clear(self) -> None:
        pass


# --- clear ------------------------------------------------------------------


def test_clear_un_blocks_the_key_with_a_fresh_budget():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(MESSAGES):
        try:
            chat()
        except GuardrailTripped:
            pass
    tripped = api._REGISTRY["user:abuser"]

    runbound.clear("user:abuser")

    assert runbound.is_tripped("user:abuser") is None
    with runbound.session("user:abuser") as fresh:
        api._record_llm_call("gpt-4o", 2000, 500)

    assert fresh is not tripped
    assert fresh.session_id != tripped.session_id
    assert fresh.total_cost_usd == pytest.approx(0.01)


def test_clear_re_arms_the_detectors_so_the_key_can_trip_again():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(5):
        try:
            chat()
        except GuardrailTripped:
            pass

    runbound.clear("user:abuser")

    trips = 0
    for _ in range(5):
        try:
            chat()
        except GuardrailTripped:
            trips += 1

    assert trips == 3  # a brand-new latch, from the same breach point
    assert runbound.is_tripped("user:abuser").detector == "budget"


def test_clear_leaves_other_keys_and_the_default_session_alone():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    with runbound.session("user:other") as other:
        api._record_llm_call("gpt-4o", 2000, 500)
    default = runbound.current_session()

    runbound.clear("user:abuser")  # never seen: a no-op

    assert list(api._REGISTRY) == ["user:other"]
    assert runbound.current_session() is default
    with runbound.session("user:other") as again:
        assert again is other


def test_clear_is_inert_before_init():
    runbound.clear("k")  # must not raise

    assert api._REGISTRY == {}


def test_clear_survives_a_broken_registry(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init()
    monkeypatch.setattr(api, "_REGISTRY", _BrokenRegistry())

    runbound.clear("k")  # fail-open: the host survives

    assert "runbound" in caplog.text


# --- reset ------------------------------------------------------------------


def test_reset_clears_the_default_sessions_latch():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    api._record_llm_call("gpt-4o", 2000, 500)
    api._record_llm_call("gpt-4o", 2000, 500)
    with pytest.raises(GuardrailTripped):
        api._record_llm_call("gpt-4o", 2000, 500)

    runbound.reset()

    assert runbound.is_tripped() is None
    api._record_llm_call("gpt-4o", 2000, 500)  # and the guard works again


def test_reset_forgets_every_keyed_latch():
    runbound.init(budget_usd=BUDGET, on_anomaly="raise")
    for _ in range(3):
        try:
            chat()
        except GuardrailTripped:
            pass

    runbound.reset()

    assert runbound.is_tripped("user:abuser") is None
    with runbound.session("user:abuser"):  # entry no longer raises
        pass


# --- state ------------------------------------------------------------------


def test_tripped_by_defaults_to_none():
    assert SessionState("s").tripped_by is None


def test_the_latch_helpers_are_exported():
    assert runbound.is_tripped is api.is_tripped
    assert runbound.clear is api.clear
    assert {"is_tripped", "clear"} <= set(runbound.__all__)
