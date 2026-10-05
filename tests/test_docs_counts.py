"""Small counts the docs state, pinned to the package that has them."""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _read(*parts: str) -> str:
    return _ROOT.joinpath(*parts).read_text()


def _extras(pyproject: str) -> set[str]:
    # tomllib is 3.11+ and the SDK supports 3.10, so read the one table by hand: its keys are its "name = [" lines.
    section = pyproject.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    return set(re.findall(r"^([A-Za-z0-9_-]+)\s*=\s*\[", section, flags=re.M))


def test_the_optional_extras_the_docs_name_are_the_ones_the_package_has():
    extras = _extras(_read("pyproject.toml"))
    shipped = {name for name in extras if name not in {"test", "dev"}}
    assert shipped == {"langchain", "otel"}
    for page in ("README.md", "docs/getting-started.md"):
        text = _read(page)
        assert "two optional extras" in text
        for name in shipped:
            assert f"runbound[{name}]" in text


def test_the_opentelemetry_counters_are_five_wherever_the_docs_count_them():
    source = _read("runbound", "otel.py")
    counters = re.findall(r'create_counter\(\s*"(runbound\.\w+)"', source)
    assert len(counters) == 5
    assert "five counters" in _read("docs", "README.md")
    guide = _read("docs", "guides", "opentelemetry.md")
    assert "Five **counters**" in guide
    for name in counters:
        assert name in guide


def test_the_readme_says_how_many_manual_pages_the_site_renders_whole():
    """The README says the site renders six manual pages whole; the site's own list of them (``manual-sources.ts``, which
    exists in the development monorepo only) has exactly that many, and each is a real file of this manual."""
    import pytest

    sources = _ROOT.parent / "website" / "scripts" / "manual-sources.ts"
    if not sources.exists():
        pytest.skip("the site is not part of this checkout (the public repository)")
    listed = re.findall(r'source:\s*"(docs/[^"]+\.md)"', sources.read_text())
    assert len(listed) == 6, "README.md says six; update it with manual-sources.ts"
    for path in listed:
        assert _ROOT.joinpath(path).is_file(), path
    readme = _read("README.md")
    assert "renders whole the six" in readme and "manual-sources.ts" in readme
