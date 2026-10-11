"""What the configuration reference's columns are checked against: the code.

``default_text`` renders a ``RunboundConfig`` field's default the way the reference prints it, ``type_text`` its
annotation, and ``tightenable`` names the ``init()`` fields a connected plane can make stricter (only the tool policy:
the engine merges the plane's policy with the local one, and the merge can only remove freedom).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "runbound" / "engine.py"
#: The one place the plane's policy reaches the engine: ``merge(local, remote, ...)`` of the tool policy, which can only
#: remove freedom. The line is named here and checked to exist, so a change to how the plane's rules arrive fails here.
MERGE_LINE = "merged = merge(local, remote, remote_dry_run=dry_run)"

#: Module prefixes an annotation's text carries that the reference leaves off.
_PREFIXES = ("collections.abc.", "typing.", "runbound.policy.")


def default_text(field: dataclasses.Field) -> str:
    """A field's default as the reference prints it: ``None``, ``True``, ``"raise"``, ``5.0``, ``()``, ``{}``."""
    if field.default is not dataclasses.MISSING:
        value = field.default
    elif field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        value = field.default_factory()  # type: ignore[misc]
    else:
        return "required"
    return _render(value)


def _render(value) -> str:
    if isinstance(value, str):
        return '"' + value + '"'
    if isinstance(value, tuple):
        inner = ", ".join(_render(v) for v in value)
        return f"({inner}{',' if len(value) == 1 else ''})"
    return repr(value)


def type_text(field: dataclasses.Field) -> str:
    """A field's annotation as the reference prints it: ``str | None``, ``ToolPolicy | dict | None``, ``float``."""
    hint = field.type
    if isinstance(hint, type):
        return hint.__name__
    text = str(hint)
    for prefix in _PREFIXES:
        text = text.replace(prefix, "")
    return text.replace("NoneType", "None")


def tightenable() -> set[str]:
    """The ``init()`` fields a plane can tighten: the tool policy, merged tighten-only in the engine."""
    assert MERGE_LINE in ENGINE.read_text(), "the engine no longer merges the plane's policy the way this reference expects"
    return {"tool_policy"}
