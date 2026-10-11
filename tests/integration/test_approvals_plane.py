"""Approvals against a REAL control plane: sync and deferred, with the seal.

Needs a plane that has the approval routes (see test_real_plane.py for how it is named).
Run: ``pytest -m plane -rs tests/integration``.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, SERVICE_TOKEN, _until, plane  # noqa: F401  (plane is a fixture)

pytestmark = pytest.mark.plane

SECRET = "approval-secret-" + "z9" * 8


@pytest.fixture(autouse=True)
def _clean(plane):  # noqa: F811
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def start(plane, service, **config):  # noqa: F811
    results = config.pop("results", [])
    runbound.init(
        control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2,
        on_deferred_result=results.append, **config,
    )
    # S7: a consequential tool waits for the first hello (or a fresh cache) before it runs
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
    return results


def pending(plane, service):  # noqa: F811
    status, body = plane.request("GET", f"/v1/admin/approvals?status=pending&service={service}")
    assert status == 200, body
    return body["approvals"]


def decide(plane, approval_id, verdict, reason="ok"):  # noqa: F811
    status, body = plane.request("POST", f"/v1/admin/approvals/{approval_id}/{verdict}", {"reason": reason})
    assert status == 200, body
    return body


def row(plane, approval_id):  # noqa: F811
    status, body = plane.request("GET", f"/v1/admin/approvals/{approval_id}")
    assert status == 200, body
    return body


def in_thread(fn, *args):
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


# --- sync -------------------------------------------------------------------------


def test_sync_approve_runs_the_tool_and_reports_it(plane):  # noqa: F811
    start(plane, "sync-approve")
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=30)
    def refund(order: str, amount: float):
        ran.append((order, amount))
        return "refunded"

    thread, box = in_thread(refund, "o-1", 25.0)
    (waiting,) = _until(lambda: pending(plane, "sync-approve"), what="the pending request")
    assert waiting["tool"] == "refund" and waiting["args"] == {"order": "o-1", "amount": 25.0}
    assert ran == []  # the body waits for a person
    decide(plane, waiting["id"], "approve")
    thread.join(10)

    assert box["value"] == "refunded" and ran == [("o-1", 25.0)]
    _until(lambda: row(plane, waiting["id"])["status"] == "executed", what="the executed report")


def _as_console(person: str) -> dict:
    """The headers the dash backend forwards a person's decision with: their name, beside the service token."""
    return {"X-Runbound-User": person, "X-Runbound-Service-Token": SERVICE_TOKEN}


def test_a_request_made_for_a_person_cannot_be_decided_by_that_person(plane):  # noqa: F811
    # P9: session(requested_for=) rides the create; the plane answers 409 self_approval to the same person and lets anyone else decide
    start(plane, "self-approval")
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=30)
    def refund(order: str):
        ran.append(order)
        return "refunded"

    def as_ana():
        with runbound.session("run-1", requested_for="Ana@Acme.example"):
            return refund("o-9")

    thread, box = in_thread(as_ana)
    (waiting,) = _until(lambda: pending(plane, "self-approval"), what="the pending request")
    assert waiting["requested_for"] == "Ana@Acme.example"
    status, body = plane.request("POST", f"/v1/admin/approvals/{waiting['id']}/approve", {"reason": "me"}, headers=_as_console("ana@acme.example"))
    assert status == 409 and body["error"] == "self_approval" and ran == []
    status, body = plane.request("POST", f"/v1/admin/approvals/{waiting['id']}/approve", {"reason": "ok"}, headers=_as_console("bo@acme.example"))
    assert status == 200, body
    thread.join(10)
    assert box["value"] == "refunded" and ran == ["o-9"]


def test_sync_deny_refuses_with_approval_denied(plane):  # noqa: F811
    start(plane, "sync-deny")

    @runbound.tool(effects={"financial"}, approval="required", timeout_s=30)
    def wire(amount: float):
        raise AssertionError("must not run")

    thread, box = in_thread(wire, 9.0)
    (waiting,) = _until(lambda: pending(plane, "sync-deny"), what="the pending request")
    decide(plane, waiting["id"], "deny", "not today")
    thread.join(10)

    error = box["error"]
    assert isinstance(error, runbound.ExecutionRefused) and error.reason == "approval_denied"
    assert "not today" in str(error)
    assert runbound.current_session().refused_actions == 1


