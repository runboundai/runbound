"""AAI-04 LC-04-1 (EARLY TRANCHE, internal, disabled by default): typed registration and governance of fresh LangChain tool calls, against the REAL packages.

Subset only (LC-EARLY-3): AT-03 (three denials), AT-09 (parity with the decorator) and the privacy and schema rows are tested for fresh allow/deny calls; AT-07/08/09/17 are NOT passed
in full by this file, and nothing here covers an approval wait, a resume, a replay or a cancellation. A call that would need a person is refused before anything is asked.

Every test builds a real ``langgraph`` graph (a bare ``ToolNode`` through ``govern_tools``, or ``langchain.agents.create_agent`` through ``RunboundMiddleware``).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402

import runbound  # noqa: E402
from runbound import api, ids, spans  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402
from test_policy_rows import FakeShared, row, start  # noqa: E402
from test_spans import Collector, attrs_of  # noqa: E402

SECRET = "s3cr3t-value-" + "z9" * 6


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.delenv(lc._ENV, raising=False)
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


class Ran:
    """What the tool bodies saw."""

    def __init__(self) -> None:
        self.calls: list = []


def make_tools(ran: Ran):
    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        ran.calls.append(("lookup_order", order_id))
        return f"order {order_id}: shipped"

    @tool
    def delete_order(order_id: str) -> str:
        """Delete an order."""
        ran.calls.append(("delete_order", order_id))
        return f"order {order_id}: deleted"

    @tool
    def add_items(order_id: str, quantity: int) -> str:
        """Add items to an order."""
        ran.calls.append(("add_items", order_id, quantity))
        return f"added {quantity}"

    @tool
    def explode(order_id: str) -> str:
        """Always fails."""
        ran.calls.append(("explode", order_id))
        raise ValueError("the body failed")

    return {"lookup_order": lookup_order, "delete_order": delete_order, "add_items": add_items, "explode": explode}


class ScriptedModel(BaseChatModel):
    """A chat model that answers with the next scripted message (the tool calls first, then a plain answer)."""

    script: list
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        message = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def calls_message(*calls):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in calls])


CONFIG = {"configurable": {"thread_id": "thread-1"}}
SDK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_node(tools, message, *, effects=None, config=CONFIG):
    """A bare ToolNode through govern_tools."""
    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools(tools, effects=effects))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return [m for m in graph.compile().invoke({"messages": [message]}, config)["messages"] if isinstance(m, ToolMessage)]


def run_agent(tools, message, *, effects=None, config=CONFIG):
    """create_agent with the middleware."""
    agent = create_agent(ScriptedModel(script=[message, AIMessage(content="done")]), tools, middleware=[lc.RunboundMiddleware(effects=effects)])
    return [m for m in agent.invoke({"messages": [("user", "go")]}, config)["messages"] if isinstance(m, ToolMessage)]


SURFACES = pytest.mark.parametrize("run", [run_node, run_agent], ids=["govern_tools", "middleware"])

ATTR = {key: f"runbound.{key}" for key in ("action_id", "attempt_id", "evaluation_id")}


def exported(collector):
    spans.exporter().flush(5.0)
    return collector.all_spans()


def call_spans(raw):
    return [attrs_of(s) for s in raw if s["name"].startswith("execute_tool")]


def markers(raw):
    return [s for s in raw if s["name"] == "execution_started"]


# --- disabled by default ------------------------------------------------------------------------------------------------------------------------------------


def test_the_early_path_is_disabled_until_it_is_enabled_on_purpose(monkeypatch):
    monkeypatch.setattr(lc, "_enabled", False)
    with pytest.raises(lc.LangChainEarlyDisabled):
        lc.RunboundMiddleware()
    with pytest.raises(lc.LangChainEarlyDisabled):
        lc.govern_tools([])
    monkeypatch.setenv(lc._ENV, "1")
    lc.RunboundMiddleware()
    lc.govern_tools([])


def test_the_exact_framework_versions_are_recorded_and_inside_the_declared_pins():
    found = lc.framework_versions()
    print("framework versions:", json.dumps(found, sort_keys=True))
    assert found["langchain"].startswith("1.") and found["langgraph"].startswith("1.") and tuple(int(n) for n in found["langgraph"].split(".")[:2]) >= (1, 2)
    assert found["langchain-core"] and found["python"]


# --- AT-03 (subset): three denials ---------------------------------------------------------------------------------------------------------------------------


@SURFACES
def test_three_denied_calls_are_three_requested_three_attempts_three_evaluations_and_no_starts(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, tool_policy={"deny": ["delete_order"]}, service="lc-early")
    tools = make_tools(ran)
    messages = run(list(tools.values()), calls_message(*[("delete_order", {"order_id": f"A-{n}"}, f"call-{n}") for n in range(3)]), effects={"lookup_order": {"read"}})
    assert [m.content for m in messages] == ["refused tool_blocked"] * 3 and all(m.status == "error" for m in messages)
    assert ran.calls == [], "a refused call never reaches the tool body"
    raw = exported(collector)
    found = call_spans(raw)
    assert len(found) == 3 and all(a["runbound.outcome"] == "refused" for a in found)
    for key in ATTR.values():
        assert len({a[key] for a in found}) == 3, f"three distinct {key}"
    assert markers(raw) == [], "0 starts"


@SURFACES
def test_an_allowed_call_runs_once_after_one_start_marker_and_returns_the_tools_own_message(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    (message,) = run(list(tools.values()), calls_message(("lookup_order", {"order_id": "A-7"}, "call-1")), effects={"lookup_order": {"read"}})
    assert message.content == "order A-7: shipped" and message.status == "success"
    assert ran.calls == [("lookup_order", "A-7")]
    raw = exported(collector)
    (found,) = call_spans(raw)
    assert found["runbound.outcome"] == "ok" and len(markers(raw)) == 1


# --- effects: explicit or unclassified, never inferred from a name -----------------------------------------------------------------------------------


@SURFACES
def test_effects_are_what_the_customer_states_and_a_name_that_sounds_dangerous_is_not_read_as_a_class(run):
    ran = Ran()
    start(FakeShared(), capabilities={"financial": "deny"}, service="lc-early")
    tools = make_tools(ran)
    # delete_order sounds destructive and the customer says it is a read: it runs; add_items is stated financial: it is refused by the class rule.
    (deleted,) = run(list(tools.values()), calls_message(("delete_order", {"order_id": "A-1"}, "c1")), effects={"delete_order": {"read"}})
    assert deleted.content == "order A-1: deleted"
    (added,) = run(list(tools.values()), calls_message(("add_items", {"order_id": "A-1", "quantity": 2}, "c2")), effects={"add_items": {"financial"}})
    assert added.status == "error" and added.content.startswith("refused ")
    assert ("add_items", "A-1", 2) not in ran.calls


@SURFACES
def test_a_tool_the_customer_did_not_classify_is_judged_as_unclassified(run):
    ran = Ran()
    start(FakeShared(), capabilities={"financial": "deny"}, service="lc-early")  # narrowing anything makes an unclassified tool refuse
    tools = make_tools(ran)
    (message,) = run(list(tools.values()), calls_message(("lookup_order", {"order_id": "A-1"}, "c1")))
    assert message.status == "error" and message.content.startswith("refused ")
    assert ran.calls == []


# --- schema fidelity, privacy ------------------------------------------------------------------------------------------------------------------------------------


@SURFACES
def test_the_evaluator_sees_the_arguments_as_the_tools_schema_reads_them(run):
    ran = Ran()
    seen: list = []
    start(FakeShared(), service="lc-early", pre_hooks=[lambda call: seen.append(dict(call.kwargs))])
    tools = make_tools(ran)
    run(list(tools.values()), calls_message(("add_items", {"order_id": "A-1", "quantity": "3"}, "c1")), effects={"add_items": {"write"}})
    assert seen == [{"order_id": "A-1", "quantity": 3}], "the model sent '3'; the schema's int is what the rules and the approval binding would see"
    assert ran.calls == [("add_items", "A-1", 3)]


@SURFACES
def test_a_call_the_tools_own_schema_refuses_is_never_an_action(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    try:
        messages = run(list(tools.values()), calls_message(("add_items", {"order_id": "A-1", "quantity": "many"}, "c1")), effects={"add_items": {"write"}})
        assert all(m.status == "error" for m in messages)
    except Exception:  # noqa: BLE001 - LangChain may raise its own validation error instead; either way the body must not run
        pass
    assert ran.calls == []
    assert call_spans(exported(collector)) == [], "LangChain refused it before the boundary: not an action, no span"


@SURFACES
def test_no_raw_argument_value_reaches_a_record_or_the_refusal_message(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, tool_policy={"deny": ["delete_order"]}, service="lc-early")
    tools = make_tools(ran)
    (message,) = run(list(tools.values()), calls_message(("delete_order", {"order_id": SECRET}, "c1")))
    assert SECRET not in message.content
    run(list(tools.values()), calls_message(("lookup_order", {"order_id": SECRET}, "c2")), effects={"lookup_order": {"read"}})
    wire = json.dumps(exported(collector))
    assert "execute_tool" in wire and SECRET not in wire


# --- AT-09 (subset): the same decisions as the decorator ----------------------------------------------------------------------------------------------------


CASES = [
    ("blocked by name", dict(tool_policy={"deny": ["probe"]}), {"probe": {"read"}}),
    ("class denied", dict(capabilities={"financial": "deny"}), {"probe": {"financial"}}),
    ("class allowed", dict(capabilities={"financial": "deny"}), {"probe": {"read"}}),
    ("unclassified, nothing narrowed", dict(), {}),
    ("unclassified, a class narrowed", dict(capabilities={"financial": "deny"}), {}),
]


@pytest.mark.parametrize("label,config,effects", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("run", [run_node, run_agent], ids=["govern_tools", "middleware"])
def test_the_decision_equals_the_decorators_for_the_same_fixture(label, config, effects, run):
    def decorated_outcome():
        api._teardown_for_tests()
        start(FakeShared(), service="lc-early", **config)

        @runbound.tool(name="probe", effects=effects.get("probe"), reviewed=True)
        def probe(order_id: str):
            return "ran"

        try:
            probe(order_id="A-1")
            return "ran"
        except runbound.ExecutionRefused as refused:
            return f"refused {refused.reason}"

    def langchain_outcome():
        api._teardown_for_tests()
        lc._REGISTERED.clear()
        start(FakeShared(), service="lc-early", **config)

        @tool
        def probe(order_id: str) -> str:
            """Probe."""
            return "ran"

        (message,) = run([probe], calls_message(("probe", {"order_id": "A-1"}, "c1")), effects=effects)
        return message.content if message.status == "error" else "ran"

    expected = decorated_outcome()
    assert langchain_outcome() == expected, label


# --- fresh only: a call that would need a person stops before dispatch ------------------------------------------------------------------------------------


@SURFACES
def test_a_call_that_would_ask_a_person_is_refused_unavailable_before_anything_is_asked_and_never_runs(run, collector):
    ran = Ran()
    shared = FakeShared([row("delete_order", approval_required=True)])
    start(shared, otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    (message,) = run(list(tools.values()), calls_message(("delete_order", {"order_id": "A-1"}, "c1")), effects={"delete_order": {"destructive"}})
    assert message.status == "error" and message.content == "refused approval_unavailable"
    assert ran.calls == [], "never falls through to allow"
    assert not [c for c in shared.calls if "/v1/approvals" in str(c)], "no approval request was filed"
    raw = exported(collector)
    assert markers(raw) == [] and [a["runbound.outcome"] for a in call_spans(raw)] == ["refused"]


# --- ids -------------------------------------------------------------------------------------------------------------------------------------------------------------------


@SURFACES
def test_ids_come_from_thread_and_tool_call_id_when_both_exist_and_are_random_otherwise(run, collector, monkeypatch):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    asked: list = []
    real = ids.derive
    monkeypatch.setattr(ids, "derive", lambda *a, **k: (asked.append(a), real(*a, **k))[1])
    tools = make_tools(ran)
    message = calls_message(("lookup_order", {"order_id": "A-1"}, "call-9"))
    run(list(tools.values()), message, effects={"lookup_order": {"read"}}, config={"configurable": {"thread_id": "thread-9"}})
    run(list(tools.values()), message, effects={"lookup_order": {"read"}}, config={"configurable": {}})
    stable, unstable = call_spans(exported(collector))
    (tenant, thread, namespace, call_id), (_, no_thread, _, _) = asked
    assert (tenant, thread, call_id, no_thread) == ("lc-early", "thread-9", "call-9", None)
    want = real(tenant, thread, namespace, call_id)
    assert (stable["runbound.action_id"], stable["runbound.attempt_id"]) == (want["action_id"], want["attempt_id"]), "the runtime's ids, derived"
    assert unstable["runbound.action_id"] not in (want["action_id"], stable["runbound.action_id"]) and len(unstable["runbound.action_id"]) == 32, "no thread_id: random ids"
    assert stable["runbound.evaluation_id"] != unstable["runbound.evaluation_id"], "the evaluation is always minted by the engine, never derived"


def test_the_derivation_is_pure_stable_and_refuses_to_guess():
    first = ids.derive("tenant", "thread", "ns", "call")
    assert first == ids.derive("tenant", "thread", "ns", "call") and first["action_id"] != first["attempt_id"]
    assert ids.derive("tenant", "thread", "ns", "other")["action_id"] != first["action_id"]
    assert ids.derive("other", "thread", "ns", "call")["action_id"] != first["action_id"]
    assert ids.derive("tenant", "thread", "ns", "call", retry_no=1)["attempt_id"] != first["attempt_id"]
    assert ids.derive("tenant", "thread", "ns", "call", retry_no=1)["action_id"] == first["action_id"]
    for missing in ((None, "call"), ("", "call"), ("thread", None), ("thread", "")):
        assert ids.derive("tenant", missing[0], "ns", missing[1]) is None
    assert ids.derive("", "thread", "ns", "call") is None


# --- errors and async -------------------------------------------------------------------------------------------------------------------------------------------------


@SURFACES
def test_a_tool_body_that_fails_is_recorded_as_an_error_and_the_error_is_not_swallowed(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    with pytest.raises(Exception, match="the body failed"):
        run(list(tools.values()), calls_message(("explode", {"order_id": "A-1"}, "c1")), effects={"explode": {"read"}})
    raw = exported(collector)
    assert [a["runbound.outcome"] for a in call_spans(raw)] == ["error"] and len(markers(raw)) == 1


def test_the_async_path_admits_once_runs_once_and_refuses_without_running(collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, tool_policy={"deny": ["delete_order"]}, service="lc-early")
    tools = make_tools(ran)
    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools(list(tools.values()), effects={"lookup_order": {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    message = calls_message(("lookup_order", {"order_id": "A-1"}, "c1"), ("delete_order", {"order_id": "A-1"}, "c2"))
    result = asyncio.run(graph.compile().ainvoke({"messages": [message]}, CONFIG))
    by_name = {m.name: m for m in result["messages"] if isinstance(m, ToolMessage)}
    assert by_name["lookup_order"].content == "order A-1: shipped" and by_name["delete_order"].content == "refused tool_blocked"
    assert ran.calls == [("lookup_order", "A-1")]
    raw = exported(collector)
    assert sorted(a["runbound.outcome"] for a in call_spans(raw)) == ["ok", "refused"] and len(markers(raw)) == 1


# --- not initialised, rewritten arguments --------------------------------------------------------------------------------------------------------------------------


def test_before_init_the_tool_just_runs_and_nothing_is_recorded():
    ran = Ran()
    tools = make_tools(ran)
    (message,) = run_node(list(tools.values()), calls_message(("lookup_order", {"order_id": "A-1"}, "c1")))
    assert message.content == "order A-1: shipped" and ran.calls == [("lookup_order", "A-1")]


def test_the_request_carries_the_arguments_the_chain_finally_allowed():
    class Request:
        tool_call = {"name": "t", "args": {"a": 1, "b": 2}, "id": "x"}

        def override(self, **overrides: Any):
            clone = Request()
            clone.tool_call = overrides["tool_call"]
            return clone

    request = Request()
    assert lc._with_args(request, {"a": 1, "b": 2}) is request, "unchanged arguments pass the original request through"
    assert lc._with_args(request, {"a": 9, "b": 2}).tool_call["args"] == {"a": 9, "b": 2}


# --- 04-2: duplicate install (the innermost boundary governs; verified duplicates only) -------------------------------------------------------------


def counting_hook(counter: list):
    def hook(call):
        counter.append(call.name)

    return hook


def decorated_lc_tool(ran: Ran, name="lookup_dec", lc_name=None):
    @tool(lc_name) if lc_name else tool
    @runbound.tool(effects={"read"}, reviewed=True)
    def lookup_dec(order_id: str) -> str:
        """Look up an order."""
        ran.calls.append((name, order_id))
        return f"order {order_id}: shipped"

    return lookup_dec


@SURFACES
def test_a_decorated_tool_under_the_middleware_is_admitted_once_by_the_decorator_and_the_outer_layer_records_one_duplicate(run, collector):
    ran, admitted = Ran(), []
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", pre_hooks=[counting_hook(admitted)])
    inner = decorated_lc_tool(ran)
    for n in range(2):
        (message,) = run([inner], calls_message(("lookup_dec", {"order_id": f"A-{n}"}, f"c{n}")))
        assert message.content == f"order A-{n}: shipped"
    assert admitted == ["lookup_dec"] * 2, "one admission per call: no double admission"
    raw = exported(collector)
    assert [a["runbound.outcome"] for a in call_spans(raw)] == ["ok", "ok"] and len(markers(raw)) == 2
    assert lc.duplicate_installs() == {("lookup_dec", "decorator"): 2}, "counted per call, recorded (warned) once per tool and kind"


def refund_tool(ran: Ran):
    @tool
    def issue_refund(order_id: str, amount: int) -> str:
        """Refund an order."""
        ran.calls.append(("issue_refund", order_id, amount))
        return f"refunded {amount} on {order_id}"

    return issue_refund


@SURFACES
def test_a_console_rule_with_an_argument_condition_is_evaluated_on_plain_tools_through_the_middleware(run, collector):
    """The headline layout (plain LangChain tools + the middleware): a row 'amount > 200 -> deny, otherwise allow' must read the call's ARGUMENTS. Draco's finding: the middleware registered no signature, the
    condition saw nothing, read as unknown, and the unknown-condition rule (K1) refused EVERY call, a 19 refund included."""
    from test_conditions import cond, line

    ran = Ran()
    start(FakeShared([row("issue_refund", lines=[line("L1", "deny", [cond("amount", ">", 200)])], otherwise="allow")]), otel_endpoint=collector.url, service="lc-early")
    effects = {"issue_refund": {"financial"}}
    (small,) = run([refund_tool(ran)], calls_message(("issue_refund", {"order_id": "A-1", "amount": 19}, "c1")), effects=effects)
    assert (small.content, small.status) == ("refunded 19 on A-1", "success"), "under the limit: the line did not match, the row's otherwise allows"
    (big,) = run([refund_tool(ran)], calls_message(("issue_refund", {"order_id": "A-2", "amount": 249}, "c2")), effects=effects)
    assert big.status == "error" and not big.content.endswith("condition_unevaluable"), "over the limit: refused by the rule, not for an argument it could not read"
    assert ran.calls == [("issue_refund", "A-1", 19)], "the 249 refund never ran"
    refused = [a for a in call_spans(exported(collector)) if a["runbound.outcome"] == "refused"]
    assert len(refused) == 1 and "L1" in str(refused[0]["runbound.decision.matched_rule_ids"]) and refused[0]["runbound.decision.reason_code"] != "condition_unevaluable"


@SURFACES
def test_a_rule_that_needs_a_person_asks_about_the_arguments_through_the_middleware(run):
    """The ask path of the same layout: 'amount > 200 -> ask' asks for the 249 refund (the bound arguments in the refusal that no approval plane answers) and lets the 19 through."""
    from test_conditions import cond, line

    ran = Ran()
    start(FakeShared([row("issue_refund", lines=[line("L1", "ask", [cond("amount", ">", 200)], approval={"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []})], otherwise="allow")]), service="lc-early")
    effects = {"issue_refund": {"financial"}}
    (small,) = run([refund_tool(ran)], calls_message(("issue_refund", {"order_id": "A-1", "amount": 19}, "c1")), effects=effects)
    assert small.status == "success" and ran.calls == [("issue_refund", "A-1", 19)], "under the limit: nothing to ask"
    (big,) = run([refund_tool(ran)], calls_message(("issue_refund", {"order_id": "A-2", "amount": 249}, "c2")), effects=effects)
    assert big.status == "error" and big.content == "refused approval_unavailable", "over the limit: it WANTED a person (no wait mode here), not 'condition_unevaluable'"
    assert len(ran.calls) == 1


def test_runbound_check_recognises_an_app_governed_by_the_middleware_and_does_not_say_nothing_is_guarded(tmp_path):
    """W6 (Draco's MVP walk): plain tools + RunboundMiddleware, the guide's lead layout, made ``python -m runbound check app.py`` print 'Nothing is guarded' and exit 1: it only counted @runbound.tool."""
    import subprocess
    import sys
    import textwrap

    app = tmp_path / "agent.py"
    app.write_text(textwrap.dedent("""
        import runbound
        from langchain_core.tools import tool
        from runbound.integrations import langchain_early
        from runbound.integrations.langchain_early import RunboundMiddleware

        runbound.init()
        langchain_early.enable()

        @tool
        def lookup_order(order_id: str) -> str:
            \"\"\"Look up an order.\"\"\"
            return "found"

        MIDDLEWARE = RunboundMiddleware(effects={"lookup_order": {"read"}})
    """))
    done = subprocess.run([sys.executable, "-m", "runbound", "check", str(app)], capture_output=True, text=True, cwd=str(tmp_path), env={**os.environ, "PYTHONPATH": SDK_ROOT})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Nothing is guarded" not in done.stdout and "GUARDED" in done.stdout
    assert "A LangChain middleware was created in this process; it governs the tools of any agent it is attached to (each tool is listed after its first call)." in done.stdout, done.stdout
    assert "installed" not in done.stdout.split("Tools")[0], "it says what is known (a middleware was built), not that an agent has it attached"
    report = json.loads(subprocess.run([sys.executable, "-m", "runbound", "check", "--json", str(app)], capture_output=True, text=True, cwd=str(tmp_path), env={**os.environ, "PYTHONPATH": SDK_ROOT}).stdout)
    assert report["guarded"] is True and report["middleware"]["installed"] is True


@SURFACES
def test_coverage_counts_the_tools_the_middleware_governs_once_they_are_called(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    assert runbound.coverage()["middleware_installed"] is False
    tools = make_tools(ran)
    run([tools["lookup_order"]], calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), effects={"lookup_order": {"read"}})
    report = runbound.coverage()
    assert report["middleware_installed"] is True and report["middleware_tool_names"] == ["lookup_order"]
    assert report["decorated_tools"] == 0, "the decorator's own count is unchanged (and is also what the heartbeat carries)"


def boundary_facts(raw):
    """(call span attributes, start markers' attributes) for what a run exported."""
    return call_spans(raw), [attrs_of(m) for m in markers(raw)]


@SURFACES
def test_a_plain_tool_the_middleware_governs_is_recorded_at_the_langchain_middleware_boundary_on_the_span_and_the_marker(run, collector):
    """F24: with no decorator anywhere the span used to say ``sdk-decorator`` and the console could not tell which boundary decided. The call span, the start marker and its declared target take ONE value."""
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    (message,) = run([tools["lookup_order"]], calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), effects={"lookup_order": {"read"}})
    assert message.status == "success" and ran.calls == [("lookup_order", "A-1")]
    (call,), (marker,) = boundary_facts(exported(collector))
    assert call["runbound.boundary"] == "langchain-middleware" and call["runbound.capability_source"] == "langchain_middleware"
    assert marker["runbound.boundary"] == marker["runbound.declared_target"] == "langchain-middleware", "the start is counted only where the marker's boundary is the declared target: the same value"
    assert str(call["runbound.contract_version"]) == "1" and str(marker["runbound.contract_version"]) == "1"


@SURFACES
def test_a_refusal_by_the_middleware_is_recorded_at_the_same_boundary_and_a_tool_with_no_stated_effects_is_undeclared(run, collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["delete_order"]})
    tools = make_tools(ran)
    (message,) = run([tools["delete_order"]], calls_message(("delete_order", {"order_id": "A-1"}, "c1")))
    assert message.content == "refused tool_blocked"
    (call,), markers_seen = boundary_facts(exported(collector))
    assert call["runbound.boundary"] == "langchain-middleware" and call["runbound.capability_source"] == "undeclared" and markers_seen == []


