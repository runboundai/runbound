"""Tests for Wave 24, T59c: throttling a loop under a running event loop.

``on_loop="throttle"`` used to warn once and skip entirely under asyncio,
because ``time.sleep`` would stall every other request this worker is
serving. Now the engine stashes the delay in a contextvar
(``engine.take_pending_delay`` / ``hooks.take_pending_delay``) instead, and an
async caller — here, the ``@runbound.tool`` async wrapper — retrieves it
and ``await asyncio.sleep``s it itself, without blocking anyone else. The
synchronous path (no running loop) is untested here; it is unchanged and
covered by ``test_loop_policy.py``.
"""

import asyncio
import logging
import time

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.state import SessionState


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- engine.take_pending_delay: the low-level contract -----------------------


def test_take_pending_delay_is_zero_with_nothing_pending():
    assert engine_module.take_pending_delay() == 0.0


def test_take_pending_delay_returns_it_once_then_zero():
    async def drive():
        engine_module._PENDING_DELAY.set((1.5, engine_module._monotonic()))
        first = engine_module.take_pending_delay()
        second = engine_module.take_pending_delay()
        return first, second

    first, second = asyncio.run(drive())
    assert first == 1.5
    assert second == 0.0


def test_pending_delay_is_scoped_per_task():
    """Two tasks throttled in the same tick never see each other's delay."""

    async def one():
        engine_module._PENDING_DELAY.set((1.0, engine_module._monotonic()))
        await asyncio.sleep(0)
        return engine_module.take_pending_delay()

    async def two():
        await asyncio.sleep(0)
        return engine_module.take_pending_delay()  # nothing was ever set here

    async def drive():
        return await asyncio.gather(one(), two())

    first, second = asyncio.run(drive())
    assert first == 1.0
    assert second == 0.0


# --- _Hooks.take_pending_delay: fail-open before init and on error ----------


def test_hooks_take_pending_delay_is_zero_before_init():
    assert api._HOOKS.take_pending_delay() == 0.0


def test_hooks_take_pending_delay_reads_the_engine_value():
    runbound.init()

    async def drive():
        engine_module._PENDING_DELAY.set((0.25, engine_module._monotonic()))
        return api._HOOKS.take_pending_delay()

    assert asyncio.run(drive()) == 0.25


# --- the engine no longer warns-and-skips under a running loop --------------


def test_throttle_under_a_running_loop_sets_the_delay_and_logs_nothing(caplog):
    engine = Engine(
        GuardrailConfig(loop_threshold=2, on_loop="throttle", throttle_base_seconds=3.0),
    )
    session = SessionState("s1")

    def tool_event(step):
        from runbound.events import Event

        return Event(
            kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash="h1"
        )

    async def drive():
        engine.process(session, tool_event(1))
        engine.process(session, tool_event(2))  # the repeat that trips the loop
        return engine_module.take_pending_delay()

    with caplog.at_level(logging.WARNING, logger="runbound"):
        delay = asyncio.run(drive())

    assert delay == pytest.approx(3.0)
    assert not _warnings(caplog)


# --- end to end: an async @runbound.tool actually delays ------------------


BASE_DELAY = 0.05


def test_async_tool_under_throttle_actually_delays(caplog):
    runbound.init(loop_threshold=2, on_loop="throttle", throttle_base_seconds=BASE_DELAY)

    @runbound.tool
    async def search(query):
        return "result"

    async def drive():
        await search("weather")  # first call: no repeat yet, no delay
        start = time.monotonic()
        await search("weather")  # second: trips the loop, throttled
        return time.monotonic() - start

    with caplog.at_level(logging.WARNING, logger="runbound"):
        elapsed = asyncio.run(drive())

    assert elapsed >= BASE_DELAY * 0.9  # real delay, not a no-op
    assert not _warnings(caplog)  # no "disabled under asyncio" warning


def test_async_tool_under_throttle_never_raises_and_keeps_running():
    runbound.init(
        loop_threshold=2, on_loop="throttle", throttle_base_seconds=0.01, on_anomaly="raise"
    )

    @runbound.tool
    async def search(query):
        return "result"

    async def drive():
        results = []
        for _ in range(5):
            results.append(await search("weather"))
        return results

    results = asyncio.run(drive())
    assert results == ["result"] * 5
    assert runbound.current_session().tripped_by is None


def test_sync_tool_off_the_loop_still_uses_time_sleep(monkeypatch):
    """Sanity check: the synchronous path is unaffected by this change."""
    delays = []
    monkeypatch.setattr(engine_module.time, "sleep", lambda s: delays.append(s))
    runbound.init(loop_threshold=2, on_loop="throttle", throttle_base_seconds=0.01)

    @runbound.tool
    def search(query):
        return "result"

    search("weather")
    search("weather")

    assert delays  # time.sleep was actually invoked, outside any event loop
