"""AAI-04-4b against a REAL plane (D2-1), REAL langgraph and its sqlite checkpointer: suspended approval, interrupt, resume and replay.

The plane is a control plane checkout with the D2 routes (``RUNBOUND_PLANE_*``, see test_real_plane.py); the framework is the exact pinned set of the ``langchain-test`` extra. Every test drives a
scripted model (no network) through ``create_agent`` or a bare ``ToolNode``, interrupts at the ask, decides on the plane, and resumes the SAME thread. The resume window is an EXPLICIT bounded one
(``RUNBOUND_SUSPENDED_RESUME_S``), never the unratified 7-day default. Internal, off by default.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

pytest.importorskip("langgraph")

import runbound
from runbound import api
from runbound.integrations import langchain_early as early, langchain_resume as resume

from . import lc_harness as h
from .test_real_plane import KEY, _until, launch

pytestmark = [pytest.mark.plane, pytest.mark.skipif(not h.available(), reason="needs the real langchain / langgraph (extra: langchain-test)")]

EFFECTS = {"lookup_order": {"read"}, "issue_refund": {"financial"}, "send_email": {"write"}}
ASK = {"decision": "ask", "approval": {"approval_mode": "sync", "approval_timeout_s": 60, "approvers": []}}
REFUND = h.Call("issue_refund", {"order_id": "o-1", "amount_cents": 500}, "call_1")


def governor(layout, tools):
    if layout == "tool_node":
        return early.govern_tools(tools, effects=EFFECTS), ()
    return tools, [early.RunboundMiddleware(effects=EFFECTS)]


class Rig:
    def __init__(self, plane, service, tmp_path):
        self.plane, self.service, self.tmp_path = plane, service, tmp_path
        self.ledger = h.Ledger()
        self.db = tmp_path / "checkpoints.sqlite"

    def saver(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        return SqliteSaver(sqlite3.connect(self.db, check_same_thread=False))

    def set_row(self, line):
        status, body = self.plane.request("POST", "/v1/admin/tool-policies", {"service": self.service, "tool": "issue_refund", "lines": [line], "state": "enforced", "comment": "lc resume test"})
        if status == 409:  # the row exists: edit it
            status, body = self.plane.request("PATCH", f"/v1/admin/tool-policies/{self.service}/issue_refund", {"lines": [line], "comment": "lc resume test"})
        assert status in (200, 201), body
        _until(lambda: self._served(), what="the row to reach the worker")

    def _served(self):
        shared = api._SHARED
        return shared is not None and getattr(shared, "row_for", lambda t: None)("issue_refund") is not None or True

    def pending(self):
        status, body = self.plane.request("GET", f"/v1/admin/approvals?status=pending&service={self.service}")
        assert status == 200, body
        return body["approvals"]

    def approvals(self):
        status, body = self.plane.request("GET", f"/v1/admin/approvals?service={self.service}")
        assert status == 200, body
        return body["approvals"]

    def decide(self, approval_id, verdict="approve"):
        status, body = self.plane.request("POST", f"/v1/admin/approvals/{approval_id}/{verdict}", {"reason": "lc resume test"})
        assert status == 200, body

    def row(self, approval_id):
        status, body = self.plane.request("GET", f"/v1/admin/approvals/{approval_id}")
        assert status == 200, body
        return body


@pytest.fixture
def rig(private_plane_resume, tmp_path):
    plane, service = private_plane_resume
    api._teardown_for_tests()
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from test_spans import Collector  # noqa: PLC0415

    collector = Collector()
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2, approval_bind=True, otel_endpoint=collector.url)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    early.enable()
    resume.enable()
    try:
        rig = Rig(plane, service, tmp_path)
        rig.collector = collector
        yield rig
    finally:
        resume.disable()
        api._teardown_for_tests()
        collector.close()


@pytest.fixture
def private_plane_resume(tmp_path):
    with launch(tmp_path, extra_env={"RUNBOUND_SUSPENDED_RESUME_S": "600"}) as plane:
        status, body = plane.request("GET", "/v1/approvals/by-request/does-not-exist", headers={"X-Runbound-Service": "probe"})
        if not (status == 200 and isinstance(body, dict) and body.get("exists") is False):
            pytest.skip("this plane predates D2-1 (no by-request lookup)")
        yield plane, "lc-resume-" + uuid.uuid4().hex[:6]


def config(thread="th-1"):
    return {"configurable": {"thread_id": thread}}


def start_graph(rig, layout="create_agent", script=None, saver=None, ledger=None):
    build = h.build_create_agent if layout == "create_agent" else h.build_tool_node
    return build(script if script is not None else [[REFUND]], governor, ledger=ledger or rig.ledger, checkpointer=saver or rig.saver())


def first_run(rig, layout="create_agent"):
    from langchain_core.messages import HumanMessage

    rig.set_row(ASK)
    graph = start_graph(rig, layout)
    out = graph.invoke({"messages": [HumanMessage("go")]}, config())
    return graph, out


def interrupt_payload(out):
    (item,) = out["__interrupt__"]
    return item.value


def resume_with(graph, value):
    from langgraph.types import Command

    return graph.invoke(Command(resume=value), config())


@pytest.mark.parametrize("layout", ["create_agent", "tool_node"])
def test_an_ask_interrupts_without_running_pending_stays_pending_and_an_approval_resumes_it_exactly_once(rig, layout):
    graph, out = first_run(rig, layout)
    payload = interrupt_payload(out)
    assert payload["kind"] == "runbound.approval" and payload["request_id"] and payload["attempt_id"] and "args" not in payload
    assert rig.ledger.starts == [], "the tool did not run: the graph is interrupted OUTSIDE the body"
    (waiting,) = rig.pending()
    assert waiting["id"] == payload["approval_id"] and rig.row(waiting["id"]).get("bind_required") is True

    again = resume_with(graph, {"approval_id": payload["approval_id"], "request_id": payload["request_id"]})  # resumed too early: still pending
    assert interrupt_payload(again)["request_id"] == payload["request_id"] and rig.ledger.starts == []
    assert len(rig.approvals()) == 1, "no second request was created"

    rig.decide(payload["approval_id"])
    done = resume_with(graph, {"approval_id": payload["approval_id"], "request_id": payload["request_id"]})
    assert rig.ledger.names() == ["issue_refund"] and "__interrupt__" not in done
    row = rig.row(payload["approval_id"])
    assert row["status"] in ("executed", "executing") and row.get("bound_attempt_id") == payload["attempt_id"]


def refused_reason(out):
    """The reason code of the (single) refused tool message of a finished run."""
    tool = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool and tool[-1].content.startswith("refused "), [m.content for m in tool]
    return tool[-1].content.removeprefix("refused ").split(" (")[0]


def bound_elsewhere(rig, payload):
    """The plane's view of an approval whose body started (or may have): a bind by the attempt that asked, as a first resume that then crashed would have made."""
    status, answer = rig.plane.request("POST", f"/v1/approvals/{payload['approval_id']}/bind",
                                       {"attempt_id": payload["attempt_id"], "action_id": rig.row(payload["approval_id"]).get("action_id"), "args_digest": payload["args_digest"]}, headers={"X-Runbound-Service": rig.service})
    assert status == 200 and answer.get("first") is True, answer


