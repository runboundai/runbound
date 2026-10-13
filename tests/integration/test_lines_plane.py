"""Decision lines against a real plane (TR2-O2): the SDK reports its features, is served lines, decides from them and records why.

The plane is a real process (see ``test_real_plane``); a plane that predates decision lines skips the module. Covers the wire of
WIRE-TR2: ``policy_features`` stored from the hello (and so lines served), a deny line and a cap on an allow line enforced by the SDK, the decision
record on the refusal event the plane stores, and a ``candidate`` revision recorded apart without deciding.
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def start(plane, service):
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def write(plane, service, tool, lines, otherwise="inherit", state="enforced"):
    status, body = plane.request("POST", "/v1/admin/tool-policies", {"service": service, "tool": tool, "lines": lines, "otherwise": otherwise})
    if status == 400 and "lines" in str(body):
        pytest.skip("this plane predates decision lines")
    assert status in (200, 201), body
    status, body = plane.request("POST", f"/v1/admin/tool-policies/{service}/{tool}/rollout", {"state": state, "comment": "test"})
    assert status == 200, body


def line(decision, **limits):
    return {"decision": decision, "limits": {"max_calls_per_session": limits.get("cap"), "quota": None} if limits else None}


def held(tool):
    return _until(lambda: api._SHARED.row_for(tool), what=f"the row for {tool}")


def test_the_worker_reports_its_features_and_is_served_lines_and_a_deny_line_refuses_with_the_record(private_plane):  # noqa: F811
    start(private_plane, "lines-deny")

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire():
        return "sent"

    write(private_plane, "lines-deny", "wire", [line("deny")])
    row = held("wire")
    assert row.matched == ("wire#L1",) and row.blocked  # decided from the served line, not from a legacy field
    with pytest.raises(runbound.ExecutionRefused) as refused:
        wire()
    assert refused.value.reason == "tool_blocked"
    evaluation = refused.value.decision.evaluation
    assert evaluation["matched_rule_ids"] == ["wire#L1"] and evaluation["contributors"] == ["row:L1"]
    api._SHARED._exporter.flush(5.0)

    def stored():
        status, body = private_plane.request("GET", "/v1/admin/anomalies?service=lines-deny")
        items = body.get("anomalies", body.get("items", body)) if isinstance(body, dict) else body
        return [a for a in items or [] if isinstance(a, dict) and a.get("record")]

    found = _until(stored, what="the refusal with its record on the plane")
    record = found[0]["record"]
    assert record["reason_code"] == "tool_blocked" and record["matched_rule_ids"] == ["wire#L1"] and record["sdk_version"] == runbound.__version__
    status, body = private_plane.request("GET", "/v1/admin/tool-policies/propagation?service=lines-deny")
    assert status == 200 and any("lines" in (item.get("features") or []) for item in body.get("items", [])), body


def test_a_cap_on_an_allow_line_is_enforced_from_the_served_lines(private_plane):  # noqa: F811
    start(private_plane, "lines-cap")

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    write(private_plane, "lines-cap", "look", [line("allow", cap=1)])
    held("look")
    assert look() == "ok"
    with pytest.raises(runbound.ExecutionRefused) as refused:
        look()
    assert refused.value.reason == "session_max_calls"


def test_a_candidate_deny_is_recorded_apart_and_the_enforced_allow_decides(private_plane):  # noqa: F811
    start(private_plane, "lines-candidate")

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ok"

    write(private_plane, "lines-candidate", "look", [line("allow")])
    held("look")
    status, body = private_plane.request("POST", "/v1/admin/tool-policies/lines-candidate/look/candidate",
                                         {"lines": [line("deny")], "comment": "try a block"})
    assert status in (200, 201), body
    _until(lambda: api._SHARED.row_for("look").candidate is not None, what="the candidate")
    assert look() == "ok"  # the enforced revision decides
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]
