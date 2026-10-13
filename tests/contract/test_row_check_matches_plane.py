"""The SDK reads a served row only when the plane's own validator would have served it (floor F2; register C2, C9, C19).

``fixtures/row_check.json`` holds ~1200 row bodies (every field's valid, edge and wrong-typed values, pairs of fields, and headline /
identifies against declared parameters) with what the plane's validator said about each. ``runbound._row_check`` must agree on all of
them, with one deliberate difference: the plane also refuses a headline or identifies that names an argument the tool does not declare, and
the SDK does not (that is display, not authorisation; Apollo's ruling on TR2-O1 item 3), so those cases are held to grammar only. The file
is kept equal to the plane by ``tests-workspace/test_row_check_fixture_is_the_planes.py``.
"""

import json
import pathlib

import pytest

from runbound import _row_check as check

pytestmark = [pytest.mark.contract(item="§4d floor")]

CASES = json.loads((pathlib.Path(__file__).parent / "fixtures" / "row_check.json").read_text())["cases"]


def _sdk_accepts(body: dict) -> bool:
    return check.problem({"tool": "t", **body}) is None


def test_every_case_is_decided_as_the_plane_decides_it():
    assert len(CASES) > 1000
    wrong = [c for c in CASES if c["params"] is None and _sdk_accepts(c["body"]) != c["plane_accepts"]]
    assert not wrong, wrong[:5]


def test_a_headline_or_identifies_naming_an_argument_the_tool_lacks_never_refuses_the_tool():
    declared = [c for c in CASES if c["params"] is not None]
    assert declared
    for case in declared:
        # held to grammar alone: the same body is judged the same whatever parameters the tool declares
        same = [c for c in declared if c["body"] == case["body"]]
        assert len({_sdk_accepts(c["body"]) for c in same}) == 1
    assert _sdk_accepts({"headline": "Refund {args.order}"}) and _sdk_accepts({"identifies": ["customer"]})
    assert not _sdk_accepts({"headline": "Refund {amount}"}) and not _sdk_accepts({"identifies": ["_private"]})


def test_a_row_the_sdk_cannot_read_is_never_a_row_that_allows():
    refused = [c for c in CASES if not c["plane_accepts"] and c["params"] is None]
    assert refused and all(not _sdk_accepts(c["body"]) for c in refused)


def test_an_unknown_key_and_a_non_object_row_are_unreadable():
    assert check.problem({"tool": "t", "geo_fence": "eu"})
    assert check.problem("junk")
