"""Claim renewal end to end (A2b): a deferred body longer than the plane's 60 s claim ttl is not re-offered while
its worker keeps saying so on every hello, and a claim the plane took back lands in ``claims_lost``.

These two tests each run longer than the claim ttl, so each takes a bit over a minute.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

import runbound
from runbound import api, approvals

from .test_approvals_plane import SECRET, decide, pending, row
from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

CLAIM_TTL_S = 60

WORKER = textwrap.dedent('''
    import sys, time
    import runbound
    url, key, service, secret, out, seconds, body_s = sys.argv[1:8]
    runbound.init(control_plane_url=url, token=key, service=service, control_plane_poll_s=0.2,
                  approval_secret=secret, worker_id="replica-b")

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def stamp(n: int):
        time.sleep(float(body_s))
        with open(out, "a") as f:
            f.write("B %s\\n" % n)
        return n

    time.sleep(float(seconds))
''')


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def start_a(plane, service, results):
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2,
                  approval_secret=SECRET, worker_id="replica-a", on_deferred_result=results.append)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def worker_b(plane, service, out, seconds, body_s):
    return subprocess.Popen([sys.executable, "-c", WORKER, plane.url, KEY, service, SECRET, str(out), str(seconds), str(body_s)])


def test_a_body_longer_than_the_claim_ttl_is_not_reoffered_while_the_worker_renews(private_plane, tmp_path):  # noqa: F811
    out = tmp_path / "ran.txt"
    results: list = []
    body_s = CLAIM_TTL_S + 12
    start_a(private_plane, "renewal", results)

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def stamp(n: int):
        time.sleep(body_s)
        with open(out, "a") as f:
            f.write("A %s\n" % n)
        return n

    stamp(1)
    (waiting,) = _until(lambda: pending(private_plane, "renewal"), what="the pending request")
    decide(private_plane, waiting["id"], "approve")
    _until(lambda: row(private_plane, waiting["id"])["status"] == "claimed", 20, "this worker's claim")
    assert row(private_plane, waiting["id"])["claimed_by"] == "replica-a"
    # a replica that WOULD take over a lapsed claim (the request is sealed, so any worker may) joins only now
    other = worker_b(private_plane, "renewal", out, CLAIM_TTL_S + 40, body_s)
    try:
        time.sleep(CLAIM_TTL_S + 4)  # past the ttl, the body still running
        current = row(private_plane, waiting["id"])
        assert current["status"] == "claimed" and current["claimed_by"] == "replica-a"  # renewed, never re-offered
        _until(lambda: row(private_plane, waiting["id"])["status"] == "executed", 40, "the one execution to be reported")
        time.sleep(3.0)
        assert out.read_text().splitlines() == ["A 1"]  # exactly one execution, and it was this worker's
    finally:
        other.terminate()
        other.wait(10)


def test_a_claim_the_plane_took_back_lands_in_claims_lost(private_plane, tmp_path):  # noqa: F811
    out = tmp_path / "ran.txt"
    results: list = []
    start_a(private_plane, "lost", results)
    began = {}

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def stamp(n: int):
        began["at"] = time.monotonic()
        time.sleep(CLAIM_TTL_S + 14)  # the body outlives the ttl
        with open(out, "a") as f:
            f.write("A %s\n" % n)
        return n

    # This worker "pauses": for CLAIM_TTL_S + 4 s it says no hello at all (so it renews nothing AND cannot be
    # re-offered its own lapsed claim, which it would win about half the time against the replica), then it is back.
    poller = api._SHARED._poller
    real_tick = poller._tick

    def paused_tick():
        if "at" in began and time.monotonic() - began["at"] < CLAIM_TTL_S + 4:
            return
        real_tick()

    poller._tick = paused_tick
    other = None
    try:
        stamp(2)
        (waiting,) = _until(lambda: pending(private_plane, "lost"), what="the pending request")
        decide(private_plane, waiting["id"], "approve")
        _until(lambda: "at" in began, 20, "this worker to start the body")
        assert row(private_plane, waiting["id"])["claimed_by"] == "replica-a"
        other = worker_b(private_plane, "lost", out, CLAIM_TTL_S + 30, 0.1)  # the replica that takes over a lapsed claim

        _until(lambda: approvals._LOST, CLAIM_TTL_S + 40, "claims_lost to reach this worker")
        assert waiting["id"] in approvals._LOST  # the plane said: this claim is no longer yours
        assert waiting["id"] not in approvals.executing_ids()  # and the worker stopped asking to renew it

        _until(lambda: row(private_plane, waiting["id"])["status"] == "executed", 30, "the other replica's report")
        assert "B 2" in out.read_text().splitlines()  # the lapsed claim was re-offered and executed elsewhere
    finally:
        poller._tick = real_tick
        if other is not None:
            other.terminate()
            other.wait(10)
