"""AAI-04 Gate B gap G-5 (AT-08, forged resume authority): a resume payload NAMING another real approval is not authority.

Real plane, real langgraph + sqlite. Two calls are suspended (A and B, different threads and arguments). Only A is approved. The application (or an attacker who can edit the resume value) resumes
B and hands it A's ``request_id`` and ``approval_id``. The plane's row for B's OWN derived request decides, never the payload, so B does not run, and A's approval is not consumed: A still runs
exactly once when it is resumed properly. Internal, off by default.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import HumanMessage  # noqa: E402

from . import lc_harness as h  # noqa: E402
from .test_langchain_resume_plane import ASK, config, governor, interrupt_payload, private_plane_resume, refused_reason, resume_with, rig  # noqa: E402,F401  (fixtures)

pytestmark = [pytest.mark.plane, pytest.mark.skipif(not h.available(), reason="needs the real langchain / langgraph (extra: langchain-test)")]


def suspend_two(rig):
    rig.set_row(ASK)
    graphs, payloads = {}, {}
    for name, order in (("a", "o-A"), ("b", "o-B")):
        script = [[h.Call("issue_refund", {"order_id": order, "amount_cents": 500}, f"call_{name}")]]
        graph = h.build_create_agent(script, governor, ledger=rig.ledger, checkpointer=rig.saver())
        out = graph.invoke({"messages": [HumanMessage("go")]}, config(f"th-{name}"))
        graphs[name], payloads[name] = graph, interrupt_payload(out)
    assert payloads["a"]["request_id"] != payloads["b"]["request_id"] and rig.ledger.starts == []
    return graphs, payloads


def names_of(payload):
    return {"request_id": payload["request_id"], "approval_id": payload["approval_id"]}


def row_of(rig, payload):
    return rig.row(payload["approval_id"])


def test_a_resume_naming_another_approved_request_does_not_run_this_one_and_does_not_consume_that_one(rig):
    graphs, payloads = suspend_two(rig)
    rig.decide(payloads["a"]["approval_id"])  # only A is approved
    forged = graphs["b"].invoke(__import__("langgraph.types", fromlist=["Command"]).Command(resume=names_of(payloads["a"])), config("th-b"))
    assert rig.ledger.starts == [], "B did not run on A's approval"
    assert interrupt_payload(forged)["request_id"] == payloads["b"]["request_id"], "B is still waiting for ITS OWN decision"
    a_row, b_row = row_of(rig, payloads["a"]), row_of(rig, payloads["b"])
    assert a_row["status"] == "approved" and not a_row.get("bound_attempt_id"), "A's approval was not consumed by the forged resume"
    assert b_row["status"] == "pending" and not b_row.get("bound_attempt_id")
    # A is then resumed properly: it runs exactly once, with ITS arguments
    done = resume_with_thread(graphs["a"], names_of(payloads["a"]), "th-a")
    assert "__interrupt__" not in done and [s[1]["order_id"] for s in rig.ledger.starts] == ["o-A"]


def test_a_forged_payload_does_not_turn_a_denial_into_a_run(rig):
    graphs, payloads = suspend_two(rig)
    rig.decide(payloads["a"]["approval_id"])
    rig.decide(payloads["b"]["approval_id"], "deny")
    done = resume_with_thread(graphs["b"], names_of(payloads["a"]), "th-b")
    assert refused_reason(done) == "denied" and rig.ledger.starts == [], "B's own row says denied; A's approval in the payload changes nothing"


@pytest.mark.parametrize("forged", [
    {"request_id": "0" * 32, "approval_id": "00000000-0000-0000-0000-000000000000"},
    {"request_id": "not-a-request", "approval_id": "not-an-approval"},
    {"approval_id": "x"},
    {},
    "approved",
    {"request_id": 5, "approval_id": ["a"]},
])
def test_a_payload_that_names_nothing_real_never_runs_a_pending_call(rig, forged):
    graphs, payloads = suspend_two(rig)
    again = resume_with_thread(graphs["b"], forged, "th-b")
    assert rig.ledger.starts == [] and interrupt_payload(again)["request_id"] == payloads["b"]["request_id"], "still waiting; nothing in the payload is authority"
    assert row_of(rig, payloads["b"])["status"] == "pending"


def test_naming_an_approval_that_was_already_executed_runs_nothing_again(rig):
    graphs, payloads = suspend_two(rig)
    rig.decide(payloads["a"]["approval_id"])
    resume_with_thread(graphs["a"], names_of(payloads["a"]), "th-a")
    assert [s[1]["order_id"] for s in rig.ledger.starts] == ["o-A"]
    rig.decide(payloads["b"]["approval_id"])
    done = resume_with_thread(graphs["b"], names_of(payloads["a"]), "th-b")  # B approved, but the payload names A's (executed) request
    assert "__interrupt__" not in done, "B runs on ITS OWN approval, whatever the payload names"
    assert [s[1]["order_id"] for s in rig.ledger.starts] == ["o-A", "o-B"], "A did not run a second time; B ran once"


def resume_with_thread(graph, value, thread):
    from langgraph.types import Command

    return graph.invoke(Command(resume=value), config(thread))
