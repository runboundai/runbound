"""R4 grants (WIRE-TR4): the SDK reports ``grants_v1``, sends the call's digest, requirement and record on the approval create, and a
``granted: true`` answer runs the call with nobody asked, taking its own quota unit and naming ``grant:<id>`` in the decision record."""

import hashlib
import json

import pytest

import runbound
from runbound import api, lines, spans
from runbound.approvals import args_digest
from test_policy_rows import FakeShared, row, start
from test_spans import Collector, attrs_of

QUOTA = {"limit": 5, "window": "hour", "per": "service"}
ASK = {"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []}


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Granting(FakeShared):
    """A plane that answers the approval create with a grant, or opens a request when ``grant`` is None."""

    def __init__(self, rows, grant=None, **kw):
        super().__init__(rows, **kw)
        self.grant = grant

    def approvals(self, method, path, body=None, timeout=5.0):
        if path == "/v1/approvals" and self.grant is not None:
            self.calls.append((method, path, body))
            return 200, dict(self.grant, quota=None)
        return super().approvals(method, path, body, timeout)


GRANT = {"granted": True, "grant_id": "g-1", "grant_scope": "session", "expires_at": "2099-01-01T00:00:00Z", "approver": "key:1", "subject": "ana"}


def asking_row(limits=None):
    return row("pay", lines=[{"id": "L2", "decision": "ask", "approval": ASK, "limits": limits}], otherwise="inherit")


def pay_tool():
    ran = []

    @runbound.tool(effects={"financial"})
    def pay(amount: float = 0.0):
        ran.append(amount)
        return "paid"

    return pay, ran


def test_the_sdk_reports_grants_v1():
    assert "grants_v1" in lines.FEATURES


def test_the_digest_is_the_planes_formula_over_the_tool_and_the_arguments():
    args = {"amount": 1500, "to": "acme", "tags": ["a", 1], "nested": {"b": 2.5, "a": None}}
    text = json.dumps({"tool": "pay", "args": args}, sort_keys=True, separators=(",", ":"))
    assert args_digest("pay", args) == hashlib.sha256(text.encode()).hexdigest()
    assert args_digest("pay", {"amount": 1}) != args_digest("pay", {"amount": 2}) != args_digest("other", {"amount": 2})


def test_the_create_carries_the_digest_the_requirement_the_subject_the_session_and_the_record():
    shared = start(Granting([asking_row()]))
    pay, _ = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        pay(amount=1200.0)
    (create,) = [c for c in shared.calls if c[1] == "/v1/approvals"]
    body = create[2]
    assert body["args_digest"] == args_digest("pay", body["args"])
    assert body["requirement"] == "L2" and body["requested_for"] == "ana" and body["run_id"]
    assert body["sdk_version"] and body["matched_rule_ids"] == ["pay#L2"] and body["contributors"] == ["row:L2"] and "policy_state" in body


def test_a_granted_answer_runs_the_call_without_waiting_and_names_the_grant_in_the_record(collector):
    shared = start(Granting([asking_row()], grant=GRANT), otel_endpoint=collector.url)
    pay, ran = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        assert pay(amount=1200.0) == "paid"
    assert ran == [1200.0]
    spans.exporter().flush(5.0)
    assert [c for c in shared.calls if c[1] == "/v1/approvals" and c[0] == "GET"] == []  # nobody was waited on
    span = [a for a in map(attrs_of, collector.spans()) if a.get("runbound.decision.contributors")]
    assert span and "grant:g-1" in span[-1]["runbound.decision.contributors"] and "row:L2" in span[-1]["runbound.decision.contributors"]


def test_a_granted_call_takes_its_own_quota_unit_because_the_plane_took_none():
    shared = start(Granting([asking_row({"max_calls_per_session": None, "quota": QUOTA})], grant=GRANT))
    pay, ran = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        pay(amount=5.0)
    assert ran == [5.0] and len(shared.takes()) == 1


def test_without_a_grant_the_request_opens_as_today():
    shared = start(Granting([asking_row()]))
    pay, ran = pay_tool()
    with runbound.session("s1", requested_for="ana"):
        assert pay(amount=1.0) == "paid"
    assert ran == [1.0]
    assert [c for c in shared.calls if c[1] == "/v1/approvals" and c[0] == "POST"][0][2]["tool"] == "pay"


def test_a_code_only_approval_sends_no_requirement():
    shared = start(Granting([]))

    @runbound.tool(effects={"financial"}, approval="required", timeout_s=30)
    def wire(amount: float = 0.0):
        return "wired"

    wire(amount=1.0)
    (create,) = [c for c in shared.calls if c[1] == "/v1/approvals"]
    assert "requirement" not in create[2] and create[2]["args_digest"]
