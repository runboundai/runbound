"""AAI-04 Gate B gap G-3 (AT-03, "recorded restrictions"): the span of a refusal that came through the LangChain path carries the same decision record as the decorator's.

``tests/contract/test_decision_record.py`` pins what a decorator refusal records (reason code, matched rule ids, policy version, policy state, contributors, sdk version). This runs the SAME
fixtures through ``govern_tools`` and ``RunboundMiddleware`` and compares every one of those attributes with the decorator's. Real langchain / langgraph, guarded.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

import runbound  # noqa: E402
from runbound import api, spans  # noqa: E402
from runbound.integrations import langchain_early as lc  # noqa: E402
from test_langchain_early import calls_message, make_tools, run_agent, run_node  # noqa: E402
from test_policy_rows import FakeShared, row, start  # noqa: E402
from test_spans import Collector, attrs_of  # noqa: E402

RECORD = ("runbound.decision.reason_code", "runbound.decision.policy_state", "runbound.decision.policy_version", "runbound.decision.matched_rule_ids",
          "runbound.decision.contributors", "runbound.sdk.version", "runbound.decision.verdict", "runbound.decision.boundary", "runbound.decision.level")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.setattr(lc, "_enabled", True)
    lc._REGISTERED.clear()
    yield
    api._teardown_for_tests()


def refused_attrs(collector):
    spans.exporter().flush(5.0)
    found = [attrs_of(s) for s in collector.all_spans() if s["name"].startswith("execute_tool")]
    (one,) = [a for a in found if a.get("runbound.outcome") == "refused"]
    return one


def decorator_record(rows, **config):
    api._teardown_for_tests()
    collector = Collector()
    start(FakeShared(rows), otel_endpoint=collector.url, service="lc-record", **config)

    @runbound.tool(name="lookup_order", effects={"read"}, reviewed=True)
    def lookup_order(order_id: str):
        return "ran"

    with pytest.raises(runbound.ExecutionRefused):
        lookup_order(order_id="A-1")
    attrs = refused_attrs(collector)
    collector.close()
    return attrs


def langchain_record(run, rows, **config):
    api._teardown_for_tests()
    lc._REGISTERED.clear()
    collector = Collector()
    start(FakeShared(rows), otel_endpoint=collector.url, service="lc-record", **config)
    (message,) = run(list(make_tools(type("R", (), {"calls": []})()).values()), calls_message(("lookup_order", {"order_id": "A-1"}, "c1")), effects={"lookup_order": {"read"}})
    assert message.status == "error"
    attrs = refused_attrs(collector)
    collector.close()
    return attrs, message.content


FIXTURES = [
    ("a row blocks the tool", [row("lookup_order", blocked=True)], {}, "tool_blocked"),
    ("the code policy denies the tool", [], {"tool_policy": {"deny": ["lookup_order"]}}, "tool_blocked"),
]


@pytest.mark.parametrize("run", [run_node, run_agent], ids=["govern_tools", "middleware"])
@pytest.mark.parametrize("label,rows,config,reason", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_a_refusal_through_the_langchain_path_records_what_the_decorators_refusal_records(run, label, rows, config, reason):
    expected = decorator_record(rows, **config)
    got, content = langchain_record(run, rows, **config)
    assert content == f"refused {reason}"
    for name in RECORD:
        assert got.get(name) == expected.get(name), f"{label}: {name}: decorator {expected.get(name)!r}, langchain {got.get(name)!r}"
    assert got["runbound.decision.reason_code"] == reason and got["runbound.decision.policy_version"], got


@pytest.mark.parametrize("run", [run_node, run_agent], ids=["govern_tools", "middleware"])
def test_a_row_refusal_names_the_matched_rule_and_the_policy_version_it_was_decided_under(run):
    got, _ = langchain_record(run, [row("lookup_order", blocked=True)])
    assert got["runbound.decision.matched_rule_ids"] == ["lookup_order#L1"], got
    assert str(got["runbound.decision.policy_version"]) == "7", "the generation the worker held (FakeShared holds 7)"
    assert got["runbound.decision.contributors"] == ["row:L1"]
    assert got["runbound.sdk.version"] == runbound.__version__
