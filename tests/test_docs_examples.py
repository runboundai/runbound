"""Runs every website docs-tab code example (`examples/docs/*.py`) offline.

Each file there is one code block the docs tab renders, extracted by a
companion generator script from a `# docs: <id>` / `# /docs` marked
region. This test is what keeps a snippet from silently drifting away from
the SDK it claims to demonstrate: it discovers every file, checks its
markers are well-formed, and runs it — in a fresh subprocess, with no
network and no API key — asserting it exits cleanly. A file that no longer
matches the SDK fails here, in CI, rather than on the docs pages.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

DOCS_DIR = Path(__file__).resolve().parent.parent / "examples" / "docs"

#: The complete set of docs ids this wave ships. A file added or removed
#: under examples/docs/ without a matching change here fails loudly, on
#: purpose — this table is the contract the generator script and the docs
#: tab are built against.
REQUIRED_IDS = (
    "quickstart-init",
    "coverage-check",
    "session-per-run",
    "chatbot-handler",
    "session-async-threads",
    "tool-rules-decorator",
    "tool-policy-init",
    "tool-policy-dict",
    "catch-policy-violation",
    "policy-dry-run",
    "require-rules",
    "repeatable-tool",
    "tool-calls-tally",
    "clear-key",
    "latch-ttl",
    "spike-ladder-limit",
    "callback-paging",
    "verify-webhook",
    "refusals-profile",
    "openai-stream-usage",
    "async-wrap",
    "budget-admission",
    "token-limits-local",
    "custom-prices",
    "unpriced-refuse",
    "pricing-as-of",
    "cached-tokens",
    "selfhosted-wrap",
    "record-call",
    "llm-decorator",
    "circuit-open",
    "langchain-handler",
    "fleet-init",
    "plane-unreachable",
    "self-hosted-init",
)

_START_PREFIX = "# docs: "
_END_MARKER = "# /docs"
_TIMEOUT_S = 60
_ID_RE_SUFFIX = " · needs token"


def _docs_files() -> list[Path]:
    """Every ``examples/docs/*.py`` that is a snippet, not scaffolding."""
    return sorted(p for p in DOCS_DIR.glob("*.py") if not p.name.startswith("_"))


@dataclass(frozen=True)
class Markers:
    """One file's parsed ``# docs: <id>`` / ``# /docs`` region."""

    id: str
    needs_token: bool
    start_line: int
    end_line: int


def _parse_markers(path: Path) -> Markers:
    """The one start/end marker pair in ``path``, or a hard failure.

    Mirrors the companion generator script's own parsing exactly, so a file
    that passes here is guaranteed to generate cleanly for the docs pages too.
    """
    lines = path.read_text().splitlines()
    starts = [
        (i, line[len(_START_PREFIX):]) for i, line in enumerate(lines) if line.startswith(_START_PREFIX)
    ]
    ends = [i for i, line in enumerate(lines) if line == _END_MARKER]

    assert len(starts) == 1, f"{path.name}: expected exactly one '# docs: <id>' marker, found {len(starts)}"
    assert len(ends) == 1, f"{path.name}: expected exactly one '# /docs' marker, found {len(ends)}"

    start_line, raw_id = starts[0]
    end_line = ends[0]
    assert start_line < end_line, f"{path.name}: '# docs: <id>' must come before '# /docs'"

    needs_token = raw_id.endswith(_ID_RE_SUFFIX)
    doc_id = raw_id[: -len(_ID_RE_SUFFIX)] if needs_token else raw_id
    assert doc_id and all(c.islower() or c.isdigit() or c == "-" for c in doc_id), (
        f"{path.name}: id {doc_id!r} must match [a-z0-9-]+"
    )

    region = lines[start_line + 1 : end_line]
    assert any(line.strip() for line in region), f"{path.name}: the region between the markers is empty"

    expected_id = path.stem.replace("_", "-")
    assert doc_id == expected_id, (
        f"{path.name}: marker id {doc_id!r} does not match the filename stem "
        f"(expected {expected_id!r})"
    )

    return Markers(id=doc_id, needs_token=needs_token, start_line=start_line, end_line=end_line)


def _requires(path: Path) -> tuple[str, ...]:
    """The ``REQUIRES = (...)`` tuple a file's prelude declares, via ``ast``.

    Never imports the file — a docs example is meant to be run as a fresh
    subprocess, not collected into the test process, so this reads the
    module's top-level assignments as a syntax tree instead.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REQUIRES" for t in node.targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return (value,)
        return tuple(value)
    return ()


def _missing_packages(requires: tuple[str, ...]) -> tuple[str, ...]:
    import importlib.util

    return tuple(name for name in requires if importlib.util.find_spec(name) is None)


# --- discovery: every file, and the id contract ------------------------------


def test_every_required_id_has_exactly_one_file() -> None:
    files = _docs_files()
    ids = [_parse_markers(f).id for f in files]

    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"ids used by more than one file: {duplicates}"

    assert sorted(ids) == sorted(REQUIRED_IDS), (
        f"examples/docs/*.py ids do not match REQUIRED_IDS.\n"
        f"missing: {sorted(set(REQUIRED_IDS) - set(ids))}\n"
        f"unexpected: {sorted(set(ids) - set(REQUIRED_IDS))}"
    )


@pytest.mark.parametrize("path", _docs_files(), ids=lambda p: p.stem)
def test_markers_are_well_formed(path: Path) -> None:
    _parse_markers(path)  # raises AssertionError, with the file name, on any rule break


# --- execution: every file runs standalone, offline, with no network --------


_NETWORK_BLOCK = """
import socket


def _refuse(*_a, **_kw):
    raise OSError("network disabled in docs examples")


socket.socket.connect = _refuse
socket.socket.connect_ex = _refuse
socket.create_connection = _refuse
socket.getaddrinfo = _refuse
"""


def _run_docs_file(path: Path) -> subprocess.CompletedProcess:
    """Run ``path`` in a fresh subprocess: no plane/API env vars, no network.

    A ``-c`` bootstrap disables outbound sockets before handing off to
    ``runpy.run_path`` (``run_name="__main__"``) so a file's own
    ``if __name__ == "__main__":`` guard, if it has one, still fires. The
    script's own directory is put on ``sys.path`` first, exactly as running
    ``python examples/docs/<file>.py`` directly would, so ``import
    _offline`` resolves the same way in both places.
    """
    bootstrap = (
        _NETWORK_BLOCK
        + "import runpy, sys\n"
        + f"sys.path.insert(0, {str(DOCS_DIR)!r})\n"
        + f"runpy.run_path({str(path)!r}, run_name='__main__')\n"
    )
    env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k not in ("RUNBOUND_TOKEN", "RUNBOUND_PLANE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    }
    return subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=str(DOCS_DIR.parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )


@pytest.mark.parametrize("path", _docs_files(), ids=lambda p: p.stem)
def test_docs_example_runs_cleanly(path: Path) -> None:
    missing = _missing_packages(_requires(path))
    if missing:
        pytest.skip(f"{path.name} needs {', '.join(missing)}, not installed here")

    result = _run_docs_file(path)

    assert result.returncode == 0, (
        f"{path.name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
