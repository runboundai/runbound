"""LC-04-1..3 on Helios's real-framework harness (``lc_harness``): the same scenarios, ledger and parity cases, driven through ``RunboundMiddleware`` (create_agent) and ``govern_tools`` (bare ToolNode).

Partial by label: fresh allow/deny only (LC-EARLY-3). AT-03 and AT-09 subsets, the unsupported-ask stop, and the two documented exclusions as they behave on the new path.
"""

from __future__ import annotations

import pytest

from . import lc_harness as h

pytestmark = pytest.mark.skipif(not h.available(), reason="needs the real langchain / langgraph (extra: langchain-test)")

import runbound  # noqa: E402
from runbound import api, spans  # noqa: E402
from test_policy_rows import FakeShared, row, start  # noqa: E402
from test_spans import Collector, attrs_of  # noqa: E402

EFFECTS = {"lookup_order": {"read"}, "issue_refund": {"financial"}, "send_email": {"write"}}


def lc():
    from runbound.integrations import langchain_early

    langchain_early.enable()
    return langchain_early


def governor(effects=EFFECTS):
    def plug(layout, tools):
        module = lc()
        if layout == "create_agent":
            return tools, [module.RunboundMiddleware(effects=effects)]
        return module.govern_tools(tools, effects=effects), ()

    return plug


LAYOUTS = pytest.mark.parametrize("run", [h.run_create_agent, h.run_tool_node], ids=["create_agent", "tool_node"])


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def call_spans(collector):
    spans.exporter().flush(5.0)
    return [attrs_of(s) for s in collector.all_spans() if s["name"].startswith("execute_tool")]


def start_markers(collector):
    return [s for s in collector.all_spans() if s["name"] == "execution_started"]


@LAYOUTS
def test_at_03_three_denials_are_three_requested_three_attempts_three_evaluations_zero_starts(run, collector):
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early", tool_policy={"deny": ["issue_refund"]})
    result = run(h.three_denials(), governor())
    assert result.error is None and result.ledger.starts == [], "0 starts: no body began"
    assert [m.content for m in result.tool_messages()] == ["refused tool_blocked"] * 3
    found = call_spans(collector)
    assert len(found) == 3 and all(a["runbound.outcome"] == "refused" for a in found)
    for key in ("runbound.action_id", "runbound.attempt_id", "runbound.evaluation_id"):
        assert len({a[key] for a in found}) == 3, key
    assert start_markers(collector) == []


@LAYOUTS
@pytest.mark.parametrize("case", h.PARITY_CASES, ids=lambda c: c["id"])
def test_at_09_the_decision_equals_the_python_sdk_decorators_for_the_same_case(run, case):
    expected = h.decorator_outcome(case)
    assert expected == case["expect"]
    api._teardown_for_tests()
    start(FakeShared(), service="lc-early", tool_policy=runbound.ToolPolicy(**case["policy"]))
    result = run([[case["call"]]], governor())
    assert result.error is None
    body_ran = bool(result.ledger.starts)
    assert ("allow" if body_ran else "deny") == expected


@LAYOUTS
def test_the_unsupported_ask_stops_before_the_body_and_before_anything_is_asked(run):
    case = h.ask_case()
    shared = FakeShared([row("issue_refund", approval_required=True)])
    start(shared, service="lc-early")
    result = run([[case["call"]]], governor())
    assert result.error is None and result.ledger.starts == []
    assert [m.content for m in result.tool_messages()] == ["refused approval_unavailable"]
    assert not [c for c in shared.calls if "/v1/approvals" in str(c)]


@LAYOUTS
def test_exclusion_parallel_calls_in_one_step_are_each_admitted_once_with_their_own_ids_and_no_replay_claim(run, collector):
    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    result = run(h.parallel_in_one_task(), governor())
    assert result.error is None and sorted(result.ledger.names()) == ["issue_refund", "lookup_order"]
    found = call_spans(collector)
    assert len(found) == 2 and len({a["runbound.action_id"] for a in found}) == 2 and len({a["runbound.attempt_id"] for a in found}) == 2


def test_a_unique_tool_call_id_with_a_thread_derives_the_ids_on_the_harness_too(collector):
    from langgraph.checkpoint.memory import InMemorySaver

    start(FakeShared(), otel_endpoint=collector.url, service="lc-early")
    h.run_create_agent([[h.Call("lookup_order", {"order_id": "o-1"}, "only")]], governor(), checkpointer=InMemorySaver(), thread_id="t-7")
    h.run_create_agent([[h.Call("lookup_order", {"order_id": "o-1"}, "only")]], governor(), checkpointer=InMemorySaver(), thread_id="t-8")
    first, second = call_spans(collector)
    assert first["runbound.action_id"] != second["runbound.action_id"]
