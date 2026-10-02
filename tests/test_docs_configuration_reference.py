"""The configuration reference names every ``init()`` parameter exactly once, with its real default (SDK-1).

``runbound.init(**kwargs)`` builds a ``GuardrailConfig``, so its fields ARE the parameters. The reference groups them
by purpose; this test reads the page and checks, row by row, against the code: every parameter appears in exactly one
row, no row names a parameter that does not exist, each Default is the field's own default, and each "the plane can
tighten it" cell says what the engine's Controls merge does (see ``tests/_config_reference.py``). The check is a
function so a dropped row and an invented one can be shown to fail it.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

import runbound
from runbound.config import GuardrailConfig

from _config_reference import default_text, tightenable

DOC = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration.md"
SECTIONS = ["Budgets and limits", "Loops, spikes and failing providers", "Postures and tools", "The plane", "Privacy", "Other"]
FIELDS = {f.name: f for f in dataclasses.fields(GuardrailConfig) if not f.name.startswith("_")}


def rows(text: str) -> list[tuple[str, list[str], list[str]]]:
    """``(section, parameter names in the first cell, the cells)`` for every table row under a ``## `` heading."""
    found, section = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("| `") and section is not None:
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            found.append((section, re.findall(r"`([a-z_0-9]+)`", cells[0]), cells))
    return found


def problems(text: str) -> list[str]:
    """Everything wrong with the reference ``text``, against the code."""
    out: list[str] = []
    tight = tightenable()
    seen: dict[str, int] = {}
    for section, names, cells in rows(text):
        if section not in SECTIONS:
            out.append(f"a row under an unknown heading {section!r}: {names}")
        for name in names:
            seen[name] = seen.get(name, 0) + 1
            if name not in FIELDS:
                out.append(f"{name} is not an init() parameter")
        defaults = [d.strip().strip("`") for d in cells[2].split(" / ")]
        if len(defaults) == len(names):
            for name, shown in zip(names, defaults):
                if name in FIELDS and shown.replace("_", "") != default_text(FIELDS[name]).replace("_", ""):
                    out.append(f"{name}: default shown {shown!r}, the code says {default_text(FIELDS[name])!r}")
        else:
            out.append(f"{names}: {len(defaults)} defaults for {len(names)} names")
        verdicts = {"yes" if n in tight else "no" for n in names if n in FIELDS}
        if len(verdicts) != 1 or cells[3].strip() not in verdicts:
            out.append(f"{names}: the plane column says {cells[3].strip()!r}, the engine says {sorted(verdicts)}")
    for name in FIELDS:
        if seen.get(name, 0) != 1:
            out.append(f"{name} appears {seen.get(name, 0)} times, not once")
    headings = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
    if headings != SECTIONS:
        out.append(f"the sections are {headings}, not {SECTIONS}")
    return out


def test_every_init_parameter_appears_exactly_once_with_its_real_default_and_plane_column():
    assert problems(DOC.read_text()) == []


def test_init_takes_exactly_the_fields_of_the_config_it_builds():
    """``init(**kwargs)`` is ``GuardrailConfig(**kwargs)``: an unknown name is a TypeError, a known one is accepted."""
    with pytest.raises(TypeError):
        GuardrailConfig(definitely_not_a_parameter=1)
    for name, field in FIELDS.items():
        if field.default is not dataclasses.MISSING and name not in ("api_key",):
            GuardrailConfig(**{name: field.default})  # accepted
    assert len(FIELDS) == 90


def test_a_dropped_row_is_caught():
    text = DOC.read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `max_steps`"))
    broken = text.replace(row + "\n", "")
    assert any("max_steps appears 0 times" in p for p in problems(broken))


def test_an_invented_row_is_caught():
    text = DOC.read_text()
    broken = text.replace("## Privacy\n", "## Privacy\n\n| `no_such_parameter` | `bool` | `False` | no | invented |\n", 1)
    assert any("no_such_parameter is not an init() parameter" in p for p in problems(broken))


def test_a_wrong_default_and_a_wrong_plane_cell_are_caught():
    text = DOC.read_text()
    wrong_default = re.sub(r"(\| `spike_confirm` \| [^|]+\| )`2`", r"\1`3`", text)
    assert any("spike_confirm: default shown '3'" in p for p in problems(wrong_default))
    wrong_plane = re.sub(r"(\| `budget_usd` \| .*?\| `None` \| )yes", r"\1no", text, count=1)
    assert any("budget_usd" in p and "plane column" in p for p in problems(wrong_plane))


def test_a_duplicated_parameter_is_caught():
    text = DOC.read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `max_steps`"))
    assert any("max_steps appears 2 times" in p for p in problems(text.replace(row, row + "\n" + row)))
