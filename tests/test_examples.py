"""The three examples in ``examples/`` run offline and print what their docstrings promise.

``examples/`` holds exactly the scripts in :data:`REQUIRED_STEMS` and a README: a script added
without being registered here, or one deleted without being deregistered, fails
:func:`test_examples_dir_holds_exactly_the_registered_scripts`. Each script runs in a fresh
subprocess with no plane, no token and outbound sockets disabled. The part of each example that
needs a control plane is run by ``tests/integration/test_examples_plane.py`` (marker ``plane``).

``langchain_otel.py`` needs the ``langchain`` and ``otel`` extras. Under an interpreter without
them it must exit non-zero with the install hint; with them it must run. Set
``RUNBOUND_EXAMPLES_PYTHON`` to an interpreter that has them to run it fully.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples"

if not EXAMPLES_DIR.is_dir():  # pragma: no cover - an sdist built without examples/
    pytest.skip("examples/ is not present next to tests/", allow_module_level=True)

#: Every example this release ships, by stem.
REQUIRED_STEMS = ("tool_rules", "deferred_approval", "langchain_otel")
#: The examples that need optional extras, and the modules they import.
EXTRAS = {"langchain_otel": ("langchain_core", "opentelemetry.sdk")}

_TIMEOUT_S = 90
_SCRUBBED = ("RUNBOUND_TOKEN", "RUNBOUND_PLANE_URL", "RUNBOUND_APPROVAL_SECRET", "OPENAI_API_KEY",
             "ANTHROPIC_API_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT")

_NETWORK_BLOCK = """
import socket
def _refuse(*_a, **_kw):
    raise OSError("network disabled in the offline example run")
socket.socket.connect = _refuse
socket.socket.connect_ex = _refuse
socket.create_connection = _refuse
"""


def python_for(stem: str) -> str:
    """The interpreter to run ``stem`` with: ``RUNBOUND_EXAMPLES_PYTHON`` for the extras example, if set."""
    if stem in EXTRAS and os.environ.get("RUNBOUND_EXAMPLES_PYTHON"):
        return os.environ["RUNBOUND_EXAMPLES_PYTHON"]
    return sys.executable


def has_extras(python: str, stem: str) -> bool:
    probe = "import " + ", ".join(EXTRAS.get(stem, ())) if stem in EXTRAS else "pass"
    return subprocess.run([python, "-c", probe], capture_output=True, timeout=60).returncode == 0


def run_offline(stem: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run ``examples/<stem>.py`` as ``__main__`` with no plane env and no network."""
    path = EXAMPLES_DIR / f"{stem}.py"
    bootstrap = _NETWORK_BLOCK + f"import runpy\nrunpy.run_path({str(path)!r}, run_name='__main__')\n"
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env["RUNBOUND_CACHE_DIR"] = str(tmp_path / "cache")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    return subprocess.run([python_for(stem), "-c", bootstrap], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=_TIMEOUT_S)


def _show(result: subprocess.CompletedProcess) -> str:
    return f"exit {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


def test_examples_dir_holds_exactly_the_registered_scripts() -> None:
    scripts = sorted(p.stem for p in EXAMPLES_DIR.glob("*.py"))
    assert scripts == sorted(REQUIRED_STEMS), f"examples/*.py {scripts} != REQUIRED_STEMS {sorted(REQUIRED_STEMS)}"
    others = sorted(p.name for p in EXAMPLES_DIR.iterdir() if p.suffix != ".py" and p.name != "__pycache__")
    assert others == ["README.md"], f"examples/ should hold the scripts and a README only, found {others}"
    readme = (EXAMPLES_DIR / "README.md").read_text()
    for stem in REQUIRED_STEMS:
        assert f"{stem}.py" in readme, f"examples/README.md does not mention {stem}.py"


@pytest.mark.parametrize("stem", REQUIRED_STEMS)
def test_each_example_compiles(stem: str) -> None:
    path = EXAMPLES_DIR / f"{stem}.py"
    compile(path.read_text(), str(path), "exec")
    assert len(path.read_text().splitlines()) <= 70, f"{path.name} should stay short"


def test_tool_rules_offline_shows_both_code_rules(tmp_path) -> None:
    result = run_offline("tool_rules", tmp_path)
    assert result.returncode == 0, _show(result)
    lines = [line.split()[0] for line in result.stdout.splitlines() if line.split()[:1] in (["ok"], ["refused"])]
    assert lines == ["ok", "refused", "ok", "ok", "refused"], _show(result)  # the refused 500 used up no max_calls
    assert "plane: local" in result.stdout
    assert "failed its constraint" in result.stdout and "limit is 3" in result.stdout, _show(result)
    assert result.stdout.count("refused  policy") == 2, _show(result)


def test_deferred_approval_offline_is_refused_at_once(tmp_path) -> None:
    result = run_offline("deferred_approval", tmp_path)
    assert result.returncode == 0, _show(result)
    assert "refused approval_unavailable" in result.stdout, _show(result)
    assert "running for request" not in result.stdout, "the body must not run without an approval"


def test_langchain_otel_runs_with_the_extras_or_names_them(tmp_path) -> None:
    python = python_for("langchain_otel")
    result = run_offline("langchain_otel", tmp_path)
    if not has_extras(python, "langchain_otel"):
        assert result.returncode != 0, _show(result)
        assert "pip install --pre 'runbound[langchain,otel]'" in result.stderr, _show(result)
        pytest.skip(f"{python} has no langchain-core / opentelemetry-sdk; checked the install hint only")
    assert result.returncode == 0, _show(result)
    out = result.stdout.splitlines()
    assert "order A-7: shipped" in out and "refused tool_blocked" in out, _show(result)
    assert "span execute_tool lookup_order ok" in out, _show(result)
    assert "span execute_tool delete_order refused" in out, _show(result)
    assert "order A-7: deleted" not in result.stdout
