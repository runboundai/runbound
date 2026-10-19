"""PS-D2-2 against a REAL plane that has D2-1 (the by-request lookup, the bind and withdraw routes). PREPARED BEFORE D2-1 LANDED: every test skips on a plane without the routes
(``require_d2``), and none of it has run against a plane that has them, so treat the first run on a D2-1 plane as the first run.

What it proves when it runs: ``approval_bind=True`` against a D2-1 plane files a bind-required request and the worker binds it before the body (the plane records the attempt); a
second attempt cannot bind it (``prior_outcome_unknown``) and ``resolve_replay`` reads the recorded state; ``withdraw`` settles a pending request; and an SDK that does NOT ask for
binding (a4/a3, or ``approval_bind=False``) gets a legacy request: no bind call, the body runs as it always did.
"""

from __future__ import annotations

import threading
import uuid

import pytest

import runbound
from runbound import api, approvals

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

SECRET = "d2-secret-" + "q7" * 8


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def require_d2(plane):
    """Skip unless the plane has the D2 routes: the bind route answers a domain error for an unknown id rather than the router's generic 404."""
    status, body = plane.request("GET", "/v1/approvals/by-request/does-not-exist", headers={"X-Runbound-Service": "d2-probe"})
    if not (status == 200 and isinstance(body, dict) and body.get("exists") is False):
        pytest.skip("this plane predates D2-1 (no by-request lookup)")


def start(plane, service, **config):
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2, approval_secret=SECRET, **config)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def pending(plane, service):
    status, body = plane.request("GET", f"/v1/admin/approvals?status=pending&service={service}")
    assert status == 200, body
    return body["approvals"]


def approval_row(plane, approval_id):
    status, body = plane.request("GET", f"/v1/admin/approvals/{approval_id}")
    assert status == 200, body
    return body


def call_in_thread(fn, *args):
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_a_bind_required_sync_request_is_bound_before_its_body_and_a_second_attempt_cannot_run_it(private_plane):  # noqa: F811
    require_d2(private_plane)
    service = "d2-real-bind"
    start(private_plane, service, approval_bind=True)
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=60, reviewed=True)
    def wire(amount: float):
        ran.append(amount)
        return "sent"

    thread, box = call_in_thread(wire, 5.0)
    (waiting,) = _until(lambda: pending(private_plane, service), what="the pending request")
    assert waiting.get("bind_required") is True, "approval_bind=True files a bind-required request on a D2-1 plane"
    status, body = private_plane.request("POST", f"/v1/admin/approvals/{waiting['id']}/approve", {"reason": "ok"})
    assert status == 200, body
    thread.join(20)
    assert box.get("value") == "sent" and ran == [5.0], box
    row = approval_row(private_plane, waiting["id"])
    assert row.get("bound_attempt_id"), "the plane recorded which attempt ran it"

    # a second attempt (a replay, another worker) tries to bind the same request: the plane says it is bound elsewhere
    status, answer = private_plane.request("POST", f"/v1/approvals/{waiting['id']}/bind",
                                           {"attempt_id": uuid.uuid4().hex, "action_id": uuid.uuid4().hex, "args_digest": waiting.get("args_digest")},
                                           headers={"X-Runbound-Service": service})
    assert status == 409, answer
    # and the lookup reads the recorded state: not a first run, nothing to dispatch
    got = approvals.resolve_replay(api._SHARED, waiting["request_id"] if "request_id" in waiting else row.get("request_id"))
    assert got.action == "no_dispatch", got


def test_a_pending_request_can_be_withdrawn_and_the_lookup_then_refuses_it(private_plane):  # noqa: F811
    require_d2(private_plane)
    service = "d2-real-withdraw"
    start(private_plane, service, approval_bind=True)
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=60, reviewed=True)
    def wire(amount: float):
        ran.append(amount)
        return "sent"

    thread, box = call_in_thread(wire, 5.0)
    (waiting,) = _until(lambda: pending(private_plane, service), what="the pending request")
    request_id = waiting.get("request_id") or approval_row(private_plane, waiting["id"]).get("request_id")
    got = approvals.withdraw(api._SHARED, request_id, "the agent gave up")
    assert got.status == "withdrawn", got
    assert approval_row(private_plane, waiting["id"])["status"] == "withdrawn"
    assert approvals.resolve_replay(api._SHARED, request_id).action == "refuse"
    status, _ = private_plane.request("POST", f"/v1/admin/approvals/{waiting['id']}/approve", {"reason": "too late"})
    assert status != 200, "a withdrawn request cannot be approved"
    assert ran == []


def test_an_sdk_that_does_not_ask_for_binding_gets_a_legacy_request_and_runs_as_always(private_plane):  # noqa: F811
    require_d2(private_plane)
    service = "d2-real-legacy"
    start(private_plane, service)  # approval_bind=False: what 1.0.0a3/a4 are
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=60, reviewed=True)
    def wire(amount: float):
        ran.append(amount)
        return "sent"

    paths = []
    real = api._SHARED.approvals
    api._SHARED.approvals = lambda method, path, body=None, timeout=5.0: (paths.append(path), real(method, path, body, timeout))[1]
    thread, box = call_in_thread(wire, 5.0)
    (waiting,) = _until(lambda: pending(private_plane, service), what="the pending request")
    assert not waiting.get("bind_required"), "a request filed without the capability is a legacy request"
    status, body = private_plane.request("POST", f"/v1/admin/approvals/{waiting['id']}/approve", {"reason": "ok"})
    assert status == 200, body
    thread.join(20)
    assert box.get("value") == "sent" and ran == [5.0]
    assert not [p for p in paths if p.endswith("/bind")], "no bind is attempted for a legacy request"
