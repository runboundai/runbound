"""Per-tool policy rows and quotas against a REAL control plane (T1 SDK half)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, plane, private_plane  # noqa: F401  (fixtures)

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def put_row(plane, service, tool, state="enforced", **fields):  # noqa: F811
    status, body = plane.request("POST", "/v1/admin/tool-policies", {"service": service, "tool": tool, **fields})
    assert status in (200, 201), body
    status, body = plane.request("POST", f"/v1/admin/tool-policies/{service}/{tool}/rollout", {"state": state, "comment": "test"})
    assert status == 200, body


def start(plane, service, **config):  # noqa: F811
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2, **config)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def have_row(tool):
    return _until(lambda: api._SHARED.row_for(tool), what=f"the row for {tool}")


def test_a_dashboard_row_blocks_a_tool_the_code_allows_and_a_dry_run_row_refuses_nothing(plane):  # noqa: F811
    start(plane, "rows-block")

    @runbound.tool(effects={"read"}, reviewed=True)
    def delete_customer(cid: int):
        return "deleted"

    @runbound.tool(effects={"read"}, reviewed=True)
    def export_report(rid: int):
        return "exported"

    assert delete_customer(1) == "deleted"
    put_row(plane, "rows-block", "delete_customer", blocked=True)
    put_row(plane, "rows-block", "export_report", state="dry_run", blocked=True)
    have_row("delete_customer"), have_row("export_report")

    with pytest.raises(runbound.ExecutionRefused) as caught:
        delete_customer(1)
    assert caught.value.reason == "tool_blocked"
    assert export_report(1) == "exported"  # dry_run: nothing refused
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]


def test_an_unchanged_rows_hash_replaces_nothing_even_when_the_version_moves(plane):  # noqa: F811
    start(plane, "rows-hash")

    @runbound.tool(effects={"read"}, reviewed=True)
    def look(x: int = 0):
        return "ok"

    put_row(plane, "rows-hash", "look", max_calls_per_session=5)
    have_row("look")
    held, digest = api._SHARED._rows, api._SHARED._rows_hash
    version = api._SHARED.policy_version
    fetches = []
    real = api._SHARED._client.policy
    api._SHARED._client.policy = lambda service: fetches.append(1) or real(service)

    # a draft row for another tool bumps the service's policy version without changing a single served row
    status, _ = plane.request("POST", "/v1/admin/tool-policies", {"service": "rows-hash", "tool": "other", "blocked": True})
    assert status in (200, 201)
    _until(lambda: api._SHARED.policy_version > version, what="the version bump")
    assert fetches and api._SHARED._rows is held and api._SHARED._rows_hash == digest  # fetched, kept
    seen = len(fetches)
    time.sleep(1.0)
    assert len(fetches) == seen  # and no refetch while the version stands still

    from runbound import policy_rows

    assert policy_rows.rows_hash(api._SHARED._rows_raw) == digest  # the SDK recomputes the plane's hash exactly


def test_three_sdk_workers_against_twenty_per_clock_minute_admit_exactly_twenty(private_plane, tmp_path):  # noqa: F811
    put_row(private_plane, "quota-fleet", "burst", quota={"limit": 20, "window": "minute", "per": "service"})
    out = tmp_path / "admitted.txt"
    worker = textwrap.dedent('''
        import sys, time
        import runbound
        url, key, out, tag = sys.argv[1:5]
        runbound.init(control_plane_url=url, token=key, service="quota-fleet", control_plane_poll_s=0.2,
                      worker_id=tag)
        deadline = time.monotonic() + 20
        while runbound.plane_status().mode != "connected" and time.monotonic() < deadline:
            time.sleep(0.05)

        @runbound.tool(effects={"read"}, reviewed=True)
        def burst(n: int):
            return n

        go = float(sys.argv[5])
        time.sleep(max(0.0, go - time.time()))
        for n in range(30):
            try:
                burst(n)
                line = "ok"
            except runbound.ExecutionRefused as exc:
                line = "refused:" + exc.reason
            with open(out, "a") as f:
                f.write(tag + " " + line + "\\n")
            time.sleep(0.02)
    ''')
    # start just after a clock-minute boundary, so the fixed window cannot roll over mid-test
    now = time.time()
    go = (int(now // 60) + 1) * 60 + 1.0 if now % 60 > 20 else now + 6.0
    procs = [subprocess.Popen([sys.executable, "-c", worker, private_plane.url, KEY, str(out), f"w{i}", str(go)])
             for i in range(3)]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    lines = out.read_text().splitlines()
    admitted = [l for l in lines if l.endswith(" ok")]
    refused = [l for l in lines if "refused:quota_exceeded" in l]
    assert len(admitted) == 20, (len(admitted), len(refused), lines)
    assert len(refused) == 3 * 30 - 20


def test_a_plane_outage_spends_the_lease_then_refuses_quota_plane_unavailable(private_plane):  # noqa: F811
    put_row(private_plane, "quota-outage", "look", quota={"limit": 20, "window": "hour", "per": "service"})
    start(private_plane, "quota-outage")

    @runbound.tool(effects={"read"}, reviewed=True)
    def look(x: int = 0):
        return "ok"

    have_row("look")
    held = _until(lambda: api._SHARED.quota.report().get("look"), what="a lease")
    assert 1 <= held <= 10  # never more than half of what is left
    private_plane.kill()
    _until(lambda: runbound.plane_status().mode in ("degraded", "unavailable"), 20, "the outage to be noticed")
    admitted = 0
    for _ in range(40):
        try:
            look()
            admitted += 1
        except runbound.ExecutionRefused as exc:
            assert exc.reason == "quota_plane_unavailable"
            break
    assert admitted == held  # exactly the lease in hand, then refusal for every class


def test_an_approval_holds_its_unit_the_sdk_takes_no_second_one(private_plane):  # noqa: F811
    # limit 4, not 1: the plane leases units to the worker on its heartbeats (up to half of what is left), and
    # an approval's create takes from the same counter, so a limit of 1 would already be leased away
    put_row(private_plane, "quota-approval", "wire", quota={"limit": 4, "window": "hour", "per": "service"},
            approval_required=True, approval_timeout_s=60)
    start(private_plane, "quota-approval")

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire(amount: float):
        return "sent"

    have_row("wire")
    paths = []
    real = api._SHARED.approvals
    api._SHARED.approvals = lambda method, path, body=None, timeout=5.0: (paths.append(path), real(method, path, body, timeout))[1]
    import threading

    box = {}
    thread = threading.Thread(target=lambda: box.update(first=wire(5.0)))
    thread.start()
    waiting = _until(lambda: private_plane.request("GET", "/v1/admin/approvals?status=pending&service=quota-approval")[1]
                     .get("approvals"), what="the pending approval")
    assert waiting[0]["quota"] is not None  # the plane took the unit at create
    status, _ = private_plane.request("POST", f"/v1/admin/approvals/{waiting[0]['id']}/approve", {"reason": "ok"})
    assert status == 200
    thread.join(20)
    assert box["first"] == "sent"
    assert "/v1/quota/take" not in paths  # the SDK took nothing of its own for a call a person was asked about


def test_a_code_declared_approval_tool_is_never_leased_and_its_create_gets_the_only_unit(private_plane):  # noqa: F811
    # limit 1, no approval_required on the row: only the tool report (@tool(approval="required")) says it asks a
    # person. Two workers heartbeat; neither may be leased the unit the approval's create needs.
    put_row(private_plane, "quota-code-approval", "wire", quota={"limit": 1, "window": "hour", "per": "service"})
    other = textwrap.dedent('''
        import sys, time
        import runbound

        @runbound.tool(effects={"read"}, reviewed=True, approval="required")
        def wire(amount: float):
            return "sent"

        runbound.init(control_plane_url=sys.argv[1], token=sys.argv[2], service="quota-code-approval",
                      control_plane_poll_s=0.2, worker_id="w2")

        time.sleep(float(sys.argv[3]))
    ''')
    proc = subprocess.Popen([sys.executable, "-c", other, private_plane.url, KEY, "12"])
    try:
        @runbound.tool(effects={"read"}, reviewed=True, approval="required")
        def wire(amount: float):
            return "sent"

        start(private_plane, "quota-code-approval")  # decorated first, as in an app: the first hello carries the report

        have_row("wire")
        time.sleep(3.0)  # many heartbeats of both workers with the row and the tool report in place
        assert api._SHARED.quota.report() == {}  # the plane leased this worker nothing

        import threading

        box = {}
        thread = threading.Thread(target=lambda: box.update(first=wire(5.0)))
        thread.start()
        waiting = _until(lambda: private_plane.request("GET", "/v1/admin/approvals?status=pending&service=quota-code-approval")[1]
                         .get("approvals"), what="the pending approval")
        assert waiting[0]["quota"] is not None  # the create got the only unit
        status, _ = private_plane.request("POST", f"/v1/admin/approvals/{waiting[0]['id']}/approve", {"reason": "ok"})
        assert status == 200
        thread.join(20)
        assert box["first"] == "sent"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
