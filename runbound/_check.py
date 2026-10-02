"""``runbound.check()`` and ``python -m runbound check``: what is guarded in this process, in one report.

A quick answer to "is anything actually protected here?" for a person at a terminal, a coding agent that just
attached the SDK, and CI. It reads what :func:`runbound.coverage`, :func:`runbound.tools`,
:func:`runbound.posture` and :func:`runbound.events` already know: it makes no call to a control plane (it reads
the link's last status, never asks it anything), and it never prints a token.

**How it inspects your process.** A report can only describe the process that holds the state, so there are two
forms and they share one report. ``runbound.check()`` is called in your own process, at the end of its setup. The
command form ``python -m runbound check TARGET`` loads the target (a script path, or a module name) in its own
process first, as ``runbound_check`` rather than ``__main__``: your ``init()``, ``wrap()`` and ``@runbound.tool``
lines at import time run, and an ``if __name__ == "__main__":`` block (a server loop, a ``main()``) does not. Code
that only wires runbound inside such a block is reported as not guarded, and the report says to call
``runbound.check()`` there instead.

Private module on purpose, like :mod:`runbound._coverage`: a public ``runbound.check`` submodule would replace the
``check`` function on the package.

Exit codes: 0 when at least one wrapped client, decorated tool or guarded call exists, 1 when nothing is guarded, 2
when the target could not be loaded. ``--json`` prints the report with a stable shape (``schema`` 1).
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from typing import Any, TextIO

from . import __version__, api

SCHEMA = 1
EVENTS_SHOWN = 10
#: The limits ``init()`` can state that this report calls "budgets in force", in the order it prints them.
BUDGET_KEYS = ("budget_usd", "budget_window", "run_budget_usd", "max_total_tokens", "max_steps", "max_actions_per_run",
               "max_events", "max_cost_per_call_usd", "max_tokens_out_per_call")
LOAD_AS = "runbound_check"


def build_report() -> dict:
    """The report as plain data. Never raises, never calls a control plane, never carries a token."""
    import platform

    coverage = api.coverage()
    engine = getattr(api, "_ENGINE", None)
    config = getattr(engine, "config", None)
    labels = coverage.get("auto_wrapped") or []
    grouped: dict[str, list[str]] = {}
    for label in labels:
        provider, _, shape = str(label).partition(":")
        grouped.setdefault(provider, []).append(shape)
    tools = [{"name": row.get("name"), "classes": sorted(row.get("effects") or [])} for row in (_tools() or [])]
    guarded = bool(coverage.get("guarded_calls") or coverage.get("decorated_tools") or coverage.get("wrapped_clients")
                   or labels)
    return {
        "schema": SCHEMA,
        "runbound": __version__,
        "python": platform.python_version(),
        "guarded": guarded,
        "initialized": engine is not None,
        "clients": {
            "wrapped_clients": int(coverage.get("wrapped_clients") or 0),
            "auto_wrapped": grouped,
            "providers_imported": list(coverage.get("providers_imported") or []),
            "providers_unguarded": list(coverage.get("providers_unguarded") or []),
        },
        "tools": tools,
        "plane": _plane(coverage, config),
        "posture": api.posture(),
        "budgets": {k: getattr(config, k) for k in BUDGET_KEYS if getattr(config, k, None) is not None} if config else {},
        "guarded_calls": int(coverage.get("guarded_calls") or 0),
        "tool_calls_seen": int(coverage.get("tool_calls_seen") or 0),
        "events": [_event(e) for e in (api.events(EVENTS_SHOWN) or [])][-EVENTS_SHOWN:],
    }


def _tools() -> list:
    try:
        return api.tools()
    except Exception:
        return []


def _plane(coverage: dict, config: Any) -> dict:
    """Where the link to a plane stands: its mode, its URL (never the token) and the coverage sentence."""
    try:
        mode = api.plane_status().mode
    except Exception:
        mode = "local"
    url = getattr(config, "control_plane_url", None) if config is not None else None
    return {"mode": mode, "url": url or None, "detail": coverage.get("fleet")}


def _event(event: dict) -> dict:
    kind = str(event.get("kind") or "")
    if kind == "posture":
        line = f"posture {event.get('from') or '?'} -> {event.get('posture') or event.get('to') or '?'}" + (
            f" ({event['reason']})" if event.get("reason") else "")
    elif kind == "runtime_change":
        line = f"{event.get('what')}: {event.get('from')} -> {event.get('to')}"
    else:
        detector = event.get("detector")
        line = f"{detector + ': ' if detector else ''}{event.get('message') or event.get('reason') or kind}"
    return {"kind": kind, "ts": event.get("at"), "line": " ".join(str(line).split())[:200]}


def render(report: dict) -> str:
    """The report for a person: short, one section a question."""
    lines = [f"runbound {report['runbound']} on python {report['python']}"]
    lines.append("GUARDED" if report["guarded"] else
                 "Nothing is guarded in this process: no wrapped client, no @runbound.tool, no guarded call.")
    if not report["initialized"]:
        lines.append("  runbound.init() has not run here. Add runbound.init(...) before your first model call, or call "
                     "runbound.check() at the end of your setup (a target's __main__ block is not run).")
    clients = report["clients"]
    lines.append("")
    lines.append("Clients")
    if clients["auto_wrapped"] or clients["wrapped_clients"]:
        for provider, shapes in sorted(clients["auto_wrapped"].items()):
            lines.append(f"  {provider}: wrapped automatically ({', '.join(shapes)})")
        if clients["wrapped_clients"]:
            lines.append(f"  {clients['wrapped_clients']} client(s) wrapped by hand")
    else:
        lines.append("  none wrapped")
    if clients["providers_unguarded"]:
        lines.append(f"  imported but not yet seen guarded: {', '.join(clients['providers_unguarded'])}")
    lines.append(f"  guarded calls so far: {report['guarded_calls']}; tool calls seen: {report['tool_calls_seen']}")
    lines.append("")
    lines.append("Tools")
    if report["tools"]:
        for tool in report["tools"]:
            lines.append(f"  {tool['name']}  [{', '.join(tool['classes']) or 'unclassified'}]")
    else:
        lines.append("  no @runbound.tool decorated")
    plane = report["plane"]
    lines += ["", "Plane", f"  {plane['mode']}" + (f" at {plane['url']}" if plane["url"] else " (state stays in this process)")
              + (f" - {plane['detail']}" if plane.get("detail") else "")]
    lines += ["", f"Posture: {report['posture']}", "", "Budgets and limits in force"]
    if report["budgets"]:
        for key, value in report["budgets"].items():
            lines.append(f"  {key}: {'$' + format(value, 'g') if key.endswith('_usd') else value}")
    else:
        lines.append("  none stated (the defaults apply)")
    lines += ["", f"Last events ({len(report['events'])})"]
    lines += [f"  {_clock(e['ts'])}  {e['kind']:<15} {e['line']}" for e in report["events"]] or ["  none yet"]
    return "\n".join(lines) + "\n"


def _clock(ts: Any) -> str:
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "--:--:--"


def check(*, file: TextIO | None = None, as_json: bool = False) -> dict:
    """Print what is guarded in THIS process and return the same report as a dict.

    Call it at the end of your setup (or in a startup log line). ``file`` is where it prints (default stdout);
    ``as_json=True`` prints the JSON form. Never raises; makes no call to a control plane and prints no token. The
    returned dict's ``"guarded"`` is what ``python -m runbound check`` turns into its exit code."""
    report = build_report()
    if _LOADING:  # ``python -m runbound check TARGET`` is loading a target that calls check() itself: it prints once, at the end
        return report
    try:
        out = file or sys.stdout
        out.write((json.dumps(report, indent=2, default=str) + "\n") if as_json else render(report))
    except Exception:  # printing is a courtesy; the report is what was asked for
        pass
    return report


