"""AAI-1 FR-1 (PS-AAI-03a): a governed call carries separate identities for its action, its attempt and its evaluation.

* ``action_id`` and ``attempt_id`` are minted when the decorator accepts the call; ``evaluation_id`` once, when the engine starts the chain;
* they ride on the span, on the refusal's anomaly details and on the approval request, and the three of one call are the same on all of them;
* a governed call made inside another governed body is a NEW action naming ``parent_action_id``; a context that is not copied (a bare thread) names none;
* the span says which boundary saw the call and the version of this contract;
* nothing here changes a decision.
"""

from __future__ import annotations

import asyncio
import re
import threading

import pytest

import runbound
from runbound import api, ids, spans
from test_policy_rows import FakeShared, row, start
from test_spans import Collector, attrs_of

HEX = re.compile(r"^[0-9a-f]{32}$")
KEYS = ("action_id", "attempt_id", "evaluation_id")
ATTR = {key: f"runbound.{key}" for key in (*KEYS, "parent_action_id")}


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


def exported(collector):
    spans.exporter().flush(5.0)
    return [attrs_of(span) for span in collector.spans()]


def ids_on(attrs):
    return {key: attrs.get(ATTR[key]) for key in (*KEYS, "parent_action_id")}


def tool(name="act", effects=("read",), **rules):
    @runbound.tool(name=name, effects=set(effects), reviewed=True, **rules)
    def act(size: int = 0):
        return "done"

    return act


def test_an_allowed_call_carries_three_distinct_ids_the_boundary_and_the_contract_version(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    tool()(size=1)
    (attrs,) = exported(collector)
    found = ids_on(attrs)
    assert all(HEX.match(found[key]) for key in KEYS), found
    assert len({found[key] for key in KEYS}) == 3, "action, attempt and evaluation are three different identities"
    assert found["parent_action_id"] is None
    assert attrs["runbound.boundary"] == "sdk-decorator" and str(attrs["runbound.contract_version"]) == "1"


def test_every_call_is_a_new_action_even_with_the_same_arguments(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    act = tool()
    act(size=1)
    act(size=1)
    first, second = (ids_on(a) for a in exported(collector))
    assert first["action_id"] != second["action_id"] and first["attempt_id"] != second["attempt_id"] and first["evaluation_id"] != second["evaluation_id"]


def test_a_refused_call_carries_the_same_ids_on_its_span_and_on_its_refusal(collector):
    start(FakeShared([row("act", blocked=True)]), otel_endpoint=collector.url)
    with pytest.raises(runbound.ExecutionRefused) as refused:
        tool()(size=1)
    (attrs,) = exported(collector)
    on_span, on_anomaly = ids_on(attrs), refused.value.anomaly.details
    assert all(HEX.match(on_span[key]) for key in KEYS)
    assert {key: on_anomaly[key] for key in KEYS} == {key: on_span[key] for key in KEYS}, "the span and the anomaly are provably the same refusal"
    assert refused.value.decision.evaluation["evaluation_id"] == on_span["evaluation_id"]


def test_the_evaluation_id_is_minted_once_even_when_a_conditional_row_builds_the_record_twice(collector, monkeypatch):
    minted = []
    real = ids.mint
    monkeypatch.setattr(ids, "mint", lambda: minted.append(1) or real())
    lines = [{"id": "L1", "decision": "deny", "when": {"all": [{"arg": "size", "op": ">", "value": 1000}]}}]
    start(FakeShared([row("act", lines=lines, otherwise="allow")]), otel_endpoint=collector.url)
    tool()(size=5)  # the conditional row re-builds the record after it resolves on this call's arguments
    assert len(minted) == 3, "one action, one attempt, one evaluation"
    (attrs,) = exported(collector)
    assert HEX.match(attrs[ATTR["evaluation_id"]])


def test_a_governed_call_inside_a_governed_body_is_a_new_action_naming_its_parent(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    inner = tool("inner")

    @runbound.tool(effects={"read"}, reviewed=True)
    def outer():
        return inner(size=1)

    outer()
    by_tool = {a["gen_ai.tool.name"]: ids_on(a) for a in exported(collector)}
    assert by_tool["outer"]["parent_action_id"] is None
    assert by_tool["inner"]["parent_action_id"] == by_tool["outer"]["action_id"]
    assert by_tool["inner"]["action_id"] != by_tool["outer"]["action_id"], "never the same action"
    assert by_tool["inner"]["attempt_id"] != by_tool["outer"]["attempt_id"]


def test_the_parent_survives_asyncio_to_thread_and_is_unknown_in_a_bare_thread(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    via_to_thread, via_bare_thread = tool("via_to_thread"), tool("via_bare_thread")

    @runbound.tool(effects={"read"}, reviewed=True)
    async def outer():
        await asyncio.to_thread(via_to_thread, size=1)
        worker = threading.Thread(target=lambda: via_bare_thread(size=1))  # a bare thread does not copy the context
        worker.start()
        worker.join()

    asyncio.run(outer())
    by_tool = {a["gen_ai.tool.name"]: ids_on(a) for a in exported(collector)}
    assert by_tool["via_to_thread"]["parent_action_id"] == by_tool["outer"]["action_id"]
    assert by_tool["via_bare_thread"]["parent_action_id"] is None, "context lost: no parent recorded (indistinguishable here from a top-level call)"


def test_a_call_after_the_body_is_not_nested(collector):
    start(FakeShared(), otel_endpoint=collector.url)
    act = tool()
    act()
    act()
    assert ids.CURRENT.get() is None
    assert all(ids_on(a)["parent_action_id"] is None for a in exported(collector))


def test_the_approval_request_carries_the_ids_of_the_call_that_asked(collector):
    shared = start(FakeShared([row("act", approval_required=True)]), otel_endpoint=collector.url)
    tool()(size=1)  # approved by the fake plane
    (_, _, body), = [c for c in shared.calls if c[1] == "/v1/approvals"]
    (attrs,) = exported(collector)
    assert {key: body[key] for key in KEYS} == {key: attrs[ATTR[key]] for key in KEYS}


def test_a_call_with_ids_decides_exactly_as_before(collector):
    start(FakeShared([row("act", blocked=True)]), otel_endpoint=collector.url)
    with pytest.raises(runbound.ExecutionRefused) as refused:
        tool()(size=1)
    assert refused.value.reason == "tool_blocked"
