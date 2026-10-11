"""The ``runbound`` this suite imports is this checkout's, with no ghost sub-packages (V3).

Deleting a package leaves its ``__pycache__/`` behind (it is git-ignored), and a directory with no
``__init__.py`` still imports, as a namespace package. ``import runbound.wrappers`` then succeeds long
after S1 removed it, and an unrelated test fails obscurely. These tests fail first and say where.
"""

import importlib.util
from pathlib import Path

import runbound

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = _ROOT / "runbound"

#: Packages and modules removed from 0.11.x: none may resolve (mirrors test_docs_no_removed_keywords).
REMOVED = ("wrappers", "autowrap", "circuit", "pricing", "detectors", "ladder", "demo")


def _ghosts(base: Path) -> list[Path]:
    """Directories under ``base`` that import as namespace packages: no ``__init__.py``, not a cache."""
    return sorted(
        path for path in base.rglob("*")
        if path.is_dir() and "__pycache__" not in path.parts and not path.name.startswith(".")
        and not (path / "__init__.py").exists() and any(path.glob("*"))
        and path != _PACKAGE
    )


def test_runbound_is_imported_from_this_checkout():
    imported = Path(runbound.__file__).resolve().parent
    assert imported == _PACKAGE, (
        f"the suite imports runbound from {imported}, not from {_PACKAGE}: a stale editable install. "
        "Run scripts/dev.sh to reinstall -e ./runbound-sdk in this worktree's venv."
    )


def test_runbound_is_one_regular_package():
    assert list(runbound.__path__) == [str(_PACKAGE)], (
        f"runbound.__path__ is {list(runbound.__path__)}: another directory named runbound is on sys.path "
        "(a leftover install or checkout) and is merged into the package."
    )


def test_no_stale_namespace_package_directory_in_the_tree():
    ghosts = _ghosts(_PACKAGE)
    assert not ghosts, (
        "directories under runbound/ with no __init__.py import as namespace packages; delete the "
        f"leftovers (usually just a __pycache__): {[str(g.relative_to(_ROOT)) for g in ghosts]}"
    )


def test_removed_packages_do_not_resolve():
    found = {}
    for name in REMOVED:
        spec = importlib.util.find_spec(f"runbound.{name}")
        if spec is not None:
            found[name] = list(spec.submodule_search_locations or [spec.origin])
    assert not found, f"removed names still import: {found} (delete these paths)"
