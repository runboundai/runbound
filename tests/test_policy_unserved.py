"""A worker the plane has told "a policy exists" but that holds none is not governed yet (HC12; Apollo's check (a)).

Over real HTTP (the SDK's own ``PlaneClient`` and ``Poller``) against a stdlib plane that answers every heartbeat and
answers ``GET /v1/policy`` with the locked failure shape ``503 {"error": "policy_rows_unavailable", "retryable": true}``.
With no policy and no cache the worker lands in the fail-closed state for the classes that fail closed, and never in
"allow everything"; once the plane answers, it is governed. The same 503 for a worker that already holds rows changes
nothing it holds.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import runbound
from runbound import api


class Plane:
    def __init__(self):
        self.announce = 4
        self.policy_status = 503
        self.rows = []
        self.rows_hash = "h1"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.startswith("/v1/hello"):
                    self._send(200, {"policy_version": outer.announce, "poll_s": 0.2, "halt": False, "fleet_state": "ok"})
                else:
                    self._send(200, {})

            def do_GET(self):  # noqa: N802
                if not self.path.startswith("/v1/policy"):
                    return self._send(404, {})
                if outer.policy_status == 503:
                    return self._send(503, {"error": "policy_rows_unavailable", "retryable": True})
                self._send(200, {"version": outer.announce, "policy": None, "rows": outer.rows, "rows_hash": outer.rows_hash})

            def log_message(self, *args):
                return None

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def plane():
    p = Plane()
    yield p
    p.server.shutdown()


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def until(fn, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def start(plane, **config):
    runbound.init(control_plane_url=plane.url, token="k", service="svc", control_plane_poll_s=0.2, **config)
    until(lambda: runbound.plane_status().mode == "connected", "the first hello")


def outcome(name, effects):
    def body():
        return "ran"

    body.__name__ = name
    tool = runbound.tool(effects=set(effects) or None)(body)
    try:
        tool()
        return "ran"
    except runbound.ExecutionRefused as refusal:
        return f"refused:{refusal.reason}"


def test_no_policy_and_no_cache_with_a_503_is_fail_closed_for_the_closed_classes_and_never_allow_everything(plane):
    start(plane)
    time.sleep(0.6)  # several polls: every one fetches again and gets the 503
    assert api._ENGINE.shared.row_for("wire") is None
    assert outcome("fin", ["financial"]) == "refused:plane_unavailable"
    assert outcome("dest", ["destructive"]) == "refused:plane_unavailable"
    assert outcome("priv", ["privileged"]) == "refused:plane_unavailable"
    assert outcome("uncl", []) == "refused:plane_unavailable"
    assert outcome("rd", ["read"]) == "ran"  # the classes that fail open still do, per their fail mode


def test_the_worker_is_governed_as_soon_as_the_plane_answers(plane):
    start(plane)
    time.sleep(0.5)
    assert outcome("fin", ["financial"]) == "refused:plane_unavailable"
    plane.policy_status = 200
    plane.rows = [{"tool": "wire", "state": "enforced", "blocked": True}]
    until(lambda: api._ENGINE.shared.row_for("wire") is not None, "the rows to be served")
    assert outcome("fin2", ["financial"]) == "ran"
    assert outcome("wire", ["read"]) == "refused:tool_blocked"


def test_a_plane_that_announces_no_policy_and_serves_an_empty_one_does_not_gate_anything(plane):
    plane.announce, plane.policy_status = 0, 200  # version 0 and an empty policy read: the service has nothing, and the worker has read that
    start(plane)
    time.sleep(0.5)
    assert outcome("fin", ["financial"]) == "ran"


def test_a_plane_that_announces_version_zero_but_cannot_serve_the_policy_gates_the_classes_that_fail_closed(plane):
    """Version 0 is not "no policy": a new service's standard defaults are served by the read (the defaults window). A read that fails leaves the worker holding none."""
    plane.announce = 0  # policy_status stays 503
    start(plane)
    time.sleep(0.5)
    assert outcome("fin", ["financial"]) == "refused:plane_unavailable"
    plane.policy_status = 200  # the plane recovers: the next hello reads the policy
    until(lambda: outcome("fin2", ["financial"]) == "ran", "the policy to be read")


def test_a_worker_that_already_holds_rows_keeps_them_through_the_503(plane):
    plane.policy_status = 200
    plane.rows = [{"tool": "wire", "state": "enforced", "blocked": True}]
    start(plane)
    until(lambda: api._ENGINE.shared.row_for("wire") is not None, "the rows to be served")
    plane.announce, plane.policy_status = 5, 503  # a new version is announced, then the rows cannot be read
    time.sleep(0.8)
    assert outcome("wire", ["read"]) == "refused:tool_blocked"  # what it holds still governs
    assert outcome("fin", ["financial"]) == "ran"  # and it is not gated: it holds a policy