def test_the_async_path_records_the_same_boundary(collector):
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    agent = create_agent(ScriptedModel(script=[calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), AIMessage(content="done")]), [tools["lookup_order"]],
                         middleware=[lc.RunboundMiddleware(effects={"lookup_order": {"read"}})])
    asyncio.run(agent.ainvoke({"messages": [("user", "go")]}, CONFIG))
    (call,), (marker,) = boundary_facts(exported(collector))
    assert call["runbound.boundary"] == marker["runbound.boundary"] == marker["runbound.declared_target"] == "langchain-middleware"


@SURFACES
def test_a_decorated_tool_under_the_middleware_and_the_callback_handler_keep_sdk_decorator(run, collector):
    """The inner ``@runbound.tool`` decides (the middleware passes the call through), so the boundary that decided is the decorator's; an attached handler is not the middleware either."""
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    inner = decorated_lc_tool(ran)
    run([inner], calls_message(("lookup_dec", {"order_id": "A-1"}, "c1")))
    (call,), (marker,) = boundary_facts(exported(collector))
    assert call["runbound.boundary"] == marker["runbound.boundary"] == marker["runbound.declared_target"] == "sdk-decorator" and call["runbound.capability_source"] == "decorator"


def test_the_legacy_callback_handler_alone_is_recorded_at_the_decorator_boundary(collector):
    from runbound.integrations.langchain import RunboundCallbackHandler

    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    agent = create_agent(ScriptedModel(script=[calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), AIMessage(content="done")]), [tools["lookup_order"]])
    agent.invoke({"messages": [("user", "go")]}, {**CONFIG, "callbacks": [RunboundCallbackHandler()]})
    calls = call_spans(exported(collector))
    assert calls and all(c["runbound.boundary"] == "sdk-decorator" for c in calls)


