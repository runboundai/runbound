"""The real-framework LangChain / LangGraph harness for AAI-04 (04-5 "support only on real-framework evidence").

Importable by Orion's LC-04-x tests. No product code, no network, no API key: a scripted fake chat model emits the tool calls a scenario names, so the same run is repeatable.

* ``versions()`` / ``record()``: the EXACT installed versions (what a support row cites); ``in_declared_pins()`` says whether they are inside the pins of RFC section 9.
* ``Ledger``: every tool BODY that starts appends here, so "0 starts" is a fact of the run, not an inference.
* ``make_tools(ledger)``: three typed tools (``lookup_order``, ``issue_refund``, ``send_email``) built from ``args_schema`` models, so schema and effects come from the framework object.
* ``run_create_agent(script, governor)`` and ``run_tool_node(script, governor)``: drive the two supported layouts with the real ``create_agent`` and a bare ``ToolNode``. ``governor`` is how a test
  plugs in the code under test: ``governor(layout, tools)`` returns ``(tools, middleware)`` (``middleware`` is used by ``create_agent`` only). With no governor the tools run ungoverned, which is
  what the harness' own tests use to prove the fixture is faithful.
* Scenarios: ``three_denials()`` (AT-03), ``PARITY_CASES`` with ``decorator_outcome()`` (AT-09 decorator parity: the Python SDK decorator's own decision for the same case), ``ask_case()`` (the
  unsupported-ask path: an ``ask`` must stop before dispatch).

Everything that imports ``langchain`` does so inside a function, so importing this module (and collecting tests) works without the extra installed; ``available()`` says whether the real packages are.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import sys
from dataclasses import dataclass, field

#: Declared pins (RFC section 9). The test extra ``langchain-test`` in pyproject pins EXACT versions inside them.
PINS = {"langchain": (1, 2), "langgraph": (1, 3)}  # [1.0, 2.0) and [1.2, 2.0): see in_declared_pins
PACKAGES = ("langchain", "langchain-core", "langgraph", "langgraph-prebuilt", "langgraph-checkpoint", "langgraph-checkpoint-sqlite")


def available() -> bool:
    try:
        import langchain.agents  # noqa: F401
        import langgraph.prebuilt  # noqa: F401
    except Exception:
        return False
    return True


def versions() -> dict:
    out = {"python": ".".join(map(str, sys.version_info[:3]))}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def _tuple(version: str | None) -> tuple:
    return tuple(int(p) for p in (version or "0").split(".")[:2] if p.isdigit())


def in_declared_pins() -> bool:
    v = versions()
    return sys.version_info >= (3, 11) and (1, 0) <= _tuple(v["langchain"]) < (2, 0) and (1, 2) <= _tuple(v["langgraph"]) < (2, 0)


def record() -> str:
    """The line a support-matrix row quotes: exact versions, JSON, sorted."""
    return json.dumps(versions(), sort_keys=True)


@dataclass
class Ledger:
    """What the tool bodies actually did."""

    starts: list = field(default_factory=list)  # (tool, args) per body that began

    def names(self) -> list:
        return [name for name, _ in self.starts]


def make_tools(ledger: Ledger) -> dict:
    """Three typed tools. The body appends to the ledger BEFORE returning, so a refused call leaves no entry."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel

    class LookupOrder(BaseModel):
        order_id: str

    class IssueRefund(BaseModel):
        order_id: str
        amount_cents: int

    class SendEmail(BaseModel):
        to: str
        subject: str

    def lookup_order(order_id: str) -> str:
        ledger.starts.append(("lookup_order", {"order_id": order_id}))
        return f"order {order_id}: shipped"

    def issue_refund(order_id: str, amount_cents: int) -> str:
        ledger.starts.append(("issue_refund", {"order_id": order_id, "amount_cents": amount_cents}))
        return f"refunded {amount_cents} on {order_id}"

    def send_email(to: str, subject: str) -> str:
        ledger.starts.append(("send_email", {"to": to, "subject": subject}))
        return f"sent to {to}"

    specs = (("lookup_order", lookup_order, LookupOrder, "Look up an order."), ("issue_refund", issue_refund, IssueRefund, "Refund an order."), ("send_email", send_email, SendEmail, "Send an email."))
    return {name: StructuredTool.from_function(func, name=name, description=doc, args_schema=schema) for name, func, schema, doc in specs}


# --- scripts: what the fake model emits ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Call:
    tool: str
    args: dict
    id: str = ""


def three_denials() -> list:
    """AT-03: three refund requests, one tool call per step, each with its own ``tool_call_id``. Under a policy that denies ``issue_refund``: 3 requested / 3 attempts / 3 evaluations / 0 starts."""
    return [[Call("issue_refund", {"order_id": f"o-{i}", "amount_cents": 1000 + i}, f"call_{i}")] for i in (1, 2, 3)]


def parallel_in_one_task() -> list:
    """The documented EXCLUSION: two tool calls in ONE model message (one ToolNode task per call under ``Send``, but parallel inside one task is not supported)."""
    return [[Call("lookup_order", {"order_id": "o-1"}, "call_a"), Call("issue_refund", {"order_id": "o-1", "amount_cents": 500}, "call_b")]]


def repeated_tool_call_id() -> list:
    """The documented EXCLUSION: an empty or repeated ``tool_call_id`` within a step has no stable attempt seed."""
    return [[Call("lookup_order", {"order_id": "o-1"}, "dup"), Call("lookup_order", {"order_id": "o-2"}, "dup")]]


