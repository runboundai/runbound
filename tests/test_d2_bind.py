"""PS-D2-2 (SDK): the execution bind. An approved request that the plane filed as bind-required is bound to THIS attempt before its body runs, so a replay or a second worker
cannot run it again; a repeated or lost bind ends ``prior_outcome_unknown`` without dispatch. Also: ``resume_admit`` (atomic reservation of the session's units for a resumed
call), ``resolve_replay`` (the by-request lookup table), ``withdraw``, and an approval that holds no quota unit.

The plane is a recording double with the three routes of the fixed interface (bind, by-request, withdraw).
"""

import threading
import time

import pytest

import runbound
from runbound import api, approvals, state as state_module
from runbound.approvals import Replay
from test_policy_rows import FakeShared, row

SECRET = "s3cret"
ASK = {"approval_mode": "sync", "approval_timeout_s": 30, "approvers": []}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class BindPlane(FakeShared):
    """Answers the create with a bind-required (or legacy) approval and the bind with a scripted answer; records every call."""

    def __init__(self, rows=(), bind=(200, {"first": True}), bind_required=True, create_extra=None, **kw):
        super().__init__(rows, **kw)
        self.bind_answer, self.bind_required, self.create_extra = bind, bind_required, create_extra or {}

    def approvals(self, method, path, body=None, timeout=5.0):
        if path == "/v1/approvals" and method == "POST":
            self.calls.append((method, path, body))
            answer = {"id": "ap1", "status": self.approve, "quota": None, **self.create_extra}
            if self.bind_required:
                answer["bind_required"] = True
            return 201, answer
        if path.endswith("/bind"):
            self.calls.append((method, path, body))
            answer = self.bind_answer
            if callable(answer):
                answer = answer()
            return answer
        if path.endswith("/report"):
            self.calls.append((method, path, body))
            return 200, {}
        return super().approvals(method, path, body, timeout)

    def paths(self):
        return [c[1] for c in self.calls]


def start(plane, **config):
    runbound.init(approval_secret=SECRET, **config)
    api._ENGINE.shared = plane
    api._SHARED = plane
    return plane


def sync_tool():
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=30)
    def pay(amount: float = 0.0):
        ran.append(amount)
        return "paid"

    return pay, ran


# --- the default and the capability ---------------------------------------------------------------------


def test_the_default_is_off_and_the_capability_is_sent_only_when_asked_for():
    plane = start(BindPlane(bind_required=False))
    pay, _ = sync_tool()
    pay(amount=1.0)
    assert "capabilities" not in plane.calls[0][2]
    api._teardown_for_tests()
    plane = start(BindPlane(bind_required=False), approval_bind=True)
    pay, _ = sync_tool()
    pay(amount=1.0)
    assert plane.calls[0][2]["capabilities"] == ["bind_v1"]


def test_approval_bind_is_validated_and_rides_the_hello():
    from runbound.config import RunboundConfig
    from test_shared_state import FakePlane, MovableClock, remote

    for bad in ("yes", 1, None):
        with pytest.raises(ValueError, match="approval_bind"):
            RunboundConfig(approval_bind=bad).validate()
    assert "capabilities" not in remote(FakePlane(), MovableClock())._hello_payload()
    assert remote(FakePlane(), MovableClock(), approval_bind=True)._hello_payload()["capabilities"] == ["bind_v1"]


# --- the sync flow ------------------------------------------------------------------------------------------


def test_a_bind_required_sync_approval_binds_after_the_answer_and_before_the_body():
    plane = start(BindPlane(), approval_bind=True)
    pay, ran = sync_tool()
    assert pay(amount=5.0) == "paid" and ran == [5.0]
    assert plane.paths()[:2] == ["/v1/approvals", "/v1/approvals/ap1/bind"]  # the executed report follows, on its own thread
    create, bind = plane.calls[0][2], plane.calls[1][2]
    assert set(bind) == {"attempt_id", "action_id", "args_digest"} and bind["args_digest"] == create["args_digest"]
    assert bind["attempt_id"] and bind["action_id"]


