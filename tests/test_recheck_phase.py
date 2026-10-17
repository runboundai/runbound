"""RVPC-2 (WIRE-VPC 12e): an approved call refused at execution entry carries its decision record in TWO phases.

The approval phase is what the call was decided under when it asked (``rule_revision``, ``cfg_hash``, ``policy_version``, ...); the re-check phase is what the execution-entry
re-check read and who refused it (``recheck_*``). Both ride the refusal, its span and the approval's superseded report. ``reason_code`` stays ``approval_superseded``.
A promote, a restore and a defaults change during the wait each give one.
"""

import pytest

import runbound
from runbound import api, lines, spans
from test_grants import ASK, pay_tool
from test_policy_rows import row, start
from test_service_defaults import Shared as DefaultsShared
from test_service_defaults import served
from test_spans import Collector, attrs_of

A = "runbound.decision."


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Moving(DefaultsShared):
    """A plane whose policy moves while the person decides: the question is asked, then the rows, the configuration and the generation change."""

    def __init__(self, rows, after_rows=None, after_defaults=None, **kw):
        super().__init__(rows, **kw)
        self.cfg, self.policy_version = "cfg-1", 7
        self.after_rows, self.after_defaults = after_rows, after_defaults

    def boot_id(self):
        return "boot-1"

    def served_cfg(self):
        return self.cfg

    def approvals(self, method, path, body=None, timeout=5.0):
        answer = super().approvals(method, path, body, timeout)
        if method == "POST" and path == "/v1/approvals":  # the live change lands after the question was asked
            from runbound import policy_rows

            if self.after_rows is not None:
                self.rows = policy_rows.parse(list(self.after_rows))
            if self.after_defaults is not None:
                self.held = lines.parse_defaults(self.after_defaults)
            self.cfg, self.policy_version = "cfg-2", 9
        return answer


def asking(revision=2):
    return row("pay", rule_revision=revision, lines=[{"id": "L2", "decision": "ask", "approval": ASK, "limits": None}], otherwise="inherit")


def refused_pay(shared):
    pay, ran = pay_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1200.0)
    assert ran == [] and refused.value.reason == "approval_superseded"
    return refused.value.decision.evaluation


def reported(shared):
    import time

    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        reports = [c for c in shared.calls if c[0] == "POST" and c[1].endswith("/report")]
        if reports:
            return reports[0][2]
        time.sleep(0.02)
    raise AssertionError("the superseded report was never sent")


def assert_two_phases(evaluation, **recheck):
    assert evaluation["reason_code"] == "approval_superseded"
    assert (evaluation["rule_revision"], evaluation["cfg_hash"], evaluation["policy_version"]) == (2, "cfg-1", 7)  # the approval phase, untouched
    assert evaluation["recheck_cfg_hash"] == "cfg-2" and evaluation["recheck_policy_version"] == 9
    for key, value in recheck.items():
        assert evaluation[f"recheck_{key}"] == value, key


def test_a_promote_during_the_wait_gives_a_two_phase_record():
    shared = start(Moving([asking(2)], after_rows=[row("pay", rule_revision=3, blocked=True)]))
    assert_two_phases(refused_pay(shared), rule_revision=3, reason="tool_blocked")


def test_a_restore_during_the_wait_names_the_deny_line_that_refused():
    deny = row("pay", rule_revision=4, lines=[{"id": "L1", "decision": "deny", "approval": None, "limits": None}], otherwise="inherit")
    shared = start(Moving([asking(2)], after_rows=[deny]))
    evaluation = refused_pay(shared)
    assert_two_phases(evaluation, rule_revision=4, reason="tool_blocked")
    assert evaluation["recheck_contributor"].startswith("row:L")


def test_a_defaults_change_during_the_wait_names_the_defaults_as_the_refuser():
    shared = start(Moving([], after_defaults=served(revision=5, decision={"financial": "deny"}), defaults=served(revision=4)))
    pay, ran = pay_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1200.0)
    evaluation = refused.value.decision.evaluation
    assert ran == [] and evaluation["reason_code"] == "approval_superseded"
    assert evaluation["service_default_revision"] == 4 and evaluation["recheck_service_default_revision"] == 5  # approved under defaults r4, re-checked under r5
    assert evaluation["recheck_contributor"] == "default:service" and evaluation["recheck_reason"] == "tool_blocked"
    assert "recheck_rule_revision" not in evaluation  # no row: nothing to read a rule revision from


def test_the_superseded_report_carries_both_phases_as_record():
    shared = start(Moving([asking(2)], after_rows=[row("pay", rule_revision=3, blocked=True)]))
    refused_pay(shared)
    record = reported(shared)["record"]
    assert record["rule_revision"] == 2 and record["cfg_hash"] == "cfg-1"
    assert record["recheck_rule_revision"] == 3 and record["recheck_cfg_hash"] == "cfg-2" and record["recheck_policy_version"] == 9
    assert record["recheck_reason"] == "tool_blocked" and reported(shared)["reason"] == "approval_superseded"


def test_the_span_carries_the_recheck_attributes():
    collector = Collector()
    try:
        shared = start(Moving([asking(2)], after_rows=[row("pay", rule_revision=3, blocked=True)]), otel_endpoint=collector.url)
        refused_pay(shared)
        spans.exporter().flush(5.0)
        attrs = attrs_of(collector.spans()[-1])
        assert int(attrs[A + "recheck_rule_revision"]) == 3 and attrs[A + "recheck_cfg_hash"] == "cfg-2" and int(attrs[A + "recheck_policy_version"]) == 9
        assert attrs[A + "recheck_reason"] == "tool_blocked" and attrs[A + "recheck_contributor"]
        assert int(attrs[A + "rule_revision"]) == 2 and attrs[A + "cfg_hash"] == "cfg-1"  # the approval phase is still there
    finally:
        collector.close()


def test_an_ordinary_refusal_and_a_call_that_ran_carry_no_recheck_keys():
    start(Moving([row("pay", rule_revision=2, blocked=True)]))
    pay, _ = pay_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1.0)
    assert not [key for key in refused.value.decision.evaluation if key.startswith("recheck_")]
    api._teardown_for_tests()
    start(Moving([asking(2)], after_rows=[asking(2)]))
    pay, ran = pay_tool()
    assert pay(amount=1.0) == "paid" and ran == [1.0]


def test_a_call_that_passed_the_recheck_and_ran_carries_no_recheck_keys_on_its_record_or_span():
    """An approved call that was re-checked (the policy moved to another revision that still lets it run), and it RAN. Its span must not carry a re-check phase: that is a refusal's evidence."""
    collector = Collector()
    try:
        start(Moving([asking(2)], after_rows=[asking(3)]), otel_endpoint=collector.url)  # a promote lands during the wait, and the new revision still asks (so the call may run)
        pay, ran = pay_tool()
        assert pay(amount=1.0) == "paid" and ran == [1.0]
        spans.exporter().flush(5.0)
        attrs = attrs_of(collector.spans()[-1])
        assert not [name for name in attrs if "recheck_" in name], [name for name in attrs if "recheck_" in name]
        assert int(attrs[A + "rule_revision"]) == 2  # what it was approved under is still there
    finally:
        collector.close()
