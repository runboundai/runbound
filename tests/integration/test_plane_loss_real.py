"""Plane loss against a real plane: kill it, and the guard fails closed where it must (S7)."""

from __future__ import annotations

import time

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


def start(plane, service="plane-loss"):
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2,
                  consequential_grace_s=15)  # the shortest grace, so the test does not wait a minute


def tools():
    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop_table(name: str):
        return "dropped"

    @runbound.tool(effects={"read"}, reviewed=True)
    def look(name: str):
        return "found"

    return drop_table, look


def test_killing_the_plane_refuses_a_destructive_tool_and_a_read_tool_runs(private_plane):  # noqa: F811
    start(private_plane)
    drop_table, look = tools()
    _until(lambda: runbound.plane_status().mode == "connected", what="a hello")
    assert drop_table("t") == "dropped" and look("t") == "found"

    private_plane.kill()
    _until(lambda: runbound.plane_status().mode == "degraded", 10, "three missed hellos")
    assert drop_table("t") == "dropped"  # degraded: everything still runs on the cache
    assert look("t") == "found"

    def refused():
        try:
            drop_table("t")
        except runbound.ExecutionRefused as exc:
            return exc

    caught = _until(refused, 30, "the grace to run out")  # 15 s of silence
    assert caught.reason == "plane_unavailable" and caught.retryable
    assert look("t") == "found"  # a read tool keeps running on what it has


def test_a_restart_enforces_the_cached_org_policy_before_the_first_hello(private_plane, tmp_path):  # noqa: F811
    status, _ = private_plane.request("POST", "/v1/admin/policies", {
        "service": "plane-loss-boot", "body": {"deny": ["look"]}, "state": "enforced"})
    assert status == 200
    start(private_plane, "plane-loss-boot")
    _, look = tools()
    _until(lambda: runbound.plane_status().applied_policy_version is not None, what="the policy fetch")
    with pytest.raises(runbound.PolicyViolation):
        look("t")
    private_plane.kill()
    api._teardown_for_tests()  # the worker restarts with the plane still dead

    start(private_plane, "plane-loss-boot")
    _, look = tools()
    assert runbound.plane_status().mode in ("connecting", "degraded")  # no hello has been answered
    with pytest.raises(runbound.PolicyViolation):  # enforced from the cache, before any hello
        look("t")


def test_a_destructive_tool_waits_for_a_hello_when_there_is_neither_a_hello_nor_a_cache(private_plane):  # noqa: F811
    start(private_plane, "plane-loss-nocache")
    drop_table, _ = tools()
    private_plane.kill()
    api._teardown_for_tests()
    start(private_plane, "plane-loss-nocache")  # a restart with the plane dead and no policy ever served
    drop_table, _ = tools()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        drop_table("t")
    assert caught.value.reason == "plane_unavailable"
