"""INVARIANTS.md names only tests that exist.

Every ``Asserted by:`` line lists ``tests/<file>.py::<test_name>`` references; each file must
exist and define that test (``def`` or ``async def``, at module level or inside a class).
Every numbered invariant above "Not yet asserted" must carry an ``Asserted by:`` line.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "INVARIANTS.md"
_REF = re.compile(r"`(tests/[\w/]+\.py)::(\w+)`")


def _asserted_part() -> str:
    return _DOC.read_text(encoding="utf-8").split("## Not yet asserted")[0]


def _references() -> list:
    lines = [line for line in _DOC.read_text(encoding="utf-8").splitlines() if line.strip().startswith("Asserted by:")]
    return [ref for line in lines for ref in _REF.findall(line)]


def test_every_asserted_by_reference_names_a_test_that_exists():
    refs = _references()
    assert refs, "INVARIANTS.md has no Asserted by: references"
    missing = []
    for path, name in refs:
        file = _ROOT / path
        if not file.is_file():
            missing.append(f"{path} (no such file)")
        elif not re.search(rf"^\s*(async\s+)?def {re.escape(name)}\(", file.read_text(encoding="utf-8"), re.M):
            missing.append(f"{path}::{name}")
    assert not missing, "INVARIANTS.md names tests that do not exist: " + ", ".join(missing)


def test_every_numbered_invariant_is_asserted():
    text = _asserted_part()
    numbered = re.findall(r"^\d+\. \*\*", text, re.M)
    asserted = re.findall(r"^\s*Asserted by: `tests/", text, re.M)
    assert numbered and len(numbered) == len(asserted)