def test_sync_timeout_refuses_with_approval_timeout(plane):  # noqa: F811
    start(plane, "sync-timeout")

    @runbound.tool(effects={"financial"}, approval="required", timeout_s=2)
    def wire(amount: float):
        raise AssertionError("must not run")

    started = time.monotonic()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        wire(1.0)
    assert caught.value.reason == "approval_timeout" and time.monotonic() - started < 15


def test_the_request_carries_the_final_arguments_after_a_rewrite_hook(plane):  # noqa: F811
    start(plane, "sync-rewrite", pre_hooks=[
        lambda call: runbound.hooks.rewrite(kwargs={**call.kwargs, "amount": min(call.kwargs["amount"], 100.0)})
    ])

    @runbound.tool(effects={"financial"}, approval="required", timeout_s=30)
    def refund(amount: float):
        return amount

    thread, box = in_thread(lambda: refund(amount=9999.0))
    (waiting,) = _until(lambda: pending(plane, "sync-rewrite"), what="the pending request")
    assert waiting["args"] == {"amount": 100.0}  # the human sees, and approves, the capped amount
    decide(plane, waiting["id"], "approve")
    thread.join(10)
    assert box["value"] == 100.0


def test_a_plane_that_is_down_is_never_read_as_a_denial(plane):  # noqa: F811
    start(plane, "sync-down")
    shared = api._SHARED
    real = shared.approvals
    shared.approvals = lambda *a, **k: (None, None)  # every call fails at the transport

    @runbound.tool(effects={"financial"}, approval="required", timeout_s=1.5)
    def wire(amount: float):
        raise AssertionError("must not run")

    with pytest.raises(runbound.ExecutionRefused) as caught:
        wire(1.0)
    shared.approvals = real
    assert caught.value.reason == "approval_timeout"  # out of time, not "denied"


# --- deferred ----------------------------------------------------------------------


def test_deferred_returns_pending_then_a_worker_claims_and_executes(plane):  # noqa: F811
    results = start(plane, "deferred-basic", approval_secret=SECRET)
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        ran.append((ticket, runbound.current_request()))
        return {"archived": ticket}

    out = archive(7)
    assert out["status"] == "pending_approval" and out["request_id"] and ran == []
    (waiting,) = _until(lambda: pending(plane, "deferred-basic"), what="the pending request")
    assert waiting["seal"]["v"] == 1 and waiting["request_id"] == out["request_id"]
    assert SECRET not in json.dumps(waiting)  # the secret never reaches the plane
    decide(plane, waiting["id"], "approve")

    _until(lambda: results, what="on_deferred_result")
    assert results[0]["outcome"] == "executed" and results[0]["result"] == {"archived": 7}, results[0]
    assert ran == [(7, out["request_id"])]  # current_request() named it while it ran
    _until(lambda: row(plane, waiting["id"])["status"] == "executed", what="the executed report")
    assert runbound.tool_results()[0]["outcome"] == "pending_approval"


WORKER = textwrap.dedent('''
    import sys, time
    import runbound
    url, key, service, secret, out, seconds = sys.argv[1:7]
    runbound.init(control_plane_url=url, token=key, service=service, control_plane_poll_s=0.2,
                  approval_secret=secret, worker_id="replica-b")

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def stamp(n: int):
        with open(out, "a") as f:
            f.write("replica-b %s\\n" % n)
        return n

    time.sleep(float(seconds))
''')


def test_two_replicas_exactly_one_executes(plane, tmp_path):  # noqa: F811
    out = tmp_path / "executions.txt"
    worker = subprocess.Popen([sys.executable, "-c", WORKER, plane.url, KEY, "deferred-dedupe", SECRET, str(out), "9"])
    try:
        start(plane, "deferred-dedupe", approval_secret=SECRET, worker_id="replica-a")

        @runbound.tool(effects={"write"}, approval="required", mode="deferred")
        def stamp(n: int):
            with open(out, "a") as f:
                f.write("replica-a %s\n" % n)
            return n

        time.sleep(1.0)  # both replicas are heartbeating
        stamp(5)
        (waiting,) = _until(lambda: pending(plane, "deferred-dedupe"), what="the pending request")
        decide(plane, waiting["id"], "approve")
        _until(lambda: row(plane, waiting["id"])["status"] == "executed", 20, "an execution")
        time.sleep(3.0)  # a second claimant would have run by now
        lines = out.read_text().splitlines()
        assert len(lines) == 1 and lines[0].endswith(" 5"), lines
    finally:
        worker.wait(30)


