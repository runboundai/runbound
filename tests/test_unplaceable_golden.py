"""The SDK half of the shared golden fixture ``tests/fixtures/unplaceable_golden.json`` (Gate B / A5 release blocker): a set of classes with one this reader does not know is judged as
its KNOWN classes plus ``unclassified``, strictest wins per key, fail mode closed if any contributing key is closed. The plane's ``policy/defaults.resolve`` gives the same answers
on the same file; do not edit the fixture to make a side pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runbound import lines

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "unplaceable_golden.json").read_text())


def defaults_of(name: str) -> "lines.Defaults":
    return lines.parse_defaults({"revision": 1, "state": "enforced", "defaults": GOLDEN["defaults"][name]})


def case_id(case: dict) -> str:
    return f'{case["defaults"]}:{"+".join(case["effects"]) or "none"}'


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=case_id)
def test_the_sdk_resolves_each_golden_case_exactly_as_the_fixture_says(case):
    got = lines.resolve(defaults_of(case["defaults"]), frozenset(case["effects"]))
    assert (got.decision, got.fail_mode, sorted(got.keys)) == (case["decision"], case["fail_mode"], sorted(case["keys"]))


def test_a_set_of_only_unknown_classes_no_longer_raises():
    """It used to give no keys at all, and ``max()`` of an empty sequence raised on the call path."""
    got = lines.resolve(defaults_of("standard"), frozenset({"teleport"}))
    assert got.keys == ("unclassified",) and got.decision == "ask" and got.fail_mode == "closed"


def test_an_unknown_class_is_never_looser_than_the_known_tag_it_came_with():
    for name in ("standard", "unclassified_looser"):
        defaults = defaults_of(name)
        order = {"allow": 0, "ask": 1, "deny": 2}
        for stated in (["read"], ["financial"], ["destructive", "write"]):
            plain, with_unknown = lines.resolve(defaults, frozenset(stated)), lines.resolve(defaults, frozenset([*stated, "teleport"]))
            assert order[with_unknown.decision] >= order[plain.decision]
            assert not (with_unknown.fail_mode == "open" and plain.fail_mode == "closed")