@pytest.mark.parametrize("answer", [(200, {"first": False}), (409, {"error": "bound_to_other_attempt"}), (409, {"error": "approval_expired"}), (409, {"status": "executed"})])
def test_a_repeated_or_refused_bind_is_prior_outcome_unknown_and_the_body_never_runs(answer):
    plane = start(BindPlane(bind=answer), approval_bind=True)
    pay, ran = sync_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=5.0)
    assert refused.value.reason == "prior_outcome_unknown" and not refused.value.retryable and ran == []


@pytest.mark.parametrize("answer", [(None, None), (503, {"error": "downgrade_in_progress"}), (404, None), (500, None), (401, None), (403, None), (200, "garbage")])
def test_a_bind_that_cannot_be_made_fails_closed_whatever_the_fail_mode(answer):
    start(BindPlane(bind=answer), approval_bind=True)

    @runbound.tool(effects={"read"}, reviewed=True, approval="required", mode="sync", timeout_s=30)  # a fail-open class: still closed here
    def look():
        return 1

    with pytest.raises(runbound.ExecutionRefused) as refused:
        look()
    assert refused.value.reason == "approval_unavailable" and refused.value.retryable


def test_a_legacy_request_is_not_bound_and_behaves_as_before():
    plane = start(BindPlane(bind_required=False), approval_bind=True)  # an older plane: it files a legacy request and ignores the capability
    pay, ran = sync_tool()
    assert pay(amount=5.0) == "paid" and ran == [5.0]
    assert "/v1/approvals/ap1/bind" not in plane.paths()


def test_a_bound_request_the_policy_has_since_blocked_is_superseded_and_the_bind_does_not_make_it_run():
    plane = start(BindPlane([row("pay", lines=[{"id": "L1", "decision": "ask", "approval": ASK, "limits": None}], otherwise="inherit")]), approval_bind=True)
    pay, ran = sync_tool()
    original = plane.approvals

    def approvals_then_block(method, path, body=None, timeout=5.0):
        answer = original(method, path, body, timeout)
        if path.endswith("/bind"):
            from runbound import policy_rows

            plane.rows = policy_rows.parse([row("pay", blocked=True)])  # the operator blocks it between the bind and the re-check
        return answer

    plane.approvals = approvals_then_block
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=5.0)
    assert refused.value.reason == "approval_superseded" and ran == [] and plane.paths().count("/v1/approvals/ap1/bind") == 1


# --- the deferred execution ---------------------------------------------------------------------------------------


def parked(plane, **config):
    start(plane, **config)
    ran, results = [], []
    api._ENGINE.config.on_deferred_result = results.append

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        ran.append(ticket)
        return ticket

    out = archive(7)
    create = [c[2] for c in plane.calls if c[1] == "/v1/approvals"][-1]
    item = {"id": "ap1", "tool": "archive", "args": create["args"], "request_id": out["request_id"], "seal": create["seal"], "service": create["service"],
            "expires_at": "2099-01-01T00:00:00Z", "bind_required": True, "claim_token": "tok-1"}
    return item, ran, results


def reports(plane):
    return [c[2] for c in plane.calls if c[1].endswith("/report")]


def test_a_deferred_execution_binds_under_its_claim_token_then_runs_and_reports():
    plane = BindPlane(approve="pending")
    item, ran, results = parked(plane, approval_bind=True)
    approvals._execute(item)
    bind = [c for c in plane.calls if c[1].endswith("/bind")]
    assert len(bind) == 1 and bind[0][2]["claim_token"] == "tok-1" and bind[0][2]["args_digest"] == plane.calls[0][2]["args_digest"]
    deadline = time.monotonic() + 5
    while not reports(plane) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ran == [7] and [r["outcome"] for r in reports(plane)] == ["executed"] and results[-1]["outcome"] == "executed"