@SURFACES
def test_a_refusal_by_the_inner_decorator_reaches_the_model_as_the_same_error_tool_message_not_an_exception(run, collector):
    """Draco's natural stacking (a ``tool(...)`` over a ``@runbound.tool`` function, with the middleware): the inner decorator still decides and still refuses; the new path only presents it as the error
    ``ToolMessage`` it uses for its own refusals, so ``agent.invoke`` does not raise and the turn is not lost."""
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["lookup_dec"]})
    inner = decorated_lc_tool(ran)
    (message,) = run([inner], calls_message(("lookup_dec", {"order_id": "A-1"}, "c1")))
    assert (message.content, message.status) == ("refused tool_blocked", "error"), "the same message the new path gives its own refusals"
    assert ran.calls == [], "the inner decorator refused: the body never ran"
    raw = exported(collector)
    assert [a["runbound.outcome"] for a in call_spans(raw)] == ["refused"] and markers(raw) == [], "one refusal, by the decorator, and no start"
    assert lc.duplicate_installs() == {("lookup_dec", "decorator"): 1}


def test_the_callback_handler_stays_authoritative_and_its_refusal_still_raises(collector):
    """Not changed: a refusal by an attached ``RunboundCallbackHandler`` is raised from ``on_tool_start`` as it always was (the dual-install caveat)."""
    from runbound.integrations.langchain import RunboundCallbackHandler

    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["lookup_order"]})
    tools = make_tools(Ran())
    agent = create_agent(ScriptedModel(script=[calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), AIMessage(content="done")]), [tools["lookup_order"]], middleware=[lc.RunboundMiddleware()])
    with pytest.raises(runbound.ExecutionRefused):
        agent.invoke({"messages": [("user", "go")]}, {**CONFIG, "callbacks": [RunboundCallbackHandler()]})


