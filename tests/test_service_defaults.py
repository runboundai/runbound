"""Service defaults in the SDK (TR2-O3; WIRE-TR2b): the fallback for a tool no line decides.

Served as ``service_defaults: {revision, state, defaults}`` on the policy envelope to a worker that reported ``defaults_v1``. A tool with no
row, or a row that decides nothing, is decided by the default for its declared effects: the strictest of the operation's and the tags'; the
unclassified default for a tool with no effect; one approval requirement per asking key; fail mode closed if any contributing key is closed
(a bare write counts as unclassified for fail mode only).
"""

import pytest

import runbound
from runbound import api, lines
from runbound.shared import RemoteState
from test_policy_rows import FakeShared, row, start

DECISION = {"read": "allow", "write": "allow", "external": "allow", "financial": "ask", "destructive": "ask", "privileged": "ask", "unclassified": "ask"}
FAIL = {"read": "open", "write": "open", "external": "open", "financial": "closed", "destructive": "closed", "privileged": "closed", "unclassified": "closed"}


def served(state="enforced", revision=4, decision=None, fail_mode=None, approval=None):
    return {"revision": revision, "state": state, "defaults": {
        "decision": {**DECISION, **(decision or {})}, "fail_mode": {**FAIL, **(fail_mode or {})},
        "approval": approval if approval is not None else {k: {"approvers": [], "wait_s": 45} for k in ("financial", "destructive", "privileged", "unclassified")}}}


class Shared(FakeShared):
    def __init__(self, rows=(), defaults=None, **kw):
        super().__init__(rows, **kw)
        self.held, self.problem = None, None
        if defaults is not None:
            try:
                self.held = lines.parse_defaults(defaults)
            except lines.LineError as error:
                self.problem = (error.code, str(error))

    def defaults(self):
        return self.held

    def defaults_problem(self):
        return self.problem


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def make_tool(name, effects):
    ran = []

    @runbound.tool(name=name, effects=set(effects) if effects is not None else None, reviewed=effects is not None)
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


def asked(shared):
    return [c for c in shared.calls if c[1] == "/v1/approvals"]


def test_without_adopted_defaults_nothing_changes():
    shared = start(Shared())
    act, ran = make_tool("pay", {"financial"})
    assert act() == "ok" and ran == [1] and asked(shared) == []


def test_a_destructive_tool_with_no_row_asks_for_approval_with_the_defaults_wait():
    shared = start(Shared(defaults=served()))
    act, ran = make_tool("wipe", {"destructive"})
    assert act() == "ok"  # approved by the fake plane
    (call,) = asked(shared)
    assert call[2]["timeout_s"] == 45
    read, _ = make_tool("look", {"read"})
    assert read() == "ok" and len(asked(shared)) == 1  # read is allow


def test_a_tool_that_declares_nothing_takes_the_unclassified_default():
    shared = start(Shared(defaults=served()))
    act, _ = make_tool("mystery", None)
    act()
    assert len(asked(shared)) == 1


def test_the_strictest_of_the_operation_and_the_tags_decides():
    start(Shared(defaults=served(decision={"financial": "deny"})))
    act, ran = make_tool("pay", {"read", "financial"})
    assert reason_of(act) == "tool_blocked" and ran == []


def test_a_bare_write_uses_the_write_default_for_its_decision_but_is_unclassified_for_fail_mode():
    defaults = lines.parse_defaults(served())
    assert lines.resolve(defaults, frozenset({"write"})).decision == "allow"
    assert lines.resolve(defaults, frozenset({"write"})).fail_mode == "closed"
    assert lines.resolve(defaults, frozenset({"read", "write"})).fail_mode == "closed"
    assert lines.resolve(defaults, frozenset({"write", "external"})).fail_mode == "open"
    assert lines.resolve(defaults, frozenset({"read"})).fail_mode == "open"
    assert lines.resolve(defaults, frozenset()).decision == "ask"