def test_a_lost_claim_drops_the_job_without_a_report_a_callback_or_a_run():
    plane = BindPlane(approve="pending", bind=(409, {"error": "claim_lost"}))
    item, ran, results = parked(plane, approval_bind=True)
    approvals._execute(item)
    time.sleep(0.2)
    assert ran == [] and reports(plane) == [] and results == []


def test_a_repeated_bind_on_a_deferred_execution_runs_nothing_and_reports_nothing_but_says_so_to_the_callback():
    plane = BindPlane(approve="pending", bind=(200, {"first": False}))
    item, ran, results = parked(plane, approval_bind=True)
    approvals._execute(item)
    time.sleep(0.2)
    assert ran == [] and reports(plane) == [] and results[-1]["error"] == "prior_outcome_unknown"


def test_a_legacy_deferred_item_is_run_without_a_bind():
    plane = BindPlane(approve="pending", bind_required=False)
    item, ran, _ = parked(plane)
    item.pop("bind_required"), item.pop("claim_token")
    approvals._execute(item)
    assert ran == [7] and not [c for c in plane.calls if c[1].endswith("/bind")]


# --- an approval that holds no quota unit -----------------------------------------------------------------------------


QUOTA_ROW = {"tool": "pay", "state": "enforced",
             "lines": [{"id": "L1", "decision": "ask", "approval": ASK, "limits": {"quota": {"limit": 5, "window": "hour", "per": "service"}}}], "otherwise": "inherit"}


def takes(plane):
    return [c for c in plane.calls if c[1] == "/v1/quota/take"]


def test_an_approval_that_holds_no_unit_takes_one_the_normal_way_and_one_that_holds_one_never_takes_a_second():
    plane = start(BindPlane([QUOTA_ROW], create_extra={"quota_held": False}), approval_bind=True)
    pay, _ = sync_tool()
    assert pay(amount=1.0) == "paid" and len(takes(plane)) == 1, "no unit held: the existing quota contract applies"
    api._teardown_for_tests()
    plane = start(BindPlane([QUOTA_ROW], create_extra={"quota_held": True, "quota": {"state": "enforced"}}), approval_bind=True)
    pay, _ = sync_tool()
    assert pay(amount=1.0) == "paid" and takes(plane) == [], "a held unit is never taken twice"


def test_an_approval_that_holds_no_unit_while_the_quota_cannot_be_verified_follows_the_fail_mode():
    plane = start(BindPlane([{**QUOTA_ROW, "fail_mode": "closed"}], create_extra={"quota_held": False}, link="degraded"), approval_bind=True)
    pay, ran = sync_tool()
    with pytest.raises(runbound.ExecutionRefused) as refused:
        pay(amount=1.0)
    assert ran == [] and refused.value.reason in ("quota_plane_unavailable", "approval_unavailable")


# --- resume_admit ---------------------------------------------------------------------------------------------------------


def engine_with(rows=(), **config):
    runbound.init(**config)
    shared = BindPlane(rows)
    api._ENGINE.shared = shared
    api._SHARED = shared
    session = api._active()[1]
    return api._ENGINE, session


def call_of(name="pay", **kw):
    from runbound.policy import ToolCall

    return ToolCall(name, (), kw, None, {})


def test_resume_admit_reserves_once_and_commit_makes_the_units_the_executed_calls():
    engine, session = engine_with()
    reservation = engine.resume_admit(session, call_of(amount=1.0))
    assert session.counted_calls("pay") == 1 and session.reserved_actions == 1 and session.executed_actions == 0
    assert reservation.commit() is True and reservation.commit() is False and reservation.release() is False
    assert session.counted_calls("pay") == 1 and session.reserved_actions == 0 and session.executed_actions == 1


