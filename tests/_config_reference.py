"""What the configuration reference's columns are checked against: the code (shared by the generator and the test).

``default_text`` renders a ``GuardrailConfig`` field's default the way the reference prints it, and ``tightenable``
reads which ``init()`` fields the plane's Controls can tighten from the engine's own source: the fields its
``_code_*`` snapshots take from ``config`` are exactly the values a Controls merge tightens against.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "runbound" / "engine.py"
#: ``init(capabilities=...)`` is read with ``getattr`` (it may be absent on a bare config-like object), so the
#: pattern below cannot see it; the line it is read on is named here and checked to exist.
CAPABILITIES_LINE = 'self._code_capabilities: dict = dict(getattr(config, "capabilities", None) or {})'


def default_text(field: dataclasses.Field) -> str:
    """A field's default as the reference prints it: ``None``, ``True``, ``"warn"``, ``5.0``, ``()``, ``{}``."""
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


def tightenable() -> set[str]:
    """The ``init()`` fields a plane's Controls can tighten, from the engine's ``_code_*`` snapshots."""
    source = ENGINE.read_text()
    start = source.index("self._code_limits: dict = {")
    end = source.index("self._controls_lock = threading.Lock()")
    block = source[start:end]
    assert CAPABILITIES_LINE in block, "the engine no longer reads capabilities the way this reference expects"
    return set(re.findall(r"config\.([a-z_0-9]+)", block)) | {"capabilities"}
