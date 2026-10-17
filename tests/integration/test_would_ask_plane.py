"""A dry run of an ask rule reaches the plane as evidence (VPC-02, demo finding (a)): 12 calls to a dry-run ask row read as 12 would-ask evaluations, not 0.

A plane that does not report ``would_ask`` in its evidence (one that predates it) skips the module."""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

CALLS = 12


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_twelve_calls_to_a_dry_run_ask_rule_are_twelve_would_ask_evaluations(private_plane):  # noqa: F811
    service, tool = "would-ask", "act"
    runbound.init(control_plane_url=private_plane.url, token=KEY, service=service, control_plane_poll_s=0.2, cache_dir=str(private_plane.db.parent / "cache"))
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")

    @runbound.tool(effects={"read"}, reviewed=True, name=tool)
    def act(n: int = 0):
        return "ran"

    status, body = private_plane.request("POST", "/v1/admin/tool-policies", {"service": service, "tool": tool, "approval_required": True})
    assert status in (200, 201), body
    status, body = private_plane.request("POST", f"/v1/admin/tool-policies/{service}/{tool}/rollout", {"state": "dry_run"})
    assert status == 200, body
    row = _until(lambda: api._SHARED.row_for(tool), what="the served dry-run row")
    assert row.dry_run and row.approval_required

    for n in range(CALLS):
        assert act(n) == "ran", "a dry run asks nobody and refuses nothing"
    api._SHARED._exporter.flush(5.0)

    def evidence():
        status, body = private_plane.request("GET", f"/v1/admin/tool-policies/{service}/{tool}")
        found = ((body or {}).get("row") or {}).get("evidence") or {}
        if status == 200 and "would_ask" not in found:
            pytest.skip("this plane's evidence does not report would_ask")
        return found if found.get("would_ask") == CALLS else None

    found = _until(evidence, what="the plane to count the would-ask events")
    assert (found["would_ask"], found["would_refuse"], found["evaluations"]) == (CALLS, 0, CALLS)
    # No approval row of ANY status exists for this service after the 12 calls: a would-ask is evidence, never a request (Apollo's addition to 1.5).
    def approvals_of(query):
        status, body = private_plane.request("GET", f"/v1/admin/approvals?service={service}&limit=100{query}")
        assert status == 200, body
        rows = body.get("approvals", body.get("items")) if isinstance(body, dict) else body
        assert isinstance(rows, list), f"this test does not understand the approvals list shape: {body!r}"
        return rows

    assert approvals_of("") == [], "an approval row was created by a dry run"
    for state in ("pending", "approved", "denied", "expired"):
        assert approvals_of(f"&status={state}") == [], f"a {state} approval exists"
