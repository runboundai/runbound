"""Approvals without a plane: the seal, registration, and failing closed. (The real-plane flows are
in tests/integration/test_approvals_plane.py.)"""

from __future__ import annotations

import datetime
import json
import time
from datetime import timedelta, timezone

import pytest

import runbound
from runbound.config import RunboundConfig
from runbound import api, approvals
from runbound.approvals import canonical_json, make_seal, verify_seal


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


SECRET = "s3cret"
ARGS = {"customer_id": 7, "note": "ünï"}


def seal(**over):
    base = dict(secret=SECRET, service="refunds", tool="delete_customer", request_id="req-1", args=ARGS, timeout_s=600)
    base.update(over)
    return make_seal(**base)


def check(s, **over):
    base = dict(secret=SECRET, service="refunds", tool="delete_customer", request_id="req-1", args=ARGS)
    base.update(over)
    return verify_seal(base["secret"], s, base["service"], base["tool"], base["request_id"], base["args"])


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 1, "a": [2, {"d": 1, "c": 2}]}) == '{"a":[2,{"c":2,"d":1}],"b":1}'


def test_the_seal_has_the_decided_shape_and_verifies():
    s = seal()
    assert set(s) == {"v", "not_after", "mac"} and s["v"] == 1 and s["not_after"].endswith("Z")
    assert len(s["mac"]) == 64 and int(s["mac"], 16) >= 0
    assert check(s) is None


def test_the_mac_is_hmac_sha256_over_the_canonical_body():
    import hashlib
    import hmac

    s = seal()
    body = canonical_json({"v": 1, "service": "refunds", "tool": "delete_customer", "request_id": "req-1",
                           "args": ARGS, "not_after": s["not_after"]})
    assert s["mac"] == hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("over", [
    {"args": {"customer_id": 8, "note": "ünï"}},
    {"tool": "other_tool"},
    {"service": "other"},
    {"request_id": "req-2"},
    {"secret": "wrong"},
])
def test_any_changed_field_or_a_wrong_secret_breaks_the_seal(over):
    assert check(seal(), **over)


def test_a_missing_secret_malformed_seal_or_non_dict_args_never_verifies():
    assert check(seal(), secret=None) and check(seal(), secret="")
    assert check(None) and check("x") and check({"v": 2, "not_after": "x", "mac": "y"})
    assert check({"v": 1, "not_after": "2030-01-01T00:00:00Z", "mac": 5})
    assert check(seal(), args=[1])


