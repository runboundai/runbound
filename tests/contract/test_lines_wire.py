"""The wire of decision lines, from the worker's side (TR2-O2; WIRE-TR2 §1, §2 and §10a): features and unsupported tools on every hello, halted tools in the reply."""

import pytest

import runbound
from runbound import api
from runbound.plane_types import HelloReply
from contract_support import Clock, engine_on_link, outcome_of

pytestmark = [pytest.mark.contract(item="TR2-O2 AC1")]


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def line(ident, decision, approval=None, limits=None):
    return {"id": ident, "decision": decision, "approval": approval, "limits": limits}


def lines_row(tool, lines, otherwise="inherit", **legacy):
    return {"tool": tool, "state": "enforced", "lines": lines, "otherwise": otherwise, **legacy}


def make_tool(name="act", effects=("read",)):
    ran = []

    @runbound.tool(name=name, effects=set(effects), reviewed=True)
    def act():
        ran.append(1)
        return "ok"

    return act, ran


def test_the_hello_reports_exactly_the_features_the_sdk_enforces_on_every_hello(tmp_path):
    shared = engine_on_link(tmp_path, clock=Clock())
    first = shared._hello_payload()
    shared.apply_hello(HelloReply())
    later = shared._hello_payload()
    for payload in (first, later):
        assert payload["policy_features"] == ["lines", "halt_tools_v1", "defaults_v1", "conditions_v1", "repetition_v1", "grants_v1", "acceptance_v1", "revisions_v1"]
        assert payload["policy_unsupported"] == []
    from runbound import lines

    assert tuple(first["policy_features"]) == lines.FEATURES  # the reported set is the implemented set, nothing more


def test_the_hello_lists_each_tool_it_cannot_read_with_a_reason(tmp_path):
    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply())
    shared._set_rows([lines_row("a", [line("L1", "allow", limits={"max_calls_per_session": None, "quota": None, "x": 1})]),
                      lines_row("b", [line("L1", "allow")], blocked=True), lines_row("c", [line("L1", "allow")])], "h")
    assert shared._hello_payload()["policy_unsupported"] == [{"tool": "a", "reason": "unknown_restriction_key"},
                                                              {"tool": "b", "reason": "legacy_view_mismatch"}]


def test_a_tool_the_plane_halted_is_refused_policy_halted_and_the_rest_run(tmp_path):
    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply(halted_tools=[{"tool": "act", "reasons": ["condition_unsupported"]}]))
    act, _ = make_tool()
    other, _ = make_tool("other")
    assert outcome_of(act) == "refused:policy_halted" and outcome_of(other) == "ran"
    assert not shared.halted()  # exactly this tool, not the fleet
    shared.apply_hello(HelloReply(halted_tools=[]))
    assert outcome_of(act) == "ran"


def test_a_halted_tool_survives_a_restart_while_the_plane_is_down(tmp_path):
    first = engine_on_link(tmp_path, clock=Clock())
    first.apply_hello(HelloReply(halted_tools=[{"tool": "act", "reasons": ["r"]}]))
    second = engine_on_link(tmp_path, clock=Clock())  # a restart, the plane silent
    act, _ = make_tool()
    assert second.halted_reasons("act") == ["r"] and outcome_of(act) == "refused:policy_halted"
