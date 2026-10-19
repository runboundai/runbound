"""AAI-04 gap G-2 (AT-07, RFC 0a.2): two suspended calls approved and resumed at the SAME instant under a cap of one action: the reserve is atomic, so exactly one dispatches.

Real plane, real langgraph + sqlite. The cap is ``init(max_actions_per_run=1)``: ``resume_admit`` reserves a slot and checks the cap under one lock, so two concurrent resumes can never both pass.
The loser is refused BEFORE its start: its approval stays approved and unbound (it can be resumed later), and no slot is left reserved. The same-REQUEST race across processes is in
test_lc_process_restart_plane.py.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("langgraph")

import runbound
from runbound import api
from runbound.integrations import langchain_early as early, langchain_resume as resume

from . import lc_harness as h
from .test_langchain_resume_plane import ASK, config, governor, interrupt_payload, private_plane_resume, refused_reason, rig  # noqa: F401  (fixtures)
from .test_real_plane import KEY, _until

pytestmark = pytest.mark.plane


@pytest.mark.parametrize("round_", [1, 2, 3])
def test_two_concurrent_resumes_under_a_cap_of_one_dispatch_exactly_one_and_the_loser_holds_nothing(rig, round_):
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    api._teardown_for_tests()
    runbound.init(control_plane_url=rig.plane.url, token=KEY, service=rig.service, control_plane_poll_s=0.2, approval_bind=True, max_actions_per_run=1)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    early.enable()
    resume.enable()
    rig.set_row(ASK)

    ledger = h.Ledger()
    graphs = [h.build_create_agent([[h.Call("issue_refund", {"order_id": f"o-{n}", "amount_cents": 500}, f"call_{n}")]], governor, ledger=ledger, checkpointer=rig.saver()) for n in (1, 2)]
    payloads = []
    for n, graph in enumerate(graphs, 1):
        out = graph.invoke({"messages": [HumanMessage("go")]}, config(f"th-cap-{round_}-{n}"))
        payloads.append(interrupt_payload(out))
    assert ledger.starts == [] and len(rig.pending()) == 2
    for payload in payloads:
        rig.decide(payload["approval_id"])

    barrier = threading.Barrier(2)
    results: dict = {}

    def resume_one(n):
        barrier.wait()
        results[n] = graphs[n].invoke(Command(resume={"request_id": payloads[n]["request_id"], "approval_id": payloads[n]["approval_id"]}), config(f"th-cap-{round_}-{n + 1}"))

    threads = [threading.Thread(target=resume_one, args=(n,)) for n in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 2, "both resumes finished"

    assert len(ledger.starts) == 1, f"exactly one dispatch under a cap of one, not {len(ledger.starts)}"
    winner = next(n for n in (0, 1) if ledger.starts[0][1]["order_id"] == f"o-{n + 1}")
    loser = 1 - winner
    assert refused_reason(results[loser]) in ("max_actions_per_run", "blast_radius", "fanout") or refused_reason(results[loser]).startswith(("max_", "action")), refused_reason(results[loser])
    row = rig.row(payloads[loser]["approval_id"])
    assert row["status"] == "approved" and not row.get("bound_attempt_id"), "refused before the bind: the loser's approval is unused and still resumable"
    assert rig.row(payloads[winner]["approval_id"]).get("bound_attempt_id") == payloads[winner]["attempt_id"]
    _engine, state = api._active()
    assert state.reserved_actions == 0 and state.executed_actions == 1, "no slot is left reserved by the loser; one action was executed"


def test_a_resumed_call_counts_as_one_executed_action_so_a_cap_of_two_admits_two_resumes(rig):
    """FOUND BY THE ABOVE: a dispatched resume was counted twice (once when it cleared admission, once when its reservation committed), so each resume used two of the
    ``max_actions_per_run`` slots and a cap of two refused the second legitimate resume."""
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    api._teardown_for_tests()
    runbound.init(control_plane_url=rig.plane.url, token=KEY, service=rig.service, control_plane_poll_s=0.2, approval_bind=True, max_actions_per_run=2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    early.enable()
    resume.enable()
    rig.set_row(ASK)
    ledger = h.Ledger()
    graphs = [h.build_create_agent([[h.Call("issue_refund", {"order_id": f"o-{n}", "amount_cents": 500}, f"call_{n}")]], governor, ledger=ledger, checkpointer=rig.saver()) for n in (1, 2)]
    payloads = [interrupt_payload(g.invoke({"messages": [HumanMessage("go")]}, config(f"th-two-{n}"))) for n, g in enumerate(graphs, 1)]
    for payload in payloads:
        rig.decide(payload["approval_id"])
    for n, graph in enumerate(graphs):
        done = graph.invoke(Command(resume={"request_id": payloads[n]["request_id"], "approval_id": payloads[n]["approval_id"]}), config(f"th-two-{n + 1}"))
        assert "__interrupt__" not in done
    assert [s[1]["order_id"] for s in ledger.starts] == ["o-1", "o-2"], "both approved resumes dispatched under a cap of two"
    _engine, state = api._active()
    assert state.executed_actions == 2 and state.admitted_actions == 2 and state.reserved_actions == 0, (state.executed_actions, state.admitted_actions, state.reserved_actions)
