"""Every ``python`` block in README.md and docs/ is real code, and every quoted example is verbatim.

1. Each fenced ```python block parses with :mod:`ast`.
2. Each ``runbound.<name>...`` it references exists: the first name is in ``runbound.__all__`` or is
   a submodule of ``runbound``, and the rest of the chain resolves (``hasattr``, importing a submodule
   where the chain names one). ``import runbound as rb`` aliases and ``from runbound... import x``
   are checked the same way.
3. A block preceded by ``<!-- example: <id> -->`` must be the ``# docs: <id>`` region of the example
   that carries it, verbatim (see ``tests/test_docs_examples.py`` for the region convention). An id
   no example carries fails.

Only pages that exist are checked, so pages added later are picked up as they land.
"""

from __future__ import annotations

import ast
import importlib
import re
import types
from pathlib import Path

import pytest

import runbound

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples"

FENCE = re.compile(r"^```python[ \t]*\n(.*?)\n```[ \t]*$", re.S | re.M)
CLAIM = re.compile(r"<!--\s*example:\s*([a-z0-9-]+)\s*-->[ \t]*\n```python[ \t]*\n(.*?)\n```", re.S)
START = re.compile(r"^# docs: ([a-z0-9-]+)$")


def pages() -> list[Path]:
    found = [ROOT / "README.md"] if (ROOT / "README.md").is_file() else []
    return found + sorted((ROOT / "docs").rglob("*.md"))


def _rel(page: Path) -> str:
    return str(page.relative_to(ROOT))


def example_regions() -> dict[str, str]:
    """``{id: region}`` over every example (the markers are checked by test_docs_examples.py)."""
    out: dict[str, str] = {}
    for path in sorted(EXAMPLES_DIR.glob("*.py")) if EXAMPLES_DIR.is_dir() else ():
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            match = START.match(line)
            if match:
                end = lines.index("# /docs", i)
                out[match[1]] = "\n".join(lines[i + 1 : end]).strip("\n")
    return out


def _resolve(chain: list[str]) -> str | None:
    """``None`` when ``runbound.<chain>`` exists, else what is missing."""
    head = chain[0]
    if head not in runbound.__all__:
        try:
            importlib.import_module(f"runbound.{head}")
        except ImportError:
            return f"runbound.{head} is neither in runbound.__all__ nor a submodule"
    obj: object = runbound
    for depth, part in enumerate(chain):
        dotted = "runbound." + ".".join(chain[: depth + 1])
        if isinstance(obj, types.ModuleType):
            try:
                importlib.import_module(f"{obj.__name__}.{part}")
            except ImportError:
                pass  # not a submodule: an attribute
            if part in getattr(obj, "__all__", ()) and not _has(obj, part):
                return None  # a lazily built export (e.g. a handler needing an optional dependency)
        if not _has(obj, part):
            return f"{dotted} does not exist"
        obj = getattr(obj, part)
    return None


def _has(obj: object, name: str) -> bool:
    try:
        return hasattr(obj, name)
    except ImportError:  # a lazy attribute whose optional dependency is absent
        return False


def _chain(node: ast.Attribute) -> list[str] | None:
    """``["a", "b"]`` for ``<root>.a.b`` where the root is a plain name; ``None`` otherwise."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    return None


def problems(source: str) -> list[str]:
    tree = ast.parse(source)
    aliases = {"runbound"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "runbound" and name.asname:
                    aliases.add(name.asname)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "runbound":
            sub = node.module.split(".")[1:]
            for name in node.names:
                why = None if name.name == "*" else _resolve(sub + [name.name])
                if why:
                    found.append(f"from {node.module} import {name.name}: {why}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain = _chain(node)
            if chain and chain[0] in aliases and len(chain) > 1:
                why = _resolve(chain[1:])
                if why:
                    found.append(why)
    return sorted(set(found))


def _blocks(page: Path) -> list[str]:
    return FENCE.findall(page.read_text(encoding="utf-8"))


ALL_PAGES = pages()


@pytest.mark.parametrize("page", ALL_PAGES, ids=_rel)
def test_every_python_block_parses_and_names_only_real_runbound_attributes(page: Path) -> None:
    failures = []
    for number, block in enumerate(_blocks(page), 1):
        try:
            missing = problems(block)
        except SyntaxError as error:
            failures.append(f"block {number}: does not parse ({error.msg}, line {error.lineno})")
            continue
        failures += [f"block {number}: {why}" for why in missing]
    assert not failures, f"{_rel(page)}:\n" + "\n".join(failures)


@pytest.mark.parametrize("page", ALL_PAGES, ids=_rel)
def test_every_quoted_example_is_the_example_verbatim(page: Path) -> None:
    claims = CLAIM.findall(page.read_text(encoding="utf-8"))
    if not claims:
        pytest.skip(f"{_rel(page)} quotes no example")
    regions = example_regions()
    for region_id, block in claims:
        assert region_id in regions, f"{_rel(page)}: no example carries '# docs: {region_id}'"
        assert block == regions[region_id], (
            f"{_rel(page)}: the block under <!-- example: {region_id} --> is not the example's region; "
            f"copy it from the example verbatim"
        )


def test_the_checker_catches_what_it_should() -> None:
    assert problems("import runbound\nrunbound.init()\nrunbound.hooks.allow()") == []
    assert problems("import runbound as rb\nrb.wrap(client)")  # removed in S1
    assert problems("from runbound import budget")
    assert problems("import runbound\nrunbound.ToolPolicy.no_such_thing")
    assert problems("from runbound.integrations.langchain import RunboundCallbackHandler") == []
    with pytest.raises(SyntaxError):
        problems(">>> runbound.init()")
