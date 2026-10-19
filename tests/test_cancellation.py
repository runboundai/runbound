"""AAI-1 (PS-AAI-03e): a cancelled governed call is settled, not left open.

A task cancelled while the tool body runs, or while the call waits for an approval, used to leave its span un-ended, write no ``tool_result`` and report nothing to the plane. Now the
``CancelledError`` branch only enqueues (it never awaits inside the cancelled scope): the span ends with outcome ``cancelled``, the ``tool_result`` is written, and a sync approval is
reported. ``KeyboardInterrupt``, ``SystemExit`` and ``GeneratorExit`` stay untouched.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import runbound
from runbound import api, approvals, spans
from test_policy_rows import FakeShared, row, start
from test_spans import Collector, attrs_of


class Results:
    """An observer that keeps every ``tool_result`` event."""

    def __init__(self) -> None:
        self.results: list = []

    def on_event(self, session, event) -> None:
        if event.kind == "tool_result":
            self.results.append(event)

    def on_anomaly(self, session, anomaly, reacted) -> None:
        pass


class Pending(FakeShared):
    """The plane files the approval as pending and answers the long-poll only once ``release`` is set."""

    def __init__(self, rows=()):
        super().__init__(rows)
        self.release = threading.Event()

    def approvals(self, method, path, body=None, timeout=5.0):
        if method == "POST" and path == "/v1/approvals":
            self.calls.append((method, path, body))
            return 201, {"id": "ap1", "status": "pending", "quota": None}
        if method == "GET" and path.startswith("/v1/approvals/ap1"):
            self.release.wait(timeout=20)
            return 200, {"id": "ap1", "status": "approved" if self.release.is_set() else "pending"}
        return super().approvals(method, path, body, timeout)


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def watch():
    seen = Results()
    api._ENGINE.observers.append(seen)
    return seen


def reports(shared):
    return [c[2] for c in shared.calls if c[1].endswith("/report")]


def until(check, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def exported(collector):
    spans.exporter().flush(5.0)
    return collector.all_spans()


def cancel_after(tool, delay=0.2, then=None, **kwargs):
    """Run ``tool`` as a task, cancel it after ``delay`` seconds, then call ``then()`` while the loop is still up (a worker thread still waiting for an approval holds the loop's
    shutdown, so a test that waits on one must release it from ``then``)."""

    async def go():
        task = asyncio.ensure_future(tool(**kwargs))
        await asyncio.sleep(delay)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if then is not None:
            then()

    asyncio.run(go())


def test_a_call_cancelled_in_its_body_ends_its_span_as_cancelled_and_writes_its_result(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    async def slow():
        await asyncio.sleep(30)

    cancel_after(slow)
    raw = exported(collector)
    (call,) = [s for s in raw if s["name"].startswith("execute_tool")]
    assert attrs_of(call)["runbound.outcome"] == "cancelled"
    assert [s["name"] for s in raw].count("execution_started") == 1, "it had started: the marker says so"
    assert [e.outcome for e in seen.results] == ["cancelled"]


def test_a_cancelled_call_is_not_a_tool_error(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    errors = []

    class Errors(Results):
        def on_event(self, session, event) -> None:
            if event.kind == "tool_error":
                errors.append(event)

    api._ENGINE.observers.append(Errors())

    @runbound.tool(effects={"read"}, reviewed=True)
    async def slow():
        await asyncio.sleep(30)

    cancel_after(slow)
    assert errors == []


def test_a_sync_approval_is_reported_when_the_call_is_cancelled_after_it_started(collector):
    shared = start(FakeShared([row("act", approval_required=True)]), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    async def act():
        await asyncio.sleep(30)

    cancel_after(act, delay=0.5)
    sent = until(lambda: reports(shared), "the approval report")
    assert [r["outcome"] for r in sent] == [approvals.CANCELLED_REPORT_OUTCOME]
    assert approvals.CANCELLED_REPORT_OUTCOME == "failed", "today's closest existing status; the held `cancelled` status is a one-line change of this constant"


def test_a_call_cancelled_while_it_waits_for_an_approval_never_runs_its_body_and_the_late_approval_is_reported_as_not_run(collector):
    shared = start(Pending([row("act", approval_required=True)]), otel_endpoint=collector.url)
    seen = watch()
    ran = []

    @runbound.tool(effects={"read"}, reviewed=True)
    async def act():
        ran.append(1)

    def after():
        raw = exported(collector)
        (call,) = [s for s in raw if s["name"].startswith("execute_tool")]
        assert attrs_of(call)["runbound.outcome"] == "cancelled"
        assert [s["name"] for s in raw].count("execution_started") == 0, "it never started"
        assert [e.outcome for e in seen.results] == ["cancelled"]
        shared.release.set()  # a person approves after the caller gave up
        sent = until(lambda: reports(shared), "the late approval to be reported")
        assert [(r["outcome"], r.get("reason")) for r in sent] == [("superseded", "cancelled_by_caller")]
        time.sleep(0.3)
        assert ran == [] and [e.outcome for e in seen.results] == ["cancelled"], "no second tool_result from the thread that finished later"

    cancel_after(act, delay=0.6, then=after)


def test_a_cancelled_wait_that_is_denied_has_nothing_to_report(collector):
    class Denies(Pending):
        def approvals(self, method, path, body=None, timeout=5.0):
            if method == "GET" and path.startswith("/v1/approvals/ap1"):
                self.release.wait(timeout=20)
                return 200, {"id": "ap1", "status": "denied"}
            return super().approvals(method, path, body, timeout)

    shared = start(Denies([row("act", approval_required=True)]), otel_endpoint=collector.url)
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    async def act():
        return 1

    def after():
        shared.release.set()
        time.sleep(0.8)
        assert reports(shared) == [] and [e.outcome for e in seen.results] == ["cancelled"]

    cancel_after(act, delay=0.6, then=after)


def test_keyboard_interrupt_and_system_exit_pass_through_untouched(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    seen = watch()

    @runbound.tool(effects={"read"}, reviewed=True)
    def interrupted():
        raise KeyboardInterrupt

    @runbound.tool(effects={"read"}, reviewed=True)
    def exiting():
        raise SystemExit(3)

    with pytest.raises(KeyboardInterrupt):
        interrupted()
    with pytest.raises(SystemExit):
        exiting()
    assert [e.outcome for e in seen.results] == [], "neither is recorded as a cancellation"


def test_no_context_is_left_attached_after_a_cancelled_call_under_a_tracer_provider(collector):
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export")
    memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    from opentelemetry import trace as trace_api

    exporter = memory.InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    trace_api._TRACER_PROVIDER = None  # noqa: SLF001
    trace_api._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001
    trace_api.set_tracer_provider(provider)
    try:
        start(FakeShared())

        @runbound.tool(effects={"read"}, reviewed=True)
        async def slow():
            await asyncio.sleep(30)

        async def go():
            task = asyncio.ensure_future(slow())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return trace_api.get_current_span().get_span_context().is_valid

        assert asyncio.run(go()) is False, "the call's span is no longer current"
        done = {s.name: s for s in exporter.get_finished_spans()}
        assert done["execute_tool slow"].attributes["runbound.outcome"] == "cancelled" and "execution_started" in done
    finally:
        trace_api._TRACER_PROVIDER = None  # noqa: SLF001
        trace_api._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001