def test_approval_requirements_are_one_per_asking_key_and_the_wait_is_the_shortest():
    defaults = lines.parse_defaults(served(approval={"financial": {"approvers": ["a@x.io"], "wait_s": 90},
                                                       "destructive": {"approvers": ["b@x.io"], "wait_s": 30}}))
    resolved = lines.resolve(defaults, frozenset({"financial", "destructive"}))
    assert [r.source for r in resolved.requirements] == ["default:financial", "default:destructive"]  # never a union
    assert [sorted(r.eligible) for r in resolved.requirements] == [["a@x.io"], ["b@x.io"]] and resolved.wait_s == 30


def test_a_line_that_decides_the_tool_keeps_deciding_it_even_looser_than_the_default():
    shared = start(Shared([row("wipe", lines=[{"id": "L1", "decision": "allow", "approval": None, "limits": None}], otherwise="inherit")],
                          defaults=served()))
    act, _ = make_tool("wipe", {"destructive"})
    assert act() == "ok" and asked(shared) == []


def test_a_row_that_decides_nothing_leaves_the_decision_to_the_default():
    shared = start(Shared([row("wipe", lines=[], otherwise="inherit")], defaults=served()))
    act, _ = make_tool("wipe", {"destructive"})
    act()
    assert len(asked(shared)) == 1


def test_dry_run_defaults_refuse_nothing_and_record_what_they_would_have_done():
    start(Shared(defaults=served(state="dry_run", decision={"destructive": "deny"})))
    act, ran = make_tool("wipe", {"destructive"})
    assert act() == "ok" and ran == [1]
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]


def test_the_record_names_the_defaults_revision_and_that_a_default_decided():
    start(Shared(defaults=served(revision=9, decision={"destructive": "deny"})))
    act, _ = make_tool("wipe", {"destructive"})
    with pytest.raises(runbound.ExecutionRefused) as refused:
        act()
    evaluation = refused.value.decision.evaluation
    assert evaluation["service_default_revision"] == 9 and evaluation["contributors"] == ["default:service"]
    assert evaluation["matched_rule_ids"] == [] and evaluation["reason_code"] == "tool_blocked"


def test_enforced_defaults_the_sdk_cannot_read_refuse_the_unruled_tool_and_are_reported():
    shared = start(Shared(defaults={"revision": 2, "state": "enforced", "defaults": {"decision": {"read": "allow"}, "fail_mode": {}}}))
    act, _ = make_tool("look", {"read"})
    assert reason_of(act) == "policy_unsupported"
    assert shared.problem is not None


def test_the_sdk_reports_defaults_v1_and_a_remote_state_holds_and_caches_served_defaults(tmp_path):
    from contract_support import Clock, engine_on_link

    assert "defaults_v1" in lines.FEATURES
    shared = engine_on_link(tmp_path, clock=Clock())
    shared._client.served = {"version": 3, "policy": None, "rows": [], "rows_hash": "h", "service_defaults": served(revision=6)}
    from runbound.plane_types import HelloReply

    shared.apply_hello(HelloReply(policy_version=3))
    assert shared._defaults_enforced and shared.defaults().revision == 6
    again = engine_on_link(tmp_path, clock=Clock())  # a restart: the sealed cache carries the defaults
    assert again._defaults_enforced and again.defaults().revision == 6
    shared._client.served = {"version": 4, "policy": None, "rows": [], "rows_hash": "h", "service_defaults": None}
    shared.apply_hello(HelloReply(policy_version=4))
    assert not shared._defaults_enforced and shared.defaults() is None  # un-adopted: today's behaviour again


# A row whose lines all carry a condition (TR3) still falls back to the service defaults when no line matches the call: otherwise inherit
# means the default decides, never an implicit allow (TPD §5.5; e.g. "IF amount > 1000 -> ask" alone on a financial tool).
def _refund():
    ran = []

    @runbound.tool(name="refund", effects={"financial"}, reviewed=True)
    def refund(amount: float = 0):
        ran.append(amount)
        return "ok"

    return refund, ran