def test_not_after_has_a_300_second_tolerance():
    s = make_seal(SECRET, "refunds", "delete_customer", "req-1", ARGS, 0.001)
    limit = datetime.datetime.strptime(s["not_after"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    args = (SECRET, s, "refunds", "delete_customer", "req-1", ARGS)
    assert verify_seal(*args, now=limit + timedelta(seconds=299)) is None
    assert "not_after" in verify_seal(*args, now=limit + timedelta(seconds=301))


def test_the_not_after_check_is_one_sided_a_clock_ahead_fails_and_a_clock_behind_still_verifies():
    # Apollo's amendment: +1 h past not_after fails, -1 h before it still verifies; the plane's claim TTL bounds replay.
    s = make_seal(SECRET, "refunds", "delete_customer", "req-1", ARGS, 0.001)
    limit = datetime.datetime.strptime(s["not_after"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    args = (SECRET, s, "refunds", "delete_customer", "req-1", ARGS)
    assert verify_seal(*args, now=limit + timedelta(hours=1)) == approvals.CLOCK_SKEW_REASON
    assert verify_seal(*args, now=limit - timedelta(hours=1)) is None
    assert verify_seal(*args, now=limit - timedelta(days=30)) is None  # no lower bound at all
    assert "ONE-SIDED" in (verify_seal.__doc__ or "") and "±" not in (verify_seal.__doc__ or "")
    assert "±" not in (approvals.__doc__ or "")


def test_the_seal_does_not_contain_the_secret():
    assert SECRET not in canonical_json(seal())


# --- registration ----------------------------------------------------------------------------------


def test_the_keywords_are_validated():
    for kwargs in ({"approval": "yes"}, {"approval": "required", "mode": "later"},
                   {"approval": "required", "timeout_s": 0}, {"approval": "required", "timeout_s": True},
                   {"mode": "sync"}, {"timeout_s": 5}):
        with pytest.raises(ValueError):
            runbound.tool(**kwargs)(lambda x: x)


def test_a_deferred_tool_must_have_json_friendly_arguments():
    with pytest.raises(ValueError, match="deferred"):
        @runbound.tool(approval="required", mode="deferred")
        def when(at: datetime.datetime): ...

    with pytest.raises(ValueError):
        @runbound.tool(approval="required", mode="deferred")
        def many(*items): ...

    @runbound.tool(approval="required", mode="deferred")
    def ok(n: int, names: list[str], opt: dict | None = None): ...


def test_a_deferred_tool_with_no_secret_says_so_once(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="runbound"):
        @runbound.tool(approval="required", mode="deferred")
        def a(n: int): ...

        @runbound.tool(approval="required", mode="deferred")
        def b(n: int): ...

    assert caplog.text.count("no approval secret is set") == 1


def test_the_secret_comes_from_init_or_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_APPROVAL_SECRET", "from-env")
    runbound.init()
    assert api._ENGINE.config.approval_secret == "from-env"
    runbound.init(approval_secret="from-init")
    assert api._ENGINE.config.approval_secret == "from-init"
    assert "from-init" not in repr(api._ENGINE.config)
    with pytest.raises(ValueError):
        runbound.init(approval_secret="")
    with pytest.raises(ValueError):
        runbound.init(on_deferred_result="nope")


# --- failing closed ----------------------------------------------------------------------------------


def test_with_no_plane_an_approval_tool_is_refused_at_once_and_the_body_never_runs():
    runbound.init()
    ran = []

    @runbound.tool(effects={"financial"}, approval="required", timeout_s=600)
    def wire(amount: float):
        ran.append(amount)

    with pytest.raises(runbound.ExecutionRefused) as caught:
        wire(1.0)
    assert caught.value.reason == "approval_unavailable" and ran == []
    assert caught.value.decision.boundary == "approval"


def test_a_tool_without_approval_is_untouched_by_all_of_it():
    runbound.init()

    @runbound.tool(effects={"read"})
    def look():
        return 1

    assert look() == 1 and runbound.current_request() is None


def test_current_request_is_none_outside_an_execution():
    assert approvals.current_request() is None


# --- a full quota (T1b's 429 on create), with a fake plane -------------------------------------------


class FakePlane:
    fleet = True

    def __init__(self, *replies):
        self.replies, self.calls, self.reports = list(replies), [], []

    def approvals(self, method, path, body=None, timeout=5.0):
        if path.endswith("/report"):  # the background report of an approved run: not a create
            self.reports.append((path, body))
            return 200, {}
        self.calls.append((method, path, body))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


QUOTA_429 = (429, {"error": "quota_exceeded", "notice": "full", "retry_after_s": 42, "window_end": "2030-01-01T00:00:00Z"})


def guarded(plane, **config):
    runbound.init(**config)
    api._ENGINE.shared = plane

    @runbound.tool(effects={"financial"}, approval="required", mode="sync", timeout_s=30)
    def wire(amount: float):
        return "sent"

    return wire


def test_a_429_on_create_is_quota_exceeded_with_retry_after_and_no_retry():
    plane = FakePlane(QUOTA_429)
    wire = guarded(plane)
    with pytest.raises(runbound.ExecutionRefused) as caught:
        wire(1.0)
    assert caught.value.reason == "quota_exceeded" and caught.value.retry_after == 42.0
    assert len(plane.calls) == 1  # one create, never retried inside the loop
    assert caught.value.decision.boundary == "approval"


def test_nothing_is_asked_again_inside_the_window():
    plane = FakePlane(QUOTA_429)
    wire = guarded(plane)
    for _ in range(3):
        with pytest.raises(runbound.ExecutionRefused) as caught:
            wire(1.0)
        assert caught.value.reason == "quota_exceeded"
    assert len(plane.calls) == 1  # the 2nd and 3rd calls were refused without a POST
    assert 0 < caught.value.retry_after <= 42.0


def test_the_block_is_per_tool_and_per_key_and_lifts_after_the_window(monkeypatch):
    plane = FakePlane(QUOTA_429, (201, {"id": "a1", "status": "approved", "quota": {"state": "enforced"}}))
    wire = guarded(plane)
    with runbound.session("user-1"):
        with pytest.raises(runbound.ExecutionRefused):
            wire(1.0)
    with runbound.session("user-2"):  # a different key is a different quota
        assert wire(1.0) == "sent"
    assert len(plane.calls) == 2 and plane.calls[0][2]["key"] != plane.calls[1][2]["key"]
    now = approvals.time.monotonic()
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now + 100)
    with runbound.session("user-1"):
        assert wire(1.0) == "sent"  # past retry_after_s the create is tried again
    assert len(plane.calls) == 3


def test_the_create_carries_the_key_hash_and_the_same_request_id_on_a_retry():
    plane = FakePlane((503, None), (201, {"id": "a1", "status": "approved", "quota": None}))
    wire = guarded(plane)
    with runbound.session("user-9"):
        assert wire(2.0) == "sent"
    creates = [c for c in plane.calls if c[0] == "POST"]
    assert len(creates) == 2 and creates[0][2]["request_id"] == creates[1][2]["request_id"]  # never takes twice
    assert creates[0][2]["key"] and "user-9" not in str(creates)  # a hash, not the key


def test_a_retry_after_that_is_not_a_number_falls_back_to_a_minute():
    plane = FakePlane((429, {"error": "quota_exceeded", "retry_after_s": "soon"}))
    wire = guarded(plane)
    with pytest.raises(runbound.ExecutionRefused) as caught:
        wire(1.0)
    assert caught.value.retry_after == 60.0


def test_an_approval_object_with_a_quota_field_is_tolerated():
    plane = FakePlane((201, {"id": "a1", "status": "approved", "quota": {"tool": "wire", "state": "enforced"}, "extra": 1}))
    assert guarded(plane)(3.0) == "sent"


# --- policy tightens between approval and deferred execution ----------------------------------------------


def test_a_policy_that_tightens_after_approval_stops_the_deferred_execution():
    """Deferred execution re-runs the whole chain except the approval question (Apollo's ruling 2)."""
    import time as _time

    plane = FakePlane((201, {"id": "a1", "status": "pending", "quota": {"tool": "archive", "state": "enforced"}}))
    runbound.init(approval_secret="s3cret")
    api._ENGINE.shared = plane
    results, ran = [], []
    api._ENGINE.config.on_deferred_result = results.append

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        ran.append(ticket)

    out = archive(7)  # filed, sealed, parked: nothing runs
    assert out["status"] == "pending_approval"
    (create,) = [c for c in plane.calls if c[1] == "/v1/approvals"]
    request_id, seal = create[2]["request_id"], create[2]["seal"]

    # ... a person approves; before this worker executes, the org tightens the policy:
    api._ENGINE.config.tool_policy = runbound.ToolPolicy(deny=["archive"])
    item = {"id": "a1", "tool": "archive", "args": {"ticket": 7}, "request_id": request_id, "seal": seal,
            "service": api._ENGINE.config.service, "expires_at": "2099-01-01T00:00:00Z"}
    approvals._execute(item)

    assert ran == []  # the tool did not run
    deadline = _time.monotonic() + 5
    while not plane.reports and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert [(path, body["outcome"]) for path, body in plane.reports] == [("/v1/approvals/a1/report", "failed")]
    assert results[0]["outcome"] == "failed"
    refused = [r for r in runbound.tool_results() if r["outcome"] == "refused"]
    assert len(refused) == 1 and refused[0]["tool"] == "archive"
    refusal = [e for e in runbound.events() if e["kind"] == "refusal"][-1]
    assert refusal["detector"] == "policy" and "deny list" in refusal["message"]  # the policy reason
    # no second quota unit: the only approval-route calls are the one create and the one report
    assert [c[1] for c in plane.calls] == ["/v1/approvals"]
    assert len(plane.reports) == 1


# --- clock_skew is not tampered; a long execution renews its claim on each hello ------------------------


def parked(plane, secret="s3cret", seal_now=None):
    """File a sealed deferred request on a fake plane; returns the approved item a hello would carry."""
    runbound.init(approval_secret=secret)
    api._ENGINE.shared = plane
    results, ran = [], []
    api._ENGINE.config.on_deferred_result = results.append

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        ran.append((ticket, runbound.current_request()))
        return ticket

    out = archive(7)
    create = [c for c in plane.calls if c[1] == "/v1/approvals"][-1][2]
    seal = create["seal"]
    if seal_now is not None:  # re-seal as if the request had been filed long ago
        seal = make_seal(secret, create["service"], "archive", create["request_id"], create["args"], 1, now=seal_now)
    item = {"id": "a1", "tool": "archive", "args": create["args"], "request_id": out["request_id"], "seal": seal,
            "service": create["service"], "expires_at": "2099-01-01T00:00:00Z"}
    return item, results, ran


def wait_reports(plane, n=1):
    deadline = time.monotonic() + 5
    while len(plane.reports) < n and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_valid_mac_with_an_expired_not_after_is_clock_skew_not_tampered():
    plane = FakePlane((201, {"id": "a1", "status": "pending"}))
    long_ago = datetime.datetime(2020, 1, 1, tzinfo=timezone.utc)
    item, results, ran = parked(plane, seal_now=long_ago)
    approvals._execute(item)
    wait_reports(plane)
    assert ran == [] and [b["outcome"] for _, b in plane.reports] == ["clock_skew"]
    assert results[0]["outcome"] == "clock_skew" and results[0]["error"] == "clock_skew"


def test_a_wrong_mac_is_still_tampered():
    plane = FakePlane((201, {"id": "a1", "status": "pending"}))
    item, results, ran = parked(plane)
    item["args"] = {"ticket": 999}
    approvals._execute(item)
    wait_reports(plane)
    assert ran == [] and [b["outcome"] for _, b in plane.reports] == ["tampered"]
    assert results[0]["outcome"] == "tampered"


def test_a_running_execution_is_named_on_every_hello_until_it_finishes():
    import threading

    plane = FakePlane((201, {"id": "a1", "status": "pending"}))
    item, results, ran = parked(plane)
    started, release = threading.Event(), threading.Event()
    real = approvals._EXECUTORS["archive"]

    def slow(**kwargs):
        started.set()
        release.wait(5)
        return real(**kwargs)

    approvals._EXECUTORS["archive"] = slow
    thread = threading.Thread(target=approvals._execute, args=(item,))
    thread.start()
    assert started.wait(5)
    assert approvals.executing_ids() == ["a1"]
    from runbound.shared import RemoteState

    shared = RemoteState(type("C", (), {"key_state": "unknown"})(), None, api._ENGINE.config)
    assert shared._hello_payload()["renew_claims"] == ["a1"]  # each hello renews the claim
    release.set()
    thread.join(5)
    assert approvals.executing_ids() == [] and "renew_claims" not in shared._hello_payload()
    assert ran and ran[0][1] == item["request_id"]  # current_request() is the idempotency key


def test_a_claim_the_plane_took_back_stops_being_renewed_and_a_cap_of_twenty_applies():
    from runbound.plane_types import HelloReply
    from runbound.shared import RemoteState

    with approvals._LOCK:
        approvals._EXECUTING.update({f"a{n}" for n in range(25)})
    shared = RemoteState(type("C", (), {"key_state": "unknown"})(), None, RunboundConfig(service="svc"))
    assert len(shared._hello_payload()["renew_claims"]) == 20
    shared.apply_hello(HelloReply(claims_lost=["a3", "a4", "not-ours"]))
    renewed = shared._hello_payload()["renew_claims"]
    assert "a3" not in renewed and "a4" not in renewed
    assert HelloReply().claims_lost == []


# --- S7b: reports the plane could not take are queued and drained ------------------------------------------


def make_queue(tmp_path):
    return approvals.ReportQueue(tmp_path / "svc.reports.jsonl")


def rec(approval_id="a1", outcome="executed", **extra):
    return {"approval_id": approval_id, "outcome": outcome, "worker_id": "w", **extra}


def test_the_queue_is_append_only_0600_and_holds_no_args_or_results(tmp_path):
    import os
    import stat

    queue = make_queue(tmp_path)
    queue.append(rec(result_digest="d" * 64))
    queue.append(rec("a2", "failed"))
    path = tmp_path / "svc.reports.jsonl"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and len(path.read_text().splitlines()) == 2
    assert queue.pending() == 2
    assert set(json.loads(path.read_text().splitlines()[0])) == {"approval_id", "outcome", "worker_id", "result_digest"}


def test_drain_sends_each_record_once_keeps_the_unsent_and_dedupes(tmp_path):
    queue = make_queue(tmp_path)
    for r in (rec("a1"), rec("a1"), rec("a2", "failed"), rec("a3")):  # a1/executed twice
        queue.append(r)
    assert queue.pending() == 3  # idempotent on (approval id, outcome)
    sent = []

    def send(record):
        sent.append(record["approval_id"])
        return record["approval_id"] != "a2"  # the plane still cannot take a2

    assert queue.drain(send) == 2 and sent == ["a1", "a2", "a3"]
    assert queue.pending() == 1 and [r["approval_id"] for r in queue._read()] == ["a2"]
    assert queue.drain(lambda r: True) == 1 and queue.pending() == 0
    assert not (tmp_path / "svc.reports.jsonl").exists()


def test_a_torn_last_line_from_a_crash_is_skipped(tmp_path):
    queue = make_queue(tmp_path)
    queue.append(rec("a1"))
    with open(tmp_path / "svc.reports.jsonl", "a") as out:
        out.write('{"approval_id": "a2", "outc')
    assert queue.pending() == 1


def test_with_no_directory_the_queue_lives_in_memory():
    queue = approvals.ReportQueue(None)
    queue.append(rec())
    assert queue.pending() == 1 and queue.drain(lambda r: True) == 1 and queue.pending() == 0


def test_a_report_the_plane_cannot_take_is_queued_and_drained_on_the_next_connected_hello(tmp_path):
    from runbound.plane_types import HelloReply
    from runbound.shared import RemoteState

    class Plane:
        def __init__(self):
            self.up, self.posted = False, []
            self.key_state = "unknown"

        def approvals(self, method, path, body=None, timeout=5.0):
            if not self.up:
                return None, None  # the plane is down
            self.posted.append((path, body["outcome"]))
            return 200, {}

    plane = Plane()
    config = RunboundConfig(token="t", control_plane_url="http://plane.example", service="svc", cache_dir=str(tmp_path))
    config.validate()
    shared = RemoteState(plane, None, config)
    engine = type("E", (), {"config": config, "shared": shared})()
    real_sleep = approvals.time.sleep
    approvals.time.sleep = lambda s: None  # do not wait out the retry backoff
    try:
        approvals.report_async(engine, "a1", "executed", {"ticket": 7})
        deadline = time.monotonic() + 5
        while shared.reports.pending() == 0 and time.monotonic() < deadline:
            real_sleep(0.01)
    finally:
        approvals.time.sleep = real_sleep
    assert shared.reports.pending() == 1 and plane.posted == []
    text = next(tmp_path.rglob("*.reports.jsonl")).read_text()
    assert "ticket" not in text and "executed" in text  # a digest at most, never the result

    shared.apply_hello(HelloReply(fleet_state="unavailable"))  # the plane answered but is not well: not a drain
    assert plane.posted == []
    plane.up = True
    shared.apply_hello(HelloReply())  # connected: drained
    assert plane.posted == [("/v1/approvals/a1/report", "executed")] and shared.reports.pending() == 0
    shared.apply_hello(HelloReply())
    assert len(plane.posted) == 1  # exactly once


# --- P9: who the call is made for ------------------------------------------------------------------------------------


def test_session_requested_for_rides_the_approval_create_and_only_inside_the_block():
    plane = FakePlane((201, {"id": "a1", "status": "approved", "quota": None}))
    wire = guarded(plane)
    with runbound.session("user-1", requested_for="  ana@acme.example "):
        assert wire(1.0) == "sent"
    assert wire(2.0) == "sent"  # outside the block: nothing is named
    first, second = [c[2] for c in plane.calls if c[0] == "POST"]
    assert first["requested_for"] == "ana@acme.example" and "requested_for" not in second


def test_a_nested_block_restores_the_outer_requested_for_and_threads_do_not_share_it():
    import threading

    plane = FakePlane((201, {"id": "a1", "status": "approved", "quota": None}))
    wire = guarded(plane)
    seen = {}
    with runbound.session("outer", requested_for="outer@x"):
        with runbound.session("inner", requested_for="inner@x"):
            wire(1.0)
        wire(2.0)
        thread = threading.Thread(target=lambda: seen.update(done=wire(3.0)))  # a new thread starts with no context
        thread.start()
        thread.join()
    who = [c[2].get("requested_for") for c in plane.calls if c[0] == "POST"]
    assert who == ["inner@x", "outer@x", None]


def test_a_requested_for_that_is_not_an_id_of_at_most_128_characters_is_ignored_not_raised():
    plane = FakePlane((201, {"id": "a1", "status": "approved", "quota": None}))
    wire = guarded(plane)
    for bad in ("x" * 129, "", "   ", 7):
        with runbound.session("k", requested_for=bad):  # type: ignore[arg-type]
            assert wire(1.0) == "sent"
    assert all("requested_for" not in c[2] for c in plane.calls if c[0] == "POST")
    assert approvals.clean_requested_for("y" * 128) == "y" * 128
