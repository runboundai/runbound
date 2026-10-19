"""Conditions and repetition on the call path (TR3-O; WIRE-TR3 §1, §5): a line's ``when`` / ``unless`` read the call's final arguments and the context.

The strictest MATCHING line decides; a line that is unknown (a missing or wrong-typed argument) and could make the decision stricter refuses the call
(``condition_unevaluable``, K1); limits and approval of a line count only while it matches; a repetition control refuses the call that makes the
(max+1)-th identical one in the session.
"""

import pytest

import runbound
from runbound import api
from test_policy_rows import FakeShared, row, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def cond(arg, op, value=None):
    return {"arg": arg, "op": op, "value": value}


def line(ident, decision, when=None, unless=None, approval=None, limits=None, repetition=None):
    return {"id": ident, "decision": decision, "approval": approval, "limits": limits,
            "when": None if when is None else {"all": when}, "unless": unless, "repetition": repetition}


def rule(tool, *lines, otherwise="inherit"):
    return row(tool, lines=list(lines), otherwise=otherwise)


def make(name="pay"):
    ran = []

    @runbound.tool(name=name, effects={"read"}, reviewed=True)
    def pay(amount: float = 0, region: str = "us"):
        ran.append(amount)
        return "ok"

    return pay, ran


def reason_of(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except runbound.ExecutionRefused as exc:
        return exc.reason
    return "ran"


def asked(shared):
    return [c for c in shared.calls if c[1] == "/v1/approvals"]


def test_a_rule_that_does_not_match_the_call_lets_it_run_and_one_that_matches_decides():
    shared = start(FakeShared([rule("pay", line("L1", "ask", [cond("amount", ">", 1000)],
                                                  approval={"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []}))]))
    pay, ran = make()
    assert pay(amount=50) == "ok" and asked(shared) == []  # IF amount > 1000 -> ask: not matched
    assert pay(amount=5000) == "ok"  # matched: asks (the fake plane approves)
    (call,) = asked(shared)
    assert call[2]["timeout_s"] == 30


def test_the_strictest_matching_line_decides_and_not_the_first():
    start(FakeShared([rule("pay", line("L1", "ask", [cond("amount", ">", 10)]), line("L2", "deny", [cond("region", "equals", "eu")]),
                      line("L3", "allow", [cond("amount", ">", 0)]))]))
    pay, _ = make()
    assert reason_of(pay, amount=50, region="eu") == "tool_blocked"  # deny beats ask beats allow among the matching lines
    assert reason_of(pay, amount=50, region="us") == "ran"  # ask (approved) beats allow


def test_an_unknown_line_that_could_be_stricter_refuses_the_call_condition_unevaluable():
    start(FakeShared([rule("pay", line("L1", "deny", [cond("missing_arg", "equals", "x")]))]))
    pay, ran = make()
    assert reason_of(pay, amount=1) == "condition_unevaluable" and ran == []


def test_an_unknown_allow_line_never_matches_and_never_refuses():
    start(FakeShared([rule("pay", line("L1", "allow", [cond("missing_arg", "equals", "x")]))]))
    pay, _ = make()
    assert pay(amount=1) == "ok"


def test_unless_removes_a_match():
    start(FakeShared([rule("pay", line("L1", "deny", [cond("amount", ">", 10)], unless=cond("region", "equals", "us")))]))
    pay, _ = make()
    assert reason_of(pay, amount=50, region="us") == "ran" and reason_of(pay, amount=50, region="eu") == "tool_blocked"


def test_limits_of_a_line_apply_only_while_it_matches():
    start(FakeShared([rule("pay", line("L1", "allow", [cond("amount", ">", 100)], limits={"max_calls_per_session": 1, "quota": None}))]))
    pay, _ = make()
    assert pay(amount=1) == "ok" and pay(amount=1) == "ok"  # the cap is not in force for small amounts
    assert pay(amount=500) == "ok" and reason_of(pay, amount=500) == "session_max_calls"


def test_a_row_with_a_condition_that_no_line_matches_falls_to_otherwise():
    start(FakeShared([rule("pay", line("L1", "allow", [cond("amount", ">", 100)]), otherwise="deny")]))
    pay, _ = make()
    assert reason_of(pay, amount=1) == "tool_blocked" and pay(amount=500) == "ok"


def test_the_conditions_read_the_arguments_the_tool_will_get_by_name_even_when_passed_positionally():
    start(FakeShared([rule("pay", line("L1", "deny", [cond("region", "is_one_of", ["eu", "uk"])]))]))
    pay, _ = make()
    assert reason_of(pay, 5, "eu") == "tool_blocked" and reason_of(pay, 5, "us") == "ran"
    assert reason_of(pay, 5, region="uk") == "tool_blocked"


def test_a_context_condition_reads_the_trusted_context(monkeypatch):
    monkeypatch.setenv("RUNBOUND_ENVIRONMENT", "prod")
    start(FakeShared([rule("pay", {"id": "L1", "decision": "deny", "approval": None, "limits": None,
                                   "when": {"all": [{"ctx": "environment", "op": "equals", "value": "prod"}]}})]))
    pay, _ = make()
    assert reason_of(pay, amount=1) == "tool_blocked"
    monkeypatch.setenv("RUNBOUND_ENVIRONMENT", "dev")
    assert reason_of(pay, amount=1) == "ran"


def test_the_record_names_the_lines_that_matched_and_the_unknown_ones():
    start(FakeShared([rule("pay", line("L1", "ask", [cond("amount", ">", 10)]), line("L2", "allow", [cond("missing", "equals", "x")]))]))
    pay, _ = make()
    pay(amount=50)
    record = runbound.tool_results()[-1]
    assert record["tool"] == "pay"
    from runbound.engine import _RECORD  # noqa: F401  (the per-call record lives in the engine; the refusal test below reads it off the exception)

    with pytest.raises(runbound.ExecutionRefused) as refused:
        start(FakeShared([rule("pay", line("L1", "deny", [cond("amount", ">", 10)]), line("L2", "deny", [cond("missing", "equals", "x")]))]))
        make()[0](amount=50)
    evaluation = refused.value.decision.evaluation
    assert evaluation["matched_rule_ids"] == ["pay#L1"] and "row:L1" in evaluation["contributors"]


# --- repetition ---------------------------------------------------------------------------------------------------------


def test_the_call_that_makes_the_max_plus_first_identical_one_in_a_session_is_refused():
    start(FakeShared([rule("pay", line("L1", "allow", repetition={"max": 2, "scope": "session"}))]))
    pay, ran = make()
    with runbound.session("s1"):
        assert pay(amount=7) == "ok" and pay(amount=7) == "ok"
        assert reason_of(pay, amount=7) == "repetition_exceeded"
        assert pay(amount=8) == "ok"  # another set of arguments has its own count
    with runbound.session("s2"):
        assert pay(amount=7) == "ok"  # another session starts at zero


def test_a_refused_call_gives_its_repetition_count_back():
    start(FakeShared([rule("pay", line("L1", "deny", [cond("region", "equals", "eu")], repetition={"max": 1, "scope": "session"}),
                           line("L2", "allow", repetition={"max": 1, "scope": "session"}))]))
    pay, _ = make()
    with runbound.session("s1"):
        assert reason_of(pay, amount=7, region="eu") == "tool_blocked"  # refused by the deny line, not counted
        assert reason_of(pay, amount=7, region="us") == "ran"  # the one allowed repetition is still available


def test_a_dry_run_repetition_only_records():
    start(FakeShared([row("pay", state="dry_run", lines=[line("L1", "allow", repetition={"max": 1, "scope": "session"})], otherwise="inherit")]))
    pay, ran = make()
    with runbound.session("s1"):
        assert pay(amount=1) == "ok" and pay(amount=1) == "ok" and len(ran) == 2
    assert "would_refuse:repetition_exceeded" in runbound.tool_results()[-1]["tags"]


def test_a_dry_run_candidate_records_would_ask_and_would_deny_and_decides_nothing():
    candidate = {"version": 3, "state": "dry_run", "otherwise": "inherit",
                 "lines": [line("L1", "ask", [cond("amount", ">", 1000)]), line("L2", "deny", [cond("region", "equals", "eu")])]}
    shared = start(FakeShared([row("pay", lines=[], otherwise="inherit", candidate=candidate)]))
    pay, _ = make()
    assert pay(amount=5000) == "ok" and asked(shared) == []
    assert "would_ask:approval" in runbound.tool_results()[-1]["tags"]
    assert pay(amount=1, region="eu") == "ok"
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]


def test_a_conditional_deny_names_the_line_it_matched_and_does_not_say_the_tool_is_blocked():
    """DRACO F8: ``amount > 200 -> deny`` refused a 249 with "tool is blocked by policy", though the tool is not blocked (a 19 ran). The code stays
    ``tool_blocked`` (callers switch on it); the message names the line that decided."""
    start(FakeShared([rule("pay", line("L1", "deny", [cond("amount", ">", 200)]))]))
    pay, ran = make()
    with pytest.raises(runbound.ExecutionRefused) as raised:
        pay(amount=249)
    assert raised.value.reason == "tool_blocked"
    message = str(raised.value)
    assert "L1" in message and "condition" in message
    assert "is blocked by policy" not in message
    assert pay(amount=19) == "ok" and ran == [19]
