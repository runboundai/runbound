"""AAI-1 FR-3 (PS-AAI-03c): the execution start marker.

A zero-length child span ``execution_started`` is ended at the one site just before the tool body runs. It exists exactly once for an attempt that reaches the body and never for a
call that does not (refused, parked for approval, refused again by the re-check after an approval). It names the call by its ids and carries no tool name and no argument.
"""

from __future__ import annotations

import asyncio

import pytest

import runbound
from runbound import api, approvals, spans
from test_approvals import FakePlane
from test_policy_rows import FakeShared, row, start
from test_spans import Collector, attrs_of

MARKER = "execution_started"


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
    return collector.all_spans()


def markers(raw):
    return [s for s in raw if s["name"] == MARKER]


def calls(raw):
    return [s for s in raw if s["name"].startswith("execute_tool")]


def test_an_allowed_call_has_exactly_one_marker_that_is_a_zero_length_child_of_its_span(collector):
    start(FakeShared(), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    def act(size: int = 0):
        return "done"

    act(size=1)
    raw = exported(collector)
    (marker,), (call,) = markers(raw), calls(raw)
    assert marker["parentSpanId"] == call["spanId"] and marker["traceId"] == call["traceId"]
    assert marker["startTimeUnixNano"] == marker["endTimeUnixNano"], "zero length"
    attrs, call_attrs = attrs_of(marker), attrs_of(call)
    assert attrs["runbound.action_id"] == call_attrs["runbound.action_id"] and attrs["runbound.attempt_id"] == call_attrs["runbound.attempt_id"]
    assert "gen_ai.tool.name" not in attrs, "no tool name under the key rollups count: a rollup of tool calls must not count the marker as one"
    assert not any("size" in key or key.endswith("args") for key in attrs), "no argument"


def test_the_marker_carries_the_attempts_identity_so_it_survives_the_loss_of_its_call_span(collector):
    """Contract rule 9: the call span is exported only when the call ENDS, so a crash in the body loses the span that carries the identity; the marker carries it too."""
    start(FakeShared(), otel_endpoint=collector.url, service="svc-x", worker_id="w-x")

    @runbound.tool(effects={"read"}, reviewed=True)
    def act(size: int = 0):
        return "done"

    with runbound.session("run-x") as state:
        act(size=1)
        session_id = state.session_id
    raw = exported(collector)
    (marker,), (call,) = markers(raw), calls(raw)
    m, c = attrs_of(marker), attrs_of(call)
    for key in ("runbound.action_id", "runbound.attempt_id", "runbound.evaluation_id"):
        assert m[key] == c[key], key
    assert m["runbound.boundary"] == "sdk-decorator" and m["runbound.declared_target"] == "sdk-decorator"
    assert m["runbound.marker.tool"] == "act" and m["runbound.service"] == "svc-x" and m["runbound.worker_id"] == "w-x" and m["runbound.run_id"] == session_id
    assert not any(key in m for key in ("runbound.args_digest", "runbound.decision.reason", "runbound.decision.verdict", "runbound.decision.reason_code")), "no argument, digest, decision or reason"


def test_a_body_that_fails_still_has_its_marker(collector):
    start(FakeShared(), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    def act():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        act()
    raw = exported(collector)
    assert len(markers(raw)) == 1 and len(calls(raw)) == 1
    assert attrs_of(calls(raw)[0])["runbound.outcome"] == "error"


def test_an_async_call_has_one_marker(collector):
    start(FakeShared(), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    async def act():
        return "done"

    asyncio.run(act())
    assert len(markers(exported(collector))) == 1


def test_a_refused_call_has_no_marker(collector):
    start(FakeShared([row("act", blocked=True)]), otel_endpoint=collector.url)

    @runbound.tool(effects={"read"}, reviewed=True)
    def act():
        return "done"

    with pytest.raises(runbound.ExecutionRefused):
        act()
    raw = exported(collector)
    assert markers(raw) == [] and len(calls(raw)) == 1


def test_an_approved_call_the_recheck_then_refuses_has_no_marker(collector):
    class Flips(FakeShared):
        def approvals(self, method, path, body=None, timeout=5.0):
            if path == "/v1/approvals":
                from runbound import policy_rows

                self.rows = policy_rows.parse([row("act", blocked=True)])  # the policy changes while the person decides
            return super().approvals(method, path, body, timeout)

    start(Flips([row("act", approval_required=True)]), otel_endpoint=collector.url)
    ran = []

    @runbound.tool(effects={"read"}, reviewed=True)
    def act():
        ran.append(1)

    with pytest.raises(runbound.ExecutionRefused) as refused:
        act()
    assert refused.value.reason == "approval_superseded" and ran == []
    assert markers(exported(collector)) == []


def test_a_parked_deferred_call_has_no_marker_and_its_resumed_execution_has_exactly_one(collector):
    plane = FakePlane((201, {"id": "a1", "status": "pending"}))
    runbound.init(otel_endpoint=collector.url, token="rb_secret_key", service="acme", worker_id="w-3")
    api._ENGINE.shared = plane

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        return ticket

    with runbound.session("run-a"):
        out = archive(7)
    assert markers(exported(collector)) == [], "parked: the body has not run"
    create = [c for c in plane.calls if c[1] == "/v1/approvals"][-1][2]
    approvals._execute({"id": "a1", "tool": "archive", "args": create["args"], "request_id": out["request_id"], "service": create["service"], "expires_at": "2099-01-01T00:00:00Z"})
    assert len(markers(exported(collector))) == 1, "marked once, at the wrapper after the re-check, not at the executor"


def _marker_and_call(collector, monkeypatch, version):
    if version is None:
        monkeypatch.delenv("RUNBOUND_SERVICE_VERSION", raising=False)
    else:
        monkeypatch.setenv("RUNBOUND_SERVICE_VERSION", version)
    start(FakeShared(), otel_endpoint=collector.url, service="svc-x", worker_id="w-x")

    @runbound.tool(effects={"read"}, reviewed=True)
    def act(size: int = 0):
        return "done"

    with runbound.session("run-v"):
        act(size=1)
    (marker,), (call,) = markers(exported(collector)), calls(exported(collector))
    return attrs_of(marker), attrs_of(call)


def test_the_marker_carries_the_service_version_the_call_span_carries(collector, monkeypatch):
    """Action drift compares cohorts by version: a start whose marker has no version would fall into 'unknown'."""
    m, c = _marker_and_call(collector, monkeypatch, "2.4.1")
    assert m["runbound.service_version"] == c["runbound.service_version"] == "2.4.1"


def test_the_marker_has_no_service_version_when_the_deployment_states_none(collector, monkeypatch):
    m, c = _marker_and_call(collector, monkeypatch, None)
    assert "runbound.service_version" not in m and "runbound.service_version" not in c
