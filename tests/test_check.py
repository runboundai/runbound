"""``python -m runbound check``: what is guarded in this process, in one command (SDK-2).

The design: ``runbound.check()`` reports on the process it is called in (call it at the end of your setup), and the
CLI form ``python -m runbound check TARGET`` imports the target script or module first, under the name
``runbound_check`` (so an ``if __name__ == "__main__"`` block does not start a server), then reports. Exit 0 when
at least one guarded call, tool or wrapped client exists, 1 otherwise, 2 when the target cannot be loaded.
"""

from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import runbound
from runbound import _check, api

CANARY = "canary-" + "token-9f8e7d6c-secret"
SDK = str(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def script(tmp_path, body: str, name: str = "agent.py") -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return str(path)


GUARDED = """
    import runbound
    runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")

    @runbound.tool(effects={"financial"})
    def issue_refund(user, amount):
        return "refunded"

    @runbound.tool(effects={"read"})
    def lookup_order(order):
        return "o-1"

    runbound.record_call("gpt-4o", 100, 50)
"""
NOTHING = "x = 1\n"


def run_cli(args, **kw):
    return subprocess.run([sys.executable, "-m", "runbound", "check", *args], capture_output=True, text=True,
                          cwd=SDK, timeout=60, **kw)


# --- exit codes -----------------------------------------------------------------------------------------------


def test_exit_0_when_the_target_guards_a_call_or_a_tool(tmp_path):
    result = run_cli([script(tmp_path, GUARDED)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_exit_1_when_the_target_guards_nothing(tmp_path):
    result = run_cli([script(tmp_path, NOTHING)])
    assert result.returncode == 1
    assert "nothing is guarded" in result.stdout.lower()


def test_exit_1_with_no_target_because_nothing_ran():
    assert run_cli([]).returncode == 1


def test_exit_2_when_the_target_cannot_be_loaded(tmp_path):
    result = run_cli([str(tmp_path / "missing.py")])
    assert result.returncode == 2 and "missing.py" in result.stderr


def test_exit_2_when_the_target_raises(tmp_path):
    result = run_cli([script(tmp_path, "raise RuntimeError('boom')\n")])
    assert result.returncode == 2 and "boom" in result.stderr


def test_a_tool_alone_is_guarded(tmp_path):
    body = """
        import runbound
        @runbound.tool(effects={"write"})
        def book(slot): ...
    """
    assert run_cli([script(tmp_path, body)]).returncode == 0


def test_a_module_name_is_a_target_too(tmp_path):
    (tmp_path / "my_agent.py").write_text(textwrap.dedent(GUARDED))
    result = subprocess.run([sys.executable, "-m", "runbound", "check", "my_agent"], capture_output=True, text=True,
                            cwd=str(tmp_path), env={"PYTHONPATH": f"{SDK}:{tmp_path}", "PATH": ""}, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_main_guarded_code_is_not_run_so_a_server_loop_never_starts(tmp_path):
    body = GUARDED + """
    if __name__ == "__main__":
        raise SystemExit("this would start the server")
    """
    assert run_cli([script(tmp_path, body)]).returncode == 0


# --- what it says -----------------------------------------------------------------------------------------------


def test_the_text_report_names_clients_tools_with_classes_plane_posture_budgets_and_events(tmp_path):
    out = run_cli([script(tmp_path, GUARDED)]).stdout
    for needle in ("issue_refund", "financial", "lookup_order", "read", "posture", "full", "budget", "$5", "max_steps",
                   "local", "guarded calls", "Last events"):
        assert needle.lower() in out.lower(), (needle, out)


def test_json_has_a_stable_documented_shape(tmp_path):
    report = json.loads(run_cli(["--json", script(tmp_path, GUARDED)]).stdout)
    assert report["schema"] == 1
    assert set(report) == {"schema", "runbound", "python", "guarded", "initialized", "clients", "tools", "plane", "posture",
                           "budgets", "guarded_calls", "tool_calls_seen", "events"}
    assert report["guarded"] is True and report["posture"] == "full" and report["guarded_calls"] == 1
    assert {t["name"]: t["classes"] for t in report["tools"]} == {"issue_refund": ["financial"], "lookup_order": ["read"]}
    assert set(report["clients"]) == {"wrapped_clients", "auto_wrapped", "providers_imported", "providers_unguarded"}
    assert set(report["plane"]) == {"mode", "url", "detail"} and report["plane"]["mode"] == "local"
    assert report["budgets"] == {"budget_usd": 5.0, "max_steps": 50}
    assert isinstance(report["events"], list)


def test_the_last_ten_events_each_with_kind_time_and_a_short_line(tmp_path):
    body = """
        import runbound
        runbound.init(on_anomaly="raise", budget_usd=0.01)
        for _ in range(12):
            try:
                runbound.record_call("gpt-4o", 1_000_000, 1_000_000)
            except runbound.ExecutionRefused:
                pass
    """
    report = json.loads(run_cli(["--json", script(tmp_path, body)]).stdout)
    assert 0 < len(report["events"]) <= 10
    for event in report["events"]:
        assert set(event) == {"kind", "ts", "line"} and event["kind"] and event["line"] and isinstance(event["ts"], (int, float))
    assert any("budget" in e["line"].lower() for e in report["events"])


def test_a_process_with_no_init_says_what_to_do(tmp_path):
    out = run_cli([script(tmp_path, NOTHING)]).stdout
    assert "runbound.init(" in out


# --- no network without a token; never a key --------------------------------------------------------------------


def test_no_network_is_touched_without_a_token(tmp_path, monkeypatch):
    """A socket is blocked for the whole run: loading a target that inits with no token and reporting on it makes no connection."""
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append(args)
        raise OSError("no network in this test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    out = io.StringIO()
    code = _check.main([script(tmp_path, GUARDED)], stdout=out, stderr=io.StringIO())
    assert code == 0 and attempts == []
    assert "local" in out.getvalue().lower()


def test_the_report_itself_never_calls_the_plane(monkeypatch):
    """Even in a process connected to a plane, building the report only reads status: it never asks the plane."""
    runbound.init(on_anomaly="raise")
    calls = []
    monkeypatch.setattr(api._SHARED, "pending_events", lambda: calls.append("pending") or 0, raising=False)
    report = _check.build_report()
    assert report["plane"]["mode"] == "local"


def test_the_plane_line_never_prints_a_key(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise OSError("no network in this test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    target = script(tmp_path, f"""
        import runbound
        runbound.init(control_plane_url="http://127.0.0.1:9", token="{CANARY}", on_anomaly="raise")
        @runbound.tool(effects={{"read"}})
        def look(x): ...
    """)
    text, as_json = io.StringIO(), io.StringIO()
    _check.main([target], stdout=text, stderr=io.StringIO())
    api._teardown_for_tests()
    _check.main(["--json", target], stdout=as_json, stderr=io.StringIO())
    for output in (text.getvalue(), as_json.getvalue()):
        assert CANARY not in output
    assert "http://127.0.0.1:9" in text.getvalue()  # the URL, not the key
    assert json.loads(as_json.getvalue())["plane"]["url"] == "http://127.0.0.1:9"


# --- the callable -----------------------------------------------------------------------------------------------


def test_runbound_check_called_in_process_prints_and_returns_the_report(capsys):
    runbound.init(on_anomaly="raise")

    @runbound.tool(effects={"write"})
    def book(slot): ...

    report = runbound.check()

    assert report["guarded"] is True and [t["name"] for t in report["tools"]] == ["book"]
    assert "book" in capsys.readouterr().out


def test_runbound_check_before_init_reports_not_guarded_and_never_raises():
    report = runbound.check(file=io.StringIO())
    assert report["guarded"] is False and report["initialized"] is False


# --- a target that calls runbound.check() itself must not corrupt the CLI's one report (SDK-2 rework) ------------------


SELF_CHECKING = """
    import runbound
    runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")

    @runbound.tool(effects={"read"})  # guarded without any provider SDK installed (CI's test job has none)
    def look(order_id): ...

    mine = runbound.check()          # getting-started: "call check() at the end of your setup"
    assert mine["guarded"]
"""
NOT_SELF_CHECKING = SELF_CHECKING.replace("mine = runbound.check()", "mine = {'guarded': True}")


def test_json_on_a_target_that_calls_check_itself_is_exactly_one_json_document_and_exit_0(tmp_path):
    result = run_cli([script(tmp_path, SELF_CHECKING), "--json"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)  # json.loads fails on anything but one document
    assert document["guarded"] is True and document["schema"] == 1


def test_text_on_a_target_that_calls_check_itself_prints_the_report_exactly_once(tmp_path):
    mine = run_cli([script(tmp_path, SELF_CHECKING, "self.py")])
    plain = run_cli([script(tmp_path, NOT_SELF_CHECKING, "plain.py")])
    assert mine.returncode == 0 and plain.returncode == 0, (mine.stderr, plain.stderr)
    for line in {line for line in plain.stdout.splitlines() if line.strip()}:
        if "--:--" in line or ":" in line.split()[0]:  # an event's clock time differs between two runs
            continue
        assert mine.stdout.count(line) == plain.stdout.count(line), line  # nothing is printed twice


def test_the_in_process_call_still_prints_when_no_cli_is_loading_anything(capsys):
    runbound.init(budget_usd=1.0)
    runbound.check()
    assert capsys.readouterr().out != ""
    assert _check._LOADING is False


def test_the_silence_ends_with_the_load_even_when_the_target_raises(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert _check.main([script(tmp_path, "raise RuntimeError('boom')")], stdout=out, stderr=err) == 2
    assert _check._LOADING is False
