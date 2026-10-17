"""VPC §11 row M19, the SDK half: a restore never resets the SDK's per-session counters.

A restore is a new revision of a tool's rules whose content equals an older one. The plane serves it like any other change; the SDK
keeps its counters in the session, keyed by the tool and the ids of the lines that matched, so a revision that keeps the line ids keeps
the count, and a new session starts at zero. A revision that renamed the lines would count from zero (the plane keeps ids stable on
restore, which is why it does: Stage C, 07.5); the second test says so, so nobody is surprised.
"""

import pytest

import runbound
from runbound import api, policy_rows
from test_conditions import cond, line, reason_of, rule
from test_policy_rows import FakeShared, start

pytestmark = [pytest.mark.contract(item="§11.M19")]

CAP = {"max_calls_per_session": 2, "quota": None}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def tools():
    @runbound.tool(effects={"read"}, reviewed=True)
    def pay(amount: float = 0):
        return "ok"

    @runbound.tool(effects={"read"}, reviewed=True)
    def send(to: str = "a"):
        return "ok"

    return pay, send


def serve(shared, *rows):
    """The plane serves a new revision: the worker now holds these rows, and nothing else about the session changes."""
    shared.rows = policy_rows.parse(list(rows))


def revision(ident="L1", cap_ident="L1"):
    return [rule("pay", line(cap_ident, "allow", [cond("amount", ">", 100)], limits=CAP)),
            rule("send", line(ident, "allow", repetition={"max": 1, "scope": "session"}))]


def test_vpc_m19_restore_never_resets_session_counters():
    shared = start(FakeShared(revision()))
    pay, send = tools()
    with runbound.session("s1"):
        assert reason_of(pay, amount=500) == "ran" and reason_of(pay, amount=500) == "ran"  # the conditional cap (2) is used up
        assert reason_of(pay, amount=500) == "session_max_calls"
        assert reason_of(send, to="x") == "ran" and reason_of(send, to="x") == "repetition_exceeded"  # so is the repetition
        serve(shared, *revision())  # the restored revision: the same content and the same line ids
        assert reason_of(pay, amount=500) == "session_max_calls"  # still used up
        assert reason_of(send, to="x") == "repetition_exceeded"
    with runbound.session("s2"):
        assert reason_of(pay, amount=500) == "ran" and reason_of(send, to="x") == "ran"  # a new session starts at zero


def test_vpc_m19_a_restored_revision_with_different_line_ids_counts_from_zero_for_a_conditional_cap():
    """Documents the edge: the count is keyed by the matched line ids. The plane keeps them stable on a restore (Stage C), so this is
    not what a restore does; it is what a rewrite that renames the lines would do."""
    shared = start(FakeShared(revision()))
    pay, _ = tools()
    with runbound.session("s1"):
        assert reason_of(pay, amount=500) == "ran" and reason_of(pay, amount=500) == "ran"
        assert reason_of(pay, amount=500) == "session_max_calls"
        serve(shared, *revision(cap_ident="L9"))
        assert reason_of(pay, amount=500) == "ran"  # a new line id is a new key: the old count does not follow it
