"""AAI-04 Gate B gap G-6 (AT-09): ASK-path and error parity between the Python SDK decorator and the LangChain path (wait mode), on the same fixtures.

AT-09 asks for "the same governance decisions and COMPATIBLE lifecycle meanings". The fresh allow/deny decisions were compared in test_langchain_early / test_lc_early_harness; this compares what
happens when a PERSON is asked (approved, denied, expired, the policy moved while they decided) and when the body fails. For each fixture both paths run against the same plane double and we compare:
the outcome (ran, or the refusal's reason code), the request each path FILED (tool, arguments, digest, mode, requirement, timeout), the span outcome, and the error record. The one stated difference
is presentation: the decorator raises ``ExecutionRefused(reason)``, the LangChain path returns ``ToolMessage("refused <reason>")``; the test pins that the REASON is the same.

Real langchain / langgraph. Partial by label: the fixtures below, not every possible ask.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402

import runbound  # noqa: E402
from runbound import api, policy_rows, spans  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402
from test_langchain_approval_wait import ApprovalPlane, in_thread  # noqa: E402
from test_policy_rows import row, start  # noqa: E402
from test_spans import Collector, attrs_of  # noqa: E402

ARGS = {"order_id": "A-1", "amount_cents": 500}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.setattr(lc, "_enabled", True)
    monkeypatch.setattr(lc, "_approval_wait", True)
    lc._REGISTERED.clear()
    lc._DUPLICATES.clear()
    yield
    api._teardown_for_tests()


class Seen:
    """What a path did: how the call ended, what it filed, how its span and error record read."""

    def __init__(self):
        self.ended = None      # "ran" or the refusal's reason code
        self.filed = None      # the approval request body
        self.span_outcome = None
        self.errors = []       # tool_error event texts
        self.ran = []


def run_path(path: str, verdict: str, *, body_fails: bool = False, move_policy: bool = False) -> Seen:
    api._teardown_for_tests()
    lc._REGISTERED.clear()
    seen, collector = Seen(), Collector()
    plane = ApprovalPlane([row("refund", approval_required=True)])
    start(plane, otel_endpoint=collector.url, service="lc-parity")

    class Errors:
        def on_event(self, session, event):
            if event.kind == "tool_error":
                seen.errors.append(event.error)

        def on_anomaly(self, session, anomaly, reacted):
            return None

    api._ENGINE.observers.insert(0, Errors())

    def body(order_id, amount_cents):
        seen.ran.append((order_id, amount_cents))
        if body_fails:
            raise ValueError("the body failed")
        return f"refunded {amount_cents} on {order_id}"

    if path == "decorator":
        wrapped = runbound.tool(name="refund", effects={"financial"}, reviewed=True)(body)

        def call():
            try:
                return wrapped(**ARGS)
            except runbound.ExecutionRefused as refused:
                seen.ended = refused.reason
            except ValueError:
                seen.ended = "error"
    else:
        @tool
        def refund(order_id: str, amount_cents: int) -> str:
            """Refund an order."""
            return body(order_id, amount_cents)

        graph = StateGraph(MessagesState)
        graph.add_node("tools", lc.govern_tools([refund], effects={"refund": {"financial"}}))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        app = graph.compile()
        message = AIMessage(content="", tool_calls=[{"name": "refund", "args": ARGS, "id": "c1"}])

        def call():
            try:
                out = app.invoke({"messages": [message]})
            except ValueError:
                seen.ended = "error"
                return
            (msg,) = [m for m in out["messages"] if isinstance(m, ToolMessage)]
            seen.ended = msg.content.removeprefix("refused ") if msg.content.startswith("refused ") else "ran"

    thread, _ = in_thread(call)
    plane.wait_for(1)
    seen.filed = plane.requests["ap1"]["body"]
    if move_policy:
        plane.rows = policy_rows.parse([row("refund", blocked=True)])
    plane.decide("ap1", verdict)
    thread.join(15)
    assert not thread.is_alive(), f"{path} did not finish"
    if seen.ended is None:
        seen.ended = "ran"
    spans.exporter().flush(5.0)
    found = [attrs_of(s) for s in collector.all_spans() if s["name"].startswith("execute_tool")]
    seen.span_outcome = [a["runbound.outcome"] for a in found]
    collector.close()
    return seen


FILED_FIELDS = ("tool", "args", "args_digest", "mode", "timeout_s", "service", "requirement")

SCENARIOS = [
    ("approved", dict(verdict="approved")),
    ("denied", dict(verdict="denied")),
    ("expired", dict(verdict="expired")),
    ("policy moved while deciding", dict(verdict="approved", move_policy=True)),
    ("approved, then the body fails", dict(verdict="approved", body_fails=True)),
]


@pytest.mark.parametrize("label,kw", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_the_two_paths_decide_file_and_record_the_same_for_an_ask(label, kw):
    sdk, langchain = run_path("decorator", **kw), run_path("langchain", **kw)
    assert langchain.ended == sdk.ended, f"{label}: the same outcome ({sdk.ended!r})"
    for field in FILED_FIELDS:
        assert langchain.filed.get(field) == sdk.filed.get(field), f"{label}: the filed request differs in {field}: {sdk.filed.get(field)!r} vs {langchain.filed.get(field)!r}"
    assert langchain.span_outcome == sdk.span_outcome, label
    assert langchain.ran == sdk.ran, f"{label}: the body ran the same number of times with the same arguments"
    assert langchain.errors == sdk.errors, f"{label}: the same tool_error record"


def test_the_fixtures_really_cover_every_lifecycle_ending():
    """Guard against a vacuous parity: the scenarios must end in different ways."""
    ends = {run_path("decorator", **kw).ended for _label, kw in SCENARIOS}
    assert {"ran", "approval_denied", "approval_timeout", "approval_superseded", "error"} <= ends, ends


def test_the_only_stated_difference_is_presentation():
    """The decorator raises ExecutionRefused(reason); the LangChain path returns a tool message naming the SAME reason."""
    sdk, langchain = run_path("decorator", verdict="denied"), run_path("langchain", verdict="denied")
    assert sdk.ended == langchain.ended == "approval_denied" and sdk.ran == langchain.ran == []
