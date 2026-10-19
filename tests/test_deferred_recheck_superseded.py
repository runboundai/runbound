"""PS-AAI-03g (SDK half): an approved DEFERRED request refused by the execution-entry re-check reports ``superseded`` with the record, not ``failed``.

The sync path already tells the plane (it knows the approval id); the resumed path did not (``admit`` returned before naming it), so the plane saw ``failed`` and kept the
quota unit the approved request held. The policy moves between the chain's first look at the row and the re-check (a hello landing in that window), which is what these tests do.
"""

import time

import pytest

import runbound
from runbound import api, approvals, policy_rows
from test_grants import Granting
from test_policy_rows import row, start

SECRET = "s3cret"
DEFERRED_ASK = {"approval_mode": "deferred", "approval_timeout_s": 30, "approvers": []}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def asking():
    return row("pay", rule_revision=2, lines=[{"id": "L2", "decision": "ask", "approval": DEFERRED_ASK, "limits": None}], otherwise="inherit")


class Moving(Granting):
    """The rows change at the instant the resumed chain reaches the approval slot: after every earlier look at the row, before the re-check."""

    def __init__(self, rows, after, **kw):
        super().__init__(rows, **kw)
        self.after = policy_rows.parse(list(after))
        self.armed = False

    def trip(self):
        if self.armed:
            self.rows = self.after


@pytest.fixture
def at_the_slot(monkeypatch):
    """Run the real ``admit`` and then let the policy move: the re-check is the first thing to read the new rows."""
    real = approvals.admit
    holder = {}

    def admit(engine, session, call, spec, info):
        real(engine, session, call, spec, info)
        if holder.get("shared") is not None:
            holder["shared"].trip()

    monkeypatch.setattr(approvals, "admit", admit)
    return holder


def reports(shared):
    return [c[2] for c in shared.calls if c[0] == "POST" and c[1].endswith("/report")]


def wait_for(check, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def pay_tool():
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="deferred", timeout_s=30)
    def pay(amount: float = 0.0):
        ran.append(amount)
        return "paid"

    return pay, ran


def filed(shared, after_filing):
    """File a deferred request, then arm the move and return the item a hello would carry."""
    pay, ran = pay_tool()
    out = pay(amount=1200.0)
    assert out["status"] == "pending_approval"
    (create,) = [c[2] for c in shared.calls if c[1] == "/v1/approvals"]
    shared.armed = after_filing
    item = {"id": "ap1", "tool": "pay", "args": create["args"], "request_id": out["request_id"], "seal": create["seal"], "service": create["service"],
            "expires_at": "2099-01-01T00:00:00Z"}
    return item, ran


def test_a_deferred_request_refused_at_the_recheck_reports_superseded_with_the_record_not_failed(at_the_slot):
    shared = start(Moving([asking()], [row("pay", rule_revision=3, blocked=True)], approve="pending"), approval_secret=SECRET)
    at_the_slot["shared"] = shared
    results = []
    api._ENGINE.config.on_deferred_result = results.append
    item, ran = filed(shared, True)

    approvals._execute(item)

    assert wait_for(lambda: reports(shared))
    (report,) = reports(shared)
    assert ran == [] and report["outcome"] == "superseded" and report["reason"] == "approval_superseded"
    assert report["record"]["rule_revision"] == 2 and report["record"]["recheck_rule_revision"] == 3 and report["record"]["recheck_reason"] == "tool_blocked"
    assert not wait_for(lambda: len(reports(shared)) > 1, 0.3), "one report: not a `superseded` followed by a `failed`"
    assert [r["outcome"] for r in results] == ["superseded"] and results[0]["error"] == "approval_superseded"


def test_a_deferred_request_that_passes_the_recheck_runs_and_reports_executed(at_the_slot):
    shared = start(Moving([asking()], [asking()], approve="pending"), approval_secret=SECRET)
    at_the_slot["shared"] = shared
    results = []
    api._ENGINE.config.on_deferred_result = results.append
    item, ran = filed(shared, True)

    approvals._execute(item)

    assert wait_for(lambda: reports(shared))
    assert ran == [1200.0] and [r["outcome"] for r in reports(shared)] == ["executed"] and [r["outcome"] for r in results] == ["executed"]


def test_a_deferred_request_the_ordinary_chain_refuses_before_the_recheck_still_reports_failed():
    """A tool blocked before the resumed chain reaches the approval slot never gets to the re-check: that is a refusal, not a superseded approval (unchanged)."""
    shared = start(Moving([asking()], [asking()], approve="pending"), approval_secret=SECRET)
    item, ran = filed(shared, False)
    shared.rows = policy_rows.parse([row("pay", rule_revision=3, blocked=True)])

    approvals._execute(item)

    assert wait_for(lambda: reports(shared))
    assert ran == [] and [r["outcome"] for r in reports(shared)] == ["failed"]
