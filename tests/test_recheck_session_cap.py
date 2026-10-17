"""RVPC-2: the re-check refusing on the SESSION CAP (``limit:session_cap``), reached by a real flow.

A call that is inside the chain counts itself, so a second call cannot slip past a first that is waiting. What can move is the cap: the session has made two calls under a cap of 3; the third asks, and while the person
decides a live change lowers the cap to 2. The approval arrives, the call counts as the third, and the re-check refuses it.
"""

import pytest

import runbound
from runbound import api, policy_rows
from test_grants import ASK, Granting, pay_tool
from test_policy_rows import row, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def asking(cap, revision):
    return row("pay", rule_revision=revision, lines=[{"id": "L2", "decision": "ask", "approval": ASK, "limits": {"max_calls_per_session": cap, "quota": None}}],
               otherwise="inherit")


class LowersTheCap(Granting):
    """The third question is asked under a cap of 3; the live change that lowers it lands while the person decides."""

    def __init__(self, rows, after, **kw):
        super().__init__(rows, **kw)
        self.after, self.asked = policy_rows.parse(list(after)), 0

    def approvals(self, method, path, body=None, timeout=5.0):
        answer = super().approvals(method, path, body, timeout)
        if method == "POST" and path == "/v1/approvals":
            self.asked += 1
            if self.asked == 3:
                self.rows = self.after
        return answer


def test_a_cap_lowered_while_the_third_call_waited_refuses_it_at_the_recheck_on_the_session_cap():
    start(LowersTheCap([asking(3, 2)], [asking(2, 3)]))
    pay, ran = pay_tool()
    with runbound.session("s1"):
        assert pay(amount=1.0) == "paid" and pay(amount=2.0) == "paid"
        with pytest.raises(runbound.ExecutionRefused) as refused:
            pay(amount=3.0)
    evaluation = refused.value.decision.evaluation
    assert refused.value.reason == "approval_superseded" and ran == [1.0, 2.0]  # the third never ran
    assert evaluation["recheck_contributor"] == "limit:session_cap" and evaluation["recheck_reason"] == "session_max_calls"
    assert evaluation["recheck_rule_revision"] == 3 and evaluation["rule_revision"] == 2  # approved under revision 2, re-checked under 3


def test_a_new_session_is_not_held_to_the_old_one_s_count():
    start(LowersTheCap([asking(3, 2)], [asking(2, 3)]))
    pay, ran = pay_tool()
    with runbound.session("s1"):
        pay(amount=1.0), pay(amount=2.0)
        with pytest.raises(runbound.ExecutionRefused):
            pay(amount=3.0)
    with runbound.session("s2"):
        assert pay(amount=4.0) == "paid"  # the lowered cap of 2 still lets a fresh session make its first calls
