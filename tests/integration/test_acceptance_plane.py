"""VPC-B5 against a real plane (WIRE-VPC 2, 3, 11): the plane stores what the worker says it accepted, serves ``rule_revision`` to a worker that reported
``revisions_v1`` (which still enforces), and counts the worker as worker-reported in a change's cohort.

A plane that predates the acceptance fields skips the module."""

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
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2, cache_dir=str(plane.db.parent / "cache"))
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def block(plane, service, tool):
    """An enforced row that blocks ``tool``: a live change, with this worker already in the cohort."""
    status, body = plane.request("POST", "/v1/admin/tool-policies", {"service": service, "tool": tool, "lines": [{"decision": "deny"}], "state": "enforced", "comment": "test"})
    if status == 400 and "lines" in str(body):
        pytest.skip("this plane predates decision lines")
    assert status in (200, 201), body


def require_changes(plane):
    """A feature probe: a plane that predates change records (no ``/v1/admin/policy-changes``) has no cohort to count a worker in, so what these tests say about
    one is not a claim about it. Skip, as every other plane-version-dependent test here does."""
    status, _body = plane.request("GET", "/v1/admin/policy-changes?service=probe")
    if status == 404:
        pytest.skip("this plane predates change records (no /v1/admin/policy-changes), so there is no cohort to count the worker in")


def propagation(plane, service):
    status, body = plane.request("GET", f"/v1/admin/tool-policies/propagation?service={service}")
    assert status == 200, body
    return body


def test_the_plane_stores_the_acceptance_and_the_change_counts_the_worker_as_worker_reported(private_plane):  # noqa: F811
    service = "accept-store"
    start(private_plane, service)

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire():
        return "sent"

    if "cfg_hash" not in propagation(private_plane, service):
        pytest.skip("this plane predates the acceptance fields (cfg_hash)")
    require_changes(private_plane)
    block(private_plane, service, "wire")
    shared = api._SHARED
    _until(lambda: shared.row_for("wire"), what="the served row")

    def accepted():
        body = propagation(private_plane, service)
        mine = [w for w in body.get("items", []) if w.get("worker_id") == shared._config.resolved_worker_id()]
        return mine and mine[0].get("report") == "worker_reported" and mine[0].get("accepted_cfg") == body["cfg_hash"] and mine[0]

    worker = _until(accepted, what="the plane to store this worker's acceptance of the served configuration")
    assert worker["boot_id"] == shared._boot_id, "the plane keeps which PROCESS accepted it"

    status, listing = private_plane.request("GET", f"/v1/admin/policy-changes?service={service}")
    assert status == 200 and listing["changes"], listing
    status, detail = private_plane.request("GET", f"/v1/admin/policy-changes/{listing['changes'][0]['id']}")
    assert status == 200, detail
    mine = [c for c in detail["cohort"] if c["worker_id"] == shared._config.resolved_worker_id()]
    assert mine and mine[0]["report"] == "worker_reported" and mine[0]["boot_id"] == shared._boot_id


def test_the_cohort_says_accepted_once_the_worker_echoes_the_hash_the_plane_served(private_plane):  # noqa: F811
    service = "accept-cohort"
    require_changes(private_plane)
    start(private_plane, service)

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire():
        return "sent"

    block(private_plane, service, "wire")
    shared = api._SHARED
    _until(lambda: shared.row_for("wire"), what="the served row")
    status, listing = private_plane.request("GET", f"/v1/admin/policy-changes?service={service}")
    change = listing["changes"][0]["id"]

    def accepted():
        _status, detail = private_plane.request("GET", f"/v1/admin/policy-changes/{change}")
        return [c for c in detail["cohort"] if c["worker_id"] == shared._config.resolved_worker_id() and c["acceptance"] == "accepted"]

    assert _until(accepted, timeout=20, what="the cohort to say accepted")


def test_a_worker_that_reported_revisions_is_served_rule_revision_and_still_enforces(private_plane):  # noqa: F811
    service = "accept-revisions"
    start(private_plane, service)

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire():
        return "sent"

    block(private_plane, service, "wire")
    shared = api._SHARED
    row = _until(lambda: shared.row_for("wire"), what="the served row")
    raw = {r.get("tool"): r for r in (shared._rows_raw or [])}["wire"]
    if "rule_revision" not in raw:
        pytest.skip("this plane does not serve rule_revision (WIRE-VPC 3)")
    assert raw["rule_revision"] == 1 and not row.unsupported, "a served rule_revision is read, not refused"
    assert shared.policy_unsupported() == []
    with pytest.raises(runbound.ExecutionRefused) as refused:
        wire()
    assert refused.value.reason == "tool_blocked", "the rule decides exactly as it did before the key existed"
