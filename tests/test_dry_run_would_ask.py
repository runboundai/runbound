"""A dry run of an ASK rule is evidence too (VPC-02, demo finding (a): 12 calls read "0 evaluations").

A dry-run row (or a candidate beside the enforced row) that would have asked a person asks nobody, refuses nothing and raises no approval; it records that it WOULD
have, as the tag ``would_ask:approval`` on the call's result and as an event filed the way a would-refuse is (reaction ``dry_run``, rule ``would_ask``), so the plane's
evidence can count it apart from would-refuse. It never pages anyone. No sockets, no sleeping."""

from __future__ import annotations

import pytest

import runbound
from runbound import api
from test_dry_run_evidence import watch
from test_policy_rows import FakeShared, row, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def act_tool():
    @runbound.tool(effects={"read"}, reviewed=True)
    def act(n: int = 0):
        return "ran"

    return act


def asked(seen):
    return [(a, r) for a, r in seen.anomalies if a.details.get("rule") == "would_ask"]


def test_a_dry_run_ask_row_runs_the_call_asks_nobody_and_files_a_would_ask_event_for_every_call():
    shared = start(FakeShared([row("act", state="dry_run", approval_required=True)]))
    seen = watch()
    act = act_tool()
    for n in range(12):
        assert act(n) == "ran"
    events = asked(seen)
    assert len(events) == 12 and all(reacted == "dry_run" for _a, reacted in events), "every call is its own piece of evidence, and none of it pages anyone"
    assert [c for c in shared.calls if c[1] == "/v1/approvals"] == [], "nobody was asked"
    assert "would_ask:approval" in runbound.tool_results()[-1]["tags"]
    assert not any(t.startswith("would_refuse") for t in runbound.tool_results()[-1]["tags"]), "an ask is not a refusal"


def test_the_event_carries_the_tool_the_rule_and_the_decision_record():
    start(FakeShared([row("act", state="dry_run", approval_required=True)]))
    seen = watch()
    act_tool()()
    (anomaly, reacted), = asked(seen)
    assert (anomaly.detector, reacted, anomaly.details["tool"], anomaly.details["state"]) == ("policy", "dry_run", "act", "dry_run")
    assert anomaly.details["reason_code"] == "approval" and anomaly.details["matched_rule_ids"] is not None, "the record rides on it like any refusal's"


def test_a_candidate_beside_an_enforced_row_that_would_ask_is_counted_the_same_way_and_decides_nothing():
    candidate = {"version": 3, "state": "dry_run", "otherwise": "inherit",
                 "lines": [{"id": "L1", "decision": "ask", "approval": {"approval_mode": "sync", "approval_timeout_s": 60, "approvers": []}}]}
    shared = start(FakeShared([row("act", lines=[{"id": "L1", "decision": "allow"}], otherwise="inherit", candidate=candidate)]))
    seen = watch()
    assert act_tool()() == "ran"
    assert len(asked(seen)) == 1 and [c for c in shared.calls if c[1] == "/v1/approvals"] == []


def test_an_enforced_ask_row_files_no_would_ask_it_really_asks():
    shared = start(FakeShared([row("act", approval_required=True)]))
    seen = watch()
    act_tool()()
    assert asked(seen) == [] and [c for c in shared.calls if c[1] == "/v1/approvals"], "a live row asks a person and records no would-ask"


def test_a_dry_run_row_that_would_not_ask_files_nothing():
    start(FakeShared([row("act", state="dry_run", max_calls_per_session=50)]))
    seen = watch()
    act_tool()()
    assert asked(seen) == []


def test_the_events_are_capped_per_session_like_every_dry_run_record_and_a_summary_stands_for_the_rest():
    start(FakeShared([row("act", state="dry_run", approval_required=True)]))
    seen = watch()
    act = act_tool()
    with runbound.session("k-1"):
        for n in range(130):
            act(n)
    counts = [a.details.get("suppressed_count") for a, _r in asked(seen)]
    assert len(asked(seen)) <= 101 and all(c is None for c in counts[:100]), "individual records up to the cap, then one summary at the end of the session"
