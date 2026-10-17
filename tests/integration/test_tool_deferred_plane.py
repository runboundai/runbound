"""The ``deferred`` trait round-trips through a REAL control plane (v1.1; Apollo's ruling: deferred needs the tool's own code).

The SDK reports ``deferred: true`` only for a tool whose decorator registered ``mode="deferred"`` and ``false`` for every other
decorated tool. The plane stores it on its tools read, and refuses a row whose lines ask for ``approval_mode: deferred`` on a tool
whose newest report says ``false`` (422 ``deferred_not_registered``); a tool that registered deferred accepts it.
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, plane  # noqa: F401  (fixture)

pytestmark = pytest.mark.plane

SVC = "deferred-trait"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def ask(mode):
    return {"decision": "ask", "approval": {"approval_mode": mode, "approval_timeout_s": 60, "approvers": ["a@b.co"]}}


def row_for(plane, tool):  # noqa: F811
    def read():
        status, body = plane.request("GET", f"/v1/admin/tools?service={SVC}")
        assert status == 200, body
        rows = {r["tool"]: r for r in body.get("tools", [])}
        return rows.get(tool)

    return _until(read, what=f"{tool} in the plane's inventory")


def test_the_plane_stores_deferred_and_refuses_a_deferred_row_on_a_tool_that_did_not_register_it(plane):  # noqa: F811
    @runbound.tool(effects={"external"}, approval="required", mode="deferred", reviewed=True)
    def later(x: int = 0):
        return "ok"

    @runbound.tool(effects={"external"}, approval="required", reviewed=True)
    def now(x: int = 0):
        return "ok"

    runbound.init(control_plane_url=plane.url, token=KEY, service=SVC, control_plane_poll_s=0.2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")

    assert row_for(plane, "later").get("deferred") is True
    assert row_for(plane, "now").get("deferred") is False

    refused = plane.request("POST", "/v1/admin/tool-policies", {"service": SVC, "tool": "now", "lines": [ask("deferred")]})
    assert refused[0] == 422 and refused[1]["error"] == "deferred_not_registered", refused
    accepted = plane.request("POST", "/v1/admin/tool-policies", {"service": SVC, "tool": "later", "lines": [ask("deferred")]})
    assert accepted[0] == 201, accepted
    assert plane.request("POST", "/v1/admin/tool-policies", {"service": SVC, "tool": "now", "lines": [ask("sync")]})[0] == 201
