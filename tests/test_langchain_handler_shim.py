"""AAI-04-5a (development only): the old ``RunboundCallbackHandler`` stays, unremoved and with every default unchanged, and becomes interrupt-tolerant and migration-aware.

Real langchain / langgraph. PARTIAL by label (LC-READY-3): this proves the shim's interrupt classification and the opt-in migration notice; it is NOT the callback-migration acceptance (that
needs the replacement's approval/cancellation/resume tests plus AT-17 and a published guide). Nothing here switches a default or removes a name.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.errors import GraphInterrupt  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from langgraph.types import interrupt  # noqa: E402

import runbound  # noqa: E402
from runbound import api  # noqa: E402
from runbound.integrations import langchain as old  # noqa: E402
from runbound.integrations import langchain_early as early  # noqa: E402
from test_langchain import recorder  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    old._HANDLER_CLASS = None
    old._NOTICED.clear()
    monkeypatch.setattr(early, "_enabled", False)
    monkeypatch.delenv(early._ENV, raising=False)
    yield
    api._teardown_for_tests()
    old._HANDLER_CLASS = None


def pausing_graph():
    @tool
    def pausing(order_id: str) -> str:
        """Pauses the graph for a person."""
        return interrupt({"order": order_id})

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([pausing]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile(checkpointer=InMemorySaver())


CALL = AIMessage(content="", tool_calls=[{"name": "pausing", "args": {"order_id": "A-1"}, "id": "c1"}])


def test_a_graph_interrupt_in_a_tool_is_not_a_tool_error_for_the_old_handler():
    runbound.init()
    spy = recorder(results=True)
    result = pausing_graph().invoke({"messages": [CALL]}, {"configurable": {"thread_id": "t"}, "callbacks": [old.RunboundCallbackHandler()]})
    assert "__interrupt__" in result, "the interrupt reached the graph"
    assert [e.kind for e in spy.events if e.kind == "tool_error"] == [], "control flow is not a failure"
    results = [e for e in spy.events if e.kind == "tool_result"]
    assert [e.outcome for e in results] == ["cancelled"], "the call did not complete: recorded cancelled, never error"


def test_a_real_tool_failure_is_still_a_tool_error_for_the_old_handler():
    runbound.init()
    spy = recorder(results=True)

    @tool
    def explode(order_id: str) -> str:
        """Fails."""
        raise ValueError("the body failed")

    with pytest.raises(Exception, match="the body failed"):
        explode.invoke({"order_id": "A-1"}, config={"callbacks": [old.RunboundCallbackHandler()]})
    assert [e.kind for e in spy.events if e.kind == "tool_error"] == ["tool_error"]
    assert [e.outcome for e in spy.events if e.kind == "tool_result"] == ["error"]


def test_control_flow_is_matched_by_isinstance_when_langgraph_is_importable_and_a_lookalike_is_not_control_flow():
    from langgraph.errors import GraphBubbleUp

    class Lookalike(Exception):  # the same NAME as LangGraph's base, but not it
        pass

    Lookalike.__name__ = "GraphBubbleUp"
    assert old._is_graph_control_flow(GraphInterrupt()) and issubclass(GraphInterrupt, GraphBubbleUp)
    assert not old._is_graph_control_flow(Lookalike()), "with langgraph importable only the real class counts"
    assert not old._is_graph_control_flow(ValueError("x"))


def test_without_langgraph_the_class_name_is_the_fallback(monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("langgraph"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    class GraphBubbleUp(Exception):
        pass

    class Paused(GraphBubbleUp):
        pass

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert old._is_graph_control_flow(Paused()) and not old._is_graph_control_flow(ValueError("x"))


def test_the_old_name_and_the_old_surface_are_still_there_with_unchanged_defaults():
    handler = old.RunboundCallbackHandler()
    assert handler.raise_error is True and old.__all__ == ["RunboundCallbackHandler"]
    with pytest.warns(DeprecationWarning, match="renamed"):
        assert old.GuardrailCallbackHandler is old.RunboundCallbackHandler


def test_no_migration_notice_unless_the_new_path_is_enabled_in_this_process(caplog):
    runbound.init()
    with caplog.at_level(logging.INFO, logger="runbound"):
        old.RunboundCallbackHandler()
    assert not [r for r in caplog.records if "migrat" in r.getMessage().lower()], "an existing user sees nothing new"


def test_with_the_new_path_enabled_the_handler_notes_the_migration_once(caplog):
    runbound.init()
    early.enable()
    with caplog.at_level(logging.INFO, logger="runbound"):
        old.RunboundCallbackHandler()
        old.RunboundCallbackHandler()
    notes = [r for r in caplog.records if "migrat" in r.getMessage().lower()]
    assert len(notes) == 1 and "RunboundMiddleware" in notes[0].getMessage(), "once per process, naming the replacement"
