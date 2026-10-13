"""Approver enforcement against a REAL control plane, seen from a worker (TR1-O4; the plane's side is TR1-A2).

The plane refuses a decide from someone who is not on the tool's approvers list: HTTP 403 ``approver_not_eligible``, and the
request stays PENDING. The SDK never makes that call (a decider does), so nothing in the SDK may change: a sync call keeps
waiting on its long poll, a deferred request stays unclaimed, and when a listed person approves, the call runs exactly once.

Skips on a plane without TR1-A2 (the probe: a decide by someone not listed must answer 403 ``approver_not_eligible``).
"""

from __future__ import annotations

import time

import pytest

import runbound
from runbound import api

from .test_approvals_plane import SECRET, _as_console, in_thread, pending, row, start
from .test_real_plane import KEY, _until, plane  # noqa: F401  (fixture)
from .test_rows_quota_plane import put_row

pytestmark = pytest.mark.plane

LISTED = "bo@acme.example"
OUTSIDER = "mallory@acme.example"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def decide_as(plane, approval_id, person, verdict="approve"):  # noqa: F811
    return plane.request("POST", f"/v1/admin/approvals/{approval_id}/{verdict}", {"reason": "test"}, headers=_as_console(person))


@pytest.fixture
def enforcing(plane):  # noqa: F811
    """The plane, after proving it enforces approvers (else the test skips)."""
    import uuid

    service = f"appr-probe-{uuid.uuid4().hex[:8]}"  # the plane is shared by the module's tests: one probe service each
    put_row(plane, service, "probe", approval_required=True, approvers=[LISTED])
    status, body = plane.request("POST", "/v1/approvals", {
        "tool": "probe", "request_id": uuid.uuid4().hex, "args": {}, "mode": "sync", "timeout_s": 60,
        "service": service, "worker_id": "w-probe"})
    assert status in (200, 201), body
    status, body = decide_as(plane, body["id"], OUTSIDER)
    if not (status == 403 and isinstance(body, dict) and body.get("error") == "approver_not_eligible"):
        pytest.skip("this plane predates TR1-A2: a decide by someone not on the approvers list is not refused with 403 approver_not_eligible")
    return plane


def test_a_sync_call_keeps_waiting_through_a_refused_decide_and_runs_once_when_a_listed_person_approves(enforcing):
    plane = enforcing  # noqa: F811
    start(plane, "appr-sync")
    put_row(plane, "appr-sync", "refund", approval_required=True, approval_mode="sync", approvers=[LISTED])
    ran = []

    @runbound.tool(effects={"financial"}, reviewed=True)
    def refund(order: str):
        ran.append(order)
        return "refunded"

    api_ready = _until(lambda: api._SHARED.row_for("refund"), what="the row")
    assert api_ready.approval_required
    thread, box = in_thread(refund, "o-1")
    (waiting,) = _until(lambda: pending(plane, "appr-sync"), what="the pending request")

    status, body = decide_as(plane, waiting["id"], OUTSIDER)
    assert status == 403 and body["error"] == "approver_not_eligible"
    time.sleep(2)  # the SDK's long poll comes back around at least once
    assert thread.is_alive() and box == {} and ran == []  # still waiting; nothing ran
    assert row(plane, waiting["id"])["status"] == "pending"

    status, body = decide_as(plane, waiting["id"], LISTED)
    assert status == 200, body
    thread.join(15)
    assert box.get("value") == "refunded" and ran == ["o-1"]  # exactly once
    _until(lambda: row(plane, waiting["id"])["status"] == "executed", what="the executed report")


def test_a_deferred_request_stays_unclaimed_through_a_refused_decide_and_runs_once_when_a_listed_person_approves(enforcing):
    plane = enforcing  # noqa: F811
    results = start(plane, "appr-deferred", approval_secret=SECRET)
    put_row(plane, "appr-deferred", "archive", approvers=[LISTED])
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        ran.append(ticket)
        return ticket

    out = archive(7)
    assert out["status"] == "pending_approval"
    (waiting,) = _until(lambda: pending(plane, "appr-deferred"), what="the pending request")

    status, body = decide_as(plane, waiting["id"], OUTSIDER)
    assert status == 403 and body["error"] == "approver_not_eligible"
    time.sleep(2)  # several heartbeats: a worker would claim it if it were approved
    assert ran == [] and results == [] and row(plane, waiting["id"])["status"] == "pending"

    status, body = decide_as(plane, waiting["id"], LISTED)
    assert status == 200, body
    _until(lambda: results, what="the deferred execution")
    time.sleep(1.5)  # and nothing runs a second time
    assert ran == [7] and [r["outcome"] for r in results] == ["executed"]
