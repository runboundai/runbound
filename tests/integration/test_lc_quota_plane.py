"""AAI-04 Gate B gap G-10 (AT-17): "no accidental duplicate quota consumption", on a REAL plane.

The earlier AT-17 tests counted ``/v1/quota/take`` calls on a fake plane. Here the unit is the plane's own: a row with a quota of THREE per hour for the service. If a duplicate install spent a
second unit per call, the third call would already be refused. For each way a LangChain tool can be governed twice (and for the plain case) exactly three calls run and the fourth is refused
``quota_exceeded``. Real plane + real langchain / langgraph, guarded. Internal, off by default.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402

import runbound  # noqa: E402
from runbound import api  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402

from .test_real_plane import KEY, _until, private_plane  # noqa: E402,F401  (private_plane is a fixture)
from .test_rows_quota_plane import put_row  # noqa: E402

pytestmark = pytest.mark.plane
LIMIT = 3


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.setattr(lc, "_enabled", True)
    lc._REGISTERED.clear()
    lc._DUPLICATES.clear()
    yield
    api._teardown_for_tests()


def connect(plane, service, tool_name):
    put_row(plane, service, tool_name, quota={"limit": LIMIT, "window": "hour", "per": "service"})
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    _until(lambda: api._SHARED.row_for(tool_name) is not None, what="the quota row to reach the worker")


def run_calls(lc_tool, count, config=None):
    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([lc_tool], effects={lc_tool.name: {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    outcomes = []
    for n in range(count):
        call = AIMessage(content="", tool_calls=[{"name": lc_tool.name, "args": {"order_id": f"A-{n}"}, "id": f"c{n}"}])
        try:
            out = app.invoke({"messages": [call]}, config or {})
        except runbound.ExecutionRefused as refused:  # an inner boundary's own refusal still raises
            outcomes.append(f"refused {refused.reason}")
            continue
        (msg,) = [m for m in out["messages"] if isinstance(m, ToolMessage)]
        outcomes.append(msg.content if msg.content.startswith("refused ") else "ran")
    return outcomes


EXPECTED = ["ran"] * LIMIT + ["refused quota_exceeded"]


def test_a_plain_langchain_tool_spends_one_unit_per_call(private_plane):  # noqa: F811
    name = "lookup_" + uuid.uuid4().hex[:5]
    connect(private_plane, "lc-quota-plain", name)

    @tool(name)
    def lookup(order_id: str) -> str:
        """Look up."""
        return "ok"

    assert run_calls(lookup, LIMIT + 1) == EXPECTED


def test_a_decorated_tool_under_the_new_path_spends_one_unit_per_call_not_two(private_plane):  # noqa: F811
    name = "lookup_" + uuid.uuid4().hex[:5]
    connect(private_plane, "lc-quota-decorated", name)

    @tool(name)
    @runbound.tool(name=name, effects={"read"}, reviewed=True)
    def lookup(order_id: str) -> str:
        """Look up."""
        return "ok"

    assert run_calls(lookup, LIMIT + 1) == EXPECTED, "the outer layer passed through: the decorator took the only unit"
    assert lc.duplicate_installs() == {(name, "decorator"): LIMIT + 1}


def test_a_runtime_callback_handler_under_the_new_path_spends_one_unit_per_call_not_two(private_plane):  # noqa: F811
    from runbound.integrations.langchain import RunboundCallbackHandler

    name = "lookup_" + uuid.uuid4().hex[:5]
    connect(private_plane, "lc-quota-handler", name)

    @tool(name)
    def lookup(order_id: str) -> str:
        """Look up."""
        return "ok"

    assert run_calls(lookup, LIMIT + 1, {"callbacks": [RunboundCallbackHandler()]}) == EXPECTED
    assert lc.duplicate_installs() == {(name, "callback_handler"): LIMIT + 1}
