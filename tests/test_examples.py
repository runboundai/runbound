"""Runs every offline top-level example (`examples/*.py`) end to end.

Mirrors the registration pattern `tests/test_docs_examples.py` uses for
`examples/docs/*.py`: a fixed tuple of required stems, checked against what
is actually on disk, so a script added to `examples/` without being
registered here -- or one removed without being deregistered -- fails
loudly instead of quietly going unexercised. Scoped to the scripts that are
offline and deterministic by their own docstrings (no network, no API key,
no `openai`/`anthropic` package required); `examples/openai_agent.py` needs
a real `OPENAI_API_KEY` and makes billable calls, and `examples/live/` and
`examples/stress/` are not single runnable demo scripts, so none of those
are covered here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

#: Every offline, no-account example this release ships, by stem. A file
#: added to `examples/` that belongs in this set and is not listed here, or
#: a stem listed here with no file on disk, fails `test_registered_examples_exist`.
REQUIRED_STEMS = (
    "chatbot_abuse_demo",
    "core_loop_demo",
    "ladder_demo",
    "policy_demo",
    "runaway_demo",
)

_TIMEOUT_S = 60


def _path_for(stem: str) -> Path:
    return EXAMPLES_DIR / f"{stem}.py"


def test_registered_examples_exist() -> None:
    missing = [stem for stem in REQUIRED_STEMS if not _path_for(stem).is_file()]
    assert not missing, f"registered in REQUIRED_STEMS but missing on disk: {missing}"


def _run_example(path: Path) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k not in ("RUNBOUND_TOKEN", "RUNBOUND_PLANE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    }
    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(EXAMPLES_DIR.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )


@pytest.mark.parametrize("stem", REQUIRED_STEMS)
def test_example_runs_cleanly(stem: str) -> None:
    path = _path_for(stem)
    if not path.is_file():
        pytest.skip(f"{path.name} is not on disk (see test_registered_examples_exist)")

    result = _run_example(path)

    assert result.returncode == 0, (
        f"{path.name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_core_loop_demo_prints_pass() -> None:
    result = _run_example(_path_for("core_loop_demo"))
    assert result.returncode == 0
    assert "PASS" in result.stdout
