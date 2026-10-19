"""The LangChain harness proves ITSELF against the real packages, ungoverned: the scripted model really drives both layouts and the ledger really records starts.

Skipped unless the real packages are installed (``pip install -e ".[langchain-test]"``); the versions it ran with are printed by ``test_the_exact_versions_are_recorded``."""

import pytest

from . import lc_harness as h

pytestmark = pytest.mark.skipif(not h.available(), reason="needs the real langchain / langgraph (extra: langchain-test)")


def test_the_exact_versions_are_recorded_and_inside_the_declared_pins():
    print("LC VERSIONS:", h.record())
    assert h.versions()["langchain"] and h.versions()["langgraph"] and h.in_declared_pins()


@pytest.mark.parametrize("layout", ["create_agent", "tool_node"])
def test_an_ungoverned_run_executes_every_scripted_call_in_order(layout):
    run = (h.run_create_agent if layout == "create_agent" else h.run_tool_node)(h.three_denials())
    assert run.error is None and run.ledger.names() == ["issue_refund"] * 3
    assert [m.tool_call_id for m in run.tool_messages()] == ["call_1", "call_2", "call_3"] and run.messages[-1].content == "done"


def test_a_governor_receives_the_layout_and_the_typed_tools_and_may_block_the_body():
    seen = []

    def deny_all(layout, tools):
        seen.append((layout, [t.name for t in tools], sorted(tools[1].args_schema.model_fields)))
        for t in tools:
            t.func = lambda **kw: (_ for _ in ()).throw(PermissionError("denied"))
        return tools, ()

    for run_layout in (h.run_create_agent, h.run_tool_node):
        run = run_layout(h.three_denials(), deny_all)
        assert isinstance(run.error, PermissionError) and run.ledger.starts == []
    assert seen[0] == ("create_agent", ["lookup_order", "issue_refund", "send_email"], ["amount_cents", "order_id"]) and seen[1][0] == "tool_node"


def test_the_exclusion_scripts_put_two_calls_in_one_step():
    run = h.run_tool_node(h.parallel_in_one_task())
    assert run.error is None and run.ledger.names() == ["lookup_order", "issue_refund"]
    assert h.repeated_tool_call_id()[0][0].id == h.repeated_tool_call_id()[0][1].id


@pytest.mark.parametrize("case", h.PARITY_CASES, ids=lambda c: c["id"])
def test_the_decorator_decides_each_parity_case_as_the_fixture_expects(case):
    assert h.decorator_outcome(case) == case["expect"]
