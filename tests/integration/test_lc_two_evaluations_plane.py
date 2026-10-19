"""AAI-04 gap G-2/G-3 (Gate B AT-05): on a LangChain-originated record BOTH evaluations of an approved call are retained.

Real plane, real langgraph + sqlite. Evaluation #1 is the admission that asked (it rides the approval request and the first, pending, span); evaluation #2 is the execution-entry
re-check (``runbound.recheck.*``, its own id), kept whether it passed or refused. The two spans and the approval row name ONE action and ONE attempt.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from runbound import spans

from . import lc_harness as h  # noqa: F401
from .test_langchain_resume_plane import ASK, first_run, interrupt_payload, private_plane_resume, refused_reason, resume_with, rig, runbound_row_wait  # noqa: F401  (fixtures)

pytestmark = pytest.mark.plane

R = "runbound.recheck."


def spans_of(rig):
    from test_spans import attrs_of  # noqa: PLC0415

    spans.exporter().flush(5.0)
    return [attrs_of(s) for s in rig.collector.all_spans() if s["name"].startswith("execute_tool")]


def one_record(rig, payload):
    found = [a for a in spans_of(rig) if a.get("runbound.attempt_id") == payload["attempt_id"]]
    assert len(found) >= 2, f"the asking span and the resumed span, got {len(found)}"
    return found


def test_a_passed_recheck_is_a_second_evaluation_on_the_same_action_and_attempt(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    done = resume_with(graph, {"request_id": payload["request_id"], "approval_id": payload["approval_id"]})
    assert rig.ledger.names() == ["issue_refund"] and "__interrupt__" not in done
    record = one_record(rig, payload)
    asked, resumed = record[0], record[-1]
    row = rig.row(payload["approval_id"])
    first_evaluation = row["evaluation_id"]
    assert first_evaluation and asked["runbound.evaluation_id"] == first_evaluation, "evaluation #1 rides the approval request and the asking span"
    assert asked["runbound.action_id"] == resumed["runbound.action_id"] and asked["runbound.attempt_id"] == resumed["runbound.attempt_id"] == payload["attempt_id"]
    second_evaluation = resumed.get(R + "evaluation_id")
    assert second_evaluation and second_evaluation != first_evaluation, "the re-check is an evaluation of its own"
    assert resumed.get(R + "outcome") == "passed" and resumed["runbound.outcome"] == "ok", resumed


def test_a_refused_recheck_keeps_both_evaluations_and_the_handler_never_starts(rig):
    graph, out = first_run(rig)
    payload = interrupt_payload(out)
    rig.decide(payload["approval_id"])
    rig.set_row({"decision": "deny"})  # the policy moved to deny while the approval waited
    runbound_row_wait(rig, blocked=True)
    done = resume_with(graph, {"request_id": payload["request_id"], "approval_id": payload["approval_id"]})
    assert refused_reason(done) == "approval_superseded" and rig.ledger.starts == []
    first_evaluation = rig.row(payload["approval_id"])["evaluation_id"]
    refused = [a for a in one_record(rig, payload) if a.get("runbound.outcome") == "refused"][-1]
    assert refused[R + "evaluation_id"] and refused[R + "evaluation_id"] != first_evaluation, "both evaluations are on the record"
    assert refused[R + "outcome"] == "refused", refused
    assert refused["runbound.action_id"] == one_record(rig, payload)[0]["runbound.action_id"]
