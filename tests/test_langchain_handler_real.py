"""AAI-04 Gate B gap G-13: the OLD ``RunboundCallbackHandler`` against the REAL ``langchain_core``.

``tests/test_langchain.py`` (28 tests) drives the handler with a FAKE ``langchain_core`` injected into ``sys.modules``. Before any behavioural migration off the handler the same scenarios
must hold on the real package. This module (1) re-runs EVERY test of test_langchain.py with the fake replaced by the real ``BaseCallbackHandler`` (the test functions are imported, not copied,
so the two files cannot drift), except the one whose premise the real package falsifies (replaced below), and (2) adds end-to-end tests through a real ``BaseTool.invoke`` / ``ainvoke`` with the
handler in ``config["callbacks"]``, so the shapes LangChain really hands the callbacks (``serialized``, ``input_str``, ``run_id``, ``inputs``) are the ones under test.

The handler is NOT changed by this file. Guarded: skipped when ``langchain_core`` is absent (the sdk-unit gate venv has none).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("langchain_core")

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import runbound  # noqa: E402
from runbound import api  # noqa: E402
from runbound.exceptions import GuardrailTripped, PolicyViolation  # noqa: E402
from runbound.integrations import langchain as lc  # noqa: E402
import test_langchain as base  # noqa: E402
from test_langchain import recorder  # noqa: E402

EXCLUDED = {"test_the_handler_has_no_model_call_hooks"}  # real BaseCallbackHandler defines no-op on_llm_* methods; replaced below
for _name in dir(base):
    if _name.startswith("test_") and _name not in EXCLUDED:
        globals()[_name] = getattr(base, _name)


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    lc._HANDLER_CLASS = None
    yield
    api._teardown_for_tests()
    lc._HANDLER_CLASS = None


@pytest.fixture
def base_handler_class():
    return BaseCallbackHandler  # the REAL base class, where test_langchain.py injects a fake one


@pytest.fixture
def handler(base_handler_class):
    return lc.RunboundCallbackHandler()


def test_the_handler_adds_no_model_call_hooks_of_its_own(handler):
    """On the real package the base class DEFINES on_llm_end / on_llm_error as no-ops; the handler must not override them (model calls are not the SDK's business)."""
    for name in ("on_llm_end", "on_llm_error", "on_llm_start", "on_chat_model_start"):
        assert getattr(type(handler), name) is getattr(BaseCallbackHandler, name), f"{name} is overridden"


def test_the_real_base_class_is_what_the_handler_extends(handler):
    assert isinstance(handler, BaseCallbackHandler) and handler.raise_error is True


# --- end to end through a real tool ------------------------------------------------------------------------------------------------------------------------------


def make_tools(ran):
    @tool
    def search(query: str) -> str:
        """Search."""
        ran.append(("search", query))
        return f"results for {query}"

    @tool
    def wire_money(amount: str) -> str:
        """Wire money."""
        ran.append(("wire_money", amount))
        return "wired"

    @tool
    def explode(query: str) -> str:
        """Always fails."""
        ran.append(("explode", query))
        raise ValueError("kaboom")

    return search, wire_money, explode


def test_a_real_tool_call_emits_a_tool_call_and_an_ok_result_with_the_real_name_and_input(handler):
    runbound.init()
    spy = recorder(results=True)
    ran: list = []
    search, _, _ = make_tools(ran)
    assert search.invoke({"query": "cats"}, config={"callbacks": [handler]}) == "results for cats"
    kinds = [(e.kind, e.tool_name) for e in spy.events]
    assert kinds == [("tool_call", "search"), ("tool_result", "search")]
    result = spy.events[1]
    assert result.outcome == "ok" and result.duration_s >= 0.0, "the real run_id paired the start with the end"
    assert ran == [("search", "cats")]


def test_a_real_denied_tool_raises_out_of_invoke_and_the_body_never_runs(handler):
    runbound.init(tool_policy=runbound.ToolPolicy(deny=["wire_money"]))
    spy = recorder(results=True)
    ran: list = []
    _, wire_money, _ = make_tools(ran)
    with pytest.raises(PolicyViolation):
        wire_money.invoke({"amount": "100"}, config={"callbacks": [handler]})
    assert ran == [], "the refusal came from the callback, before the body"
    assert [e.outcome for e in spy.events if e.kind == "tool_result"] == ["refused"]
    assert runbound.current_session().refused_actions == 1


def test_a_real_tool_failure_is_recorded_and_the_tools_own_error_is_the_one_raised(handler):
    runbound.init()
    spy = recorder(results=True)
    ran: list = []
    _, _, explode = make_tools(ran)
    with pytest.raises(ValueError, match="kaboom"):
        explode.invoke({"query": "x"}, config={"callbacks": [handler]})
    assert [e.kind for e in spy.events if e.kind == "tool_error"] == ["tool_error"]
    assert [e.outcome for e in spy.events if e.kind == "tool_result"] == ["error"]
    assert [e.error for e in spy.events if e.kind == "tool_error"] == ["kaboom"]


def test_the_same_real_input_hashes_identically_and_a_different_one_does_not(handler):
    runbound.init()
    spy = recorder()
    search, _, _ = make_tools([])
    for query in ("cats", "cats", "dogs"):
        search.invoke({"query": query}, config={"callbacks": [handler]})
    hashes = [e.args_hash for e in spy.events if e.kind == "tool_call"]
    assert hashes[0] == hashes[1] != hashes[2]


def test_max_calls_counts_real_tool_calls(handler):
    runbound.init(tool_policy=runbound.ToolPolicy(max_calls={"search": 2}))
    search, _, _ = make_tools([])
    search.invoke({"query": "a"}, config={"callbacks": [handler]})
    search.invoke({"query": "b"}, config={"callbacks": [handler]})
    with pytest.raises(GuardrailTripped):
        search.invoke({"query": "c"}, config={"callbacks": [handler]})
    assert runbound.tool_calls() == {"search": 3}


def test_a_real_tool_joins_the_report_undecorated(handler):
    runbound.init()
    search, _, _ = make_tools([])
    search.invoke({"query": "a"}, config={"callbacks": [handler]})
    (entry,) = [t for t in runbound.tools() if t["name"] == "search"]
    assert entry["decorated"] is False and entry["effects"] == []


def test_the_async_path_raises_a_refusal_and_records_results_too(handler):
    runbound.init(tool_policy=runbound.ToolPolicy(deny=["wire_money"]))
    spy = recorder(results=True)
    ran: list = []
    search, wire_money, _ = make_tools(ran)

    async def main():
        assert await search.ainvoke({"query": "cats"}, config={"callbacks": [handler]}) == "results for cats"
        with pytest.raises(PolicyViolation):
            await wire_money.ainvoke({"amount": "100"}, config={"callbacks": [handler]})

    asyncio.run(main())
    assert ran == [("search", "cats")]
    assert [e.outcome for e in spy.events if e.kind == "tool_result"] == ["ok", "refused"]


def test_the_handler_never_breaks_a_real_chain_when_the_engine_fails(handler, monkeypatch):
    runbound.init()

    def boom(session, event):
        raise RuntimeError("engine is broken")

    monkeypatch.setattr(api._ENGINE, "process", boom)
    search, _, _ = make_tools([])
    assert search.invoke({"query": "cats"}, config={"callbacks": [handler]}) == "results for cats", "fail-open: the host's chain still ran"
