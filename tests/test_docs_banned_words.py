"""The front door and the manual describe the tool boundary, not the product that was cut away.

README.md, ``docs/`` and ``examples/`` must not use the vocabulary of the removed runaway-detection / provider-wrapper
product (S9). CHANGELOG.md is exempt: it has to name what was removed.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED = ("runaway", "circuit", "spike", "budget", "posture", "safe mode", "safe-mode")
SUFFIXES = {".md", ".py", ".txt", ".toml"}


def _files() -> list[Path]:
    files = [ROOT / "README.md"]
    for folder in ("docs", "examples"):
        files += [p for p in (ROOT / folder).rglob("*") if p.is_file() and p.suffix in SUFFIXES]
    return sorted(files)


def banned_hits(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in BANNED if word in lowered]


def test_the_banned_words_are_found_when_present():
    assert banned_hits("a Circuit breaker") == ["circuit"]
    assert banned_hits("keeps your budget in safe mode") == ["budget", "safe mode"]
    assert banned_hits("a tool boundary") == []


def test_readme_docs_and_examples_use_none_of_the_banned_words():
    offenders = {str(p.relative_to(ROOT)): hits for p in _files() if (hits := banned_hits(p.read_text(encoding="utf-8")))}
    assert not offenders, offenders


def test_the_readme_names_what_it_is_about():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("@runbound.tool", "approval", "RUNBOUND_APPROVAL_SECRET", "consequential_grace_s", "per clock hour",
                   "execute_tool", "Decorate before", "dry run", "would_refuse"):
        assert needle in text, needle
