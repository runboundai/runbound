"""R4 grants against a REAL control plane, seen from a worker (TR4-O; the plane's side is TR4-A).

An operator approves a waiting call "for the session"; the same tool with the SAME arguments for the same requester then runs with nobody
asked, DIFFERENT arguments ask again, a call without a requester asks, and Revoke ends the grant at once. Skips on a plane without grants.
"""

from __future__ import annotations

import uuid

import pytest

import runbound
from runbound import api

from .test_approvals_plane import _as_console, in_thread, pending, start
from .test_real_plane import _until, plane  # noqa: F401  (fixture)
from .test_rows_quota_plane import put_row

pytestmark = pytest.mark.plane

REQUESTER = "ana@acme.example"
APPROVER = "bo@acme.example"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def grant_plane(plane):  # noqa: F811
    status, _ = plane.request("GET", "/v1/admin/grants?status=all&limit=1")
    if status != 200:
        pytest.skip("this plane has no grants (TR4-A)")
    return plane


def approve_for_session(plane, approval_id):  # noqa: F811
    status, body = plane.request("POST", f"/v1/admin/approvals/{approval_id}/approve", {"grant_scope": "session", "reason": "test"},
                                 headers=_as_console(APPROVER))
    assert status == 200 and body["grant"]["grant_scope"] == "session", body
    return body["grant"]


def test_a_session_grant_admits_the_same_arguments_for_the_same_requester_and_nothing_else(plane):  # noqa: F811
    plane = grant_plane(plane)
    service = f"grants-{uuid.uuid4().hex[:8]}"
    start(plane, service)
    put_row(plane, service, "refund", approval_required=True, approval_mode="sync")
    _until(lambda: api._SHARED.row_for("refund"), what="the row")
    ran = []

    @runbound.tool(effects={"financial"}, reviewed=True)
    def refund(order: str):
        ran.append(order)
        return "refunded"

    def call(order, requester=REQUESTER):
        if requester is None:
            return refund(order)
        with runbound.session("s-grants", requested_for=requester):
            return refund(order)

    thread, box = in_thread(call, "o-1")  # first call: nobody has approved yet, so it waits
    (waiting,) = _until(lambda: pending(plane, service), what="the pending request")
    grant = approve_for_session(plane, waiting["id"])
    thread.join(15)
    assert box.get("value") == "refunded" and ran == ["o-1"]

    assert call("o-1") == "refunded" and ran == ["o-1", "o-1"]  # the same arguments, the same requester: no request opened
    assert pending(plane, service) == []

    thread, box = in_thread(call, "o-2")  # different arguments ask again
    (other,) = _until(lambda: pending(plane, service), what="a request for different arguments")
    assert other["args"] == {"order": "o-2"} and ran == ["o-1", "o-1"]
    status, _ = plane.request("POST", f"/v1/admin/approvals/{other['id']}/deny", {"reason": "no"}, headers=_as_console(APPROVER))
    assert status == 200
    thread.join(15)
    assert isinstance(box.get("error"), runbound.ExecutionRefused)

    status, body = plane.request("DELETE", f"/v1/admin/grants/{grant['id']}", {"reason": "done"})  # Revoke: the very next call asks
    assert status == 200, body
    thread, box = in_thread(call, "o-1")
    (again,) = _until(lambda: pending(plane, service), what="a request after the revoke")
    plane.request("POST", f"/v1/admin/approvals/{again['id']}/deny", {"reason": "no"}, headers=_as_console(APPROVER))
    thread.join(15)
    assert ran == ["o-1", "o-1"]