def test_a_limit_that_filled_while_the_approval_waited_refuses_before_the_start_and_takes_nothing():
    engine, session = engine_with([row("pay", max_calls_per_session=1)])
    session.enter_chain("pay")
    session.leave_chain("pay", True)  # another call took the last unit while the person decided
    with pytest.raises(runbound.ExecutionRefused) as refused:
        engine.resume_admit(session, call_of(amount=1.0))
    assert refused.value.reason == "session_max_calls"
    assert session.counted_calls("pay") == 1 and session.reserved_actions == 0


def test_the_actions_limit_and_the_repetition_count_are_reserved_and_released():
    engine, session = engine_with([row("pay", lines=[{"id": "L1", "decision": "allow", "repetition": {"max": 1, "scope": "session"}}], otherwise="inherit")], max_actions_per_run=2)
    first = engine.resume_admit(session, call_of(amount=1.0))
    with pytest.raises(runbound.ExecutionRefused) as refused:
        engine.resume_admit(session, call_of(amount=1.0))
    assert refused.value.reason == "repetition_exceeded"
    first.release()
    assert session.reserved_actions == 0 and session.counted_calls("pay") == 0 and session.repeated("pay", engine.call_digest(call_of(amount=1.0))) == 0
    again = engine.resume_admit(session, call_of(amount=1.0))  # released: the same call can reserve again
    other = engine.resume_admit(session, call_of("other", n=1))
    with pytest.raises(runbound.ExecutionRefused) as capped:
        engine.resume_admit(session, call_of("third", n=1))
    assert capped.value.reason == "blast_radius", "the action limit (2) is full with two reservations"
    again.release(), other.release()


def test_every_pre_start_refusal_gives_the_reservation_back():
    engine, session = engine_with()
    reservation = engine.resume_admit(session, call_of(amount=1.0))
    assert reservation.release() is True
    assert session.counted_calls("pay") == 0 and session.reserved_actions == 0 and session.executed_actions == 0