def _ask_over(limit):
    from test_conditions import cond, line

    return line("L1", "ask", [cond("amount", ">", limit)], approval={"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []})


def test_a_conditional_row_that_matches_no_line_is_decided_by_the_enforced_default():
    shared = start(Shared([row("refund", lines=[_ask_over(1000)], otherwise="inherit")], defaults=served()))
    refund, ran = _refund()
    assert refund(500) == "ok" and len(asked(shared)) == 1  # no line matched: the financial default (ask) decided
    assert refund(1500) == "ok" and len(asked(shared)) == 2  # the line matched: it asked
    assert ran == [500, 1500]


def test_a_conditional_row_that_matches_no_line_is_refused_when_the_default_denies():
    start(Shared([row("refund", lines=[_ask_over(1000)], otherwise="inherit")], defaults=served(decision={"financial": "deny"})))
    refund, ran = _refund()
    assert reason_of(lambda: refund(500)) == "tool_blocked" and ran == []


def test_an_allow_line_beside_the_conditional_one_lets_the_small_call_run_without_asking():
    from test_conditions import line

    shared = start(Shared([row("refund", lines=[line("L0", "allow"), _ask_over(1000)], otherwise="inherit")], defaults=served()))
    refund, ran = _refund()
    assert refund(500) == "ok" and asked(shared) == []  # the allow line matched: the default does not decide
    assert refund(1500) == "ok" and len(asked(shared)) == 1  # the strictest matching line (ask) decided


def test_an_explicit_otherwise_keeps_deciding_a_conditional_row_over_the_default():
    shared = start(Shared([row("refund", lines=[_ask_over(1000)], otherwise="allow")], defaults=served()))
    refund, _ = _refund()
    assert refund(500) == "ok" and asked(shared) == []


def test_without_adopted_defaults_a_conditional_row_that_matches_no_line_runs_as_today():
    shared = start(Shared([row("refund", lines=[_ask_over(1000)], otherwise="inherit")]))
    refund, _ = _refund()
    assert refund(500) == "ok" and asked(shared) == []


def test_envelope_a_enforced_defaults_decide_and_the_dry_run_candidate_beside_them_is_only_recorded():
    """service_defaults {state: enforced, candidate: {state: dry_run}}: the enforced revision decides; the candidate records would_refuse."""
    envelope = {**served(decision={"destructive": "allow"}), "candidate": served(state="dry_run", revision=5, decision={"destructive": "deny"})}
    shared = start(Shared(defaults=envelope))
    act, ran = make_tool("wipe", {"destructive"})
    assert act() == "ok" and ran == [1]  # the enforced allow ran the call
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]
    assert asked(shared) == []  # and the candidate asked nobody


def test_envelope_a_the_enforced_deny_still_refuses_whatever_the_candidate_says():
    envelope = {**served(decision={"destructive": "deny"}), "candidate": served(state="dry_run", revision=5, decision={"destructive": "allow"})}
    start(Shared(defaults=envelope))
    act, _ = make_tool("wipe", {"destructive"})
    assert reason_of(act) == "tool_blocked"


def test_envelope_a_with_a_null_or_unreadable_candidate_is_just_the_enforced_defaults():
    for candidate in (None, {"revision": 1, "state": "dry_run", "defaults": {"decision": {}}}):
        runbound.api._teardown_for_tests()
        start(Shared(defaults={**served(decision={"destructive": "allow"}), "candidate": candidate}))
        act, ran = make_tool("wipe", {"destructive"})
        assert act() == "ok"
        assert not any(t.startswith("would_") for t in runbound.tool_results()[-1]["tags"])


def test_envelope_b_dry_run_defaults_with_no_candidate_enforce_nothing():
    shared = start(Shared(defaults={**served(state="dry_run", decision={"destructive": "deny"}), "candidate": None}))
    act, ran = make_tool("wipe", {"destructive"})
    assert act() == "ok" and ran == [1] and asked(shared) == []
    assert "would_refuse:tool_blocked" in runbound.tool_results()[-1]["tags"]