@SURFACES
def test_a_decorated_function_is_recognised_by_the_function_not_by_a_matching_name(run):
    ran, admitted = Ran(), []
    start(FakeShared(), service="lc-early", pre_hooks=[counting_hook(admitted)])
    inner = decorated_lc_tool(ran, lc_name="renamed")  # the LangChain name differs from the decorator's
    (message,) = run([inner], calls_message(("renamed", {"order_id": "A-1"}, "c1")))
    assert message.content == "order A-1: shipped" and admitted == ["lookup_dec"]
    assert lc.duplicate_installs() == {("renamed", "decorator"): 1}


@SURFACES
def test_a_callback_handler_attached_at_runtime_is_the_inner_boundary_and_the_outer_layer_passes_through(run):
    from runbound.integrations.langchain import RunboundCallbackHandler

    ran, admitted = Ran(), []
    start(FakeShared(), service="lc-early", pre_hooks=[counting_hook(admitted)])
    tools = make_tools(ran)
    (message,) = run([tools["lookup_order"]], calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), effects={"lookup_order": {"read"}},
                     config={**CONFIG, "callbacks": [RunboundCallbackHandler()]})
    assert message.content == "order A-1: shipped" and admitted == ["lookup_order"], "admitted once, by the handler"
    assert lc.duplicate_installs() == {("lookup_order", "callback_handler"): 1}


