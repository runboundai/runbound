"""R5 (VPC-07): a call a person approved, or a grant admitted, re-checks the policy held NOW before it runs."""

import pytest

import runbound
from runbound import api, policy_rows
from test_grants import ASK, GRANT, Granting, asking_row, pay_tool
from test_policy_rows import row, start


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Changing(Granting):
    """A plane whose policy changes while the person decides: the approval is answered, then the rows are replaced."""

    def __init__(self, rows, after, grant=None, **kw):
        super().__init__(rows, grant=grant, **kw)
        self.after = policy_rows.parse(list(after))

    def approvals(self, method, path, body=None, timeout=5.0):
        answer = super().approvals(method, path, body, timeout)
        if method == "POST" and path == "/v1/approvals":
            self.rows = self.after  # the live change lands after the question was asked
        return answer


def test_vpc_07_3_a_tool_blocked_while_the_person_decided_is_refused_not_run():
    start(Changing([asking_row()], [row("pay", blocked=True)]))
    pay, ran = pay_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1200.0)
    assert refused.value.reason == "approval_superseded" and ran == []
    assert refused.value.retryable is False
    assert "approved" in str(refused.value) and "policy changed" in str(refused.value)


def test_vpc_07_4_a_grant_answer_is_rechecked_too():
    start(Changing([asking_row()], [row("pay", blocked=True)], grant=GRANT))
    pay, ran = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        with pytest.raises(runbound.ExecutionRefused) as refused:
            pay(amount=1200.0)
    assert refused.value.reason == "approval_superseded" and ran == []


def test_vpc_07_5_a_deny_line_that_arrives_during_the_wait_refuses():
    deny = row("pay", lines=[{"id": "L9", "decision": "deny", "approval": None, "limits": None}], otherwise="inherit")
    start(Changing([asking_row()], [deny]))
    pay, ran = pay_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1200.0)
    assert refused.value.reason == "approval_superseded" and ran == []


def test_vpc_07_6_a_policy_that_still_asks_lets_the_approved_call_run():
    start(Changing([asking_row()], [asking_row()]))
    pay, ran = pay_tool()
    assert pay(amount=1200.0) == "paid" and ran == [1200.0]


def test_vpc_07_10_an_approval_never_overrides_a_code_ceiling():
    start(Granting([asking_row()]))
    ran = []

    @runbound.tool(effects={"financial"}, blocked=True)
    def pay(amount: float = 0.0):
        ran.append(amount)

    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1.0)
    assert refused.value.reason == "tool_blocked" and ran == []


class Moving(Granting):
    """A plane whose STATE moves while the person decides: a fleet halt is served, or the policy goes unavailable."""

    def __init__(self, rows, move, **kw):
        super().__init__(rows, **kw)
        self.move, self._halt, self._state = move, False, "fresh"

    def approvals(self, method, path, body=None, timeout=5.0):
        answer = super().approvals(method, path, body, timeout)
        if method == "POST" and path == "/v1/approvals":
            if self.move == "halt":
                self._halt = True
            else:
                self._state = "unavailable"
        return answer

    def halted(self):
        return self._halt

    def halt_mode(self):
        return "all"

    def policy_state(self):
        return self._state

    def plane_refusal(self, effects):
        return "the control plane is unreachable and the policy has expired" if self._state == "unavailable" else None


def test_vpc_07_8_a_fleet_halt_served_while_the_person_decided_refuses_the_approved_call():
    start(Moving([asking_row()], "halt"))
    pay, ran = pay_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1200.0)
    assert refused.value.reason == "halt" and ran == []


def test_vpc_07_9_a_policy_gone_unavailable_during_the_wait_refuses_a_fail_closed_tool():
    start(Moving([asking_row()], "unavailable"))
    pay, ran = pay_tool()  # financial: fails closed
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1200.0)
    assert refused.value.reason in ("policy_unavailable", "policy_expired_restricted", "plane_unavailable") and ran == []


def _reports(shared):
    return [c for c in shared.calls if c[0] == "POST" and c[1].endswith("/report")]


def _until(check, seconds=5.0):
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_vpc_07_11_a_superseded_call_tells_the_plane_so_it_gives_the_quota_unit_back():
    shared = start(Changing([asking_row()], [row("pay", blocked=True)]))
    pay, ran = pay_tool()
    with pytest.raises(runbound.ExecutionRefused):
        pay(amount=1200.0)
    assert _until(lambda: _reports(shared))
    (report,) = _reports(shared)
    assert report[1] == "/v1/approvals/ap1/report"
    assert report[2]["outcome"] == "superseded" and report[2]["reason"] == "approval_superseded" and ran == []


def test_a_grant_that_is_superseded_reports_nothing_because_no_unit_was_held():
    shared = start(Changing([asking_row()], [row("pay", blocked=True)], grant=GRANT))
    pay, _ = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        with pytest.raises(runbound.ExecutionRefused):
            pay(amount=1200.0)
    assert not _until(lambda: _reports(shared), 0.3)


def test_a_granted_call_refused_at_the_recheck_takes_no_quota_unit():
    quota = {"max_calls_per_session": None, "quota": {"limit": 5, "window": "hour", "per": "service"}}
    shared = start(Changing([asking_row(quota)], [row("pay", blocked=True)], grant=GRANT))
    pay, ran = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        with pytest.raises(runbound.ExecutionRefused) as refused:
            pay(amount=5.0)
    assert refused.value.reason == "approval_superseded" and ran == [] and shared.takes() == []
