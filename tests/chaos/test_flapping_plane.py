"""SDK-E1b chaos harness: S7's invariants hold while the plane misbehaves on a script.

The invariants (API.md, "Plane loss and the policy cache"):

* a consequential tool (financial, destructive, privileged, or no class at all) refuses ``plane_unavailable`` once the grace has
  passed since the last answered hello; before that it runs;
* a read, write or external tool never refuses for plane loss, at any point of any outage;
* the last policy is enforced from the on-disk cache BEFORE the first hello;
* modes only ever walk connecting -> connected -> degraded -> unavailable -> connected (a hello answered is the only way back);
* a halt follows the plane's word at once and holds until the plane lifts it, however long the plane is quiet;
* one approval offered twice while it runs executes once.
"""

from __future__ import annotations

import threading
import time

import pytest

import runbound
from runbound import approvals
from runbound.plane_types import HelloReply

from .flapping import Phase, Rig  # noqa: F401  (Rig for type readers)


def tools():
    ran = []

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        ran.append("look")
        return "ok"

    @runbound.tool(effects={"financial"}, reviewed=True)
    def pay():
        ran.append("pay")
        return "ok"

    @runbound.tool(reviewed=True)
    def unclassified():
        ran.append("unclassified")
        return "ok"

    return look, pay, unclassified, ran


def outcome(fn):
    try:
        return fn()
    except runbound.ExecutionRefused as exc:
        return exc.reason


def test_a_503_burst_shorter_than_the_grace_changes_nothing(rig_factory):
    rig = rig_factory([Phase(10), Phase(30, "503"), Phase(60)], grace_s=60)
    look, pay, unclassified, ran = tools()
    for _ in range(14):  # 70 s: through the whole burst and out the far side
        rig.tick()
        assert (outcome(look), outcome(pay), outcome(unclassified)) == ("ok", "ok", "ok")
    assert "unavailable" not in rig.modes_seen()


def test_a_burst_longer_than_the_grace_refuses_consequential_tools_and_never_the_read_tool(rig_factory):
    rig = rig_factory([Phase(10), Phase(60, "503"), Phase(60)], grace_s=15)
    look, pay, unclassified, ran = tools()
    seen = []
    for _ in range(24):  # 120 s
        rig.tick()
        seen.append((rig.clock() - rig.plane.started, outcome(look), outcome(pay), outcome(unclassified)))
    during = [row for row in seen if 10 + 15 + 5 < row[0] < 70]  # well past the grace, still inside the burst
    assert during and all(row[1] == "ok" and row[2] == "plane_unavailable" and row[3] == "plane_unavailable" for row in during)
    after = [row for row in seen if row[0] > 80]  # answered again
    assert after and all(row[1:] == ("ok", "ok", "ok") for row in after)
    assert all(row[1] == "ok" for row in seen)  # the read tool never refused, at any point


def test_plane_unavailable_is_retryable_and_the_body_never_ran(rig_factory):
    rig = rig_factory([Phase(5), Phase(300, "refused")], grace_s=15)
    look, pay, unclassified, ran = tools()
    rig.run(60)
    with pytest.raises(runbound.ExecutionRefused) as caught:
        pay()
    assert caught.value.reason == "plane_unavailable" and caught.value.retryable is True
    assert "pay" not in ran


def test_the_modes_walk_connected_degraded_unavailable_and_back_and_only_that_way(rig_factory):
    rig = rig_factory([Phase(10), Phase(100, "503"), Phase(30)])
    rig.run(150)
    assert rig.modes_seen() == ["connected", "degraded", "unavailable", "connected"]
    assert rig.plane.hellos[-1][1] == "ok"  # the way back is an answered hello


def test_slow_hellos_past_the_timeout_count_as_misses_and_three_in_a_row_are_degraded(rig_factory):
    rig = rig_factory([Phase(10), Phase(40, "slow"), Phase(30)])
    look, pay, unclassified, ran = tools()
    modes = []
    for _ in range(16):
        rig.tick()
        modes.append(rig.shared.status().mode)
        assert outcome(look) == "ok"
    assert "degraded" in modes and modes[-1] == "connected"
    first_degraded = modes.index("degraded")
    assert rig.plane.hellos[first_degraded][1] == "slow" and first_degraded >= 2 + 2  # two ok hellos, then three misses


def test_the_cache_is_enforced_before_the_first_hello(rig_factory):
    rig = rig_factory([Phase(300, "refused")], cached_deny=("pay",))
    look, pay, unclassified, ran = tools()
    assert rig.shared.status().mode == "connecting" and rig.plane.hellos == []  # nothing has been asked of the plane yet
    assert outcome(pay) == "tool_blocked" and "pay" not in ran  # the cached deny rule holds with no plane at all
    assert outcome(look) == "ok"


