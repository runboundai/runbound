"""A wait a dashboard row imposes never stops the host's event loop (V1).

A policy row can add a sync approval, or a quota, to a tool whose code declares neither. Both wait on the
plane (a long poll; ``POST /v1/quota/take``), and an ``async def`` tool must not hold the loop while they do:
a 50 ms ticker task running beside the call has to keep ticking. Real plane, as in test_rows_quota_plane.py.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (fixture)
from .test_rows_quota_plane import have_row, put_row, start

pytestmark = pytest.mark.plane

TICK_S = 0.05
WAIT_S = 1.5
EXPECTED = WAIT_S / TICK_S


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


async def _ticks_during(call, release=None) -> tuple[object, int]:
    """Run ``call()`` (a coroutine) while a ticker counts every 50 ms; ``release`` runs on a thread after WAIT_S."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(TICK_S)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(TICK_S)  # let the ticker start
    ticks = 0
    if release is not None:
        threading.Timer(WAIT_S, release).start()
    started = time.monotonic()
    try:
        result = await call()
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        await task
    assert elapsed >= WAIT_S * 0.8, f"the call did not wait ({elapsed:.2f} s): the test proves nothing"
    return result, ticks


def test_an_async_tool_under_a_row_imposed_sync_approval_keeps_the_loop_running(private_plane):  # noqa: F811
    put_row(private_plane, "row-wait", "wire", approval_required=True, approval_mode="sync", approval_timeout_s=60)
    start(private_plane, "row-wait")

    @runbound.tool(effects={"read"}, reviewed=True)  # the code declares NO approval: only the row asks
    async def wire(amount: float):
        return "sent"

    have_row("wire")

    def approve():
        waiting = _until(lambda: private_plane.request("GET", "/v1/admin/approvals?status=pending&service=row-wait")[1]
                         .get("approvals"), what="the pending approval")
        status, body = private_plane.request("POST", f"/v1/admin/approvals/{waiting[0]['id']}/approve", {"reason": "ok"})
        assert status == 200, body

    result, ticks = asyncio.run(_ticks_during(lambda: wire(5.0), release=approve))
    assert result == "sent"
    assert ticks >= 0.9 * EXPECTED, f"the loop ticked {ticks} times, expected about {EXPECTED:.0f}"


def test_an_async_tool_under_a_quota_take_keeps_the_loop_running(private_plane):  # noqa: F811
    # a per-key quota is never leased: every call takes from the plane. Slow the plane to make the wait visible.
    put_row(private_plane, "row-quota", "lookup", quota={"limit": 100, "window": "hour", "per": "key"})
    start(private_plane, "row-quota")

    @runbound.tool(effects={"read"}, reviewed=True)
    async def lookup(x: int = 0):
        return "ok"

    have_row("lookup")
    real = api._SHARED.approvals
    paths = []

    def slow(method, path, body=None, timeout=5.0):
        paths.append(path)
        if path == "/v1/quota/take":
            time.sleep(WAIT_S)
        return real(method, path, body, timeout)

    api._SHARED.approvals = slow

    async def call():
        with runbound.session("key-1"):
            return await lookup(1)

    result, ticks = asyncio.run(_ticks_during(call))
    assert "/v1/quota/take" in paths
    assert result == "ok"
    assert ticks >= 0.9 * EXPECTED, f"the loop ticked {ticks} times, expected about {EXPECTED:.0f}"


def test_an_async_tool_with_a_code_declared_sync_approval_keeps_the_loop_running(private_plane):  # noqa: F811
    # the code-declared path had the same to_thread hop but api.py never imported asyncio: a NameError on every call
    start(private_plane, "code-wait")

    @runbound.tool(effects={"read"}, reviewed=True, approval="required")
    async def wire(amount: float):
        return "sent"

    def approve():
        waiting = _until(lambda: private_plane.request("GET", "/v1/admin/approvals?status=pending&service=code-wait")[1]
                         .get("approvals"), what="the pending approval")
        status, body = private_plane.request("POST", f"/v1/admin/approvals/{waiting[0]['id']}/approve", {"reason": "ok"})
        assert status == 200, body

    result, ticks = asyncio.run(_ticks_during(lambda: wire(5.0), release=approve))
    assert result == "sent"
    assert ticks >= 0.9 * EXPECTED, f"the loop ticked {ticks} times, expected about {EXPECTED:.0f}"
