"""PS-AAI-03g against a REAL plane: an approved DEFERRED request whose execution-entry re-check refuses is settled ``superseded`` and its quota unit comes back.

The tool is code-declared ``approval="required", mode="deferred"`` under a row that allows ONE call an hour for the service, so the approval's create takes the only unit.
A person approves; the instant the resumed chain reaches the approval slot an operator rolls out a deny line the worker then holds; the re-check refuses
``approval_superseded``. The plane must be told ``superseded`` (not ``failed``), and once the deny line is gone the next call must get the unit again.
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api, approvals

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

SERVICE = "deferred-superseded"
SECRET = "supersede-secret-" + "k3" * 8
QUOTA = {"limit": 1, "window": "hour", "per": "service"}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def put_row(plane, change=False, **fields):
    if change:
        status, body = plane.request("PATCH", f"/v1/admin/tool-policies/{SERVICE}/wire", {"comment": "changed during the wait", **fields})
    else:
        status, body = plane.request("POST", "/v1/admin/tool-policies", {"service": SERVICE, "tool": "wire", **fields})
    assert status in (200, 201), body
    if change:  # an edit of an enforced rule is live at once
        return
    status, body = plane.request("POST", f"/v1/admin/tool-policies/{SERVICE}/wire/rollout", {"state": "enforced", "comment": "test"})
    assert status == 200, body


def pending(plane):
    status, body = plane.request("GET", f"/v1/admin/approvals?status=pending&service={SERVICE}")
    assert status == 200, body
    return body["approvals"]


def test_a_superseded_deferred_request_is_settled_superseded_and_returns_its_quota_unit(private_plane, monkeypatch):  # noqa: F811
    plane = private_plane
    put_row(plane, quota=QUOTA)
    results = []
    runbound.init(control_plane_url=plane.url, token=KEY, service=SERVICE, control_plane_poll_s=0.2, approval_secret=SECRET, on_deferred_result=results.append)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="deferred", timeout_s=120)
    def wire(amount: float):
        ran.append(amount)
        return "sent"

    _until(lambda: api._SHARED.row_for("wire"), what="the quota row")
    first = wire(5.0)
    assert first["status"] == "pending_approval", first
    (waiting,) = _until(lambda: pending(plane), what="the pending request")
    assert waiting["quota"] is not None, "the plane took the only unit at create"

    real = approvals.admit
    moved = []

    def admit(engine, session, call, spec, info):
        real(engine, session, call, spec, info)
        if info.get("approval_id") and not moved:  # the resumed run, at the approval slot: the policy moves now
            moved.append(1)
            put_row(plane, change=True, lines=[{"decision": "deny", "limits": None}], otherwise="inherit")
            _until(lambda: (row := api._SHARED.row_for("wire")) is not None and row.blocked, what="the worker to hold the deny row")

    monkeypatch.setattr(approvals, "admit", admit)
    status, body = plane.request("POST", f"/v1/admin/approvals/{waiting['id']}/approve", {"reason": "ok"})
    assert status == 200, body

    def settled():
        status, got = plane.request("GET", f"/v1/admin/approvals/{waiting['id']}")
        return got if status == 200 and got.get("status") in ("superseded", "failed", "executed") else None

    row = _until(settled, 30, "the plane to settle the request")
    assert row["status"] == "superseded", (row, results)
    assert ran == [] and results and results[-1]["outcome"] == "superseded" and results[-1]["error"] == "approval_superseded"

    monkeypatch.setattr(approvals, "admit", real)
    put_row(plane, change=True, lines=[], otherwise=None)  # the deny line is gone; the quota row stands
    _until(lambda: (row := api._SHARED.row_for("wire")) is not None and not row.blocked, what="the worker to hold the quota row again")
    second = wire(6.0)
    assert second["status"] == "pending_approval", f"the unit came back: the next call takes it ({second})"
