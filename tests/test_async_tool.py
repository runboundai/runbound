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
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped


class EventRecorder:
    """An observer that records every event the engine hands it."""

    def __init__(self, order: list | None = None) -> None:
        self.events: list[Event] = []
        self.order = order

    def on_event(self, session, event) -> None:
        if event.kind == "tool_result":  # the call-side events are what these tests are about
            return
        self.events.append(event)
        if self.order is not None:
            self.order.append(f"event:{event.kind}")

    def on_anomaly(self, session, anomaly, reacted) -> None:
        pass


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def recorder(order: list | None = None) -> EventRecorder:
    spy = EventRecorder(order)
    api._ENGINE.observers.append(spy)
    return spy


# --- the decorated function keeps its async identity ------------------------


def test_decorated_async_function_is_still_a_coroutine_function():
    runbound.init()

    @runbound.tool
    async def fetch(url):
        return url

    assert inspect.iscoroutinefunction(fetch)
    assert inspect.iscoroutinefunction(fetch)
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


def test_a_refused_async_call_never_runs_its_body():
    runbound.init()
    calls: list[str] = []

    @runbound.tool(max_calls=2)
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

    assert tripped.anomaly.detector == "policy"
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
    """The tool's own error is the one the caller needs, not GuardrailTripped.

    The tool_error event is recorded after the call was admitted; a latch that
    appeared in between must not replace the tool's exception.
    """
    runbound.init()

    @runbound.tool
    async def broken():
        runbound.current_session().tripped_by = Anomaly("policy", "critical", "latched", {})
        raise ValueError("kaboom")

    async def drive():
        with pytest.raises(ValueError, match="kaboom"):
            await broken()

    asyncio.run(drive())


# --- the sync path is untouched ---------------------------------------------


def test_sync_tools_still_trip_before_the_body_runs():
    runbound.init()
    calls: list[str] = []

    @runbound.tool(max_calls=2)
    def search(query):
        calls.append(query)
        return "ok"

    search("cats")
    search("cats")

    with pytest.raises(GuardrailTripped):
        search("cats")

    assert calls == ["cats", "cats"]
    assert not inspect.iscoroutinefunction(search)
