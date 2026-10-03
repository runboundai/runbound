"""Every code block the new manual pages show is an executed example, verbatim.

A page that says "it prints this" must be showing code a test ran. For the pages below, each fenced
``python`` block has to be the marked region of one file under ``examples/docs/`` (which
``test_docs_examples.py`` runs offline), so the page cannot drift from the SDK.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "docs"

if not EXAMPLES.is_dir():  # pragma: no cover
    pytest.skip("examples/docs/ is not present", allow_module_level=True)

#: page -> the example ids whose regions it must show, in order.
PAGES = {
    "docs/guides/a-runaway.md": ["runaway-arc"],
    "docs/guides/what-changed.md": ["runtime-change"],
    "docs/guides/opentelemetry.md": ["otel-export"],
}
#: pages that also carry hand-written fragments: only these ids must appear verbatim.
MUST_CONTAIN = {"docs/getting-started.md": ["graded-loop", "verify-events"]}


def _region(example_id: str) -> str:
    lines = (EXAMPLES / f"{example_id.replace('-', '_')}.py").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# docs: "))
    return "\n".join(lines[start + 1 : lines.index("# /docs")]).strip("\n")


def _python_blocks(page: str) -> list[str]:
    return re.findall(r"```python\n(.*?)\n```", (ROOT / page).read_text(), flags=re.S)


@pytest.mark.parametrize("page", PAGES)
def test_every_python_block_on_the_page_is_an_executed_example(page):
    assert _python_blocks(page) == [_region(i) for i in PAGES[page]]


@pytest.mark.parametrize("page", MUST_CONTAIN)
def test_the_page_shows_these_examples_verbatim(page):
    blocks = _python_blocks(page)
    for example_id in MUST_CONTAIN[page]:
        assert _region(example_id) in blocks, example_id
