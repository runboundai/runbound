"""The pure contract has exactly one importer: ``runbound/lines.py`` is the sanctioned site (TR2-O2).

R0 imported nothing from ``_rules`` outside the package; R2 wires the evaluator, the approval composition and the policy-state model into
the call path through ``runbound.lines`` only. Every other module reaches them through it, so a change to how the SDK meets the contract is
a change to one file, and the contract modules can stay pure (no I/O, no clock, no globals).
"""

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.contract(item="TR2-O2 AC2")]

ROOT = pathlib.Path(__file__).resolve().parents[2] / "runbound"


def _imports_rules(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if "_rules" in a.name.split(".")]
        elif isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            if "_rules" in parts or any(a.name == "_rules" for a in node.names):
                found.append(node.module or "." * node.level)
    return found


def test_only_lines_py_outside_the_rules_package_imports_it():
    offenders = {}
    for path in ROOT.rglob("*.py"):
        if "_rules" in path.relative_to(ROOT).parts:
            continue
        hits = _imports_rules(path)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert set(offenders) == {"lines.py"}, offenders


def test_the_rules_package_imports_nothing_from_the_rest_of_the_sdk_but_itself():
    for path in (ROOT / "_rules").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level >= 2:
                raise AssertionError(f"{path.name} reaches outside _rules: {node.module}")
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("runbound"):
                raise AssertionError(f"{path.name} imports {node.module}")


def test_the_rules_package_is_pure_no_io_clock_or_globals():
    banned = {"time", "datetime", "os", "socket", "threading", "random", "urllib", "pathlib", "open"}
    for path in (ROOT / "_rules").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not banned & {a.name.split(".")[0] for a in node.names}, (path.name, node.names)
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] not in banned, (path.name, node.module)
            assert not isinstance(node, ast.Global), path.name
