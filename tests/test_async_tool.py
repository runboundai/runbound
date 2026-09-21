"""Tests for ``@runbound.tool`` on ``async def`` functions.

An async tool has to keep every promise the sync one makes — the ``tool_call``
event before the body, ``tool_error`` when it fails, the original exception
unchanged — while staying a coroutine function, because frameworks
(FastAPI, LangChain) dispatch on ``inspect.iscoroutinefunction``.
"""

import asyncio
import inspect

import pytest

import runbound
from runbound import api
from runbound.events import Event
from runbound.exceptions import GuardrailTripped


class EventRecorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self, order: list | None = None) -> None:
        self.events: list[Event] = []
        self.order = order

    def check(self, state, event, config):
        self.events.append(event)
        if self.order is not None:
            self.order.append(f"event:{event.kind}")
        return None


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def recorder(order: list | None = None) -> EventRecorder:
    spy = EventRecorder(order)
    api._ENGINE.detectors.insert(0, spy)
    return spy


# --- the decorated function keeps its async identity ------------------------


def test_decorated_async_function_is_still_a_coroutine_function():
    runbound.init()

    @runbound.tool
    async def fetch(url):
        return url

    assert inspect.iscoroutinefunction(fetch)
    assert asyncio.iscoroutinefunction(fetch)
    assert fetch.__name__ == "fetch"
    assert asyncio.run(fetch("https://example.test")) == "https://example.test"


def test_named_form_works_on_an_async_tool():
    runbound.init()
    spy = recorder()

    @runbound.tool(name="web_fetch")
    async def fetch(url):
        return "ok"

    assert inspect.iscoroutinefunction(fetch)
    assert asyncio.run(fetch("u")) == "ok"

    (event,) = spy.events
    assert (event.kind, event.tool_name) == ("tool_call", "web_fetch")
    assert len(event.args_hash) == 64


def test_async_tool_is_inert_before_init():
    @runbound.tool
    async def add(a, b):
        return a + b

    assert inspect.iscoroutinefunction(add)
    assert asyncio.run(add(2, 3)) == 5
    assert runbound.current_session() is None


# --- ordering: the event is emitted before the body is awaited --------------


def test_tool_call_event_is_emitted_before_the_body_is_awaited():
    runbound.init()
    order: list[str] = []
    recorder(order)

    @runbound.tool
    async def search(query):
        order.append("body")
        await asyncio.sleep(0)
        order.append("body-done")
        return "ok"

    assert asyncio.run(search("cats")) == "ok"
    assert order == ["event:tool_call", "body", "body-done"]


def test_loop_trips_before_the_third_async_body_runs():
    runbound.init(loop_threshold=3, on_anomaly="raise")
    calls: list[str] = []

    @runbound.tool
    async def search(query):
        calls.append(query)
        await asyncio.sleep(0)
        return "ok"

    async def drive():
        await search("cats")
        await search("cats")
        with pytest.raises(GuardrailTripped) as excinfo:
            await search("cats")
        return excinfo.value

    tripped = asyncio.run(drive())

    assert tripped.anomaly.detector == "loop"
    assert calls == ["cats", "cats"]  # the tripping call never ran the tool


# --- failures ---------------------------------------------------------------


def test_tool_error_is_emitted_and_the_original_exception_re_raised():
    runbound.init()
    spy = recorder()

    @runbound.tool
    async def broken():
        await asyncio.sleep(0)
        raise ValueError("kaboom")

    async def drive():
        with pytest.raises(ValueError, match="kaboom") as excinfo:
            await broken()
        return excinfo.value

    asyncio.run(drive())

    call, error = spy.events
    assert call.kind == "tool_call"
    assert (error.kind, error.tool_name, error.error) == ("tool_error", "broken", "kaboom")
    assert error.step == 2


def test_a_cancelled_async_tool_is_recorded_and_stays_cancelled():
    """CancelledError is a BaseException; the host's cancellation must survive."""
    runbound.init()
    spy = recorder()

    @runbound.tool
    async def slow():
        await asyncio.sleep(10)

    async def drive():
        task = asyncio.ensure_future(slow())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())

    assert [event.kind for event in spy.events] == ["tool_call"]


def test_a_trip_while_recording_an_async_failure_keeps_the_tools_exception():
    """The tool's own error is the one the caller needs, not GuardrailTripped."""
    runbound.init(max_steps=2, on_anomaly="raise")

    @runbound.tool
    async def ping():
        return "pong"

    @runbound.tool
    async def broken():
        raise ValueError("kaboom")

    async def drive():
        await ping()  # step 1
        with pytest.raises(ValueError, match="kaboom"):
            await broken()  # tool_call step 2, tool_error step 3 -> over the limit

    asyncio.run(drive())


# --- the sync path is untouched ---------------------------------------------


def test_sync_tools_still_trip_before_the_body_runs():
    runbound.init(loop_threshold=3, on_anomaly="raise")
    calls: list[str] = []

    @runbound.tool
    def search(query):
        calls.append(query)
        return "ok"

    search("cats")
    search("cats")

    with pytest.raises(GuardrailTripped):
        search("cats")

    assert calls == ["cats", "cats"]
    assert not inspect.iscoroutinefunction(search)
