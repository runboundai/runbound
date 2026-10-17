"""C26 against a REAL control plane: a per-service quota whose limit is under LEASE_MIN_PER_WORKER per live worker is taken synchronously, so one worker is never refused
below the limit because another worker holds an unused lease.

Limit 3 per hour over two live workers (this process and a subprocess "w2" that only heartbeats). Before the fix the plane leased w1 two units and w2 one: w1's third call was
refused with only two calls run. Now there is no lease: all three of w1's calls run, and the next call (from anyone) is refused.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (fixtures)
from .test_rows_quota_plane import have_row, put_row, start

pytestmark = pytest.mark.plane

SERVICE = "quota-sync-small"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_a_small_per_service_quota_over_two_workers_lets_the_third_call_run(private_plane):  # noqa: F811
    put_row(private_plane, SERVICE, "pay", quota={"limit": 3, "window": "hour", "per": "service"})
    other = textwrap.dedent('''
        import sys, time
        import runbound

        @runbound.tool(effects={"read"}, reviewed=True)
        def pay(n: int):
            return "ran"

        runbound.init(control_plane_url=sys.argv[1], token=sys.argv[2], service=sys.argv[4], control_plane_poll_s=0.2, worker_id="w2")
        time.sleep(float(sys.argv[3]))
    ''')
    proc = subprocess.Popen([sys.executable, "-c", other, private_plane.url, KEY, "25", SERVICE])
    try:
        @runbound.tool(effects={"read"}, reviewed=True)
        def pay(n: int):
            return "ran"

        start(private_plane, SERVICE)
        have_row("pay")
        time.sleep(3.0)  # many heartbeats of BOTH workers with the row and the report in place: any lease would have been handed out by now
        assert api._SHARED.quota.report() == {}  # the plane leased this worker nothing: the limit is under 10 per live worker

        assert [pay(n) for n in range(3)] == ["ran", "ran", "ran"]  # all three run (before the fix: the third was refused with two run)
        with pytest.raises(runbound.ExecutionRefused) as caught:
            pay(3)
        assert "quota" in str(caught.value).lower()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
