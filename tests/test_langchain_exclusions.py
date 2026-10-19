"""AAI-04-5b: the exclusions of the LangChain path, each with a fixture that shows WHY it is an exclusion (docs/design/aai-04-langchain-support.md section 3).

Real langchain / langgraph. These prove what is NOT covered; they claim nothing is supported. Provider-hosted tools have no fixture (the call never reaches a local boundary and needs a
provider); a call LangChain's own schema refuses is covered in test_langchain_early (``test_a_call_the_tools_own_schema_refuses_is_never_an_action``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

import runbound  # noqa: E402
from runbound import api, ids  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402
from test_policy_rows import FakeShared, start  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.setattr(lc, "_enabled", True)
    lc._REGISTERED.clear()
    lc._DUPLICATES.clear()
    yield
    api._teardown_for_tests()


def admitted_log():
    seen: list = []
    start(FakeShared(), service="lc-excl", tool_policy={"deny": ["lookup_order"]}, pre_hooks=[lambda call: seen.append(call.name)])
    return seen


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"


def one_node_graph(node, name="tools"):
    graph = StateGraph(MessagesState)
    graph.add_node(name, node)
    graph.add_edge(START, name)
    graph.add_edge(name, END)
    return graph.compile()


def test_a_custom_node_that_calls_a_tool_itself_never_passes_the_boundary():
    seen = admitted_log()  # the tool is DENIED by policy: a governed call would be refused

    def custom(state):
        return {"messages": [AIMessage(content=lookup_order.invoke({"order_id": "A-1"}))]}

    result = one_node_graph(custom, "custom").invoke({"messages": []})
    assert result["messages"][-1].content == "order A-1: shipped", "ran, although the policy denies it"
    assert seen == [], "the middleware never saw the call: the boundary is the wrapper, not the tool"


def test_a_tool_node_that_was_not_built_by_govern_tools_is_an_unwrapped_native_path():
    seen = admitted_log()
    lc.RunboundMiddleware()  # a layer exists in the process, but this node does not use it
    node = ToolNode([lookup_order])
    result = one_node_graph(node).invoke({"messages": [AIMessage(content="", tool_calls=[{"name": "lookup_order", "args": {"order_id": "A-1"}, "id": "c1"}])]})
    assert [m.content for m in result["messages"] if isinstance(m, ToolMessage)] == ["order A-1: shipped"] and seen == []


def test_parallel_calls_in_one_task_share_one_checkpoint_namespace_so_per_call_identity_cannot_be_kept_apart(monkeypatch):
    start(FakeShared(), service="lc-excl")
    asked: list = []
    real = ids.derive
    monkeypatch.setattr(ids, "derive", lambda *a, **k: (asked.append(a), real(*a, **k))[1])

    @tool
    def refund(order_id: str) -> str:
        """Refund."""
        return "ok"

    node = lc.govern_tools([lookup_order, refund], effects={"lookup_order": {"read"}, "refund": {"financial"}})
    calls = [{"name": "lookup_order", "args": {"order_id": "A-1"}, "id": "call_a"}, {"name": "refund", "args": {"order_id": "A-1"}, "id": "call_b"}]
    one_node_graph(node).invoke({"messages": [AIMessage(content="", tool_calls=calls)]}, {"configurable": {"thread_id": "t1"}})
    assert len(asked) == 2 and asked[0][2] == asked[1][2], "both calls ran in ONE task: the same namespace, distinguished only by tool_call_id (a resume would match them by order)"


def test_an_empty_tool_call_id_has_no_stable_seed_so_the_ids_are_random_every_time(monkeypatch):
    start(FakeShared(), service="lc-excl")
    seen: list = []
    monkeypatch.setattr(api, "_begin", _spy_begin(api._begin, seen))
    node = lc.govern_tools([lookup_order], effects={"lookup_order": {"read"}})
    for _ in range(2):
        one_node_graph(node).invoke({"messages": [AIMessage(content="", tool_calls=[{"name": "lookup_order", "args": {"order_id": "A-1"}, "id": ""}])]}, {"configurable": {"thread_id": "t1"}})
    assert seen == [None, None], "no ids were handed off: random ids, so the same call twice is two actions (and no replay safety is claimed)"
    assert ids.derive("lc-excl", "t1", "", "") is None


def _spy_begin(real, seen):
    def spy(*args, **kwargs):
        seen.append(kwargs.get("ids") if "ids" in kwargs else (args[9] if len(args) > 9 else None))
        return real(*args, **kwargs)

    return spy