def test_a_handler_attached_to_the_tool_itself_is_found_too():
    from runbound.integrations.langchain import RunboundCallbackHandler

    ran, admitted = Ran(), []
    start(FakeShared(), service="lc-early", pre_hooks=[counting_hook(admitted)])
    lookup = make_tools(ran)["lookup_order"]
    lookup.callbacks = [RunboundCallbackHandler()]
    (message,) = run_node([lookup], calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), effects={"lookup_order": {"read"}})
    assert message.content == "order A-1: shipped" and admitted == ["lookup_order"]
    assert lc.duplicate_installs() == {("lookup_order", "callback_handler"): 1}


@SURFACES
def test_a_same_named_function_elsewhere_is_not_a_duplicate(run):
    ran, admitted = Ran(), []
    start(FakeShared(), service="lc-early", pre_hooks=[counting_hook(admitted)])

    @runbound.tool(name="lookup_order", effects={"read"}, reviewed=True)
    def somewhere_else(order_id: str) -> str:  # decorated, same name, but not the function behind the LangChain tool
        return "elsewhere"

    tools = make_tools(ran)
    (message,) = run([tools["lookup_order"]], calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), effects={"lookup_order": {"read"}})
    assert message.content == "order A-1: shipped" and admitted == ["lookup_order"], "governed by this layer, once"
    assert lc.duplicate_installs() == {}


