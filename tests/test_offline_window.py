"""PS-OFFLINE-2: the offline-execution window. A service with enforced defaults runs consequential tools on its last POLICY CONFIRMATION for a bounded window while the plane is
away: 15 minutes for a new service, 24 hours until an operator changes it for an existing one. Only an authenticated, whole hello after which the worker holds the policy the plane
announced renews it; a restart resumes from the cache's ``confirmed_at``; expiry only ever refuses more.

Every clock is injected (monotonic and wall); nothing sleeps.
"""

import logging

import pytest

from runbound import cache as cache_module
from runbound.plane_types import HelloReply
from test_service_defaults import served
from test_shared_state import FakePlane, MovableClock, remote

DESTRUCTIVE = frozenset({"destructive"})
WALL0 = 1_800_000_000.0
ABSENT = object()


def body(offline=ABSENT, version=1):
    defaults = served(state="enforced", revision=1, decision={"destructive": "ask"})
    if offline is not ABSENT:
        defaults["offline"] = offline
    return {"version": version, "policy": None, "rows": [], "rows_hash": "h%d" % version, "cfg_hash": "h%d" % version, "service_defaults": defaults}


def world(offline=ABSENT, tmp_path=None, **cfg):
    plane = FakePlane(policy_body=body(offline))
    clock, wall = MovableClock(), MovableClock(WALL0)
    if tmp_path is None:  # never the platform cache dir: another test's (or run's) cache would be loaded
        import tempfile

        tmp_path = tempfile.mkdtemp(prefix="rb-offline-")
    cfg["cache_dir"] = str(tmp_path)
    shared = remote(plane, clock, **cfg)
    shared._wall = wall
    return shared, plane, clock, wall


def hello(shared, version=1, **kw):
    shared.apply_hello(HelloReply(policy_version=version, **kw))


def advance(clock, wall, seconds):
    clock.advance(seconds)
    wall.advance(seconds)


def test_inside_the_window_a_consequential_tool_runs_and_at_its_end_it_is_refused():
    shared, _, clock, wall = world({"execution_s": 900, "source": "default", "review": False})
    hello(shared)
    assert shared.offline_window_s() == 900
    advance(clock, wall, 899)
    assert shared.plane_refusal(DESTRUCTIVE) is None
    advance(clock, wall, 2)
    assert shared.plane_refusal(DESTRUCTIVE) == "the offline-execution window of 900 s has ended and the plane has not confirmed the policy"


def test_an_unconfirmed_hello_does_not_extend_the_window_and_a_confirmed_one_does():
    shared, plane, clock, wall = world({"execution_s": 900})
    hello(shared)
    advance(clock, wall, 800)
    hello(shared, plane_degraded="ledger_unreachable")  # answered, authenticated, but the plane says it is not whole
    hello(shared, fleet_state="unavailable")  # answered, but the plane cannot read its own state
    advance(clock, wall, 101)
    assert shared.plane_refusal(DESTRUCTIVE) is not None, "neither hello was a confirmation: 901 s since the last one"
    hello(shared)  # a whole hello holding the announced policy
    assert shared.plane_refusal(DESTRUCTIVE) is None
    advance(clock, wall, 899)
    assert shared.plane_refusal(DESTRUCTIVE) is None


def test_a_hello_whose_policy_fetch_failed_is_not_a_confirmation():
    shared, plane, clock, wall = world({"execution_s": 900})
    hello(shared)
    advance(clock, wall, 800)
    plane.policy_body = None  # the plane announces version 2 and the read fails
    hello(shared, version=2)
    advance(clock, wall, 101)
    assert shared.plane_refusal(DESTRUCTIVE) is not None
    plane.policy_body = body({"execution_s": 900}, version=2)
    hello(shared, version=2)  # the fetch works: now it holds what was announced
    assert shared.plane_refusal(DESTRUCTIVE) is None


def test_a_restart_resumes_from_the_cache_and_is_never_renewed_by_booting(tmp_path):
    first, _, clock, wall = world({"execution_s": 900}, tmp_path)
    hello(first)
    advance(clock, wall, 800)
    hello(first, plane_degraded="ledger_unreachable")  # an unconfirmed hello that rewrites the file
    advance(clock, wall, 61)
    hello(first, plane_degraded="ledger_unreachable")  # past the refresh throttle: the file is written again, still unconfirmed
    for restart_at, refused in ((WALL0 + 899, False), (WALL0 + 901, True), (WALL0 + 5000, True)):
        second, _, _, wall2 = world({"execution_s": 900}, tmp_path)  # a new process: its monotonic clock knows nothing of the confirmation
        wall2.value = restart_at
        assert second.awaiting_first_policy() is False
        assert (second.plane_refusal(DESTRUCTIVE) is not None) is refused, restart_at
        assert second.offline_window_s() == 900


