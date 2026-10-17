"""RVPC-2 (WIRE-VPC 12e) against a REAL plane: an approved call whose policy changed during the wait carries both phases, and Explain shows them.

A person is asked; while they decide an admin writes a deny line and rolls it out; the worker holds the new row; the person approves; the execution-entry re-check refuses the call
``approval_superseded``. The refusal's record (what the SDK really built, not a fixture) is explained by the plane, and the plane's request for the approval is settled ``superseded``.
A plane that predates the supersession block skips the explain half.
"""

from __future__ import annotations

import threading

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

SERVICE = "recheck-phase"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_a_deny_line_rolled_out_during_the_wait_gives_a_two_phase_record_and_explain_shows_both(private_plane):  # noqa: F811
    plane = private_plane
    runbound.init(control_plane_url=plane.url, token=KEY, service=SERVICE, control_plane_poll_s=0.2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=60)
    def refund(order: str):
        ran.append(order)
        return "refunded"

    box: dict = {}

    def call():
        try:
            box["value"] = refund("o-1")
        except Exception as error:  # noqa: BLE001
            box["error"] = error

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    status, body = None, None

    def waiting():
        nonlocal status, body
        status, body = plane.request("GET", f"/v1/admin/approvals?status=pending&service={SERVICE}")
        return body["approvals"] if status == 200 and body["approvals"] else None

    (pending,) = _until(waiting, what="the pending request")

    status, body = plane.request("POST", "/v1/admin/tool-policies", {"service": SERVICE, "tool": "refund", "lines": [{"decision": "deny", "limits": None}], "otherwise": "inherit"})
    assert status in (200, 201), body
    status, body = plane.request("POST", f"/v1/admin/tool-policies/{SERVICE}/refund/rollout", {"state": "enforced", "comment": "closed during the wait"})
    assert status == 200, body
    _until(lambda: (row := api._SHARED.row_for("refund")) is not None and row.blocked, what="the worker to hold the deny row")
    status, body = plane.request("POST", f"/v1/admin/approvals/{pending['id']}/approve", {"reason": "ok"})
    assert status == 200, body
    thread.join(20)

    error = box.get("error")
    assert isinstance(error, runbound.ExecutionRefused) and error.reason == "approval_superseded" and ran == []
    evaluation = error.decision.evaluation
    assert evaluation["reason_code"] == "approval_superseded"
    assert evaluation["recheck_rule_revision"] >= 1 and evaluation["recheck_reason"] == "tool_blocked" and evaluation["recheck_contributor"] == "row:L1"
    assert evaluation["recheck_policy_version"] > 0 and evaluation["recheck_cfg_hash"]
    assert "rule_revision" not in evaluation  # it asked on code alone: no row existed then

    def settled():
        status, got = plane.request("GET", f"/v1/admin/approvals/{pending['id']}")
        return got if status == 200 and got.get("status") == "superseded" else None

    _until(settled, what="the plane to settle the request superseded")

    status, explained = plane.request("POST", "/v1/admin/explain", {"service": SERVICE, "tool": "refund", "record": evaluation})
    assert status == 200, explained
    if "supersession" not in explained:
        pytest.skip("this plane predates the supersession block of Explain")
    supersession = explained["supersession"]
    assert supersession["refused_by"] == "row:L1" and supersession["reason"] == "tool_blocked"
    assert supersession["rechecked_under"]["rule_revision"] == evaluation["recheck_rule_revision"]
    assert supersession["rechecked_under"]["cfg_hash"] == evaluation["recheck_cfg_hash"]
    assert supersession["approved_under"]["generation"] == evaluation["policy_version"]
    assert explained["recheck_rule"]["found_by"] == "revision"
