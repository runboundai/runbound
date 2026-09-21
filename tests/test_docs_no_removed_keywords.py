"""A rename with no compatibility shim (this package carries no
compatibility burden and ships no deprecation shims) removes a keyword
from the *code* instantly, but
prose in ``docs/`` and ``examples/`` does not fail an import — nothing in
``test_docs_examples.py`` (which only *executes* ``examples/docs/*.py``) can
catch a paragraph of Markdown that still teaches a keyword that now raises
``TypeError``. This is that cheap guard: a removed keyword's exact spelling
must not appear anywhere in ``docs/`` or ``examples/``.

``repeatable=`` was removed in favor of ``polling=`` — found stale in
four places (three docs pages, one example) that nothing in the suite
had protected. This test is the fix for that gap, not just for that one
keyword:
add to ``REMOVED_KEYWORDS`` the next time a keyword is removed rather than
aliased, and the same class of gap cannot recur silently.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

#: Keywords removed (not aliased) from the public API. Each entry is the
#: exact, no-shim spelling that must never appear again in docs or examples —
#: a false positive would mean the keyword's replacement itself contains the
#: old spelling as a substring, which none of these do.
REMOVED_KEYWORDS = (
    "repeatable=",  # @runbound.tool(repeatable=True) -> polling=True
    # A larger, later removal of twenty-seven `init()` keywords (postures=,
    # capabilities=, budget_soft=, on_budget_soft=, max_actions_per_run=,
    # the eight circuit-rate knobs, the three loop-shape knobs, and the
    # eleven spike/ladder knobs) was itself reversed: every one is a real,
    # free, local `init()` keyword again, so none of them belongs in this
    # list any more.
)


def _prose_files() -> list[Path]:
    """Every Markdown page and example script this package ships."""
    files = list((_ROOT / "docs").rglob("*.md"))
    examples_dir = _ROOT / "examples"
    if examples_dir.is_dir():
        files += list(examples_dir.rglob("*.py"))
    return sorted(files)


def test_no_doc_or_example_teaches_a_removed_keyword():
    files = _prose_files()
    assert files, "expected docs/ or examples/ to exist next to tests/"

    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for keyword in REMOVED_KEYWORDS:
            if keyword in text:
                offenders.append(f"{path.relative_to(_ROOT)}: {keyword!r}")

    assert not offenders, (
        "removed keyword(s) still taught in docs/examples (would raise "
        "TypeError if a reader copied them):\n" + "\n".join(offenders)
    )
