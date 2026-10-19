"""AAI-04-4b, the SDK side of the suspended request (Apollo's four conditions on ``approvals.admit``'s suspend path), against a recording plane double.

The end-to-end rows (interrupt, resume, replay, AT-04/05/06/08) are in tests/integration/test_langchain_resume_plane.py against a real plane.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.tools import StructuredTool  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import runbound  # noqa: E402
from runbound import api, approvals  # noqa: E402
from runbound.exceptions import ExecutionRefused  # noqa: E402
from runbound.integrations import langchain_early as early  # noqa: E402
from test_d2_bind import BindPlane, start  # noqa: E402
from test_policy_rows import row  # noqa: E402
from test_spans import Collector, attrs_of  # noqa: E402

ASK = {"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []}
ASK_ROW = row("issue_refund", rule_revision=1, lines=[{"id": "L1", "decision": "ask", "approval": ASK, "limits": None}], otherwise="inherit")
IDS = {"action_id": "a" * 32, "attempt_id": "b" * 32}


class Refund(BaseModel):
    order_id: str
    amount_cents: int


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    api._teardown_for_tests()
    monkeypatch.setattr(early, "_enabled", True)
    early._REGISTERED.clear()
    yield
    api._teardown_for_tests()


def too_old(refused) -> bool:
    """The refusal's public ``reason`` stays in the closed set (approval_unavailable); the specific code rides the decision, so no public API grew."""
    return refused.value.reason == "approval_unavailable" and refused.value.decision.evaluation["code"] == approvals.PLANE_TOO_OLD


def suspend(**config):
    ran = []
    tool = StructuredTool.from_function(lambda order_id, amount_cents: ran.append(amount_cents) or "refunded", name="issue_refund", description="refund", args_schema=Refund)
    validator, effects = early._register("issue_refund", tool, {"issue_refund": {"financial"}})
    return lambda: api._govern_suspendable("issue_refund", {"order_id": "o", "amount_cents": 5}, lambda final: ran.append(final) or "x", effects=effects, validator=validator, ids=IDS,
                                         text=str, passthrough=(), suspend={"request_id": "req-1"}), ran


def test_a_suspended_ask_is_filed_bind_required_and_raised_before_any_body_as_pending_not_refused():
    collector = Collector()
    try:
        plane = start(BindPlane([ASK_ROW], approve="pending"), approval_bind=True, otel_endpoint=collector.url)
        call, ran = suspend()
        with pytest.raises(approvals.Suspended) as asked:
            call()
        assert ran == [], "no body ran"
        (_, path, body), = [c for c in plane.calls if c[1] == "/v1/approvals"]
        assert body["mode"] == "suspended" and body["capabilities"] == ["bind_v1"] and body["request_id"] == "req-1"
        assert asked.value.args_digest == body["args_digest"] and asked.value.request_id == "req-1" and asked.value.attempt_id == IDS["attempt_id"]
        assert not [c for c in plane.calls if c[1].endswith("/bind")], "nothing is bound at the ask"
        from runbound import spans

        spans.exporter().flush(5.0)
        outcomes = [attrs_of(s).get("runbound.outcome") for s in collector.spans() if s["name"].startswith("execute_tool")]
        assert outcomes == ["pending_approval"], "recorded as pending approval: not refused, not executed"
    finally:
        collector.close()


def test_a_suspended_ask_keeps_no_session_unit():
    plane = start(BindPlane([ASK_ROW], approve="pending"), approval_bind=True, tool_policy=runbound.ToolPolicy(max_calls={"issue_refund": 1}))
    call, _ = suspend()
    with pytest.raises(approvals.Suspended):
        call()
    from runbound import policy_rows

    plane.rows = policy_rows.parse([row("issue_refund", rule_revision=2, lines=[{"id": "L1", "decision": "allow", "approval": None, "limits": None}], otherwise="inherit")])
    call, ran = suspend()
    assert call() == "x" and len(ran) == 1, "the one allowed call of the session is still available: the suspended pass kept no max_calls unit"


def test_the_capability_off_refuses_plane_too_old_before_anything_is_filed():
    plane = start(BindPlane([ASK_ROW], approve="pending"))  # approval_bind is off
    call, ran = suspend()
    with pytest.raises(ExecutionRefused) as refused:
        call()
    assert too_old(refused) and ran == []
    assert not [c for c in plane.calls if c[1] == "/v1/approvals"], "nothing was filed: never a fallback to sync or deferred"


def test_a_plane_that_does_not_know_the_mode_or_files_it_without_the_bind_is_plane_too_old_and_never_waited_on():
    class Old(BindPlane):
        def approvals(self, method, path, body=None, timeout=5.0):
            if path == "/v1/approvals" and method == "POST":
                self.calls.append((method, path, body))
                return 400, {"error": "invalid_approval"}
            return super().approvals(method, path, body, timeout)

    start(Old([ASK_ROW], approve="pending"), approval_bind=True)
    call, ran = suspend()
    with pytest.raises(ExecutionRefused) as refused:
        call()
    assert too_old(refused) and ran == []

    api._teardown_for_tests()
    plane = start(BindPlane([ASK_ROW], approve="pending", bind_required=False), approval_bind=True)  # a plane with the bind off files a legacy request
    call, ran = suspend()
    with pytest.raises(ExecutionRefused) as refused:
        call()
    assert too_old(refused) and ran == []
    assert [c for c in plane.calls if c[1].endswith("/withdraw")], "the request it did file is taken back, never left to expire"


def test_a_quota_refusal_on_a_resume_that_holds_no_unit_comes_before_the_bind_so_nothing_is_left_executing():
    row_q = {"tool": "issue_refund", "state": "enforced", "fail_mode": "closed",
             "lines": [{"id": "L1", "decision": "ask", "approval": ASK, "limits": {"quota": {"limit": 5, "window": "hour", "per": "service"}}}], "otherwise": "inherit"}
    plane = start(BindPlane([row_q], link="degraded"), approval_bind=True)  # the quota cannot be verified and the row fails closed
    tool = StructuredTool.from_function(lambda order_id, amount_cents: "x", name="issue_refund", description="refund", args_schema=Refund)
    validator, effects = early._register("issue_refund", tool, {"issue_refund": {"financial"}})
    digest = approvals.args_digest("issue_refund", {"order_id": "o", "amount_cents": 5})
    ran = []
    with pytest.raises(ExecutionRefused) as refused:
        api._govern_resumed("issue_refund", {"order_id": "o", "amount_cents": 5}, lambda final: ran.append(final), effects=effects, validator=validator, ids=IDS, text=str, passthrough=(),
                            resumed={"request_id": "req-1", "approval_id": "ap1", "args_digest": digest, "quota_held": False})
    assert refused.value.reason in ("quota_plane_unavailable", "approval_unavailable"), refused.value.reason  # the quota contract, not a digest or limit refusal
    assert ran == [] and not [c for c in plane.calls if c[1].endswith("/bind")], "refused before the bind: the plane row is never moved to executing"
    assert not [c for c in plane.calls if c[1].endswith("/report")], "and there is nothing to report: it was never bound"


def test_a_replay_refusal_always_raises_even_with_no_active_engine_and_dispatches_nothing():
    api._teardown_for_tests()  # no init(): there is no engine to record anything with
    assert api._active()[0] is None
    with pytest.raises(ExecutionRefused) as refused:
        api._refuse_replayed("issue_refund", {"order_id": "o"}, frozenset({"financial"}), IDS, "req-1", "prior_outcome_unknown", None, "ap1")
    assert refused.value.reason == "prior_outcome_unknown", "the closed public reason, raised: 'returned' can never be read as 'not refused'"
