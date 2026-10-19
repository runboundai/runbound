"""The SDK talks to the CURRENT control plane: a real plane process, not a fake.

init -> hello -> policy fetch -> tool report (hash ack) -> a refused tool's /v1/events.

The plane is a control plane checkout, started as ``uvicorn`` in a subprocess on a
throwaway SQLite file and an in-process live store (never a shared database). It
is named by three environment variables, so nothing about where it lives is
written here: ``RUNBOUND_PLANE_PYTHON`` (an interpreter that can import the
plane's app; the plane pins the published SDK, so it lives in its own
environment, not the one under test), ``RUNBOUND_PLANE_DIR`` (its working
directory) and ``RUNBOUND_PLANE_APP`` (its ``module:factory``). Any unset, or
unusable, and the whole module skips: the SDK suite never needs a plane.

    RUNBOUND_PLANE_PYTHON=... RUNBOUND_PLANE_DIR=... RUNBOUND_PLANE_APP=... \
        pytest -m plane tests/integration
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import runbound
from runbound import api

pytestmark = pytest.mark.plane

KEY = "rb_it_" + "a1b2" * 8


def _plane_target() -> tuple[str, str, str]:
    python, directory, app = (os.environ.get(name) for name in
                              ("RUNBOUND_PLANE_PYTHON", "RUNBOUND_PLANE_DIR", "RUNBOUND_PLANE_APP"))
    if not (python and directory and app):
        pytest.skip("RUNBOUND_PLANE_PYTHON, RUNBOUND_PLANE_DIR and RUNBOUND_PLANE_APP are not all set")
    if not Path(directory).is_dir():
        pytest.skip(f"{directory} is not a directory")
    module = app.split(":", 1)[0]
    probe = subprocess.run([python, "-c", f"import {module}, uvicorn"], capture_output=True, timeout=60,
                           cwd=directory)
    if probe.returncode != 0:
        pytest.skip(f"RUNBOUND_PLANE_PYTHON cannot import {module}")
    return python, directory, app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: No wait in the real-plane tests is shorter than this: they share a machine with other suites and containers,
#: and a wait that is generous on an idle laptop is flaky under load (the claim tests also outlive a 60 s ttl).
LOAD_FLOOR_S = 45.0


def wait_scale() -> float:
    """``RUNBOUND_TEST_WAIT_SCALE`` (default 1, clamped to 1..3): multiplies every wait of the real-plane tests on a host so loaded that the floor above is not enough (a gate run at load 25-78)."""
    try:
        return min(3.0, max(1.0, float(os.environ.get("RUNBOUND_TEST_WAIT_SCALE", "1"))))
    except ValueError:
        return 1.0


def _until(check, timeout=15.0, what="condition"):
    """Poll ``check`` until it returns something truthy; ``timeout`` is a hint, never less than ``LOAD_FLOOR_S``, and both are multiplied by :func:`wait_scale`."""
    deadline = time.monotonic() + max(timeout, LOAD_FLOOR_S) * wait_scale()
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Plane:
    def __init__(self, url: str, db: Path | None = None) -> None:
        self.url = url
        self.db = db
        self.proc = None

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def request(self, method: str, path: str, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


#: The service token this test plane shares with "the dash backend": a decision forwarded with it may name its person.
SERVICE_TOKEN = "s" * 64


@contextlib.contextmanager
def launch(directory, app=None, extra_pythonpath=None, extra_env=None):
    """A plane process on a throwaway SQLite file under ``directory``; killed on exit.

    ``app`` (``module:factory``) replaces the plane's own factory, with ``extra_pythonpath`` put in front of the plane
    directory so a test can wrap the real app (see ``test_policy_unserved_plane.py``); ``extra_env`` adds to the environment.
    """
    python, plane_dir, plane_app = _plane_target()
    plane_app = app or plane_app
    port = _free_port()
    db = directory / "plane.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db}",
        "REDIS_URL": "",
        "BOOTSTRAP_API_KEY": KEY,
        "PLANE_MODE": "self_hosted",
        "PLANE_SECRET_KEY": "0" * 64,
        # The dash backend's secret: a decision names its person only beside it (the plane's H4 rule).
        "PLANE_SERVICE_TOKEN": SERVICE_TOKEN,
        "POLL_S": "1",
        "LOG_LEVEL": "WARNING",
        "MAINTENANCE_INTERVAL_S": "0",
        "PYTHONPATH": os.pathsep.join([str(extra_pythonpath), plane_dir]) if extra_pythonpath else plane_dir,
        **(extra_env or {}),
    }
    log = directory / "plane.log"
    with open(log, "w") as out:
        proc = subprocess.Popen(
            [python, "-m", "uvicorn", plane_app, "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=plane_dir, env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        handle = Plane(url, db)
        handle.proc = proc
        try:
            def healthy():
                if proc.poll() is not None:
                    raise AssertionError("the plane exited:\n" + log.read_text())
                try:
                    with urllib.request.urlopen(url + "/healthz", timeout=1):
                        return True
                except Exception:
                    return False

            _until(healthy, 30, "the plane's /healthz")
            yield handle
        finally:
            handle.kill()


@pytest.fixture(scope="module")
def plane(tmp_path_factory):
    with launch(tmp_path_factory.mktemp("plane")) as handle:
        yield handle


@pytest.fixture
def private_plane(tmp_path):
    """A plane of this test's own, so it can be killed."""
    with launch(tmp_path) as handle:
        yield handle


