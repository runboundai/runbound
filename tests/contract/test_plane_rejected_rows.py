"""A served row the plane's own validator would refuse is "cannot interpret safely", never "no rule" (floor F2; register C2, C9, C19).

The 18 bodies are the ones the plane's suites show it refusing with 400 (``tests/legacy/legacy_script.PLANE_REJECTED``, formerly O-LEG-ROW-600..617). The other three (618..620) name an argument the tool does not declare; the SDK does not check that (display, not authorisation), so they keep their legacy outcome.
Fed to the SDK as if a plane had served them enforced, each refuses ITS tool with ``policy_unsupported`` and no other tool.
"""

import pytest

import runbound
from contract_support import Clock, engine_on_link, outcome_of
from runbound.plane_types import HelloReply
from tests.legacy.legacy_script import MOVED_TO_CONTRACT, PLANE_REJECTED

pytestmark = [pytest.mark.contract(item="§4d floor")]


@pytest.mark.parametrize("fields", [fields for fields, _source in PLANE_REJECTED[:MOVED_TO_CONTRACT]], ids=[f"O-LEG-ROW-{600 + i}" for i in range(MOVED_TO_CONTRACT)])
def test_a_body_the_plane_would_refuse_refuses_its_tool_only(tmp_path, fields):
    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply())
    shared._set_rows([{"tool": "refund", "state": "enforced", **fields}], "h")

    @runbound.tool(effects={"read"}, reviewed=True)
    def refund(amount: int = 0):
        return "ran"

    @runbound.tool(effects={"read"}, reviewed=True)
    def lookup(amount: int = 0):
        return "ran"

    assert outcome_of(refund) == "refused:policy_unsupported"
    assert outcome_of(lookup) == "ran"
