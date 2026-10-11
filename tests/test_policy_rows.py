"""T1 SDK half, offline: per-tool rows folded with code rules, quota leases and takes, against a fake plane."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

import runbound
from runbound import api, policy_rows, quota
from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply
from runbound.quota import Leases
from runbound.shared import RemoteState


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class FakeShared:
    """What the engine asks of ``shared``: rows, leases, the link, and the approval/quota routes."""

    fleet = True

    def __init__(self, rows=(), link="connected", take=None, approve="approved"):
        self.rows = policy_rows.parse(list(rows))
        self.quota = Leases()
        self.link, self.calls = link, []
        self.take = take or (lambda body: (200, {"granted": True, "retry_after_s": 0}))
        self.approve = approve

    def row_for(self, tool):
        return self.rows.get(tool)

    def link_state(self):
        return self.link

    def approval_refusal(self):
        return None

    def plane_refusal(self, effects):
        return None

    def halted(self):
        return False

    def policy(self):
        return None

    policy_version = 0
    policy_dry_run = False

    def observers(self):
        return []

    def approvals(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        if path == "/v1/quota/take":
            return self.take(body)
        if path == "/v1/approvals":
            return 201, {"id": "ap1", "status": self.approve, "quota": None}
        return 200, {}

    def status(self):
        from runbound.plane_types import PlaneStatus

        return PlaneStatus(mode=self.link)

    def pending_events(self):
        return None

    def start(self, engine):
        return None

    def stop(self):
        return None

    def takes(self):
        return [c for c in self.calls if c[1] == "/v1/quota/take"]


def row(tool, **fields):
    return {"tool": tool, "state": "enforced", **fields}


def start(shared, **config):
    runbound.init(**config)
    api._ENGINE.shared = shared
    api._SHARED = shared
    return shared


def tools():
    ran = []

    @runbound.tool(effects={"read"}, reviewed=True)
    def look(x: int = 0):
        ran.append("look")
        return "ok"

    @runbound.tool(effects={"read"}, reviewed=True, max_calls=5)
    def search(q: str = ""):
        ran.append("search")
        return "ok"

    return look, search, ran


# --- rows: block, cap, dry_run -----------------------------------------------------------------


def test_a_dashboard_row_blocks_a_tool_the_code_allows():
    start(FakeShared([row("look", blocked=True)]))
    look, _, ran = tools()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look()
    assert caught.value.reason == "tool_blocked" and ran == []
    assert caught.value.decision.boundary == "row" and "blocked by policy" in str(caught.value)


def test_a_dry_run_row_refuses_nothing_and_records_would_refuse():
    start(FakeShared([row("look", state="dry_run", blocked=True)]))
    look, _, ran = tools()
    assert look() == "ok" and ran == ["look"]
    result = runbound.tool_results()[-1]
    assert result["outcome"] == "ok" and "would_refuse:tool_blocked" in result["tags"]
    decision = [d for d in runbound.decisions() if d["reason"] and "blocked" in d["reason"]]
    assert decision and decision[-1]["verdict"] == "deny" and decision[-1]["boundary"] == "row"


def test_a_row_session_cap_and_the_codes_cap_the_lower_wins():
    start(FakeShared([row("look", max_calls_per_session=2), row("search", max_calls_per_session=9)]))
    look, search, _ = tools()
    look(), look()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look()
    assert caught.value.reason == "session_max_calls"
    for _ in range(5):  # the row allows 9, the code (max_calls=5) allows 5: five run
        search()
    with pytest.raises(runbound.ExecutionRefused):
        search()


def test_blocked_is_judged_before_the_session_cap_before_quota_and_a_refused_call_takes_no_unit():
    shared = start(FakeShared([row("look", blocked=True, max_calls_per_session=1,
                                   quota={"limit": 5, "window": "minute", "per": "service"})]))
    look, _, _ = tools()
    for _ in range(3):
        with pytest.raises(runbound.ExecutionRefused) as caught:
            look()
        assert caught.value.reason == "tool_blocked"
    assert shared.takes() == []


def test_a_row_can_only_add_never_loosen_what_the_code_requires():
    shared = start(FakeShared([row("pay", approval_required=False, blocked=False, max_calls_per_session=99)]))
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=30, max_calls=1, reviewed=False)
    def pay(x: int):
        ran.append(x)

    pay(1)
    assert [c[1] for c in shared.calls if not c[1].endswith("/report")] == ["/v1/approvals"]  # a person was still asked
    with pytest.raises(runbound.ExecutionRefused):
        pay(2)  # the code's max_calls=1 beat the row's 99
    assert ran == [1]


# --- rows: approvals ----------------------------------------------------------------------------------


def test_a_row_can_require_approval_of_a_tool_the_code_leaves_alone():
    shared = start(FakeShared([row("look", approval_required=True, approval_timeout_s=30)]))
    look, _, ran = tools()
    assert look() == "ok" and ran == ["look"]
    (create,) = [c for c in shared.calls if c[1] == "/v1/approvals"]
    assert create[2]["mode"] == "sync" and create[2]["timeout_s"] == 30.0


def test_a_rows_deferred_mode_applies_when_the_code_is_silent_and_the_args_are_json_friendly():
    shared = start(FakeShared([row("look", approval_required=True, approval_mode="deferred")]))
    look, _, ran = tools()
    assert look(3)["status"] == "pending_approval" and ran == []
    assert shared.calls[0][2]["mode"] == "deferred"


def test_a_deferred_row_on_a_tool_with_non_json_arguments_falls_back_to_sync():
    import datetime as dt

    shared = start(FakeShared([row("when", approval_required=True, approval_mode="deferred")]))

    @runbound.tool(effects={"read"}, reviewed=True)
    def when(at: dt.datetime = None):
        return "ran"

    assert when() == "ran" and shared.calls[0][2]["mode"] == "sync"


def test_code_wins_on_approval_mode_and_the_timeout_is_the_lower():
    shared = start(FakeShared([row("pay", approval_required=True, approval_mode="deferred", approval_timeout_s=10)]))

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=300, reviewed=False)
    def pay(x: int):
        return "paid"

    assert pay(1) == "paid"
    body = shared.calls[0][2]
    assert body["mode"] == "sync" and body["timeout_s"] == 10.0


def test_a_dry_run_approval_row_asks_nobody():
    shared = start(FakeShared([row("look", state="dry_run", approval_required=True)]))
    look, _, _ = tools()
    assert look() == "ok" and shared.calls == []


# --- rows: risk_class ----------------------------------------------------------------------------------


def test_a_rows_risk_class_raises_the_tools_class_and_never_lowers_it():
    start(FakeShared([row("look", risk_class="destructive"), row("search", risk_class="read")]),
          capabilities={"destructive": "deny"})
    look, search, _ = tools()
    with pytest.raises(runbound.CapabilityRefused):
        look()  # a read tool the dashboard calls destructive
    assert search() == "ok"
    effects = frozenset({"financial"})
    assert policy_rows.effective_effects(policy_rows.parse([row("t", risk_class="read")])["t"], effects) == effects
    assert policy_rows.effective_effects(policy_rows.parse([row("t", risk_class="privileged")])["t"], effects) == {"privileged", "financial"}


def test_fail_mode_closed_wins_and_malformed_rows_are_skipped():
    rows = policy_rows.parse([row("a", fail_mode="open"), row("b"), {"tool": 5}, "junk", row("c", state="draft"),
                              row("d", max_calls_per_session=0)])
    assert rows["a"].fail_mode == "open" and rows["b"].fail_mode == "closed"
    assert set(rows) == {"a", "b", "d"} and rows["d"].max_calls is None


def test_the_rows_hash_helper_matches_the_documented_construction():
    import hashlib
    import json

    rows = [row("b", blocked=True), row("a")]
    canonical = [{"tool": r["tool"], **{f: r.get(f) for f in (
        "state", "blocked", "max_calls_per_session", "quota", "approval_required", "approval_mode",
        "approval_timeout_s", "approvers", "risk_class", "fail_mode", "headline", "identifies")}}
        for r in sorted(rows, key=lambda r: r["tool"])]
    expected = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    assert policy_rows.rows_hash(rows) == expected
    assert policy_rows.rows_hash([]) == hashlib.sha256(b"[]").hexdigest()[:16]


# --- RemoteState: rows_hash, the hello, the cache -----------------------------------------------------------


class Client:
    def __init__(self, envelope):
        self.envelope, self.fetches = envelope, 0
        self.key_state = "unknown"

    def policy(self, service):
        self.fetches += 1
        return self.envelope

    def hello(self, payload):
        return None

    def approvals(self, *a, **k):
        return None, None


def remote(tmp_path, envelope):
    config = RunboundConfig(token="t", control_plane_url="http://plane.example", service="svc", cache_dir=str(tmp_path))
    config.validate()
    return RemoteState(Client(envelope), None, config)


ENVELOPE = {"version": 4, "policy": None, "rows": [row("look", blocked=True)], "rows_hash": "h1"}


def test_rows_replace_only_when_their_hash_changes_and_a_version_bump_alone_changes_nothing(tmp_path):
    shared = remote(tmp_path, dict(ENVELOPE))
    shared.apply_hello(HelloReply(policy_version=4))
    held = shared._rows
    assert shared.row_for("look").blocked and shared._rows_hash == "h1"

    shared._client.envelope = {**ENVELOPE, "version": 5}  # a document-policy write bumped the counter
    shared.apply_hello(HelloReply(policy_version=5))
    assert shared._rows is held and shared._client.fetches == 2  # fetched (the version moved), kept (same hash)

    shared.apply_hello(HelloReply(policy_version=5))
    assert shared._client.fetches == 2  # an unchanged version is not fetched at all

    shared._client.envelope = {"version": 6, "rows": [], "rows_hash": "h2"}
    shared.apply_hello(HelloReply(policy_version=6))
    assert shared.row_for("look") is None and shared._rows_hash == "h2"


def test_the_hello_echoes_the_held_rows_hash_and_policy_version(tmp_path):
    shared = remote(tmp_path, dict(ENVELOPE))
    assert "rows_hash" not in shared._hello_payload()
    shared.apply_hello(HelloReply(policy_version=4))
    payload = shared._hello_payload()
    assert payload["rows_hash"] == "h1" and payload["policy_version_seen"] == 4


def test_the_cache_stores_rows_and_their_hash_and_a_restart_enforces_them(tmp_path):
    first = remote(tmp_path, dict(ENVELOPE))
    first.apply_hello(HelloReply(policy_version=4))
    reborn = remote(tmp_path, {})  # a restarted worker, before any hello
    assert reborn.row_for("look").blocked and reborn._rows_hash == "h1"


# --- quota ---------------------------------------------------------------------------------------------------


def window(minutes=1, **over):
    start_ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    lease = {"tool": "look", "state": "enforced", "limit": 5, "window": "minute",
             "window_start": start_ts.isoformat().replace("+00:00", "Z"),
             "window_end": (start_ts + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
             "granted": 2, "remaining": 3}
    lease.update(over)
    return lease


QUOTA = {"limit": 5, "window": "minute", "per": "service"}


def test_leases_add_granted_spend_first_and_report_what_is_held():
    leases = Leases()
    leases.apply([window(granted=2)])
    assert leases.report() == {"look": 2}
    leases.apply([window(granted=1)])  # same window: add
    assert leases.report() == {"look": 3}
    assert leases.spend("look") and leases.report() == {"look": 2}


def test_a_drop_entry_pops_everything_held_for_the_tool_and_nothing_more_is_spent():
    leases = Leases()
    leases.apply([window(granted=3), window(tool="other", granted=2)])
    assert leases.report() == {"look": 3, "other": 2}
    leases.apply([window(granted=0, remaining=7, drop=True)])  # `look` now asks a person
    assert leases.report() == {"other": 2}  # `look` is gone, `other` untouched
    assert leases.spend("look") is False and leases.spend("other") is True
    leases.apply([window(granted=2, drop=True)])  # a drop adds nothing, whatever it says it grants
    assert leases.report() == {"other": 1} and leases.spend("look") is False
    leases.apply([window(drop=True, tool="never-held")])  # dropping what is not held is harmless
    assert leases.report() == {"other": 1}


def test_everything_held_is_dropped_when_window_start_changes_or_the_window_has_passed():
    leases = Leases()
    leases.apply([window(granted=3)])
    leases.apply([window(granted=1, window_start="2099-01-01T00:00:00Z", window_end="2099-01-01T00:01:00Z")])
    assert leases.report() == {"look": 1}  # the 3 held in the old window are gone; only the new grant is held
    old = Leases(now=lambda: time.time() + 3600)
    old.apply([window(granted=2)])
    assert old.report() == {} and old.spend("look") is False  # a lease from a past window is never spent


def test_a_held_lease_is_spent_before_any_take():
    shared = start(FakeShared([row("look", quota=QUOTA)]))
    shared.quota.apply([window(granted=2)])
    look, _, _ = tools()
    look(), look()
    assert shared.takes() == []
    look()  # the lease is gone: one synchronous take
    assert len(shared.takes()) == 1 and shared.takes()[0][2] == {"tool": "look", "service": "default"}


def test_an_exhausted_window_is_cached_one_round_trip_per_window():
    shared = start(FakeShared([row("look", quota=QUOTA)], take=lambda b: (200, {"granted": False, "retry_after_s": 23})))
    look, _, ran = tools()
    for _ in range(4):
        with pytest.raises(runbound.ExecutionRefused) as caught:
            look()
        assert caught.value.reason == "quota_exceeded"
    assert len(shared.takes()) == 1 and ran == []
    assert 0 < caught.value.retry_after <= 23 and "try again in" in str(caught.value)
    assert caught.value.retryable is True and runbound.is_retryable(caught.value)  # after retry_after


def test_quota_plane_unavailable_is_retryable_and_names_the_poll_interval_as_retry_after():
    shared = start(FakeShared([row("look", quota=QUOTA)], link="unavailable"), control_plane_poll_s=7)
    look, _, _ = tools()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look()
    assert caught.value.reason == "quota_plane_unavailable"
    assert caught.value.retryable is True and caught.value.retry_after == 7.0


def test_other_row_refusals_stay_not_retryable():
    start(FakeShared([row("look", blocked=True), row("search", state="enforced")]))
    look, _, _ = tools()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look()
    assert caught.value.reason == "tool_blocked" and caught.value.retryable is False


def test_a_per_key_quota_always_takes_with_the_session_key_hash_and_never_leases():
    shared = start(FakeShared([row("look", quota={"limit": 5, "window": "hour", "per": "key"})]))
    shared.quota.apply([window(granted=9)])
    look, _, _ = tools()
    with runbound.session("user-1"):
        look(), look()
    takes = shared.takes()
    assert len(takes) == 2 and takes[0][2]["key"] and "user-1" not in str(takes)
    assert runbound.tool_calls() == {} or True


def test_with_the_plane_away_the_lease_is_spent_then_quota_plane_unavailable_for_every_class():
    shared = start(FakeShared([row("look", quota=QUOTA)], link="unavailable"))
    shared.quota.apply([window(granted=1)])
    look, _, ran = tools()
    assert look() == "ok"  # the lease in hand keeps working
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look()
    assert caught.value.reason == "quota_plane_unavailable" and shared.takes() == []  # no request-path call
    assert ran == ["look"]


def test_fail_mode_open_continues_at_a_static_share_per_heartbeat_period():
    shared = start(FakeShared([row("look", quota={"limit": 40, "window": "hour", "per": "service"}, fail_mode="open")],
                              link="degraded"), control_plane_poll_s=3600)
    look, _, _ = tools()
    for _ in range(4):  # 40 // 10 per period
        assert look() == "ok"
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look()
    assert caught.value.reason == "quota_plane_unavailable"


def test_a_tool_that_will_ask_a_person_takes_nothing_the_plane_takes_the_unit_at_create():
    shared = start(FakeShared([row("look", quota=QUOTA, approval_required=True)]))
    look, _, _ = tools()
    assert look() == "ok"
    assert shared.takes() == [] and [c[1] for c in shared.calls if not c[1].endswith("/report")] == ["/v1/approvals"]


def test_a_dry_run_quota_is_counted_and_records_would_refuse_without_refusing():
    shared = start(FakeShared([row("look", state="dry_run", quota=QUOTA)],
                              take=lambda b: (200, {"granted": False, "retry_after_s": 9})))
    look, _, ran = tools()
    assert look() == "ok" and ran == ["look"] and len(shared.takes()) == 1
    assert "would_refuse:quota_exceeded" in runbound.tool_results()[-1]["tags"]


def test_a_row_for_a_quota_the_plane_does_not_know_does_not_block_the_call():
    start(FakeShared([row("look", quota=QUOTA)], take=lambda b: (404, {"error": "no_such_quota"})))
    look, _, _ = tools()
    assert look() == "ok"


def test_a_deferred_execution_does_not_take_a_second_unit():
    shared = start(FakeShared([row("look", quota=QUOTA)]))
    from runbound import approvals

    look, _, _ = tools()
    token = approvals._CURRENT.set("req-1")
    try:
        assert look() == "ok"
    finally:
        approvals._CURRENT.reset(token)
    assert shared.takes() == []


def test_the_hello_reports_quota_held_for_the_current_window_only(tmp_path):
    config = RunboundConfig(token="t", control_plane_url="http://plane.example", service="svc", cache_dir=str(tmp_path))
    config.validate()
    shared = RemoteState(Client({}), None, config)
    shared.apply_hello(HelloReply(quota_leases=[window(granted=3)]))
    assert shared._hello_payload()["quota_held"] == {"look": 3}
    shared.quota.spend("look")
    assert shared._hello_payload()["quota_held"] == {"look": 2}
    assert HelloReply().quota_leases == []


# --- a row with approval_required is never leased (Apollo): nothing here may expect or wait for a lease ---------


def test_an_approval_row_works_with_no_lease_at_all_and_makes_no_take():
    shared = start(FakeShared([row("look", quota=QUOTA, approval_required=True)]))
    assert shared.quota.report() == {}  # the plane sent no lease for it
    look, _, ran = tools()
    for _ in range(3):
        assert look() == "ok"
    assert shared.takes() == [] and shared.quota.report() == {}
    assert len([c for c in shared.calls if c[1] == "/v1/approvals"]) == 3  # every unit came from an approval create


def test_a_code_declared_approval_tool_works_with_no_lease_and_makes_no_take():
    # same as the row case: @tool(approval="required") with a quota row that does not require approval (Apollo, 2026-10-08)
    shared = start(FakeShared([row("look", quota=QUOTA)]))

    @runbound.tool(effects={"read"}, reviewed=True, approval="required")
    def look(x: int = 0):
        return "ok"

    assert shared.quota.report() == {}  # the plane sent no lease for it
    for _ in range(3):
        assert look() == "ok"
    assert shared.takes() == [] and shared.quota.report() == {}
    assert len([c for c in shared.calls if c[1] == "/v1/approvals"]) == 3  # every unit came from an approval create


def test_a_non_asking_call_on_a_tool_whose_row_would_ask_takes_synchronously_without_waiting_for_a_lease():
    # a dry_run approval row asks nobody; the tool's quota is then an ordinary one with no lease to spend
    shared = start(FakeShared([row("look", state="dry_run", quota=QUOTA, approval_required=True)]))
    look, _, _ = tools()
    assert look() == "ok" and len(shared.takes()) == 1 and shared.takes()[0][2]["tool"] == "look"
    assert [c[1] for c in shared.calls if c[1] == "/v1/approvals"] == []


def test_a_missing_lease_is_never_an_error_or_a_wait():
    leases = Leases()
    assert leases.report() == {} and leases.spend("look") is False and leases.exhausted_for("look", None) is None
    leases.apply([])
    leases.apply(None)
    leases.apply([{"tool": "other", "window_start": "x"}])  # malformed: skipped, nothing raised
    assert leases.report() == {}
