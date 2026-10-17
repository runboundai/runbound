"""An async tool whose approval is asked by the SERVICE DEFAULTS keeps the event loop free while it waits.

The pre-chain of a call that may wait on the plane (an approval, a quota take) runs off the loop. A dashboard row that asks was always covered;
a tool with no row that the service defaults ask for (a destructive-only or unclassified tool) was not, so the wait froze every other
coroutine in the process (a plain worker's health route, a server's other requests).
"""

import asyncio
import time

import pytest

import runbound
from runbound import api
from test_policy_rows import row, start
from test_service_defaults import Shared as DefaultsShared
from test_service_defaults import served

WAIT_S = 0.6


class SlowAsk(DefaultsShared):
    """The plane answers an approval request only after ``WAIT_S``, in a blocking call, as the real HTTP client does."""

    def approvals(self, method, path, body=None, timeout=5.0):
        if path == "/v1/approvals":
            time.sleep(WAIT_S)
        return super().approvals(method, path, body, timeout)


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


async def worst_gap_during(call) -> tuple:
    """Run ``call()`` (a coroutine) beside a 10 ms ticker; return (the ticker's largest gap in seconds, how long the call took)."""
    gaps, stop = [], asyncio.Event()

    async def tick():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    try:
        await call()
    finally:
        took = time.monotonic() - started
        stop.set()
        await ticker
    return max(gaps), took


def test_a_defaults_decided_ask_keeps_the_event_loop_responsive():
    shared = start(SlowAsk(defaults=served()))

    @runbound.tool(effects={"destructive"}, reviewed=True)
    async def wipe():
        return "wiped"

    async def go():
        gap, took = await worst_gap_during(wipe)
        assert took >= WAIT_S * 0.9  # the call did wait for the answer
        assert gap < WAIT_S / 3, f"the loop stalled for {gap:.2f}s of a {took:.2f}s approval wait"

    asyncio.run(go())
    assert [c for c in shared.calls if c[1] == "/v1/approvals"]


def test_an_unclassified_tool_decided_by_the_defaults_keeps_the_event_loop_responsive():
    start(SlowAsk(defaults=served()))

    @runbound.tool
    async def mystery():
        return "ok"

    async def go():
        gap, _ = await worst_gap_during(mystery)
        assert gap < WAIT_S / 3

    asyncio.run(go())


def test_a_row_that_asks_is_still_off_the_loop():
    start(SlowAsk([row("wipe", approval_required=True)]))

    @runbound.tool(effects={"destructive"}, reviewed=True)
    async def wipe():
        return "wiped"

    async def go():
        gap, _ = await worst_gap_during(wipe)
        assert gap < WAIT_S / 3

    asyncio.run(go())


def went_off_the_loop(monkeypatch, effects, shared) -> bool:
    """Whether the pre-chain of one async call was handed to a worker thread (``asyncio.to_thread``) rather than run on the loop."""
    start(shared)
    handed = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        handed.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(api.asyncio, "to_thread", spy)

    @runbound.tool(effects=effects, reviewed=effects is not None)
    async def act():
        return "ok"

    try:
        asyncio.run(act())
    except runbound.ExecutionRefused:
        pass
    return bool(handed)


def test_a_tool_the_defaults_allow_stays_on_the_loop(monkeypatch):
    assert not went_off_the_loop(monkeypatch, {"read"}, SlowAsk(defaults=served()))


def test_a_service_without_defaults_stays_on_the_loop_for_a_tool_with_no_row(monkeypatch):
    assert not went_off_the_loop(monkeypatch, {"destructive"}, SlowAsk())


def test_a_tool_the_defaults_ask_for_leaves_the_loop(monkeypatch):
    assert went_off_the_loop(monkeypatch, {"destructive"}, SlowAsk(defaults=served()))


def test_a_tool_the_defaults_deny_stays_on_the_loop(monkeypatch):
    assert not went_off_the_loop(monkeypatch, {"destructive"}, SlowAsk(defaults=served(decision={"destructive": "deny"})))