def test_a_restart_between_request_and_approval_a_sealed_request_runs_on_another_worker(plane):  # noqa: F811
    start(plane, "deferred-restart", approval_secret=SECRET, worker_id="before")

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def archive(ticket: int):
        return ticket

    out = archive(11)
    (waiting,) = _until(lambda: pending(plane, "deferred-restart"), what="the pending request")
    api._teardown_for_tests()  # the requesting worker is gone

    results = start(plane, "deferred-restart", approval_secret=SECRET, worker_id="after")
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")  # noqa: F811
    def archive(ticket: int):  # noqa: F811
        ran.append(ticket)
        return ticket

    decide(plane, waiting["id"], "approve")
    _until(lambda: results, what="the other worker's execution")
    assert ran == [11] and results[0]["outcome"] == "executed" and results[0]["request_id"] == out["request_id"]


def test_a_restart_with_no_seal_an_unsealed_request_is_never_run_elsewhere(plane):  # noqa: F811
    start(plane, "deferred-unsealed", worker_id="before")

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def archive(ticket: int):
        return ticket

    archive(12)
    (waiting,) = _until(lambda: pending(plane, "deferred-unsealed"), what="the pending request")
    assert waiting["seal"] is None
    api._teardown_for_tests()

    results = start(plane, "deferred-unsealed", worker_id="after")
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")  # noqa: F811
    def archive(ticket: int):  # noqa: F811
        ran.append(ticket)

    decide(plane, waiting["id"], "approve")
    time.sleep(3.0)
    assert ran == [] and results == []  # it waits for the worker that parked it, and expires if that never returns
    assert row(plane, waiting["id"])["status"] in ("approved", "claimed", "expired")


def test_a_tampered_seal_is_refused_and_reported_tampered(plane):  # noqa: F811
    results = start(plane, "deferred-tamper", approval_secret=SECRET)
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def archive(ticket: int):
        ran.append(ticket)

    archive(13)
    (waiting,) = _until(lambda: pending(plane, "deferred-tamper"), what="the pending request")
    connection = sqlite3.connect(str(plane.db))
    connection.execute("UPDATE approvals SET args = ? WHERE id = ?", (json.dumps({"ticket": 999}), waiting["id"]))
    connection.commit()
    connection.close()
    decide(plane, waiting["id"], "approve")

    _until(lambda: results, what="the tamper result")
    assert ran == [] and results[0]["outcome"] == "tampered" and results[0]["error"] == "approval_tampered"
    _until(lambda: row(plane, waiting["id"])["status"] == "failed", what="the failed status")


def test_a_rewrite_after_the_seal_is_a_hook_error(plane):  # noqa: F811
    results = start(plane, "deferred-rewrite", approval_secret=SECRET)
    ran = []

    def always_changes(call):
        if call.args:
            return runbound.hooks.rewrite(args=(call.args[0] + 1000,))
        return runbound.hooks.rewrite(kwargs={"ticket": call.kwargs["ticket"] + 1000})

    @runbound.tool(effects={"financial"}, approval="required", mode="deferred", pre_hooks=[always_changes])
    def archive(ticket: int):
        ran.append(ticket)

    out = archive(1)  # before the seal a rewrite is fine: the request carries ticket 1001
    (waiting,) = _until(lambda: pending(plane, "deferred-rewrite"), what="the pending request")
    assert waiting["args"] == {"ticket": 1001} and out["status"] == "pending_approval"
    decide(plane, waiting["id"], "approve")

    _until(lambda: results, what="the result")
    assert ran == [] and results[0]["outcome"] == "failed"  # the rewrite after the seal refused it (hook_error)


def test_registration_rejects_a_deferred_tool_with_arguments_that_are_not_json():
    with pytest.raises(ValueError, match="deferred"):

        @runbound.tool(approval="required", mode="deferred")
        def schedule(when: datetime.datetime): ...

    with pytest.raises(ValueError):

        @runbound.tool(approval="required", mode="deferred")
        def spread(*items): ...

    @runbound.tool(approval="required", mode="sync")  # a sync tool may take anything
    def fine(when: datetime.datetime): ...
