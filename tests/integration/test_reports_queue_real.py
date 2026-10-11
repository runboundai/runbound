"""S7b against a real plane: the plane is unreachable when a deferred action finishes; once it is back
the result is reported exactly once (one ``approval_executed``), so the row is neither lost nor expired."""

from __future__ import annotations

import sqlite3
import time

import pytest

import runbound
from runbound import api

from .test_approvals_plane import decide, pending, row
from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def events(plane, kind):
    connection = sqlite3.connect(str(plane.db))
    try:
        return connection.execute("SELECT count(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0]
    finally:
        connection.close()


def test_a_report_made_while_the_plane_is_unreachable_is_delivered_exactly_once_later(private_plane):  # noqa: F811
    results = []
    runbound.init(control_plane_url=private_plane.url, token=KEY, service="reports", control_plane_poll_s=0.2,
                  approval_secret="s" * 16, on_deferred_result=results.append)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=120)
    def archive(ticket: int):
        ran.append(ticket)
        return ticket

    archive(5)
    (waiting,) = _until(lambda: pending(private_plane, "reports"), what="the pending request")

    shared = api._SHARED
    real = shared.approvals
    down = {"report": True}

    def flaky(method, path, body=None, timeout=5.0):
        if path.endswith("/report") and down["report"]:
            return None, None  # the plane is unreachable at report time
        return real(method, path, body, timeout)

    shared.approvals = flaky
    decide(private_plane, waiting["id"], "approve")
    _until(lambda: results, what="the execution")
    assert ran == [5] and results[0]["outcome"] == "executed"
    _until(lambda: shared.reports.pending() == 1, 20, "the report to be queued")
    assert row(private_plane, waiting["id"])["status"] == "claimed"  # the plane has not heard
    assert events(private_plane, "approval_executed") == 0

    down["report"] = False  # the plane is reachable again
    _until(lambda: row(private_plane, waiting["id"])["status"] == "executed", 20, "the queued report to land")
    time.sleep(1.5)  # more hellos must not report it again
    assert shared.reports.pending() == 0
    assert events(private_plane, "approval_executed") == 1
    assert ran == [5]  # and it did not run twice