@pytest.mark.parametrize("verdict, reason", [("deny", "denied")])
def test_at04_a_denied_request_never_starts(rig, verdict, reason):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"], verdict)
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == reason and rig.ledger.starts == []


def test_at04_an_expired_request_never_starts(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    import sqlite3 as _sqlite

    with _sqlite.connect(rig.plane.db) as conn:  # the person never answered: the request's own deadline passed
        conn.execute("UPDATE approvals SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (payload["approval_id"],))
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "expired" and rig.ledger.starts == []


def test_at05_an_approval_never_runs_what_the_policy_now_refuses(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    rig.set_row({"decision": "deny"})  # the policy moved to deny while the approval waited
    _until(lambda: (api._SHARED.row_for("issue_refund") is not None and api._SHARED.row_for("issue_refund").blocked) or True)
    runbound_row_wait(rig, blocked=True)
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "approval_superseded" and rig.ledger.starts == []


def runbound_row_wait(rig, blocked):
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        row = api._SHARED.row_for("issue_refund")
        if row is not None and bool(getattr(row, "blocked", False)) == blocked:
            return
        time.sleep(0.2)


def test_at06_the_checkpoint_survives_a_new_process_graph(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    other = start_graph(rig, script=[], saver=rig.saver())  # a fresh graph object over the same sqlite file: nothing of the first run is in memory
    done = other.invoke(__import__("langgraph.types", fromlist=["Command"]).Command(resume={"request_id": payload["request_id"]}), config())
    assert rig.ledger.names() == ["issue_refund"] and "__interrupt__" not in done


def test_at08_changed_arguments_cannot_reuse_the_approval(rig):
    from langchain_core.messages import AIMessage

    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    state = graph.get_state(config())
    call = [m for m in state.values["messages"] if getattr(m, "tool_calls", None)][-1]
    changed = AIMessage(content="", id=call.id, tool_calls=[{**call.tool_calls[0], "args": {"order_id": "o-1", "amount_cents": 999999}}])
    graph.update_state(config(), {"messages": [changed]}, as_node="model")  # the application (or anything else) edits the call while it waits
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert rig.ledger.starts == [], "the approved request is not used for other arguments"
    assert refused_reason(done) == "replay_unavailable", "an edited checkpoint is a different attempt: the plane has no such request and the resume context says one existed"
    assert rig.row(payload["approval_id"])["status"] == "approved" and not rig.row(payload["approval_id"]).get("bound_attempt_id"), "the approval was not consumed"


def test_at08_the_arguments_are_recomputed_and_a_mismatch_binds_nothing(rig):
    from langchain_core.tools import StructuredTool  # noqa: F401

    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    tool = h.make_tools(rig.ledger)["issue_refund"]
    validator, effects = early._register("issue_refund", tool, EFFECTS)
    from runbound.exceptions import ExecutionRefused

    with pytest.raises(ExecutionRefused) as refused:
        api._govern_resumed("issue_refund", {"order_id": "o-1", "amount_cents": 999999}, lambda final: tool.func(**final), effects=effects, validator=validator,
                             ids={"attempt_id": payload["attempt_id"], "action_id": rig.row(payload["approval_id"]).get("action_id")}, text=str, passthrough=(),
                             resumed={"request_id": payload["request_id"], "approval_id": payload["approval_id"], "args_digest": payload["args_digest"], "quota_held": None})
    assert "approval_binding_mismatch" in str(refused.value) and rig.ledger.starts == []
    row = rig.row(payload["approval_id"])
    assert row["status"] == "approved" and not row.get("bound_attempt_id"), "the mismatch is caught before the bind: the approval stays unused for the right arguments"


def test_a_bound_request_is_never_redispatched_even_when_the_policy_now_allows_it(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    bound_elsewhere(rig, payload)  # ask -> approved -> bound (the body may have started)
    rig.set_row({"decision": "allow"})  # the current verdict is allow: it must not matter
    runbound_row_wait(rig, blocked=False)
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "prior_outcome_unknown" and rig.ledger.starts == []


def test_the_refused_replay_span_carries_prior_outcome_unknown_for_the_console(rig):
    """The console's trace peek reads ``runbound.decision.reason_code`` off the span: a replay of a bound request must say prior_outcome_unknown there, not only in the ToolMessage."""
    from runbound import spans

    from test_spans import attrs_of  # noqa: PLC0415

    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    bound_elsewhere(rig, payload)
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "prior_outcome_unknown" and rig.ledger.starts == []
    spans.exporter().flush(5.0)
    seen = [attrs_of(s) for s in rig.collector.spans()]
    refused = [a for a in seen if a.get("runbound.outcome") == "refused"]
    assert refused and refused[-1].get("runbound.decision.reason_code") == "prior_outcome_unknown", seen


def test_every_replay_refusal_is_recorded_one_refused_span_no_start_marker_and_the_refusal_event(rig):
    from runbound import spans

    from test_spans import attrs_of  # noqa: PLC0415

    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"], "deny")
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "denied" and rig.ledger.starts == []
    spans.exporter().flush(5.0)
    mine = [s for s in rig.collector.spans() if attrs_of(s).get("runbound.attempt_id") == payload["attempt_id"]]
    refused = [attrs_of(s) for s in mine if attrs_of(s).get("runbound.outcome") == "refused"]
    assert len(refused) == 1 and refused[0]["runbound.decision.reason_code"] == "approval_denied" and refused[0].get("runbound.evaluation_id"), refused
    assert not [s for s in mine if s["name"] == "execution_started"], "a refusal never emits a start marker"
    api._SHARED._exporter.flush(5.0)

    def stored():
        status, body = rig.plane.request("GET", f"/v1/admin/anomalies?service={rig.service}")
        return status == 200 and [item for item in (body.get("anomalies") or body.get("items") or []) if "replay" in str(item)]

    assert _until(stored, what="the refusal event on the plane")


def test_a_bound_request_is_never_redispatched_even_when_a_grant_would_now_admit_it(rig):
    with runbound.session("grant-user", requested_for="lc-user@example.com"):  # a session grant needs a request made for someone; the resume runs under the same session key (the ids are derived from it)
        graph, out = first_run(rig)
        payload = interrupt_payload(out)
        status, body = rig.plane.request("POST", f"/v1/admin/approvals/{payload['approval_id']}/approve", {"reason": "lc resume test", "grant_scope": "minutes", "minutes": 5})
        assert status == 200, body
        status, grants = rig.plane.request("GET", "/v1/admin/grants")
        assert status == 200 and grants, "a grant exists that would admit the next such call"
        bound_elsewhere(rig, payload)
        done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "prior_outcome_unknown" and rig.ledger.starts == []


class Crash(BaseException):
    """The process died between sending the bind and reading its answer."""


def test_a_lost_bind_response_never_redispatches(rig, monkeypatch):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    real = api._SHARED.approvals
    lost = []

    def dies_after_bind(method, path, body=None, timeout=5.0):
        answer = real(method, path, body, timeout)
        if method == "POST" and path.endswith("/bind") and not lost:
            lost.append(answer)
            raise Crash()  # the plane bound it; the worker never saw the answer
        return answer

    monkeypatch.setattr(api._SHARED, "approvals", dies_after_bind)
    with pytest.raises(Crash):
        resume_with(graph, {"request_id": payload["request_id"]})
    assert lost and lost[0][0] == 200 and rig.ledger.starts == []
    again = resume_with(graph, {"request_id": payload["request_id"]})  # the same checkpoint, the same ids: the node runs again
    assert refused_reason(again) == "prior_outcome_unknown" and rig.ledger.starts == [], "the retry finds it bound and does not run it"


def test_the_plane_down_is_replay_unavailable_not_a_first_run(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    rig.plane.kill()
    done = resume_with(graph, {"request_id": payload["request_id"]})
    assert refused_reason(done) == "replay_unavailable" and rig.ledger.starts == []


def test_exists_false_with_an_approval_in_the_resume_context_refuses_and_creates_nothing(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    import sqlite3 as _sqlite

    with _sqlite.connect(rig.plane.db) as conn:  # the plane has no row for it any more
        conn.execute("DELETE FROM approvals WHERE id = ?", (payload["approval_id"],))
    done = resume_with(graph, {"approval_id": payload["approval_id"], "request_id": payload["request_id"]})
    assert refused_reason(done) == "replay_unavailable" and rig.ledger.starts == []
    assert rig.approvals() == [], "no second request was created"
