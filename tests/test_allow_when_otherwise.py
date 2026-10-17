"""A row that allows only when a condition holds and says what happens otherwise ("allow when amount > 1000, otherwise deny") works on a real plane's served shape.

The plane serves such a row with a legacy view of ``blocked: true`` (the strictest reading of the row, the ``otherwise`` included). The SDK checks that legacy view is never stricter than
the lines; its static reading of the lines used to ignore the row's ``otherwise`` once any line existed, saw only ``allow``, and refused the whole tool as ``policy_unsupported``
even for a call the allow line matches. The check now compares against the strictest of the lines AND the explicit ``otherwise``; a legacy view stricter than both is still refused.
"""

import pytest

import runbound
from runbound import api
from test_conditions import cond, line, make, reason_of
from test_policy_rows import FakeShared, row, start

ASK = {"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def served(otherwise, legacy):
    """The row as the plane serves it: the lines, the ``otherwise``, and the legacy fields (``blocked`` / ``approval_required``) it derives from them."""
    return row("pay", lines=[line("L1", "allow", [cond("amount", ">", 1000)])], otherwise=otherwise, **legacy)


def test_allow_when_over_a_threshold_otherwise_deny_runs_the_big_call_and_refuses_the_small_one_as_the_otherwise():
    start(FakeShared([served("deny", {"blocked": True})]))
    pay, ran = make()

    assert reason_of(pay, amount=2500) == "ran" and ran == [2500]
    assert reason_of(pay, amount=500) == "tool_blocked" and ran == [2500]  # no line matched: the row's own otherwise decides


def test_the_refusal_names_the_otherwise_not_the_service_default():
    start(FakeShared([served("deny", {"blocked": True})]))
    pay, _ = make()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=500)
    assert refused.value.decision.evaluation["contributors"] == ["otherwise:deny"]


def test_allow_when_otherwise_ask_runs_the_big_call_and_asks_for_the_small_one():
    shared = start(FakeShared([served("ask", {"approval_required": True, "approval_mode": "sync"})]))
    pay, ran = make()

    assert reason_of(pay, amount=2500) == "ran"
    assert reason_of(pay, amount=500) == "ran"  # the fake approver approves; what matters is that it was asked
    assert [c for c in shared.calls if c[1] == "/v1/approvals"] and ran == [2500, 500]


def test_a_legacy_view_stricter_than_both_the_lines_and_the_otherwise_is_still_refused_as_unsupported():
    start(FakeShared([served("allow", {"blocked": True})]))  # the lines and the otherwise allow, the legacy view says blocked: the lines payload is looser than the plane meant
    pay, ran = make()

    assert reason_of(pay, amount=2500) == "policy_unsupported" and ran == []


def test_a_legacy_view_looser_than_the_otherwise_is_not_a_mismatch():
    """The check guards against a lines payload LOOSER than the plane's own reading; a looser legacy view is the plane's old-SDK compatibility and never refuses."""
    start(FakeShared([served("deny", {})]))
    pay, _ = make()

    assert reason_of(pay, amount=2500) == "ran" and reason_of(pay, amount=500) == "tool_blocked"
