"""``python -m runbound.demo``: the whole core loop, for a stranger with no
clone and no key.

These tests run the demo the way a stranger actually would -- as a fresh
subprocess (`python -m runbound.demo`, and calling `runbound.demo.main()`
from one) -- because the demo's own job is to prove a *process* works end
to end, and running it in-process here would leave this test suite's own
global runbound state entangled with the demo's `init()` call.
"""

from __future__ import annotations

import subprocess
import sys


def _run_module() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "runbound.demo"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_demo_prints_pass_and_exits_zero() -> None:
    result = _run_module()

    assert result.returncode == 0, (
        f"exited {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "PASS" in result.stdout


def test_demo_broken_assertion_exits_nonzero() -> None:
    """A genuine break in the demo's own fake provider -- not a hidden
    test-only flag -- drives the FAIL / non-zero-exit path.

    Monkeypatches ``_FakeCompletions.create`` so it no longer counts the
    call; the demo's own ``assert completions.calls == 1`` then fails
    exactly the way a real regression would, and `main()` handles it
    exactly the way it handles any other broken assertion: prints `FAIL:`
    and returns 1.
    """
    script = (
        "import sys\n"
        "import runbound.demo as demo\n"
        "def _broken_create(self, **kwargs):\n"
        "    return demo._Response('demo-model')\n"
        "demo._FakeCompletions.create = _broken_create\n"
        "sys.exit(demo.main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0, (
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "FAIL" in result.stdout
    assert "PASS" not in result.stdout


def test_demo_narrative_prints_in_order() -> None:
    """The four numbered steps appear on stdout in order, and none of the
    library's own `[runbound]` refusal lines leak onto stderr -- the demo
    quiets that logger and narrates each refusal itself instead, so the
    story reads in order wherever it runs (piped to a file, under a test
    harness, or to a terminal)."""
    result = _run_module()
    assert result.returncode == 0

    stdout = result.stdout
    markers = [
        "1. A runaway",
        "2. Detected",
        "3. A financial action is denied",
        "4. Stopped",
        "PASS",
    ]
    positions = [stdout.index(m) for m in markers]
    assert positions == sorted(positions), f"steps out of order:\n{stdout}"

    assert "[runbound]" not in result.stderr, f"--- stderr ---\n{result.stderr}"


def test_importing_runbound_does_not_import_the_demo() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import runbound; sys.exit(1 if 'runbound.demo' in sys.modules else 0)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"'runbound.demo' was imported at package scope\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