#: AT-09: cases whose decision the Python SDK decorator also makes. ``policy`` is ToolPolicy keyword arguments (no approval here; see ask_case).
PARITY_CASES = (
    {"id": "allow-read", "call": Call("lookup_order", {"order_id": "o-1"}, "c1"), "policy": {}, "expect": "allow"},
    {"id": "deny-listed", "call": Call("issue_refund", {"order_id": "o-1", "amount_cents": 100}, "c2"), "policy": {"deny": ["issue_refund"]}, "expect": "deny"},
    {"id": "outside-allow-list", "call": Call("send_email", {"to": "a@b.c", "subject": "hi"}, "c3"), "policy": {"allow": ["lookup_order"]}, "expect": "deny"},
    {"id": "inside-allow-list", "call": Call("lookup_order", {"order_id": "o-9"}, "c4"), "policy": {"allow": ["lookup_order"]}, "expect": "allow"},
)


def ask_case() -> dict:
    """The unsupported-ask path: ``issue_refund`` requires approval. The released slice must stop BEFORE the body with an explicit unsupported / approval-unavailable result: never allow, never the old callback."""
    return {"id": "ask-refund", "call": Call("issue_refund", {"order_id": "o-1", "amount_cents": 100}, "c5"), "policy": {"require_approval": ["issue_refund"]}, "expect": "unsupported_ask"}


def decorator_outcome(case: dict) -> str:
    """What the Python SDK DECORATOR decides for ``case`` under the same policy: ``"allow"`` when the body ran, ``"deny"`` when the SDK refused it. Runs the real SDK engine in free mode."""
    import runbound
    from runbound import api
    from runbound.exceptions import ExecutionRefused

    api._teardown_for_tests()
    ran: list = []
    try:
        runbound.init(tool_policy=runbound.ToolPolicy(**case["policy"]))

        @runbound.tool(name=case["call"].tool)
        def body(**kwargs):
            ran.append(kwargs)
            return "ok"

        try:
            body(**case["call"].args)
        except ExecutionRefused:
            return "deny"
        return "allow" if ran else "deny"
    finally:
        api._teardown_for_tests()


# --- the scripted model and the two layouts -----------------------------------------------------------------------------------------------------

def scripted_model(script: list):
    """A fake chat model that emits ``script`` step by step (each step: the tool calls of one AI message), then a final plain message. ``bind_tools`` is a no-op: the script decides."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    messages = [AIMessage(content="", tool_calls=[{"name": c.tool, "args": c.args, "id": c.id or f"auto_{i}_{n}", "type": "tool_call"} for n, c in enumerate(step)]) for i, step in enumerate(script)]
    messages.append(AIMessage(content="done"))

    class Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    return Scripted(messages=iter(messages))


@dataclass
class Run:
    messages: list
    error: BaseException | None
    ledger: Ledger

    def tool_messages(self) -> list:
        return [m for m in self.messages if getattr(m, "type", "") == "tool"]


def _tools(ledger: Ledger, names=None) -> list:
    made = make_tools(ledger)
    return [made[n] for n in (names or made)]


def build_create_agent(script: list, governor=None, *, ledger: Ledger, checkpointer=None, tools=None):
    """The compiled ``create_agent`` graph of layout 1. ``governor("create_agent", tools)`` -> ``(tools, middleware)``."""
    from langchain.agents import create_agent

    use, middleware = governor("create_agent", _tools(ledger, tools)) if governor else (_tools(ledger, tools), ())
    return create_agent(scripted_model(script), use, middleware=list(middleware), checkpointer=checkpointer)


def build_tool_node(script: list, governor=None, *, ledger: Ledger, checkpointer=None, tools=None):
    """The compiled model -> ``ToolNode`` -> model graph of layout 2. ``governor("tool_node", tools)`` -> ``(tools_or_a_ToolNode, _)``."""
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    use, _ = governor("tool_node", _tools(ledger, tools)) if governor else (_tools(ledger, tools), ())
    model = scripted_model(script)

    def call_model(state):
        return {"messages": [model.invoke(state["messages"])]}

    def route(state):  # no annotations: this module's postponed annotations cannot be resolved by langgraph from a local scope
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", use if isinstance(use, ToolNode) else ToolNode(use, handle_tool_errors=False))  # a refusal must surface, not be turned into a tool message; a governor may hand back its own node (govern_tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, ["tools", END])
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)


def run_create_agent(script: list, governor=None, *, ledger: Ledger | None = None, checkpointer=None, thread_id: str = "t1", tools=None) -> Run:
    """Layout 1: ``create_agent`` (with the governor's middleware)."""
    from langchain_core.messages import HumanMessage

    ledger = ledger or Ledger()
    config = {"configurable": {"thread_id": thread_id}} if checkpointer is not None else None
    try:
        out = build_create_agent(script, governor, ledger=ledger, checkpointer=checkpointer, tools=tools).invoke({"messages": [HumanMessage("go")]}, config)
        return Run(out["messages"], None, ledger)
    except BaseException as error:  # a refusal that propagates is a result of the run, not a harness failure
        return Run([], error, ledger)


def run_tool_node(script: list, governor=None, *, ledger: Ledger | None = None, checkpointer=None, thread_id: str = "t1", tools=None) -> Run:
    """Layout 2: a bare ``ToolNode`` in a small graph."""
    from langchain_core.messages import HumanMessage

    ledger = ledger or Ledger()
    config = {"configurable": {"thread_id": thread_id}} if checkpointer is not None else None
    try:
        out = build_tool_node(script, governor, ledger=ledger, checkpointer=checkpointer, tools=tools).invoke({"messages": [HumanMessage("go")]}, config)
        return Run(out["messages"], None, ledger)
    except BaseException as error:
        return Run([], error, ledger)