def test_a_halt_follows_the_plane_at_once_and_holds_when_the_plane_goes_quiet(rig_factory):
    rig = rig_factory([Phase(10), Phase(20, "ok", {"halt": True}), Phase(180, "503")])
    look, pay, unclassified, ran = tools()
    rig.run(10)
    assert outcome(look) == "ok"
    rig.run(5)  # a halting hello
    assert rig.shared.halted() is True and outcome(look) == "halt"
    rig.run(15)
    rig.run(80)  # the plane has been quiet well past a minute: the halt is still enforced (F1)
    assert rig.shared.halted() is True and outcome(look) == "halt"


def test_a_halt_that_flips_on_and_off_is_followed_each_time(rig_factory):
    script = [Phase(10), Phase(10, "ok", {"halt": True}), Phase(10), Phase(10, "ok", {"halt": True}), Phase(20)]
    rig = rig_factory(script)
    verdicts = []
    for _ in range(12):
        rig.tick()
        verdicts.append(rig.shared.halted())
    assert verdicts == [False, False, True, True, False, False, True, True, False, False, False, False]


def test_a_consequential_tool_refuses_exactly_when_the_grace_has_passed_over_a_long_flapping_run(rig_factory):
    # ok 20 s / dead 50 s, repeated: the verdict at every tick must follow "the grace has been reached since the last answered hello".
    cycle = [Phase(20), Phase(50, "503")]
    rig = rig_factory(cycle * 6, grace_s=30)
    look, pay, unclassified, ran = tools()
    last_ok = rig.clock()
    for _ in range(80):
        before = len(rig.plane.hellos)
        rig.tick()
        if rig.plane.hellos[before][1] == "ok":
            last_ok = rig.clock() - rig.poll_s  # the hello's own instant
        since = rig.clock() - last_ok
        verdict = outcome(pay)
        assert (verdict == "plane_unavailable") == (since >= 30), (since, verdict)  # at the grace, not after it
        assert outcome(look) == "ok"


def test_the_same_approval_offered_twice_while_it_runs_executes_once(rig_factory):
    rig_factory([Phase(300)])
    runs, gate, done = [], threading.Event(), []

    runbound.init(approval_secret="chaos-secret", on_deferred_result=done.append)
    from runbound import api

    # the rig's shared stays the engine's: re-point after init
    shared = api._SHARED

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def delete(customer_id: str):
        runs.append((customer_id, runbound.current_request()))
        gate.wait(5)
        return "deleted"

    seal = approvals.make_seal("chaos-secret", api._ENGINE.config.service, "delete", "req-1", {"customer_id": "c1"}, 60)
    item = {"id": "a-1", "tool": "delete", "request_id": "req-1", "args": {"customer_id": "c1"}, "seal": seal,
            "service": api._ENGINE.config.service, "expires_at": None}
    approvals.handle_approved([item])
    deadline = time.monotonic() + 5
    while not runs and time.monotonic() < deadline:
        time.sleep(0.01)
    approvals.handle_approved([item])  # the plane offers it again (a second hello, a replica) while the body is still running
    gate.set()
    deadline = time.monotonic() + 5
    while not done and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    assert runs == [("c1", "req-1")] and shared is api._SHARED  # once, and its idempotency key was the request id


def test_an_approval_offered_again_after_it_finished_runs_again_with_the_same_request_id(rig_factory):
    # What the SDK does NOT do: remember finished approvals. The plane's report and claim bound a second offer; the body's own
    # protection is current_request() as an idempotency key. Pinned so a change here is a decision, not an accident.
    rig_factory([Phase(300)])
    runs, done = [], []
    runbound.init(approval_secret="chaos-secret", on_deferred_result=done.append)
    from runbound import api

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def delete(customer_id: str):
        runs.append(runbound.current_request())
        return "deleted"

    seal = approvals.make_seal("chaos-secret", api._ENGINE.config.service, "delete", "req-2", {"customer_id": "c1"}, 60)
    item = {"id": "a-2", "tool": "delete", "request_id": "req-2", "args": {"customer_id": "c1"}, "seal": seal,
            "service": api._ENGINE.config.service, "expires_at": None}
    for expected in (1, 2):
        approvals.handle_approved([item])
        deadline = time.monotonic() + 5
        while len(runs) < expected and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
    assert runs == ["req-2", "req-2"]


def test_plane_degraded_flaps_with_the_script_and_each_change_is_one_event(rig_factory):
    from runbound import local_events

    script = [Phase(15, "ok", {"plane_degraded": "ledger_unreachable"}), Phase(15), Phase(15, "ok", {"plane_degraded": "ledger_unreachable"}),
              Phase(15)]
    rig = rig_factory(script)
    before = len([e for e in local_events.events(1000) if e["kind"] == "plane_degraded"])
    seen = []
    for _ in range(12):
        rig.tick()
        seen.append(rig.shared.status().plane_degraded)
    assert seen == ["ledger_unreachable"] * 3 + [None] * 3 + ["ledger_unreachable"] * 3 + [None] * 3
    events = [e for e in local_events.events(1000) if e["kind"] == "plane_degraded"][before:]
    assert [e["to"] for e in events] == ["ledger_unreachable", None, "ledger_unreachable", None]
    assert all(rig.shared.status().mode == "connected" for _ in [0])  # a plane that says it is degraded is still a connected link