def test_a_cache_written_by_an_older_sdk_is_aged_by_its_written_at(tmp_path):
    first, _, clock, wall = world({"execution_s": 900}, tmp_path)
    hello(first)
    path = first._cache_path
    stored, _ = cache_module.read(path, "k", "checkout")
    stored.pop("confirmed_at")  # the field an older SDK never wrote
    stored["written_at"] = WALL0 + 100
    path.unlink()
    assert cache_module.write(path, "k", stored)
    second, _, _, wall2 = world({"execution_s": 900}, tmp_path)
    wall2.value = WALL0 + 100 + 899
    assert second.plane_refusal(DESTRUCTIVE) is None
    wall2.value = WALL0 + 100 + 901
    assert second.plane_refusal(DESTRUCTIVE) is not None


def test_a_confirmation_dated_in_the_future_or_a_wall_clock_that_went_back_is_expired():
    shared, _, clock, wall = world({"execution_s": 900})
    hello(shared)
    wall.value = WALL0 - 6  # the wall clock moved back past the confirmation
    assert shared.plane_refusal(DESTRUCTIVE) is not None
    wall.value = WALL0 - 4
    assert shared.plane_refusal(DESTRUCTIVE) is None, "within 5 s is tolerated"
    wall.value, clock.value = WALL0 + 50, clock.value + 600  # 600 s passed on the monotonic clock, 50 on the wall clock: it went back
    assert shared.plane_refusal(DESTRUCTIVE) is not None


def test_a_loaded_confirmation_in_the_future_is_expired(tmp_path):
    first, _, _, _ = world({"execution_s": 900}, tmp_path)
    hello(first)
    second, _, _, wall2 = world({"execution_s": 900}, tmp_path)
    wall2.value = WALL0 - 60
    assert second.plane_refusal(DESTRUCTIVE) is not None


@pytest.mark.parametrize("effects, bound", [
    (frozenset({"destructive"}), True), (frozenset({"write", "destructive"}), True), (frozenset({"financial"}), True), (frozenset({"privileged"}), True),
    (frozenset(), True), (frozenset({"read", "financial"}), True),
    (frozenset({"read"}), False), (frozenset({"external"}), False), (frozenset({"write", "external"}), False), (frozenset({"write"}), False),
])
def test_every_consequential_class_is_bounded_and_the_rest_are_not(effects, bound):
    shared, _, clock, wall = world({"execution_s": 900})
    hello(shared)
    advance(clock, wall, 2000)
    assert (shared.plane_refusal(effects) is not None) is bound


def test_the_window_comes_from_the_plane_and_init_may_only_shorten_it(caplog):
    shorter, *_ = world({"execution_s": 900}, offline_execution_s=120)
    hello(shorter)
    assert shorter.offline_window_s() == 120
    caplog.set_level(logging.WARNING, logger="runbound")
    longer, *_ = world({"execution_s": 900}, offline_execution_s=3600)
    hello(longer)
    assert longer.offline_window_s() == 900 and "may only shorten" in caplog.text
    bigger, *_ = world({"execution_s": 7200})
    hello(bigger)
    assert bigger.offline_window_s() == 7200


def test_nothing_served_is_exactly_todays_behaviour_and_init_can_still_shorten_it():
    shared, _, clock, wall = world()  # an old plane: no `offline` key
    hello(shared)
    assert shared.offline_window_s() == cache_module.STALE_AFTER_S
    advance(clock, wall, 7200)
    assert shared.plane_refusal(DESTRUCTIVE) is None, "an SDK upgrade alone changes nothing: the sealed policy holds for its 24 h"
    shorter, _, clock2, wall2 = world(offline_execution_s=300)
    hello(shorter)
    advance(clock2, wall2, 301)
    assert shorter.offline_window_s() == 300 and shorter.plane_refusal(DESTRUCTIVE) is not None


