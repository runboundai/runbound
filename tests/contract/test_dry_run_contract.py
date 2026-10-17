"""The dry-run contract (TPD §4c.5, §12 item 5; named test 3). Ids O-DRY-*. O-DRY-1 passes since TR2-O2; O-DRY-2 passes since VPC B6b (a dry run takes no unit, it peeks).

Vocabulary does not grow in R0: the TPD's ``would_deny`` is today's ``would_refuse:<code>`` tag (here
``would_refuse:tool_blocked`` for a deny), so these tests use the tag the engine already writes.
"""

import pytest

import runbound
from runbound import api
from test_policy_rows import FakeShared, row, start

pytestmark = [pytest.mark.contract(item="§4c.5")]

QUOTA = {"limit": 5, "window": "hour", "per": "service"}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.mark.contract(item="§4c.5/named test 3")
def test_o_dry_1_a_candidate_deny_beside_an_enforced_allow_records_would_deny_and_the_call_runs():
    """The wire form (WIRE-TR2 §3, AC6): the enforced revision decides; ``candidate`` is a dry-run revision served beside it."""
    enforced = row("look", lines=[{"id": "L1", "decision": "allow", "approval": None, "limits": {"max_calls_per_session": 1, "quota": None}}],
                   otherwise="inherit", max_calls_per_session=1,
                   candidate={"version": 3, "state": "dry_run", "otherwise": "inherit",
                              "lines": [{"id": "L1", "decision": "deny", "approval": None, "limits": None}]})
    start(FakeShared([enforced]))
    ran = []

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        ran.append(1)
        return "ok"

    assert look() == "ok" and ran == [1]  # the candidate changed nothing
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]  # ...and was recorded apart
    with pytest.raises(runbound.ExecutionRefused) as refused:  # the enforced policy alone governs
        look()
    assert refused.value.reason == "session_max_calls"


def test_o_dry_1a_a_candidate_never_moves_a_live_counter_or_asks_anyone():
    enforced = row("look", lines=[], otherwise="inherit",
                   candidate={"version": 3, "state": "dry_run", "otherwise": "inherit",
                              "lines": [{"id": "L1", "decision": "ask", "approval": {"approval_mode": "sync", "approval_timeout_s": 60, "approvers": []},
                                         "limits": {"max_calls_per_session": None, "quota": QUOTA}}]})
    shared = start(FakeShared([enforced]))

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    assert look() == "ok"
    assert shared.takes() == [] and [c for c in shared.calls if c[1] == "/v1/approvals"] == []


@pytest.mark.contract(item="§12.5", ships="R3")
def test_o_dry_2_a_dry_run_line_over_a_limit_leaves_the_live_counter_unchanged_and_creates_no_approval_row():
    shared = start(FakeShared([row("look", state="dry_run", quota=QUOTA, approval_required=True)]))

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    assert look() == "ok"
    assert [c for c in shared.takes() if not c[2].get("peek")] == []  # no live unit taken: the plane is only ever PEEKED (read, never moved)
    assert shared.quota.report() == {}  # and no lease spent
    assert [c for c in shared.calls if c[1] == "/v1/approvals"] == []  # no approval row


def test_o_dry_2a_the_approval_half_already_holds_a_dry_run_approval_row_asks_nobody():
    shared = start(FakeShared([row("look", state="dry_run", approval_required=True)]))

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    assert look() == "ok"
    assert [c for c in shared.calls if c[1] == "/v1/approvals"] == []