#: True while the CLI is running a target. An in-process ``check()`` the target makes (getting-started tells people to put
#: one at the end of their setup) stays silent then, still returning its dict, so the CLI's one report, text or JSON, is the
#: only thing on stdout and ``--json`` stays machine-readable.
_LOADING = False


def _load(target: str) -> None:
    """Run ``target`` (a script path, or a module name) as ``runbound_check``."""
    if os.path.exists(target) or target.endswith(".py"):
        path = os.path.abspath(target)
        sys.path.insert(0, os.path.dirname(path))
        runpy.run_path(path, run_name=LOAD_AS)
    else:
        sys.path.insert(0, os.getcwd())
        runpy.run_module(target, run_name=LOAD_AS, alter_sys=False)


def main(argv: list[str], *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """``runbound check [--json] [TARGET]``; the exit code described in the module docstring."""
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    as_json = "--json" in argv
    rest = [a for a in argv if a != "--json"]
    if any(a in ("-h", "--help") for a in rest):
        stdout.write("usage: python -m runbound check [--json] [TARGET]\n\n"
                     "Say what runbound guards in a process. TARGET is a script path or a module name, loaded as\n"
                     f"'{LOAD_AS}' (its __main__ block does not run); with none, the empty process is reported.\n"
                     "Exit 0: something is guarded. 1: nothing is. 2: the target could not be loaded.\n"
                     "--json prints the report as JSON (schema 1).\n")
        return 0
    if len(rest) > 1:
        stderr.write("runbound check: at most one target\n")
        return 2
    if rest:
        global _LOADING
        _LOADING = True
        try:
            _load(rest[0])
        except BaseException as exc:  # a target that raises or exits is a failed load, not a report
            stderr.write(f"runbound check: could not load {rest[0]}: {type(exc).__name__}: {exc}\n")
            return 2
        finally:
            _LOADING = False
    report = check(file=stdout, as_json=as_json)
    return 0 if report["guarded"] else 1


def cli(argv: list[str] | None = None) -> int:
    """The ``python -m runbound`` entry point."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["check"]:
        return main(argv[1:])
    sys.stderr.write("usage: python -m runbound check [--json] [TARGET]\n"
                     "(python -m runbound.demo runs the demo)\n")
    return 2
