"""After a restart, a consequential call made before the first policy confirmation waits a bounded time for it, then is judged as ever (``init(confirm_wait_s)``, default 3 s).

The binding (Codex): a restart cannot renew the offline window. The wait only waits: it never sets or extends a confirmation, an unconfirmed hello ends it and the call is still refused,
and a read tool, a fresh confirmation, a service without enforced defaults or a call after the first ``confirm_wait_s`` seconds of the process never waits."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import runbound
from runbound import api
from test_offline_window import DESTRUCTIVE, WALL0, advance, body, hello, world
from test_policy_rows import FakeShared, start
from test_shared_state import MovableClock, config, remote

WINDOW = {"execution_s": 900}
WORDS = "the offline-execution window of 900 s has ended"


def restarted(tmp_path, wait_s=2.0, quiet_s=1800):
    """A first process that confirmed the policy at WALL0, then a NEW process quiet_s later whose cache says so: its confirmation is expired and its first hello is still in flight."""
    first, _, clock, wall = world(WINDOW, tmp_path)
    hello(first)
    second, _, clock2, wall2 = world(WINDOW, tmp_path, confirm_wait_s=wait_s)
    wall2.value = WALL0 + quiet_s
    assert second._confirmed_wall == WALL0 and second.awaiting_first_policy() is False
    return second, clock2, wall2


def admitted(shared, effects=DESTRUCTIVE):
    """What one admission does, in its order: the bounded wait first (``Engine.admit_call``), the plane gate's judgment after (``gates.plane``)."""
    shared.await_first_confirmation(effects)
    return shared.plane_refusal(effects)


def later(seconds, fn):
    thread = threading.Timer(seconds, fn)
    thread.start()
    return thread


def test_a_call_waits_for_the_first_confirmation_and_then_runs(tmp_path):
    second, _, wall2 = restarted(tmp_path)
    timer = later(0.15, lambda: hello(second))
    started = time.monotonic()
    assert admitted(second) is None, "the first hello confirmed the policy while the call waited"
    timer.join()
    assert 0.1 < time.monotonic() - started < 1.5 and 100 <= second.take_waited_ms() < 1500
    assert second._confirmed_wall == wall2.value, "the confirmation is the hello's, stamped when it arrived"


def test_with_no_hello_in_time_the_call_is_refused_as_before_after_the_whole_budget(tmp_path):
    second, _, _ = restarted(tmp_path, wait_s=0.3)
    started = time.monotonic()
    assert admitted(second).startswith(WORDS)
    assert time.monotonic() - started >= 0.25 and second.take_waited_ms() >= 250
    assert second.take_waited_ms() is None, "read once"


def test_an_unconfirmed_hello_ends_the_wait_and_the_call_is_still_refused(tmp_path):
    second, _, _ = restarted(tmp_path, wait_s=5.0)
    before = second._confirmed_wall
    timer = later(0.1, lambda: hello(second, plane_degraded="ledger_unreachable"))
    started = time.monotonic()
    assert admitted(second).startswith(WORDS), "an answer that confirmed nothing allows nothing"
    timer.join()
    assert time.monotonic() - started < 2.0, "the answer ended the wait"
    assert second._confirmed_wall == before, "and renewed nothing"


def test_only_the_first_seconds_after_boot_wait(tmp_path):
    second, clock2, _ = restarted(tmp_path, wait_s=0.3)
    clock2.advance(5.0)  # the process is older than the budget
    started = time.monotonic()
    assert admitted(second).startswith(WORDS)
    assert time.monotonic() - started < 0.2 and second.take_waited_ms() is None


@pytest.mark.parametrize("effects", [frozenset({"read"}), frozenset({"external"})])
def test_a_tool_that_does_not_fail_closed_never_waits(tmp_path, effects):
    second, _, _ = restarted(tmp_path, wait_s=0.5)
    assert second.boot_wait_pending(effects) is False
    started = time.monotonic()
    admitted(second, effects)
    assert time.monotonic() - started < 0.2


def test_a_fresh_confirmation_or_the_switch_off_never_waits(tmp_path):
    first, _, clock, wall = world(WINDOW, tmp_path, confirm_wait_s=2.0)
    hello(first)
    assert first.boot_wait_pending(DESTRUCTIVE) is False, "inside the window"
    off, _, _ = restarted(tmp_path, wait_s=0)
    assert off.boot_wait_pending(DESTRUCTIVE) is False and off.plane_refusal(DESTRUCTIVE).startswith(WORDS)