@SURFACES
def test_a_governed_call_made_inside_a_tool_body_is_a_distinct_boundary_not_a_duplicate(run, collector):
    ran, admitted = Ran(), []
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", pre_hooks=[counting_hook(admitted)])

    @runbound.tool(effects={"read"}, reviewed=True)
    def inner_fn(order_id: str) -> str:
        return f"inner {order_id}"

    @tool
    def outer_tool(order_id: str) -> str:
        """Calls a governed function."""
        return inner_fn(order_id=order_id)

    (message,) = run([outer_tool], calls_message(("outer_tool", {"order_id": "A-1"}, "c1")), effects={"outer_tool": {"read"}})
    assert message.content == "inner A-1" and admitted == ["outer_tool", "inner_fn"], "two boundaries, two admissions"
    assert lc.duplicate_installs() == {}
    outer, inner = sorted(call_spans(exported(collector)), key=lambda a: a.get("runbound.parent_action_id") is not None)
    assert inner["runbound.parent_action_id"] == outer["runbound.action_id"], "nested, not a duplicate: the inner names the outer as its parent"


def test_two_layers_of_this_module_over_one_tool_call_admit_it_once():
    ran, admitted = Ran(), []
    start(FakeShared(), service="lc-early", pre_hooks=[counting_hook(admitted)])
    tools = make_tools(ran)

    class SecondLayer(lc.RunboundMiddleware):  # create_agent refuses two instances of one class
        pass

    agent = create_agent(ScriptedModel(script=[calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), AIMessage(content="done")]), [tools["lookup_order"]],
                         middleware=[lc.RunboundMiddleware(effects={"lookup_order": {"read"}}), SecondLayer(effects={"lookup_order": {"read"}})])
    agent.invoke({"messages": [("user", "go")]}, CONFIG)
    assert admitted == ["lookup_order"] and ran.calls == [("lookup_order", "A-1")]
    assert lc.duplicate_installs() == {("lookup_order", "new_path"): 1}