@pytest.mark.parametrize("bad", [{"execution_s": "900"}, {"execution_s": 59}, {"execution_s": 86401}, {"execution_s": True}, {"execution_s": None}, {}, "soon", 900, []])
def test_a_served_value_that_cannot_be_read_uses_the_shorter_15_minutes(bad):
    shared, _, clock, wall = world(bad)
    hello(shared)
    assert shared.offline_window_s() == 900
    advance(clock, wall, 901)
    assert shared.plane_refusal(DESTRUCTIVE) is not None


def test_a_service_without_enforced_defaults_keeps_the_consequential_grace():
    plane = FakePlane(policy_body={"version": 0, "policy": None, "rows": [], "rows_hash": "h0"})
    clock, wall = MovableClock(), MovableClock(WALL0)
    shared = remote(plane, clock)
    shared._wall = wall
    hello(shared, version=0)
    assert shared.offline_window_s() is None and shared.offline_deadline() is None
    advance(clock, wall, 30)
    assert shared.plane_refusal(DESTRUCTIVE) is None
    advance(clock, wall, 40)
    assert "has not answered" in shared.plane_refusal(DESTRUCTIVE)  # the grace's own reason, not the offline window's


def test_the_hello_reports_the_window_and_the_status_estimates_its_end():
    shared, plane, clock, wall = world({"execution_s": 900})
    assert shared._hello_payload()["offline_window_s"] is None, "before the first policy there is no window to report"
    hello(shared)
    assert shared._hello_payload()["offline_window_s"] == 900
    status = shared.status()
    assert status.offline_window_s == 900 and status.offline_deadline == WALL0 + 900
    plain = FakePlane(policy_body={"version": 0, "policy": None, "rows": [], "rows_hash": "h0"})
    assert remote(plain, MovableClock())._hello_payload()["offline_window_s"] is None


def test_the_cache_carries_confirmed_at_and_an_unconfirmed_rewrite_does_not_advance_it(tmp_path):
    shared, _, clock, wall = world({"execution_s": 900}, tmp_path)
    hello(shared)
    advance(clock, wall, 120)
    hello(shared, plane_degraded="ledger_unreachable")
    stored, _ = cache_module.read(shared._cache_path, "k", "checkout")
    assert stored["confirmed_at"] == WALL0
    advance(clock, wall, 10)
    hello(shared)
    shared._save_cache(force=True)
    stored, _ = cache_module.read(shared._cache_path, "k", "checkout")
    assert stored["confirmed_at"] == WALL0 + 130


def test_offline_execution_s_is_validated():
    from test_shared_state import config

    for bad in (59, 86401, 90.5, "900", True, -1):
        with pytest.raises(ValueError, match="offline_execution_s"):
            config(offline_execution_s=bad).validate()
    for good in (60, 86400, None):
        config(offline_execution_s=good).validate()


# --- expiry only ever adds refusals ---------------------------------------------------------------------


def engine_world(rows):
    """A real RemoteState under the real engine, with the clocks in hand, serving ``rows`` next to enforced defaults."""
    import runbound
    from runbound import api

    api._teardown_for_tests()
    shared, plane, clock, wall = world({"execution_s": 900})
    plane.policy_body = {**body({"execution_s": 900}), "rows": rows, "rows_hash": "hrows", "cfg_hash": "hrows"}
    runbound.init()
    api._ENGINE.shared = shared
    api._SHARED = shared
    hello(shared)
    return shared, clock, wall


def test_an_explicit_deny_row_refuses_inside_the_window_and_after_it_and_a_row_allow_runs_only_inside():
    import runbound
    from runbound import api

    rows = [{"tool": "drop_deny", "blocked": True, "rule_revision": 1}, {"tool": "drop_allow", "lines": [{"id": "L1", "decision": "allow", "approval": None, "limits": None}], "otherwise": "allow", "rule_revision": 1}]
    shared, clock, wall = engine_world(rows)
    ran = []

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop_deny():
        ran.append("deny")

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop_allow():
        ran.append("allow")

    try:
        with pytest.raises(runbound.ExecutionRefused) as inside:
            drop_deny()
        assert inside.value.reason == "tool_blocked"  # the row decides
        drop_allow()
        assert ran == ["allow"]
        advance(clock, wall, 901)  # the window has ended
        for tool in (drop_deny, drop_allow):
            with pytest.raises(runbound.ExecutionRefused) as after:
                tool()
            assert after.value.reason == "plane_unavailable" and "offline-execution window of 900 s" in str(after.value)
        assert ran == ["allow"], "expiry added a refusal (the row-allowed tool) and removed none (the deny stays refused)"
    finally:
        api._teardown_for_tests()