def test_a_restart_still_cannot_renew_the_window(tmp_path):
    second, _, _ = restarted(tmp_path, wait_s=0.1)
    assert admitted(second).startswith(WORDS)
    assert second._confirmed_wall == WALL0, "waiting changed no confirmation"
    assert admitted(second).startswith(WORDS), "and a second call after the budget is refused too"


@pytest.mark.parametrize("bad", [-1, 11, True, "3", None])
def test_confirm_wait_s_is_validated_between_zero_and_ten(bad):
    with pytest.raises(ValueError, match="confirm_wait_s"):
        config(confirm_wait_s=bad).validate()


@pytest.mark.parametrize("good", [0, 0.5, 3, 10])
def test_confirm_wait_s_accepts_zero_to_ten(good):
    config(confirm_wait_s=good).validate()


def test_an_async_tool_that_waits_does_not_stall_the_event_loop():
    class Waiting(FakeShared):
        """A worker that has just restarted: its first confirmation arrives 0.6 s after the call is made."""

        def boot_wait_pending(self, effects):
            return True

        def await_first_confirmation(self, effects):
            time.sleep(0.6)

    api._teardown_for_tests()
    start(Waiting())

    @runbound.tool(effects={"destructive"}, reviewed=True)
    async def purge(item: str):
        return "purged"

    async def main():
        ticks = []

        async def ticker():
            for _ in range(10):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        result, _ = await asyncio.gather(purge(item="x"), ticker())
        return result, ticks

    try:
        result, ticks = asyncio.run(main())
    finally:
        api._teardown_for_tests()
    assert result == "purged"
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.35, f"the loop kept running while the call waited (largest gap {max(gaps):.2f} s)"


# --- the safety rule (Apollo's pre-read): the wait is BEFORE the policy is read -----------------------------------------------------------------------------------------------


ALLOWS = {"tool": "purge", "state": "enforced", "lines": [{"id": "L1", "decision": "allow", "approval": None, "limits": None}], "otherwise": "allow", "rule_revision": 1}
BLOCKS = {"tool": "purge", "state": "enforced", "blocked": True, "rule_revision": 2}


def policy(version, served_row):
    served_body = body(WINDOW, version=version)
    served_body["rows"] = [served_row]
    served_body["rows_hash"] = served_body["cfg_hash"] = f"h{version}"
    return served_body


def process(plane_body, tmp_path, **cfg):
    from test_shared_state import FakePlane as Plane

    plane = Plane(policy_body=plane_body)
    clock, wall = MovableClock(), MovableClock(WALL0)
    cfg["cache_dir"] = str(tmp_path)
    shared = remote(plane, clock, **cfg)
    shared._wall = wall
    return shared, wall


def test_a_tool_the_first_hello_restricts_is_judged_by_the_new_rule_and_never_runs_on_the_stale_cached_allow(tmp_path):
    """An operator BLOCKED the tool while the worker was down. The cache still ALLOWS it outright. The call that arrives before the first hello waits, the hello confirms the NEWER policy (blocking the tool,
    fetched in that same hello), and the call must be decided by that policy, not by the row read before the wait."""
    first, _ = process(policy(1, ALLOWS), tmp_path)
    hello(first, version=1)
    assert not first.row_for("purge").blocked
    second, wall2 = process(policy(2, BLOCKS), tmp_path, confirm_wait_s=3.0)
    wall2.value = WALL0 + 1800  # restarted after a quiet period: the cached confirmation has expired
    assert not second.row_for("purge").blocked and not second.row_for("purge").inherit, "what the cache says before the first hello: allowed outright"
    ran = []
    api._teardown_for_tests()
    start(second)

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def purge():
        ran.append(1)
        return "purged"

    timer = later(0.2, lambda: hello(second, version=2))
    try:
        with pytest.raises(runbound.ExecutionRefused) as refused:
            purge()
    finally:
        timer.join()
        api._teardown_for_tests()
    assert ran == [], "the tool the served policy blocks did not run"
    assert second.row_for("purge").blocked and refused.value.reason != "plane_unavailable", "refused by the NEW rule (blocked), not by the expired window"
    assert refused.value.reason == "tool_blocked"
