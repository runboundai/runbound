"""Tests for the Wave 24 T59 review fixes (SHOULD 5, 6, 8, 9 + NIT d).

An outside review of the T59 diff found five things worth a test each:

- SHOULD 5: the throttle delay stashed under a running event loop
  (``_PENDING_DELAY``) leaked into an unrelated child task when a *sync*
  ``@runbound.tool`` tripped it — a sync wrapper has no way to await the
  delay, and used to leave it stashed for whoever asked next. Fixed two ways:
  the sync wrapper now discards it right away, and a value nobody collected
  within a second is dropped by ``take_pending_delay`` itself as a backstop.
- SHOULD 6: an ``on_plane_loss="refuse"`` outage alerted once *per session*
  instead of once *per outage*, because the engine's alert-dedup key still
  included the session id for detector ``"plane"``.
- SHOULD 8: nothing surfaced the fact that some of a session's spend was a
  guess (``priced == "estimated"``); ``SessionState.estimated_cost_usd`` and
  ``BudgetDetector``'s ``details["estimated_cost_usd"]`` fix that.
- SHOULD 9: pin that an unpriced refusal at the door is a true no-op on
  everything downstream of it — no in-flight slot taken, no provider circuit
  failure recorded.
- NIT (d): ``responses.BUILTIN`` gained a ``"plane"`` entry so a plane-loss
  refusal resolves to product copy instead of the generic 429 default.
"""

import asyncio

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound.config import GuardrailConfig
from runbound.detectors import BudgetDetector
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class RecordingObserver:
    def __init__(self) -> None:
        self.sent = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


# --- SHOULD 5: the throttle delay must not leak into an unrelated task ------


def test_sync_tool_throttle_inside_a_coroutine_leaves_nothing_for_a_later_task():
    """A sync tool tripped on the event-loop thread cannot await the delay
    the engine stashes, so its wrapper must consume and discard it right
    away — not leave it in the contextvar for a task spawned afterward to
    inherit and mistakenly sleep."""
    runbound.init(loop_threshold=2, on_loop="throttle", throttle_base_seconds=5.0)

    @runbound.tool
    def poll(x):
        return "ok"

    async def unrelated_child():
        # A child task spawned after the sync tool tripped throttle: it never
        # asked for anything and must see nothing pending.
        return engine_module.take_pending_delay()

    async def drive():
        poll("a")
        poll("a")  # trips the loop detector; on this thread _in_event_loop() is True
        task = asyncio.create_task(unrelated_child())
        return await task

    leaked = asyncio.run(drive())
    assert leaked == 0.0


def test_sync_tool_wrapper_takes_the_delay_off_the_loop_thread_too(monkeypatch):
    """Off the event loop, the engine never stashes anything (it time.sleeps
    directly), so the sync wrapper's new discard call is a harmless no-op —
    the tool must still work and still eventually throttle via time.sleep."""
    delays = []
    monkeypatch.setattr(engine_module.time, "sleep", lambda s: delays.append(s))
    runbound.init(loop_threshold=2, on_loop="throttle", throttle_base_seconds=0.01)

    @runbound.tool
    def poll(x):
        return "ok"

    assert poll("a") == "ok"
    assert poll("a") == "ok"

    assert delays  # the synchronous throttle path still ran


def test_stale_pending_delay_is_dropped_instead_of_leaking_forever():
    """A value nobody collected within a second is a leak, not a debt owed to
    whoever asks next — the backstop half of the SHOULD 5 fix."""

    async def drive():
        stale_set_at = engine_module._monotonic() - 1.5
        engine_module._PENDING_DELAY.set((5.0, stale_set_at))
        return engine_module.take_pending_delay()

    assert asyncio.run(drive()) == 0.0


def test_fresh_pending_delay_within_the_grace_window_is_still_honored():
    async def drive():
        engine_module._PENDING_DELAY.set((5.0, engine_module._monotonic()))
        return engine_module.take_pending_delay()

    assert asyncio.run(drive()) == 5.0


# --- SHOULD 6: a plane-loss outage alerts once, not once per session --------


