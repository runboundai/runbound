"""A RELEASED SDK against this plane (VPC B6b, register C4): a dry-run quota row must keep a 1.0.0a2 worker's would-refuse evidence and still never move the live counter.

1.0.0a2 TAKES from the plane on every dry-run call and learns "over the quota" from the take. If the plane answered a dry-run take as a read-only peek, the counter would
never move and that worker would record no evidence at all (found by the stack: Playwright "(c) Quota 3 per minute per service"). The worker here is the real a2 package,
unpacked from this repository's ``v1.0.0a2`` tag into a directory of its own and run in a separate process; a clone without the tag skips the module."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from .test_real_plane import KEY, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane

REPO = Path(__file__).resolve().parents[3]
LIMIT = 3

CLIENT = textwrap.dedent('''
    import json, sys, time, urllib.request
    import runbound
    from runbound import api

    url, key, service, cache = sys.argv[1:5]

    def call(method, path, body=None):
        request = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(), method=method,
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read() or b"null")

    def until(check, what):
        deadline = time.time() + 45
        while time.time() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.1)
        raise SystemExit("timed out waiting for " + what)

    runbound.init(control_plane_url=url, token=key, service=service, control_plane_poll_s=0.2, cache_dir=cache)
    until(lambda: runbound.plane_status().mode == "connected", "the first hello")

    @runbound.tool(effects={"financial"}, reviewed=True)
    def act(n: int = 0):
        return "ok"

    quota = {"limit": %d, "window": "hour", "per": "service"}
    status, body = call("POST", "/v1/admin/tool-policies", {"service": service, "tool": "act", "fail_mode": "closed", "lines": [{"decision": "allow", "limits": {"quota": quota}}]})
    assert status in (200, 201), body
    status, body = call("POST", "/v1/admin/tool-policies/%%s/act/rollout" %% service, {"state": "dry_run"})
    assert status == 200, body
    until(lambda: api._SHARED.row_for("act"), "the served dry-run row")
    time.sleep(1.5)
    tags = []
    for n in range(%d):
        assert act(n) == "ok"  # a dry run never refuses
        tags.append(runbound.tool_results()[-1]["tags"])
    print("A2RESULT " + json.dumps({"sdk": runbound.__version__, "tags": tags}))
''' % (LIMIT, LIMIT + 1))


def test_a_released_a2_worker_keeps_its_dry_run_evidence_and_the_live_counter_never_moves(private_plane, tmp_path):  # noqa: F811
    if subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "-q", "v1.0.0a2^{commit}"], capture_output=True).returncode != 0:
        pytest.skip("the v1.0.0a2 tag is not in this clone")
    site = tmp_path / "a2"
    site.mkdir()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "v1.0.0a2", "runbound-sdk/runbound"], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "--strip-components=1", "-C", str(site)], input=archive.stdout, check=True)
    service = "released-a2"
    done = subprocess.run([sys.executable, "-c", CLIENT, private_plane.url, KEY, service, str(tmp_path / "cache")], capture_output=True, text=True, timeout=120, cwd=str(tmp_path),  # not this repo: its runbound must not shadow the unpacked a2
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(site), "HOME": str(tmp_path)})
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(next(line for line in done.stdout.splitlines() if line.startswith("A2RESULT ")).split(" ", 1)[1])
    assert result["sdk"] == "1.0.0a2", "this really was the released package"
    assert result["tags"][:LIMIT] == [[]] * LIMIT and "would_refuse:quota_exceeded" in result["tags"][LIMIT], \
        "the plane's simulation counter let the released worker learn the window was full, so its evidence is recorded"

    status, answer = private_plane.request("POST", "/v1/quota/take", {"tool": "act", "service": service, "peek": True})
    assert status == 200 and answer["state"] == "dry_run" and answer["remaining"] == 0, "the dry run's own simulation counter filled up"
    enforced_status, _ = private_plane.request("POST", f"/v1/admin/tool-policies/{service}/act/rollout", {"state": "enforced", "comment": "go"})
    assert enforced_status == 200
    status, answer = private_plane.request("POST", "/v1/quota/take", {"tool": "act", "service": service, "peek": True})
    assert status == 200 and answer["state"] == "enforced" and answer["remaining"] == LIMIT, "the live window the enforced row counts was never touched by the dry run"
