"""The goodbye (DR-F12, ``.team/mvp-goodbye-contract.md``): a worker that stops cleanly tells the plane, so the console shows it as stopped and not as disconnected.

A real ``PlaneClient`` talks to a local HTTP stand-in for the plane. The plane's own side (the same-key check, the audit, the read fields) is tested in the plane."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import runbound
from runbound import api
from runbound.config import RunboundConfig


class Plane:
    """Answers hello (so the SDK is connected), events and traces with 200, and goodbye as scripted: ``ok`` | ``404`` | ``403`` | ``hang``."""

    def __init__(self, goodbye="ok", hello_status=200):
        self.goodbye, self.hello_status = goodbye, hello_status
        self.requests: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                outer.requests.append((self.path, body, time.monotonic()))
                status, reply = 200, {}
                if self.path == "/v1/hello":
                    status = outer.hello_status
                elif self.path == "/v1/goodbye":
                    if outer.goodbye == "404":
                        status, reply = 404, {"detail": "not found"}
                    elif outer.goodbye == "403":
                        status, reply = 403, {"error": "goodbye_key_mismatch"}
                    elif outer.goodbye == "hang":
                        time.sleep(15)
                    else:
                        reply = {"recorded": True, "worker_id": body.get("worker_id"), "stopped_at": "2026-10-21T00:00:00Z"}
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return None

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self) -> list:
        return [p for p, _, _ in self.requests]

    def goodbyes(self) -> list:
        return [b for p, b, _ in self.requests if p == "/v1/goodbye"]

    def wait_for(self, path, seconds=10.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if path in self.paths():
                return True
            time.sleep(0.05)
        return False

    def close(self):
        self.server.shutdown()


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane():
    p = Plane()
    yield p
    p.close()


def connect(plane, **extra):
    runbound.init(token="rb_test_key", control_plane_url=plane.url, service="cron", worker_id="cron-worker-1", control_plane_poll_s=0.2, **extra)
    assert plane.wait_for("/v1/hello"), "the worker said hello"
    for _ in range(100):  # the answered hello has been applied
        if runbound.plane_status().mode == "connected":
            break
        time.sleep(0.05)


def test_shutdown_says_goodbye_once_with_its_own_ids_after_the_queue_and_the_heartbeat_stopped(plane):
    connect(plane)
    runbound.shutdown()
    (body,) = plane.goodbyes()
    assert (body["service"], body["worker_id"], body["reason"]) == ("cron", "cron-worker-1", "shutdown") and body["boot_id"] and body["sdk_version"]
    hello_boot = next(b["boot_id"] for p, b, _ in plane.requests if p == "/v1/hello")
    assert body["boot_id"] == hello_boot, "the goodbye names the boot the plane saw in the hello"
    said = max(t for p, _, t in plane.requests if p == "/v1/goodbye")
    time.sleep(0.7)  # three poll intervals
    assert not [1 for p, _, t in plane.requests if p == "/v1/hello" and t > said], "no hello of this boot can follow the goodbye and bring the worker back"
    runbound.shutdown()
    assert len(plane.goodbyes()) == 1, "once per process"


def test_a_re_init_says_no_goodbye(plane):
    connect(plane)
    runbound.init(token="rb_test_key", control_plane_url=plane.url, service="cron", worker_id="cron-worker-1", control_plane_poll_s=0.2)
    time.sleep(0.5)
    assert plane.goodbyes() == [], "stop() of the replaced state is not a goodbye"


def test_a_process_that_never_completed_a_hello_has_nothing_to_stop():
    p = Plane(hello_status=503)
    try:
        runbound.init(token="rb_test_key", control_plane_url=p.url, service="cron", worker_id="w", control_plane_poll_s=0.2)
        assert p.wait_for("/v1/hello")
        runbound.shutdown()
        assert p.goodbyes() == []
    finally:
        p.close()


@pytest.mark.parametrize("answer", ["404", "403"])
def test_an_old_plane_or_a_refusal_is_swallowed_and_never_raises(answer):
    p = Plane(goodbye=answer)
    try:
        connect(p)
        runbound.shutdown()
        assert len(p.goodbyes()) == 1, "tried once, no retry"
    finally:
        p.close()


def test_a_plane_that_never_answers_cannot_hold_shutdown_past_the_bound():
    p = Plane(goodbye="hang")
    try:
        connect(p)
        began = time.monotonic()
        runbound.shutdown(timeout=60)
        assert time.monotonic() - began < 5.5
    finally:
        p.close()


def test_goodbye_on_exit_false_registers_no_exit_hook_and_the_default_does(plane):
    connect(plane, goodbye_on_exit=False)
    assert api._SHARED._exit_hook is None
    api._teardown_for_tests()
    connect(plane)
    assert api._SHARED._exit_hook is not None
    api._SHARED.stop()
    assert api._SHARED._exit_hook is None, "a stopped state leaves no exit hook behind"


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_goodbye_on_exit_must_be_a_boolean(bad):
    with pytest.raises(ValueError, match="goodbye_on_exit"):
        RunboundConfig(token="t", goodbye_on_exit=bad).validate()


CHILD = textwrap.dedent('''
    import os, signal, sys, time
    import runbound
    mode, url = sys.argv[1], sys.argv[2]
    runbound.init(token="rb_test_key", control_plane_url=url, service="cron", worker_id="cron-" + mode, control_plane_poll_s=0.2,
                  goodbye_on_exit=(mode != "noexit"))
    for _ in range(100):
        if runbound.plane_status().mode == "connected":
            break
        time.sleep(0.05)
    print("READY", flush=True)
    if mode in ("exit", "noexit"):
        sys.exit(0)             # a job that simply finishes
    time.sleep(60)              # killed by the parent
''')


def run_child(plane, mode, how=None, tmp_path=None):
    script = tmp_path / f"child_{mode}.py"
    script.write_text(CHILD)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)}
    proc = subprocess.Popen([sys.executable, str(script), mode, plane.url], stdout=subprocess.PIPE, text=True, env=env)
    assert proc.stdout.readline().strip() == "READY"
    if how:
        proc.send_signal(how)
    proc.wait(20)
    return proc


def test_a_job_that_exits_normally_says_goodbye_with_no_code_of_its_own(plane, tmp_path):
    run_child(plane, "exit", tmp_path=tmp_path)
    (body,) = plane.goodbyes()
    assert (body["worker_id"], body["reason"]) == ("cron-exit", "exit")


def test_a_crash_or_the_default_sigterm_or_the_opt_out_says_nothing_and_stays_disconnected(plane, tmp_path):
    run_child(plane, "killed", signal.SIGKILL, tmp_path)
    run_child(plane, "termed", signal.SIGTERM, tmp_path)  # the default action skips the exit hooks: the documented recipe is runbound.shutdown() in your handler
    run_child(plane, "noexit", tmp_path=tmp_path)
    assert plane.goodbyes() == []