@pytest.fixture
def sdk(plane):
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_the_new_sdk_talks_to_the_current_plane(plane, sdk):
    # 1. init: a plane link starts, with a short heartbeat so the test is quick.
    runbound.init(control_plane_url=plane.url, token=KEY, service="it-service", control_plane_poll_s=0.2)

    @runbound.tool(effects={"read"}, reviewed=True)
    def lookup(order_id): ...

    @runbound.tool(effects={"destructive"}, blocked=True)
    def wipe_database(name): ...

    shared = api._SHARED

    # 2. hello: the plane answers the heartbeat; the link is connected.
    _until(lambda: runbound.plane_status().mode == "connected", what="a hello reply")

    # 3. tool report: the report rides the heartbeat, the plane acknowledges its hash,
    #    and the plane's own inventory lists both tools.
    _until(lambda: shared._tools_sent_hash is not None, what="the tool report's hash ack")
    def inventory():
        status, body = plane.request("GET", "/v1/admin/tools?service=it-service")
        rows = body.get("tools", body) if isinstance(body, dict) else body
        names = {row.get("tool") for row in rows or [] if isinstance(row, dict)}
        return status == 200 and {"lookup", "wipe_database"} <= names
    _until(inventory, what="the plane's tool inventory")

    gone = plane.request("POST", "/v1/admin/policies", {"service": "it-service", "body": {"deny": ["x"]}, "state": "enforced"})
    assert gone[0] == 410 and gone[1]["error"] == "policies_removed"  # H6: the organisation document is retired
    # 4. policy fetch: roll out a console row for a tool; the next heartbeat announces its version and
    #    the worker fetches it and applies it. (H6: the organisation document is no longer served: 410.)
    status, created = plane.request("POST", "/v1/admin/tool-policies", {"service": "it-service", "tool": "send_email", "blocked": True})
    assert status in (200, 201), created
    status, rolled = plane.request("POST", "/v1/admin/tool-policies/it-service/send_email/rollout", {"state": "enforced", "comment": "test"})
    assert status == 200, rolled
    _until(lambda: shared.row_for("send_email"), what="the row for send_email")

    @runbound.tool(effects={"read"}, reviewed=True)
    def send_email(to): ...

    with pytest.raises(runbound.ExecutionRefused) as blocked:
        send_email("a@example.com")  # refused by the console row the plane served
    assert blocked.value.reason == "tool_blocked"

    # 5. a refused tool reaches the plane as /v1/events.
    with pytest.raises(runbound.ExecutionRefused):
        wipe_database("prod")

    @runbound.tool(effects={"read"}, reviewed=True)
    def count_rows(limit: int): ...

    with pytest.raises(runbound.ExecutionRefused):
        count_rows("many")  # invalid arguments: a new refusal kind the plane has never seen
    shared._exporter.flush(5.0)

    def refused_on_the_plane():
        status, body = plane.request("GET", "/v1/admin/anomalies?service=it-service")
        text = json.dumps(body)
        return status == 200 and "wipe_database" in text and "count_rows" in text
    _until(refused_on_the_plane, what="the refusal on the plane")
    assert shared._exporter.pending == 0


@pytest.mark.parametrize("value,expected", [(None, 1.0), ("1", 1.0), ("2.5", 2.5), ("3", 3.0), ("9", 3.0), ("0.1", 1.0), ("fast", 1.0)])
def test_the_wait_scale_is_read_from_the_environment_and_clamped_to_one_to_three(monkeypatch, value, expected):
    monkeypatch.delenv("RUNBOUND_TEST_WAIT_SCALE", raising=False)
    if value is not None:
        monkeypatch.setenv("RUNBOUND_TEST_WAIT_SCALE", value)
    assert wait_scale() == expected
