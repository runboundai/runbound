"""Decision lines in the call path (TR2-O2): what a worker that reads ``lines`` does with a served row.

R2 lines are unconditional, so the strictest decision wins (deny > ask > allow), the limits of every line apply, ``otherwise`` decides only
with no lines, and a row whose legacy view is stricter than its lines, or which this SDK cannot read, refuses its tool (``policy_unsupported``).
"""

import pytest

import runbound
from runbound import api
from runbound.plane_types import HelloReply
from test_policy_rows import FakeShared, row, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def line(ident, decision, approval=None, limits=None):
    return {"id": ident, "decision": decision, "approval": approval, "limits": limits}


def lines_row(tool, lines, otherwise="inherit", **legacy):
    return row(tool, lines=lines, otherwise=otherwise, **legacy)


def make_tool(name="act", effects=("read",), **kwargs):
    ran = []

    @runbound.tool(name=name, effects=set(effects), reviewed=True, **kwargs)
    def act():
        ran.append(1)
        return "ok"

    return act, ran


def reason_of(fn):
    try:
        fn()
    except runbound.ExecutionRefused as exc:
        return exc.reason
    return "ran"


def test_the_strictest_line_wins():
    start(FakeShared([lines_row("act", [line("L1", "allow"), line("L2", "ask", {"approval_mode": None, "approval_timeout_s": None, "approvers": []}),
                                        line("L3", "deny")], blocked=True)]))
    act, ran = make_tool()
    assert reason_of(act) == "tool_blocked" and ran == []


def test_an_allow_line_and_no_other_lets_the_call_run():
    start(FakeShared([lines_row("act", [line("L1", "allow")])]))
    act, ran = make_tool()
    assert act() == "ok"


def test_the_limits_of_every_line_apply_the_smallest_cap_wins():
    start(FakeShared([lines_row("act", [line("L1", "allow", limits={"max_calls_per_session": 5, "quota": None}),
                                        line("L2", "ask", {"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []},
                                             {"max_calls_per_session": 1, "quota": None})],
                                approval_required=True, max_calls_per_session=1)]))
    shared = api._ENGINE.shared
    assert shared.row_for("act").max_calls == 1 and shared.row_for("act").approval_required
    assert shared.row_for("act").approval_timeout_s == 30  # the ask line's own requirement


def test_a_quota_on_two_lines_with_the_same_window_takes_the_smaller_limit():
    quota = lambda n: {"limit": n, "window": "hour", "per": "service"}  # noqa: E731
    start(FakeShared([lines_row("act", [line("L1", "allow", limits={"max_calls_per_session": None, "quota": quota(9)}),
                                        line("L2", "ask", {"approval_mode": None, "approval_timeout_s": None, "approvers": []},
                                             {"max_calls_per_session": None, "quota": quota(4)})], approval_required=True, quota=quota(4))]))
    assert api._ENGINE.shared.row_for("act").quota["limit"] == 4


def test_otherwise_decides_only_when_there_are_no_lines():
    start(FakeShared([lines_row("denied", [], otherwise="deny", blocked=True), lines_row("open", [], otherwise="inherit"),
                      lines_row("asked", [], otherwise="ask", approval_required=True)]))
    denied, _ = make_tool("denied")
    assert reason_of(denied) == "tool_blocked"
    open_, _ = make_tool("open")
    assert open_() == "ok"
    shared = api._ENGINE.shared
    assert shared.row_for("asked").approval_required and not shared.row_for("asked").blocked


def test_a_legacy_view_stricter_than_the_lines_refuses_the_tool():
    """WIRE-TR2 §3 [Apollo a]: a corrupted lines payload can never be looser than the legacy view."""
    start(FakeShared([lines_row("act", [line("L1", "allow")], blocked=True)]))
    act, ran = make_tool()
    assert reason_of(act) == "policy_unsupported" and ran == []
    unsupported = api._ENGINE.shared.rows["act"]
    assert unsupported.unsupported_code == "legacy_view_mismatch"


@pytest.mark.parametrize("bad, code", [
    ([{"id": "L1", "decision": "allow", "when": "x"}], "row_unparseable"),
    ([{"id": "L1", "decision": "allow", "foo": 1}], "unknown_restriction_key"),
    ([{"id": "L1", "decision": "maybe"}], "invalid_body"),
    ([{"id": "L1", "decision": "allow"}, {"id": "L1", "decision": "deny"}], "invalid_body"),
    (["junk"], "row_unparseable"),
    ("not a list", "invalid_body"),
    ([{"id": "L1", "decision": "allow", "limits": {"quota": None, "burst": 3}}], "unknown_restriction_key"),
    ([{"id": "L1", "decision": "allow", "limits": {"quota": {"limit": 3, "window": "hour", "per": "service"}}},
      {"id": "L2", "decision": "ask", "limits": {"quota": {"limit": 3, "window": "day", "per": "service"}}}], "invalid_body"),
])
def test_lines_the_sdk_cannot_read_refuse_their_tool_and_say_why(bad, code):
    start(FakeShared([lines_row("act", bad), lines_row("fine", [line("L1", "allow")])]))
    act, _ = make_tool()
    fine, _ = make_tool("fine")
    assert reason_of(act) == "policy_unsupported" and fine() == "ok"
    assert api._ENGINE.shared.rows["act"].unsupported_code == code


def test_a_code_rule_still_wins_over_a_looser_line():
    start(FakeShared([lines_row("act", [line("L1", "allow")])]))

    @runbound.tool(name="act", effects={"read"}, blocked=True)
    def blocked():
        return "no"

    with pytest.raises(runbound.PolicyViolation):
        blocked()


def test_a_code_approval_and_an_ask_line_are_two_requirements_and_the_wait_is_the_shortest():
    """TPD §4b step 6 through the engine's own fold: the code's 300 s and the line's 30 s are separate requirements; the call waits 30 s."""
    from runbound import policy_rows

    ask = {"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []}
    parsed = policy_rows.parse([lines_row("act", [line("L1", "ask", ask)], approval_required=True, approval_timeout_s=30)])["act"]
    assert [r.source for r in parsed.requirements] == ["row:L1"] and parsed.requirements[0].wait_s == 30
    spec = policy_rows.approval_spec(parsed, type("Code", (), {"mode": "sync", "timeout_s": 300.0, "names": ()})(), (), True, _Spec)
    assert spec.timeout_s == 30.0
    longer = policy_rows.parse([lines_row("act", [line("L1", "ask", {**ask, "approval_timeout_s": 900})], approval_required=True)])["act"]
    spec = policy_rows.approval_spec(longer, type("Code", (), {"mode": "sync", "timeout_s": 300.0, "names": ()})(), (), True, _Spec)
    assert spec.timeout_s == 300.0  # the code's own wait is shorter


class _Spec:
    def __init__(self, mode, timeout_s, names, requirement=None):
        self.mode, self.timeout_s, self.names, self.requirement = mode, timeout_s, names, requirement
