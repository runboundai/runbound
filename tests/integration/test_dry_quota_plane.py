"""VPC B6b against a real plane (register C4, O-DRY-2, J-DRY-1): a dry-run quota row takes no live unit, so the plane's counter does not move, yet the worker still
records that a live row would have been over. A plane without the quota peek (one that predates ``cfg_hash``) skips the module."""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

LIMIT = 2


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def peek(plane, service, tool):
    return plane.request("POST", "/v1/quota/take", {"tool": tool, "service": service, "peek": True})


def test_a_dry_run_quota_moves_no_live_counter_and_still_records_what_a_live_row_would_have_refused(private_plane):  # noqa: F811
    service, tool = "dry-quota", "act"
    runbound.init(control_plane_url=private_plane.url, token=KEY, service=service, control_plane_poll_s=0.2, cache_dir=str(private_plane.db.parent / "cache"))
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")

    @runbound.tool(effects={"read"}, reviewed=True, name=tool)
    def act():
        return "ok"

    status, body = private_plane.request("POST", "/v1/admin/tool-policies", {"service": service, "tool": tool, "state": "dry_run",
                                                                             "quota": {"limit": LIMIT, "window": "hour", "per": "service"}})
    assert status in (200, 201), body
    row = _until(lambda: api._SHARED.row_for(tool), what="the served dry-run row")
    assert row.dry_run and row.quota
    if not api._SHARED.quota_peek_supported():
        pytest.skip("this plane serves no cfg_hash, so it has no quota peek")

    outcomes = []
    for _ in range(LIMIT + 2):
        assert act() == "ok", "a dry run never refuses"
        outcomes.append("would_refuse:quota_exceeded" in runbound.tool_results()[-1]["tags"])
    assert outcomes == [False] * LIMIT + [True, True], "the worker counts what it saw, so the calls past the limit read as would-refuse"

    status, answer = peek(private_plane, service, tool)
    assert status == 200 and answer.get("peek") is True, answer
    assert answer["remaining"] == LIMIT, "the plane's counter for the window never moved: not one live unit was taken"
