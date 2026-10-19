"""Gate B AT-13 (PRD section 9), the SDK half: a label the application REPORTS (``session(requested_for=...)`` and the session tags) is never presented as an authenticated identity.

On the SDK's two paths, the decorator and the LangChain path, the label does not reach a span or a start marker at all, and no span attribute names a principal or an identity. (The plane
stores ``requested_for`` as reported and the dash and console show it as "reported by the caller, not verified"; those halves are tested where they live.)"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402

import runbound  # noqa: E402
from runbound import api, spans  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402
from test_policy_rows import FakeShared, start  # noqa: E402
from test_spans import Collector  # noqa: E402

LABEL = "ceo@victim.example"
AGENT_TAG = "super-admin-agent"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.setattr(lc, "_enabled", True)
    lc._REGISTERED.clear()
    lc._DUPLICATES.clear()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def exported(collector) -> list:
    spans.exporter().flush(5.0)
    return collector.all_spans()


def assert_label_is_not_identity(raw: list) -> None:
    assert raw, "the run exported spans"
    text = json.dumps(raw)
    assert LABEL not in text and AGENT_TAG not in text, "a reported label is not written into a span or a start marker"
    keys = {item["key"] for span in raw for item in span["attributes"]}
    assert not {k for k in keys if any(word in k.lower() for word in ("principal", "identity", "authenticated", "user_id", "actor"))}, keys


def test_on_the_decorator_path_a_reported_label_never_reaches_the_record_as_an_identity(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="at13")

    @runbound.tool(name="lookup_order", effects={"read"}, reviewed=True)
    def lookup_order(order_id: str):
        return "ran"

    with runbound.session("sess-1", tags={"agent": AGENT_TAG}, requested_for=LABEL):
        assert lookup_order(order_id="A-1") == "ran"
    assert_label_is_not_identity(exported(collector))


def test_on_the_langchain_path_a_reported_label_never_reaches_the_record_as_an_identity(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="at13")

    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        return f"order {order_id}: shipped"

    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([lookup_order], effects={"lookup_order": {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    call = AIMessage(content="", tool_calls=[{"name": "lookup_order", "args": {"order_id": "A-1"}, "id": "c1"}])
    with runbound.session("sess-2", tags={"agent": AGENT_TAG}, requested_for=LABEL):
        graph.compile().invoke({"messages": [call]}, {"configurable": {"thread_id": "t-at13"}})
    assert_label_is_not_identity(exported(collector))
