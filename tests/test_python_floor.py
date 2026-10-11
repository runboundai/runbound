"""Python 3.10 is the floor: the metadata says so, and an older interpreter gets a message that names it."""

import importlib
import re
import sys
from pathlib import Path

import pytest

import runbound

_ROOT = Path(__file__).resolve().parent.parent


def test_requires_python_is_3_10():
    assert re.search(r'requires-python\s*=\s*">=3\.10"', (_ROOT / "pyproject.toml").read_text())


def test_the_readme_states_the_floor():
    assert "Python 3.10 or newer" in (_ROOT / "README.md").read_text()


def test_importing_on_python_3_9_raises_an_import_error_that_names_the_floor(monkeypatch):
    saved = {n: m for n, m in sys.modules.items() if n == "runbound" or n.startswith("runbound.")}
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))
    try:
        for name in saved:
            del sys.modules[name]
        with pytest.raises(ImportError, match=r"Python 3\.10 or newer.*Python 3\.9"):
            importlib.import_module("runbound")
    finally:
        for name in [n for n in sys.modules if n == "runbound" or n.startswith("runbound.")]:
            del sys.modules[name]
        sys.modules.update(saved)
    assert runbound.init  # the real package is back