def test_plane_loss_refusals_for_two_sessions_alert_only_once():
    runbound.init()
    observer = RecordingObserver()
    api._ENGINE.observers.append(observer)

    session_a = SessionState("session-a")
    session_b = SessionState("session-b")

    def plane_loss_anomaly() -> Anomaly:
        # Shaped exactly like runbound.shared.RemoteState._plane_loss_refusal.
        return Anomaly(
            detector="plane",
            severity="critical",
            message=(
                "Control plane unreachable; refusing session entry "
                "(on_plane_loss='refuse')"
            ),
            details={"reason": "plane_unreachable", "mode": "timeout"},
        )

    api._ENGINE.notify_door(session_a, plane_loss_anomaly())
    api._ENGINE.notify_door(session_b, plane_loss_anomaly())

    assert len(observer.sent) == 1


def test_plane_loss_refusals_with_different_reasons_are_two_incidents():
    runbound.init()
    observer = RecordingObserver()
    api._ENGINE.observers.append(observer)

    session = SessionState("session-a")
    timeout = Anomaly(
        detector="plane",
        severity="critical",
        message="unreachable",
        details={"reason": "plane_unreachable", "mode": "timeout"},
    )
    degraded = Anomaly(
        detector="plane",
        severity="critical",
        message="degraded",
        details={"reason": "plane_degraded", "mode": "degraded"},
    )

    api._ENGINE.notify_door(session, timeout)
    api._ENGINE.notify_door(session, degraded)

    assert len(observer.sent) == 2


# --- SHOULD 8: estimated_cost_usd -------------------------------------------


def test_session_state_accumulates_estimated_cost_only_for_estimated_events():
    state = SessionState("s1")
    state.record(
        Event(kind="llm_call", ts=1.0, step=1, cost_usd=2.0, priced="estimated")
    )
    state.record(Event(kind="llm_call", ts=2.0, step=2, cost_usd=3.0))  # exactly priced

    assert state.estimated_cost_usd == pytest.approx(2.0)
    assert state.total_cost_usd == pytest.approx(5.0)


def test_session_state_estimated_cost_starts_at_zero():
    assert SessionState("s1").estimated_cost_usd == 0.0


def test_budget_detail_omits_estimated_cost_when_zero():
    config = GuardrailConfig(budget_usd=1.0)
    state = SessionState("s1")
    event = Event(kind="llm_call", ts=1.0, step=1, cost_usd=1.5)
    state.record(event)

    anomaly = BudgetDetector().check(state, event, config)

    assert anomaly is not None
    assert "estimated_cost_usd" not in anomaly.details


def test_budget_detail_carries_estimated_cost_when_positive():
    config = GuardrailConfig(budget_usd=1.0)
    state = SessionState("s1")
    event = Event(kind="llm_call", ts=1.0, step=1, cost_usd=1.5, priced="estimated")
    state.record(event)

    anomaly = BudgetDetector().check(state, event, config)

    assert anomaly is not None
    assert anomaly.details["estimated_cost_usd"] == pytest.approx(1.5)


def test_end_to_end_record_call_under_estimate_mode_raises_the_sessions_estimated_cost():
    runbound.init(on_unpriced_model="estimate", unpriced_price_per_1m_usd=(1.0, 2.0))

    runbound.record_call("mystery-model", 1_000_000, 1_000_000)

    session = runbound.current_session()
    assert session.estimated_cost_usd == pytest.approx(3.0)
    assert session.total_cost_usd == pytest.approx(3.0)


# --- SHOULD 9: an unpriced refusal at the door is a true no-op downstream ---


def test_unpriced_refusal_takes_no_inflight_slot():
    runbound.init(on_unpriced_model="refuse", max_inflight_calls=1)

    with pytest.raises(GuardrailTripped):
        api._HOOKS.before("openai", "mystery-model")

    assert runbound.inflight_calls("openai") == 0
    # The slot is still fully available to a real, priced call.
    api._HOOKS.before("openai", "gpt-4o")
    assert runbound.inflight_calls("openai") == 1


def test_unpriced_refusal_records_no_provider_circuit_failure():
    runbound.init(on_unpriced_model="refuse")

    for _ in range(5):
        with pytest.raises(GuardrailTripped):
            api._HOOKS.before("openai", "mystery-model")

    assert runbound.circuit_state("openai") == "closed"


# --- NIT (d): responses.BUILTIN carries a "plane" entry ---------------------


def test_plane_detector_refusal_resolves_to_503():
    anomaly = Anomaly(
        detector="plane",
        severity="critical",
        message="Control plane unreachable",
        details={"reason": "plane_unreachable"},
    )

    assert GuardrailTripped(anomaly).refusal.status == 503
