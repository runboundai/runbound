"""AAI-04 LC-04-4a (EARLY, internal, off by default): a fresh LangChain tool call that needs a person WAITS for the decision, with the LOCAL cancellation fence.

Real langchain / langgraph. The plane is a stateful double with the three routes this uses (create, long-poll, withdraw). Named races (LC-READY-2): pending, denied and expired never
start; an approval still meets the current policy; the arguments are bound to their own request; an abandoned invocation never dispatches, even after the person approves or when the
withdrawal fails; a withdrawal that loses to an approval is NOT recorded as withdrawn; if dispatch wins the body is not claimed stopped.

Partial by label: fresh allow/ask/deny only. Not here: interrupt/resume/replay/bind (04-4b), AT-06, a callback migration.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402

import runbound  # noqa: E402
from runbound import api, approvals, spans  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402
from test_policy_rows import FakeShared, row, start  # noqa: E402
from test_spans import Collector, attrs_of  # noqa: E402


class ApprovalPlane(FakeShared):
    """Create files a PENDING request; the long-poll answers when decided (or after ``poll_s``); withdraw answers as scripted."""

    def __init__(self, rows=(), *, withdraw="ok", create_delay=0.0, poll_s=0.15, **kw):
        super().__init__(rows, **kw)
        self.requests: dict = {}
        self.cond = threading.Condition()
        self.withdraw_mode, self.create_delay, self.poll_s = withdraw, create_delay, poll_s
        self.reports: list = []
        self.withdraws: list = []
        self.polls = 0

    def approvals(self, method, path, body=None, timeout=5.0):
        if path.endswith("/report"):
            self.reports.append((path, body))
            return 200, {}
        if method == "POST" and path == "/v1/approvals":
            time.sleep(self.create_delay)
            with self.cond:
                approval_id = f"ap{len(self.requests) + 1}"
                self.requests[approval_id] = {"id": approval_id, "status": "pending", "quota": None, "body": body}
                self.calls.append((method, path, body))
                return 201, {k: v for k, v in self.requests[approval_id].items() if k != "body"}
        if method == "GET" and path.startswith("/v1/approvals/"):
            approval_id = path.split("/")[3].split("?")[0]
            self.polls += 1
            with self.cond:
                if self.requests[approval_id]["status"] == "pending":
                    self.cond.wait(self.poll_s)
                return 200, {k: v for k, v in self.requests[approval_id].items() if k != "body"}
        if method == "POST" and path.endswith("/withdraw"):
            approval_id = path.split("/")[3]
            self.withdraws.append(approval_id)
            if self.withdraw_mode == "down":
                return 503, None
            with self.cond:
                current = self.requests[approval_id]["status"]
                if self.withdraw_mode == "approved_wins" or current != "pending":
                    if self.withdraw_mode == "approved_wins":
                        self.requests[approval_id]["status"] = "approved"
                    return 409, {"status": self.requests[approval_id]["status"]}
                self.requests[approval_id]["status"] = "withdrawn"
                return 200, {"status": "withdrawn"}
        return super().approvals(method, path, body, timeout)

    def decide(self, approval_id, status):
        with self.cond:
            self.requests[approval_id]["status"] = status
            self.cond.notify_all()

    def wait_for(self, count, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with self.cond:
                if len(self.requests) >= count:
                    return
            time.sleep(0.02)
        raise AssertionError(f"{count} request(s) were not filed in time")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.delenv(lc._ENV, raising=False)
    monkeypatch.delenv(lc._ENV_WAIT, raising=False)
    monkeypatch.setattr(lc, "_enabled", True)
    monkeypatch.setattr(lc, "_approval_wait", True)
    lc._REGISTERED.clear()
    lc._DUPLICATES.clear()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


class Ran:
    def __init__(self):
        self.calls: list = []


def make_tool(ran: Ran, delay: float = 0.0):
    @tool
    def refund(order_id: str, amount_cents: int) -> str:
        """Refund an order."""
        ran.calls.append((order_id, amount_cents))
        if delay:
            time.sleep(delay)
        return f"refunded {amount_cents} on {order_id}"

    return refund


def make_async_tool(ran: Ran, delay: float = 0.0):
    @tool
    async def refund(order_id: str, amount_cents: int) -> str:
        """Refund an order."""
        ran.calls.append((order_id, amount_cents))
        if delay:
            await asyncio.sleep(delay)
        return f"refunded {amount_cents} on {order_id}"

    return refund


def graph_of(refund):
    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([refund], effects={"refund": {"financial"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


def message(order_id="A-1", amount=100, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": "refund", "args": {"order_id": order_id, "amount_cents": amount}, "id": call_id}])


def in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as error:  # noqa: BLE001
            box["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def tool_message(box):
    assert "error" not in box, box.get("error")
    (found,) = [m for m in box["result"]["messages"] if isinstance(m, ToolMessage)]
    return found


def exported(collector):
    spans.exporter().flush(5.0)
    return collector.all_spans()


def calls_of(raw):
    return [attrs_of(s) for s in raw if s["name"].startswith("execute_tool")]


def markers(raw):
    return [s for s in raw if s["name"] == "execution_started"]


def ask_plane(**kw):
    return ApprovalPlane([row("refund", approval_required=True)], **kw)


def wait_flag_off_refuses(monkeypatch):
    monkeypatch.setattr(lc, "_approval_wait", False)


# --- the flag ---------------------------------------------------------------------------------------------------------------------------------------------------------


def test_with_the_wait_flag_off_an_ask_is_still_refused_unavailable_and_nothing_is_filed(monkeypatch, collector):
    wait_flag_off_refuses(monkeypatch)
    ran, plane = Ran(), ask_plane()
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    result = graph_of(make_tool(ran)).invoke({"messages": [message()]})
    (msg,) = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert msg.content == "refused approval_unavailable" and ran.calls == [] and plane.requests == {}


# --- AT-04: pending / denied / expired never start ------------------------------------------------------------------------------------------------------------------


def test_a_pending_request_does_not_start_the_body_and_an_approval_does_once(collector):
    ran, plane = Ran(), ask_plane()
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    thread, box = in_thread(lambda: graph_of(make_tool(ran)).invoke({"messages": [message()]}))
    plane.wait_for(1)
    time.sleep(0.5)
    assert ran.calls == [] and thread.is_alive(), "still pending: nothing started"
    assert markers(exported(collector)) == []
    plane.decide("ap1", "approved")
    thread.join(10)
    assert tool_message(box).content == "refunded 100 on A-1" and ran.calls == [("A-1", 100)]
    raw = exported(collector)
    assert len(markers(raw)) == 1 and [a["runbound.outcome"] for a in calls_of(raw)] == ["ok"]
    filed = plane.requests["ap1"]["body"]
    assert filed["tool"] == "refund" and filed["args"] == {"order_id": "A-1", "amount_cents": 100} and filed["args_digest"], "the request carries the call's own arguments and their digest"


@pytest.mark.parametrize("verdict,reason", [("denied", "approval_denied"), ("expired", "approval_timeout")])
def test_a_denied_or_expired_request_never_starts_the_body(verdict, reason, collector):
    ran, plane = Ran(), ask_plane()
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    thread, box = in_thread(lambda: graph_of(make_tool(ran)).invoke({"messages": [message()]}))
    plane.wait_for(1)
    plane.decide("ap1", verdict)
    thread.join(10)
    assert tool_message(box).content == f"refused {reason}" and ran.calls == []
    raw = exported(collector)
    assert markers(raw) == [] and [a["runbound.outcome"] for a in calls_of(raw)] == ["refused"]


# --- AT-08 (subset): the arguments are bound to their own request ------------------------------------------------------------------------------------------------


def test_each_call_files_its_own_request_and_one_approval_releases_only_its_own_arguments(collector):
    ran, plane = Ran(), ask_plane()
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    refund = make_tool(ran)
    first, fbox = in_thread(lambda: graph_of(refund).invoke({"messages": [message("A-1", 100, "c1")]}))
    plane.wait_for(1)
    second, sbox = in_thread(lambda: graph_of(refund).invoke({"messages": [message("A-1", 99999, "c2")]}))  # same order, a different amount
    plane.wait_for(2)
    digests = {plane.requests[k]["body"]["args_digest"] for k in ("ap1", "ap2")}
    assert len(digests) == 2, "a changed argument is a different request, never a reuse of the approved one"
    plane.decide("ap1", "approved")
    first.join(10)
    assert tool_message(fbox).content == "refunded 100 on A-1"
    time.sleep(0.4)
    assert second.is_alive() and ran.calls == [("A-1", 100)], "the other call is still waiting for ITS decision"
    plane.decide("ap2", "denied")
    second.join(10)
    assert tool_message(sbox).content == "refused approval_denied" and ran.calls == [("A-1", 100)]


# --- AT-05 (subset): the approval still meets the CURRENT policy ----------------------------------------------------------------------------------------------------


def test_an_approval_never_runs_a_call_the_policy_now_forbids(collector):
    ran, plane = Ran(), ask_plane()
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    thread, box = in_thread(lambda: graph_of(make_tool(ran)).invoke({"messages": [message()]}))
    plane.wait_for(1)
    plane.rows = approvals_blocked_rows()  # the tool was blocked while the person was deciding
    plane.decide("ap1", "approved")
    thread.join(10)
    assert tool_message(box).content == "refused approval_superseded" and ran.calls == []
    assert any(body and body.get("outcome") == "superseded" for _path, body in plane.reports), "the plane is told the approved request did not run"
    assert markers(exported(collector)) == []


def approvals_blocked_rows():
    from runbound import policy_rows

    return policy_rows.parse([row("refund", blocked=True)])


# --- cancellation versus dispatch (LC-READY-2) -------------------------------------------------------------------------------------------------------------------


def run_cancelled(plane, tool_obj, cancel_after, *, release=None):
    """Start an async invocation, cancel it ``cancel_after`` seconds in. Returns once the cancelled task has finished."""
    app = graph_of(tool_obj)

    async def main():
        task = asyncio.create_task(app.ainvoke({"messages": [message()]}))
        await asyncio.sleep(cancel_after)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def settle(seconds=1.5):
    time.sleep(seconds)  # the worker thread and the background withdrawal finish on their own clock


def test_a_cancel_during_the_wait_withdraws_the_request_and_nothing_dispatches_even_after_the_person_approves(collector):
    ran, plane = Ran(), ask_plane(withdraw="ok")
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    run_cancelled(plane, make_async_tool(ran), 0.6)
    settle()
    plane.decide("ap1", "approved") if plane.requests["ap1"]["status"] == "pending" else None
    settle(0.5)
    assert plane.withdraws == ["ap1"] and plane.requests["ap1"]["status"] == "withdrawn"
    assert approvals.WITHDRAWALS[-1] == ("%s" % plane.requests["ap1"]["body"]["request_id"], "withdrawn", None), "the plane's actual answer is recorded"
    assert ran.calls == []
    raw = exported(collector)
    assert markers(raw) == [] and [a["runbound.outcome"] for a in calls_of(raw)] == ["cancelled"]


def test_an_approval_that_wins_the_race_with_the_withdrawal_is_not_recorded_as_withdrawn_and_the_call_still_never_runs(collector):
    ran, plane = Ran(), ask_plane(withdraw="approved_wins")
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    run_cancelled(plane, make_async_tool(ran), 0.6)
    settle()
    request_id = plane.requests["ap1"]["body"]["request_id"]
    assert approvals.WITHDRAWALS[-1] == (request_id, "not_withdrawable", "approved"), "recorded as what it is: NOT withdrawn; a person decided first"
    assert not approvals.is_withdrawn(request_id)
    assert ran.calls == [], "abandoned means never dispatched, whoever won on the plane"
    assert any(body and body.get("outcome") == "superseded" for _path, body in plane.reports), "the approved request is reported not run, so the plane gives its unit back"


def test_a_failed_withdrawal_is_recorded_local_and_the_call_still_never_runs_after_a_later_approval(collector):
    ran, plane = Ran(), ask_plane(withdraw="down")
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    run_cancelled(plane, make_async_tool(ran), 0.6)
    settle()
    request_id = plane.requests["ap1"]["body"]["request_id"]
    assert approvals.WITHDRAWALS[-1] == (request_id, "local", None) and approvals.is_withdrawn(request_id), "withdrawn HERE only; the plane was not reached"
    plane.decide("ap1", "approved")  # the person approves a request nobody is waiting for
    settle(0.8)
    assert ran.calls == [], "failed withdrawal + later approval: still no dispatch"


def test_a_cancel_before_the_request_is_filed_is_withdrawn_by_the_thread_that_files_it(collector):
    ran, plane = Ran(), ask_plane(withdraw="ok", create_delay=0.8)
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    run_cancelled(plane, make_async_tool(ran), 0.3)  # cancelled while the create is still in flight
    settle(2.0)
    assert plane.withdraws == ["ap1"] and plane.requests["ap1"]["status"] == "withdrawn"
    assert ran.calls == []


def test_when_dispatch_wins_nothing_is_withdrawn_and_the_body_is_not_claimed_stopped(collector):
    ran, plane = Ran(), ask_plane()
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    refund = make_async_tool(ran, delay=3.0)
    app = graph_of(refund)

    async def main():
        task = asyncio.create_task(app.ainvoke({"messages": [message()]}))
        await asyncio.sleep(0.4)
        plane.decide("ap1", "approved")
        await asyncio.sleep(0.8)  # the approval is seen, the body has started
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    settle(0.5)
    assert ran.calls == [("A-1", 100)], "the body had started"
    assert plane.withdraws == [] and approvals.WITHDRAWALS == deque_empty(), "dispatch won: no withdrawal was requested"
    raw = exported(collector)
    assert len(markers(raw)) == 1 and [a["runbound.outcome"] for a in calls_of(raw)] == ["cancelled"], "recorded cancelled (effects unknown), never 'stopped'"


def deque_empty():
    return type(approvals.WITHDRAWALS)()


def test_an_abandoned_invocation_stops_polling_the_plane(collector):
    ran, plane = Ran(), ask_plane(withdraw="down")  # the plane never confirms a withdrawal: only the local fence can stop the thread
    start(plane, otel_endpoint=collector.url, service="lc-wait")
    run_cancelled(plane, make_async_tool(ran), 0.6)
    settle(1.0)
    seen = plane.polls
    time.sleep(1.0)
    assert plane.polls == seen, "the waiting thread stopped; it does not poll a request nobody awaits until its timeout"
