"""The three examples against a REAL control plane, each run as its own process.

What a person does in the console is done here with the admin API: a row for example 1 before it
starts, the approval of example 2's pending request while it waits. Each example must exit 0 and
print the outcome its docstring documents. ``langchain_otel.py`` needs the ``langchain`` and
``otel`` extras; ``RUNBOUND_EXAMPLES_PYTHON`` names an interpreter that has them (else this one is
used, and the test skips when they are missing).

    RUNBOUND_PLANE_PYTHON=... RUNBOUND_PLANE_DIR=... RUNBOUND_PLANE_APP=... \\
        RUNBOUND_EXAMPLES_PYTHON=... pytest -m plane tests/integration/test_examples_plane.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_approvals_plane import decide, pending
from .test_real_plane import KEY, _until, plane  # noqa: F401  (plane is a fixture)
from .test_rows_quota_plane import put_row

pytestmark = pytest.mark.plane

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SECRET = "examples-approval-secret-" + "k7" * 8
_TIMEOUT_S = 180


def _python(extras: bool = False) -> str:
    return (os.environ.get("RUNBOUND_EXAMPLES_PYTHON") or sys.executable) if extras else sys.executable


def _env(plane, tmp_path: Path, **extra: str) -> dict:  # noqa: F811
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RUNBOUND_", "OTEL_"))}
    env.update(RUNBOUND_PLANE_URL=plane.url, RUNBOUND_TOKEN=KEY, RUNBOUND_CACHE_DIR=str(tmp_path / "cache"),
               PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])), **extra)
    return env


def _start(stem: str, env: dict, python: str | None = None) -> subprocess.Popen:
    return subprocess.Popen([python or sys.executable, str(EXAMPLES / f"{stem}.py")], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _finish(proc: subprocess.Popen) -> tuple[str, str]:
    try:
        out, err = proc.communicate(timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise AssertionError(f"the example did not finish\n--- stdout ---\n{out}\n--- stderr ---\n{err}")
    assert proc.returncode == 0, f"exit {proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
    return out, err


def test_tool_rules_a_console_row_tightens_the_code_rules(plane, tmp_path):  # noqa: F811
    put_row(plane, "credits", "issue_credit", max_calls_per_session=1)
    out, err = _finish(_start("tool_rules", _env(plane, tmp_path)))
    lines = out.splitlines()
    assert "plane: connected" in lines, out + err
    calls = [line.split()[:2] for line in lines if line.startswith(("ok", "refused"))]
    # ok; the code's constraint; then the row's cap of 1 (the code alone allows 3) for every call after the first
    assert calls == [["ok", "{'customer':"], ["refused", "policy"], ["refused", "session_max_calls"],
                     ["refused", "session_max_calls"], ["refused", "session_max_calls"]], out + err
    assert "failed its constraint" in lines[2], out


def test_deferred_approval_runs_once_a_person_approves(plane, tmp_path):  # noqa: F811
    proc = _start("deferred_approval", _env(plane, tmp_path, RUNBOUND_APPROVAL_SECRET=SECRET))
    try:
        (waiting,) = _until(lambda: pending(plane, "refunds"), 60, "the example's pending request")
        assert waiting["tool"] == "refund" and waiting["seal"]["v"] == 1, waiting  # sealed: any replica may run it
        assert SECRET not in str(waiting)
        decide(plane, waiting["id"], "approve")
        out, err = _finish(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    request_id = waiting["request_id"]
    lines = out.splitlines()
    assert f"pending {request_id}" in lines, out + err
    assert f"running for request {request_id}" in lines, out + err  # current_request() named it
    assert f"deferred {request_id} executed {{'order_id': 'A-1001', 'refunded': 40.0}}" in lines, out + err
    assert lines.index(f"pending {request_id}") < lines.index(f"running for request {request_id}")


def test_langchain_otel_runs_connected(plane, tmp_path):  # noqa: F811
    python = _python(extras=True)
    probe = subprocess.run([python, "-c", "import langchain_core, opentelemetry.sdk"], capture_output=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"{python} lacks langchain-core / opentelemetry-sdk (set RUNBOUND_EXAMPLES_PYTHON)")
    # A LangChain tool declares no effects, so a new service's plane defaults ASK before it runs (requirement default:unclassified; a console row can
    # raise how a tool is judged, never relax it). The example blocks on that approval, as in production: a person approves it, here with the admin API.
    proc = _start("langchain_otel", _env(plane, tmp_path), python)
    try:
        (waiting,) = _until(lambda: pending(plane, "orders"), 60, "the example's pending approval for lookup_order")
        assert waiting["tool"] == "lookup_order" and waiting["requirement"] == "default:unclassified", waiting
        decide(plane, waiting["id"], "approve")
        out, err = _finish(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    lines = out.splitlines()
    assert lines == ["order A-7: shipped", "refused tool_blocked", "span execute_tool lookup_order ok",
                     "span execute_tool delete_order refused"], out + err
