"""An "allow when ..., otherwise deny" row against a REAL plane (MCP-X1 S2 finding): the big call runs, the small one is refused as the row's own otherwise.

The plane serves the row with a legacy view of ``blocked: true``. The SDK used to refuse the whole tool as ``policy_unsupported`` because its static reading of the lines ignored the
``otherwise``; the same row through the MCP boundary and through a plain SDK worker behaved identically (S2), so this is the SDK's row check, tested here with a plain worker.
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, plane  # noqa: F401  (fixture)

pytestmark = pytest.mark.plane

ALLOW_OVER_1000_OTHERWISE_DENY = {"lines": [{"decision": "allow", "when": {"all": [{"arg": "amount", "op": ">", "value": 1000}]}, "limits": None}], "otherwise": "deny"}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def outcome(fn, amount):
    try:
        fn("c-100", amount)
        return "ran"
    except runbound.ExecutionRefused as refused:
        return refused.reason


def test_allow_when_over_a_threshold_otherwise_deny_runs_the_big_call_and_refuses_the_small_one(plane):  # noqa: F811
    service, ran = "allow-when-otherwise", []
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2)

    @runbound.tool(effects={"write"})
    def issue_refund(customer_id: str, amount: float):
        ran.append(amount)
        return "refunded"

    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    _until(lambda: plane.request("GET", f"/v1/admin/tools?service={service}")[1].get("tools"), what="the tool report")
    status, body = plane.request("POST", "/v1/admin/tool-policies", {"service": service, "tool": "issue_refund", **ALLOW_OVER_1000_OTHERWISE_DENY})
    assert status in (200, 201), body
    status, body = plane.request("POST", f"/v1/admin/tool-policies/{service}/issue_refund/rollout", {"state": "enforced", "comment": "allow-when/otherwise"})
    assert status == 200, body
    held = _until(lambda: api._SHARED.row_for("issue_refund"), what="the row to reach the worker")
    assert held.conditional, "served with its lines"
    assert not api._SHARED.policy_unsupported(), api._SHARED.policy_unsupported()

    assert outcome(issue_refund, 2500.0) == "ran" and ran == [2500.0]
    assert outcome(issue_refund, 500.0) == "tool_blocked" and ran == [2500.0], "no line matched: the row's own otherwise (deny) decided"