def test_the_async_path_also_passes_a_decorated_tool_through(collector):
    ran, admitted = Ran(), []
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", pre_hooks=[counting_hook(admitted)])
    inner = decorated_lc_tool(ran)
    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([inner]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = asyncio.run(graph.compile().ainvoke({"messages": [calls_message(("lookup_dec", {"order_id": "A-1"}, "c1"))]}, CONFIG))
    assert [m.content for m in result["messages"] if isinstance(m, ToolMessage)] == ["order A-1: shipped"]
    assert admitted == ["lookup_dec"] and lc.duplicate_installs() == {("lookup_dec", "decorator"): 1}


# --- interrupt classification: a graph interrupt is control flow, not a tool failure ----------------------------------------------------------------------


def test_a_graph_interrupt_raised_in_a_tool_body_is_not_recorded_as_a_tool_error(collector):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import interrupt

    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")

    @tool
    def pausing(order_id: str) -> str:
        """Asks the graph to pause."""
        return interrupt({"order": order_id})

    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([pausing], effects={"pausing": {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = graph.compile(checkpointer=InMemorySaver()).invoke({"messages": [calls_message(("pausing", {"order_id": "A-1"}, "c1"))]}, CONFIG)
    assert "__interrupt__" in result, "the interrupt reached the graph untouched"
    (found,) = call_spans(exported(collector))
    assert found["runbound.outcome"] == "cancelled" and "exception.type" not in found and "error.type" not in found, found


def test_the_classification_holds_for_the_async_path_too(collector):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import interrupt

    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")

    @tool
    async def pausing(order_id: str) -> str:
        """Asks the graph to pause."""
        return interrupt({"order": order_id})

    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([pausing], effects={"pausing": {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = asyncio.run(graph.compile(checkpointer=InMemorySaver()).ainvoke({"messages": [calls_message(("pausing", {"order_id": "A-1"}, "c1"))]}, CONFIG))
    assert "__interrupt__" in result
    (found,) = call_spans(exported(collector))
    assert found["runbound.outcome"] == "cancelled" and "exception.type" not in found


# --- 04-3: sync/async execution and per-invocation context isolation (fresh calls only) ---------------------------------------------------------------
# AT-07 is NOT passed in full by these: no approval, resume, or cancel-stops-the-body claim is made; a cancelled await is recorded as cancelled and nothing more.

import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

QUOTA = {"limit": 50, "window": "minute", "per": "service"}


def lookup_graph(effects=None):
    ran = Ran()
    tools = make_tools(ran)
    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools(list(tools.values()), effects=effects or {"lookup_order": {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile(), ran


def per_call(n: int, deny: bool):
    name = "delete_order" if deny else "lookup_order"
    return calls_message((name, {"order_id": f"A-{n}"}, f"call-{n}")), {"configurable": {"thread_id": f"thread-{n}"}}


def check_isolation(results, collector, count):
    """Every invocation got ITS OWN answer, ids and (hashed) arguments; nothing leaked between them."""
    for n, messages in enumerate(results):
        (message,) = [m for m in messages if isinstance(m, ToolMessage)]
        if n % 2:
            assert message.content == "refused tool_blocked", n
        else:
            assert message.content == f"order A-{n}: shipped", n
    found = call_spans(exported(collector))
    assert len(found) == count
    assert len({a["runbound.action_id"] for a in found}) == count and len({a["runbound.evaluation_id"] for a in found}) == count
    assert sorted(a["runbound.outcome"] for a in found) == ["ok"] * (count // 2) + ["refused"] * (count // 2)
    assert ids.CURRENT.get() is None and lc._HELD.get() == frozenset(), "nothing is left set on the calling context"


def test_concurrent_async_invocations_never_share_ids_answers_or_context(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["delete_order"]})
    app, ran = lookup_graph()

    async def many():
        return await asyncio.gather(*[app.ainvoke({"messages": [m]}, c) for m, c in (per_call(n, deny=bool(n % 2)) for n in range(8))])

    results = [r["messages"] for r in asyncio.run(many())]
    check_isolation(results, collector, 8)
    assert sorted(c[1] for c in ran.calls) == [f"A-{n}" for n in range(0, 8, 2)], "each allowed call ran exactly once, with its own argument"


def test_concurrent_sync_invocations_in_threads_never_share_ids_answers_or_context(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["delete_order"]})
    app, ran = lookup_graph()

    def one(n: int):
        message, config = per_call(n, deny=bool(n % 2))
        return app.invoke({"messages": [message]}, config)["messages"]

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(one, range(8)))
    check_isolation(results, collector, 8)
    assert sorted(c[1] for c in ran.calls) == [f"A-{n}" for n in range(0, 8, 2)]


def test_a_sync_graph_and_an_async_graph_running_at_the_same_time_do_not_cross(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["delete_order"]})
    app, ran = lookup_graph()
    out: dict = {}

    def sync_side():
        message, config = per_call(0, deny=False)
        out["sync"] = app.invoke({"messages": [message]}, config)["messages"]

    thread = threading.Thread(target=sync_side)
    thread.start()
    message, config = per_call(1, deny=True)
    out["async"] = asyncio.run(app.ainvoke({"messages": [message]}, config))["messages"]
    thread.join()
    assert [m.content for m in out["sync"] if isinstance(m, ToolMessage)] == ["order A-0: shipped"]
    assert [m.content for m in out["async"] if isinstance(m, ToolMessage)] == ["refused tool_blocked"]
    assert sorted(a["runbound.outcome"] for a in call_spans(exported(collector))) == ["ok", "refused"]


@SURFACES
def test_several_tool_calls_in_one_step_are_each_admitted_once_with_their_own_arguments_and_ids(run, collector):
    ran, admitted = Ran(), []
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", pre_hooks=[lambda call: admitted.append((call.name, call.kwargs["order_id"]))], tool_policy={"deny": ["delete_order"]})
    tools = make_tools(ran)
    message = calls_message(*[("lookup_order" if n % 2 == 0 else "delete_order", {"order_id": f"A-{n}"}, f"call-{n}") for n in range(4)])
    messages = run(list(tools.values()), message, effects={"lookup_order": {"read"}})
    assert sorted((m.tool_call_id, m.content) for m in messages) == [("call-0", "order A-0: shipped"), ("call-1", "refused tool_blocked"), ("call-2", "order A-2: shipped"), ("call-3", "refused tool_blocked")]
    assert sorted(admitted) == [("delete_order", "A-1"), ("delete_order", "A-3"), ("lookup_order", "A-0"), ("lookup_order", "A-2")], "the pre-hook stage precedes the policy rules: all four were admitted once, each with its own argument"
    found = call_spans(exported(collector))
    assert len(found) == 4 and len({a["runbound.action_id"] for a in found}) == 4


def test_an_admission_that_waits_on_the_plane_does_not_block_the_event_loop(collector):
    def slow_take(body):
        time.sleep(0.5)
        return 200, {"granted": True, "retry_after_s": 0}

    start(FakeShared([row("lookup_order", quota=QUOTA)], take=slow_take), otel_endpoint=collector.url, service="lc-early")
    app, _ = lookup_graph()
    ticks: list = []

    async def main():
        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        task = asyncio.create_task(ticker())
        message, config = per_call(0, deny=False)
        result = await app.ainvoke({"messages": [message]}, config)
        task.cancel()
        return result

    result = asyncio.run(main())
    assert [m.content for m in result["messages"] if isinstance(m, ToolMessage)] == ["order A-0: shipped"]
    assert len(ticks) >= 6, f"the loop kept running while the plane was awaited ({len(ticks)} ticks in ~0.5 s)"


def test_a_cancelled_await_is_recorded_as_cancelled_and_the_body_is_not_started_after_it(collector):
    release = threading.Event()

    def slow_take(body):
        release.wait(5)
        return 200, {"granted": True, "retry_after_s": 0}

    start(FakeShared([row("lookup_order", quota=QUOTA)], take=slow_take), otel_endpoint=collector.url, service="lc-early")
    app, ran = lookup_graph()

    async def main():
        message, config = per_call(0, deny=False)
        task = asyncio.create_task(app.ainvoke({"messages": [message]}, config))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    release.set()  # the worker thread cannot be interrupted: it finishes its wait on its own
    time.sleep(0.3)
    raw = exported(collector)
    assert [a["runbound.outcome"] for a in call_spans(raw)] == ["cancelled"], "recorded as cancelled; nothing claims the wait was stopped"
    assert ran.calls == [] and markers(raw) == [], "cancelled while admitting: the body never started"


def test_an_async_body_cancelled_mid_run_is_recorded_as_cancelled_with_its_start_marker(collector):
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    @tool
    async def slow_lookup(order_id: str) -> str:
        """A slow async tool."""
        await asyncio.sleep(5)
        return "late"

    graph = StateGraph(MessagesState)
    graph.add_node("tools", lc.govern_tools([slow_lookup], effects={"slow_lookup": {"read"}}))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()

    async def main():
        task = asyncio.create_task(app.ainvoke({"messages": [calls_message(("slow_lookup", {"order_id": "A-1"}, "c1"))]}, CONFIG))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    raw = exported(collector)
    assert [a["runbound.outcome"] for a in call_spans(raw)] == ["cancelled"] and len(markers(raw)) == 1, "the body had started: cancelled, effects unknown"


def test_exclusion_a_repeated_tool_call_id_in_one_task_gets_random_ids_never_a_merged_action(collector):
    """In a bare ToolNode both calls of a step run in ONE task (one checkpoint namespace), so a shared tool_call_id would derive ONE action_id for two calls."""
    ran = Ran()
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    tools = make_tools(ran)
    message = calls_message(("lookup_order", {"order_id": "A-1"}, "dup"), ("lookup_order", {"order_id": "A-2"}, "dup"))
    run_node(list(tools.values()), message, effects={"lookup_order": {"read"}})
    found = call_spans(exported(collector))
    assert len(found) == 2
    assert len({a["runbound.action_id"] for a in found}) == 2 and len({a["runbound.attempt_id"] for a in found}) == 2


QUOTA_ROW = {"limit": 50, "window": "minute", "per": "service"}


@SURFACES
def test_at17_a_decorated_tool_under_the_new_path_takes_exactly_one_quota_unit_per_call(run):
    ran = Ran()
    shared = FakeShared([row("lookup_dec", quota=QUOTA_ROW)])
    start(shared, service="lc-early")
    inner = decorated_lc_tool(ran)
    for n in range(3):
        run([inner], calls_message(("lookup_dec", {"order_id": f"A-{n}"}, f"c{n}")))
    assert len(ran.calls) == 3
    assert len(shared.takes()) == 3, "one quota take per call, never two: the outer layer passed through"


@SURFACES
def test_at17_a_runtime_callback_handler_under_the_new_path_takes_exactly_one_quota_unit_per_call(run):
    from runbound.integrations.langchain import RunboundCallbackHandler

    ran = Ran()
    shared = FakeShared([row("lookup_order", quota=QUOTA_ROW)])
    start(shared, service="lc-early")
    tools = make_tools(ran)
    for n in range(3):
        run([tools["lookup_order"]], calls_message(("lookup_order", {"order_id": f"A-{n}"}, f"c{n}")), effects={"lookup_order": {"read"}},
            config={**CONFIG, "callbacks": [RunboundCallbackHandler()]})
    assert len(ran.calls) == 3 and len(shared.takes()) == 3, "the handler is the inner boundary: one take per call"
