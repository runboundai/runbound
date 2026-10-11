"""The configuration reference names every ``init()`` parameter exactly once, with its real type and default.

``runbound.init(**kwargs)`` builds a ``RunboundConfig``, so its fields ARE the parameters. The reference groups them
by purpose; this test reads the page and checks, row by row, against the code: every parameter appears in exactly one
row under a known heading, no row names a parameter that does not exist, each Type is the field's annotation, each
Default is the field's own default, and each "Plane can tighten" cell says what the engine's merge of the plane's
policy does (see ``tests/_config_reference.py``). The check is a function, so a dropped row, an invented one, a wrong
default, a wrong type and a wrong plane cell can each be shown to fail it.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from runbound.config import RunboundConfig

from _config_reference import default_text, tightenable, type_text

DOC = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration.md"
SECTIONS = ["The plane", "Rules and hooks", "Approvals", "Results", "Telemetry and privacy", "Local"]
HEADER = "| Name | Type | Default | Plane can tighten | Meaning |"
FIELDS = {f.name: f for f in dataclasses.fields(RunboundConfig) if not f.name.startswith("_")}


def _cell(text: str) -> str:
    """A table cell as plain text: backticks off, ``\\|`` back to ``|``."""
    return text.strip().strip("`").replace("\\|", "|")


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
        if len(cells) != 5:
            out.append(f"{names}: {len(cells)} cells, not 5")
            continue
        if len(names) != 1:
            out.append(f"a row must name exactly one parameter, this one names {names}")
            continue
        (name,) = names
        seen[name] = seen.get(name, 0) + 1
        if name not in FIELDS:
            out.append(f"{name} is not an init() parameter")
            continue
        field = FIELDS[name]
        if _cell(cells[1]) != type_text(field):
            out.append(f"{name}: type shown {_cell(cells[1])!r}, the code says {type_text(field)!r}")
        if _cell(cells[2]).replace("_", "") != default_text(field).replace("_", ""):
            out.append(f"{name}: default shown {_cell(cells[2])!r}, the code says {default_text(field)!r}")
        verdict = "yes" if name in tight else "no"
        if cells[3].strip() != verdict:
            out.append(f"{name}: the plane column says {cells[3].strip()!r}, the engine says {verdict!r}")
        if not cells[4].strip():
            out.append(f"{name}: the Meaning cell is empty")
    for name in FIELDS:
        if seen.get(name, 0) != 1:
            out.append(f"{name} appears {seen.get(name, 0)} times, not once")
    headings = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
    if headings != SECTIONS:
        out.append(f"the sections are {headings}, not {SECTIONS}")
    if text.count(HEADER) != len(SECTIONS):
        out.append(f"expected one {HEADER!r} table per section")
    return out


def _row(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"| `{name}`"))


def test_every_init_parameter_appears_exactly_once_with_its_real_type_default_and_plane_column():
    assert problems(DOC.read_text()) == []


def test_init_takes_exactly_the_fields_of_the_config_it_builds():
    """``init(**kwargs)`` is ``RunboundConfig(**kwargs)``: an unknown name is a TypeError, a known one is accepted."""
    with pytest.raises(TypeError):
        RunboundConfig(definitely_not_a_parameter=1)
    for name, field in FIELDS.items():
        if field.default is not dataclasses.MISSING:
            RunboundConfig(**{name: field.default})  # accepted
    assert len(FIELDS) == 25  # the 25 init() fields; _plane_mode is private


def test_only_the_tool_policy_is_tightened_by_the_plane():
    assert tightenable() == {"tool_policy"}


def test_a_dropped_row_is_caught():
    text = DOC.read_text()
    broken = text.replace(_row(text, "max_actions_per_run") + "\n", "")
    assert any("max_actions_per_run appears 0 times" in p for p in problems(broken))


def test_an_invented_row_is_caught():
    text = DOC.read_text()
    invented = "| `no_such_parameter` | `bool` | `False` | no | invented |"
    broken = text.replace(_row(text, "export_events"), _row(text, "export_events") + "\n" + invented, 1)
    assert any("no_such_parameter is not an init() parameter" in p for p in problems(broken))


def test_a_wrong_default_is_caught():
    text = DOC.read_text()
    wrong = re.sub(r"(\| `max_sessions` \| [^|]+\| )`10000`", r"\1`5`", text)
    assert wrong != text
    assert any("max_sessions: default shown '5'" in p for p in problems(wrong))
    wrong = re.sub(r"(\| `on_halt` \| [^|]+\| )`\"raise\"`", r'\1`"warn"`', text)
    assert wrong != text
    assert any("on_halt: default shown" in p for p in problems(wrong))


def test_a_wrong_type_is_caught():
    text = DOC.read_text()
    wrong = text.replace("| `hook_timeout_s` | `float \\| None` |", "| `hook_timeout_s` | `float` |")
    assert wrong != text
    assert any("hook_timeout_s: type shown 'float'" in p for p in problems(wrong))


def test_a_wrong_plane_cell_is_caught():
    text = DOC.read_text()
    wrong = re.sub(r"(\| `tool_policy` \| .*?\| `None` \| )yes", r"\1no", text, count=1)
    assert wrong != text
    assert any("tool_policy" in p and "plane column" in p for p in problems(wrong))
    wrong = re.sub(r"(\| `capabilities` \| .*?\| `None` \| )no", r"\1yes", text, count=1)
    assert wrong != text
    assert any("capabilities" in p and "plane column" in p for p in problems(wrong))


def test_a_duplicated_parameter_is_caught():
    text = DOC.read_text()
    row = _row(text, "max_actions_per_run")
    assert any("max_actions_per_run appears 2 times" in p for p in problems(text.replace(row, row + "\n" + row)))


def test_a_row_under_an_unknown_heading_is_caught():
    text = DOC.read_text()
    broken = text.replace("## Local\n", "## Miscellaneous\n", 1)
    found = problems(broken)
    assert any("unknown heading 'Miscellaneous'" in p for p in found)
    assert any("the sections are" in p for p in found)
