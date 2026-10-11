"""The ``# docs: <id>`` regions of the three examples: the code the documentation pages quote.

Convention. An example marks the lines a page may show between a line ``# docs: <id>`` and a line
``# /docs`` (both at column 0, ids ``[a-z0-9-]+``, unique across ``examples/``). A page that shows a
region puts ``<!-- example: <id> -->`` on the line right before the fenced ``python`` block, and
``tests/test_docs_pages_show_executed_snippets.py`` checks the block is the region, verbatim. Since
the region is part of a script ``tests/test_examples.py`` runs, a page cannot quote code that does
not run. This file checks the markers themselves.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples"

if not EXAMPLES_DIR.is_dir():  # pragma: no cover
    pytest.skip("examples/ is not present next to tests/", allow_module_level=True)

#: The regions each example carries, in file order.
REQUIRED_IDS = {
    "tool_rules": ["tool-rules"],
    "deferred_approval": ["deferred-approval"],
    "langchain_otel": ["langchain-otel"],
}

START = re.compile(r"^# docs: (?P<id>.*)$")
END = "# /docs"


def regions(path: Path) -> dict[str, str]:
    """``{id: region text}`` for one example; asserts the markers are well formed."""
    lines = path.read_text().splitlines()
    found: dict[str, str] = {}
    open_at = open_id = None
    for number, line in enumerate(lines):
        start = START.match(line)
        if start:
            assert open_id is None, f"{path.name}:{number + 1}: region {open_id!r} is still open"
            open_id, open_at = start["id"], number
            assert re.fullmatch(r"[a-z0-9-]+", open_id), f"{path.name}: id {open_id!r} must match [a-z0-9-]+"
            assert open_id not in found, f"{path.name}: id {open_id!r} used twice"
        elif line == END:
            assert open_id is not None, f"{path.name}:{number + 1}: '# /docs' with no open region"
            body = "\n".join(lines[open_at + 1 : number]).strip("\n")
            assert body.strip(), f"{path.name}: region {open_id!r} is empty"
            found[open_id] = body
            open_id = None
    assert open_id is None, f"{path.name}: region {open_id!r} is never closed"
    return found


def all_regions() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(EXAMPLES_DIR.glob("*.py")):
        for region_id, body in regions(path).items():
            assert region_id not in out, f"region id {region_id!r} is used by more than one example"
            out[region_id] = body
    return out


@pytest.mark.parametrize("stem", sorted(REQUIRED_IDS))
def test_each_example_carries_its_regions(stem: str) -> None:
    path = EXAMPLES_DIR / f"{stem}.py"
    assert path.is_file(), f"{path.name} is registered in REQUIRED_IDS but missing"
    assert list(regions(path)) == REQUIRED_IDS[stem]


def test_region_ids_are_unique_and_registered() -> None:
    assert sorted(all_regions()) == sorted(i for ids in REQUIRED_IDS.values() for i in ids)
    assert sorted(p.stem for p in EXAMPLES_DIR.glob("*.py")) == sorted(REQUIRED_IDS)


@pytest.mark.parametrize("region_id", sorted(i for ids in REQUIRED_IDS.values() for i in ids))
def test_each_region_parses_on_its_own(region_id: str) -> None:
    """A page shows the region alone, so it must be valid Python alone."""
    ast.parse(all_regions()[region_id])