def test_two_concurrent_resumes_cannot_both_pass_on_the_last_unit(monkeypatch):
    engine, session = engine_with([row("pay", max_calls_per_session=1)])
    original = state_module.SessionState.counted_calls

    def slow_count(self, tool):
        value = original(self, tool)
        time.sleep(0.05)  # a read-only check would let the other thread read the same value here
        return value

    monkeypatch.setattr(state_module.SessionState, "counted_calls", slow_count)
    outcomes, barrier = [], threading.Barrier(2)

    def resume():
        barrier.wait(5)
        try:
            engine.resume_admit(session, call_of(amount=1.0))
            outcomes.append("passed")
        except runbound.ExecutionRefused as refused:
            outcomes.append(refused.reason)

    threads = [threading.Thread(target=resume) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert sorted(outcomes) == ["passed", "session_max_calls"]
    assert session.counted_calls.__func__ is slow_count and original(session, "pay") == 1


def test_a_resume_spends_no_plane_quota_and_asks_nobody():
    engine, session = engine_with([QUOTA_ROW])
    engine.resume_admit(session, call_of(amount=1.0), approval_ref={"id": "ap1"})
    assert engine.shared.calls == []


# --- resolve_replay -----------------------------------------------------------------------------------------------------------


class Lookup:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def approvals(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def exists(**fields):
    return (200, {"exists": True, "approval": {"id": "ap1", "status": "approved", "mode": "sync", "bind_required": True, "bound_attempt_id": None, "resume_by": "2099-01-01T00:00:00Z", **fields}})


@pytest.mark.parametrize("answer, context, action, reason, detail", [
    ((200, {"exists": False}), None, "first_run", None, None),
    ((200, {"exists": False}), {}, "first_run", None, None),
    ((200, {"exists": False}), {"approval_id": "ap1"}, "refuse", "replay_unavailable", "absent_but_expected"),
    ((200, {"exists": False}), {"request_id": "r1"}, "refuse", "replay_unavailable", "absent_but_expected"),
    (exists(status="pending"), None, "pending", None, None),
    (exists(), None, "resume", None, None),
    (exists(bound_attempt_id="att-1"), None, "no_dispatch", "prior_outcome_unknown", "approved"),
    (exists(status="executing", bound_attempt_id="att-1"), None, "no_dispatch", "prior_outcome_unknown", "executing"),
    (exists(status="executed"), None, "no_dispatch", "executed", "executed"),
    (exists(status="failed"), None, "no_dispatch", "failed", "failed"),
    (exists(status="superseded"), None, "no_dispatch", "superseded", "superseded"),
    (exists(status="cancelled"), None, "no_dispatch", "cancelled", "cancelled"),
    (exists(status="denied"), None, "refuse", "denied", "denied"),
    (exists(status="expired"), None, "refuse", "expired", "expired"),
    (exists(status="withdrawn"), None, "refuse", "withdrawn", "withdrawn"),
    (exists(status="approved_unconsumed"), None, "refuse", "approved_unconsumed", "approved_unconsumed"),
    (exists(resume_by="2000-01-01T00:00:00Z"), None, "refuse", "approved_unconsumed", "resume_by_passed"),
    (exists(bind_required=False), None, "refuse", "replay_unavailable", "not_bind_required"),
    ((403, {"error": "not_requester"}), None, "refuse", "replay_unavailable", "key_mismatch"),
    ((403, {"error": "forbidden"}), None, "refuse", "replay_unavailable", None),
    ((401, None), None, "refuse", "replay_unavailable", None),
    ((404, {"detail": "Not Found"}), None, "refuse", "replay_unavailable", None),
    ((500, None), None, "refuse", "replay_unavailable", None),
    ((503, None), None, "refuse", "replay_unavailable", None),
    ((None, None), None, "refuse", "replay_unavailable", None),
    ((200, {"exists": "maybe"}), None, "refuse", "replay_unavailable", None),
    ((200, {"exists": True}), None, "refuse", "replay_unavailable", None),
    ((200, ["x"]), None, "refuse", "replay_unavailable", None),
    (RuntimeError("boom"), None, "refuse", "replay_unavailable", None),
])
def test_resolve_replay_maps_every_answer_in_the_table(answer, context, action, reason, detail):
    plane = Lookup(answer)
    got = approvals.resolve_replay(plane, "r1", context)
    assert (got.action, got.reason, got.detail) == (action, reason, detail)
    assert plane.calls == [("GET", "/v1/approvals/by-request/r1", 1.0)], "one bounded lookup"
    assert got.action in ("first_run", "resume") or got.action not in ("first_run", "resume")


def test_only_an_exists_false_answer_is_ever_a_first_run():
    results = [approvals.resolve_replay(Lookup(a), "r", None).action for a in [(None, None), (404, None), (401, None), (500, None), (200, {}), (200, {"exists": None})]]
    assert "first_run" not in results


def test_resume_by_ends_the_window_at_the_instant_and_not_a_moment_after():
    at = 1_800_000_000.0
    iso = "2027-01-15T08:00:00Z"
    import datetime

    edge = datetime.datetime.fromisoformat("2027-01-15T08:00:00+00:00").timestamp()
    plane = Lookup(exists(resume_by=iso))
    assert approvals.resolve_replay(plane, "r", None, now=edge - 0.001).action == "resume"
    assert approvals.resolve_replay(plane, "r", None, now=edge).action == "refuse"
    assert approvals.resolve_replay(plane, "r", None, now=edge + 0.001).action == "refuse"


# --- withdraw ------------------------------------------------------------------------------------------------------------------------


class Withdrawing:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def approvals(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_withdraw_posts_to_the_approval_and_records_the_withdrawal():
    plane = Withdrawing((200, {"status": "withdrawn"}))
    got = approvals.withdraw(plane, "r1", "gave up", approval_id="ap1")
    assert got.status == "withdrawn" and plane.calls == [("POST", "/v1/approvals/ap1/withdraw", {"reason": "gave up"})] and approvals.is_withdrawn("r1")


def test_withdraw_finds_the_approval_by_request_when_it_is_not_given():
    plane = Withdrawing((200, {"exists": True, "approval": {"id": "ap9", "status": "pending"}}), (200, {}))
    assert approvals.withdraw(plane, "r2").status == "withdrawn"
    assert [c[1] for c in plane.calls] == ["/v1/approvals/by-request/r2", "/v1/approvals/ap9/withdraw"]


def test_a_decided_request_is_not_withdrawable_and_says_its_true_status():
    plane = Withdrawing((409, {"error": "not_withdrawable", "status": "approved"}))
    got = approvals.withdraw(plane, "r3", approval_id="ap3")
    assert (got.status, got.plane_status) == ("not_withdrawable", "approved") and not approvals.is_withdrawn("r3")
    assert approvals.withdraw(Withdrawing((409, {"status": "withdrawn"})), "r4", approval_id="ap4").status == "withdrawn", "idempotent for the same key"


@pytest.mark.parametrize("answer", [(None, None), (500, None), (404, None), RuntimeError("down")])
def test_a_transport_failure_leaves_the_call_withdrawn_locally_and_never_dispatched(answer):
    got = approvals.withdraw(Withdrawing(answer), "r5", approval_id="ap5")
    assert got.status == "local" and approvals.is_withdrawn("r5")


def test_a_withdrawn_deferred_request_is_never_executed_here():
    plane = BindPlane(approve="pending", bind_required=False)
    item, ran, results = parked(plane)
    approvals.withdraw(Withdrawing(RuntimeError("down")), item["request_id"], approval_id="ap1")
    approvals._execute(item)
    assert ran == [] and results == [] and not [c for c in plane.calls if c[1].endswith("/bind")]


# --- pending, duplicate, same-process and fresh-process resumes ------------------------------------------------------------------------


def test_a_pending_resume_reserves_nothing_because_the_lookup_says_pending_before_any_admission():
    engine, session = engine_with()
    got = approvals.resolve_replay(Lookup(exists(status="pending")), "r1")
    assert got.action == "pending" and session.counted_calls("pay") == 0 and session.reserved_actions == 0  # nothing but a resume reserves, and a pending one never gets that far


def test_a_duplicate_resume_gets_first_false_and_its_reservation_is_released():
    engine, session = engine_with()
    winner = engine.resume_admit(session, call_of(amount=1.0))
    duplicate = engine.resume_admit(session, call_of(amount=1.0))  # the second resume of the same approval reserved too, until its bind answers
    assert session.counted_calls("pay") == 2
    duplicate.release()  # its bind answered first:false: nothing started
    assert session.counted_calls("pay") == 1 and session.reserved_actions == 1
    winner.commit()
    assert session.counted_calls("pay") == 1 and session.executed_actions == 1


def test_a_marker_lost_in_export_does_not_release_a_committed_reservation():
    engine, session = engine_with([row("pay", max_calls_per_session=1)])
    reservation = engine.resume_admit(session, call_of(amount=1.0))
    reservation.commit()  # the start marker was emitted LOCALLY: the dispatch is committed whether or not it is ever delivered
    assert reservation.release() is False
    with pytest.raises(runbound.ExecutionRefused) as refused:
        engine.resume_admit(session, call_of(amount=1.0))
    assert refused.value.reason == "session_max_calls", "the unit is still spent: the attempt reads 'no start observed', not 'free'"


def test_a_fresh_process_resumes_against_empty_session_counters_and_a_same_process_one_sees_its_history():
    engine, session = engine_with([row("pay", max_calls_per_session=1)])
    session.enter_chain("pay")
    session.leave_chain("pay", True)  # the same process already spent the unit before the approval came back
    with pytest.raises(runbound.ExecutionRefused):
        engine.resume_admit(session, call_of(amount=1.0))
    api._teardown_for_tests()
    engine, session = engine_with([row("pay", max_calls_per_session=1)])  # a restarted process: session counters are process-local and start empty (documented; no distributed counter)
    assert engine.resume_admit(session, call_of(amount=1.0)).commit() is True
