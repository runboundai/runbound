"""The SDK supports OpenAI and Anthropic. Its source must not name another provider as if it were supported or coming.

The one place that may name them is ``_coverage.py``, which lists the SDK shapes runbound does NOT guard so that
``runbound.coverage()`` can say so."""

from __future__ import annotations

import re
from pathlib import Path

import runbound

PACKAGE = Path(runbound.__file__).parent
NAMES = re.compile(r"\b(azure|gemini|bedrock|vertex)\b", re.IGNORECASE)
ALLOWED = {"_coverage.py"}


def test_no_python_file_of_the_sdk_names_another_provider_outside_the_coverage_report():
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if NAMES.search(line):
                hits.append(f"{path.relative_to(PACKAGE)}:{number}: {line.strip()}")
    assert hits == []
