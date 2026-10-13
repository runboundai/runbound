"""The on-disk policy cache (S7): where it lives, how it is written, what it rejects, and what a
worker does with it when the plane is not there."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import stat
import sys
import time

import pytest

import runbound
from runbound import api, cache
from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply
from runbound.shared import DEGRADE_AFTER, UNAVAILABLE_AFTER_S, RemoteState

TOKEN = "rb_live_cache_token"
SERVICE = "svc"
URL = "http://plane.example"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    cache.reset_for_tests()
    yield
    api._teardown_for_tests()


def body(version=1, policy=None, **over):
    base = cache.body_for(SERVICE, version, {"deny": ["wipe"]} if policy is None else policy, False)
    base.update(over)
    return base


def where(tmp_path):
    return cache.resolve(str(tmp_path), URL, SERVICE)


# --- where -------------------------------------------------------------------------------------


def test_the_layout_and_permissions(tmp_path):
    path = where(tmp_path)
    digest = __import__("hashlib").sha256(URL.encode()).hexdigest()[:12]
    assert path == tmp_path / "runbound" / digest / f"{SERVICE}.json"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert cache.write(path, TOKEN, body())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_explicit_dir_then_the_env_then_the_platform_dir_then_tmp(tmp_path, monkeypatch):
    explicit, env, home = tmp_path / "a", tmp_path / "b", tmp_path / "home"
    monkeypatch.setenv("RUNBOUND_CACHE_DIR", str(env))
    assert str(cache.resolve(str(explicit), URL, SERVICE)).startswith(str(explicit))
    assert str(cache.resolve(None, URL, SERVICE)).startswith(str(env))
    monkeypatch.delenv("RUNBOUND_CACHE_DIR")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    resolved = str(cache.resolve(None, URL, SERVICE))
    assert resolved.startswith(str(home))


def test_xdg_runtime_dir_is_never_used(tmp_path, monkeypatch):
    runtime = tmp_path / "run-user"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("RUNBOUND_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert not str(cache.resolve(None, URL, SERVICE)).startswith(str(runtime))
    assert not runtime.exists()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_a_read_only_home_falls_back_to_tmp(tmp_path, monkeypatch):
    import tempfile

    home = tmp_path / "ro-home"
    home.mkdir()
    home.chmod(0o500)
    monkeypatch.delenv("RUNBOUND_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    try:
        path = cache.resolve(None, URL, SERVICE)
        assert path is not None and str(path).startswith(tempfile.gettempdir()) and not str(path).startswith(str(home))
    finally:
        home.chmod(0o700)


def test_no_writable_directory_is_memory_only_and_said_once(monkeypatch, caplog):
    monkeypatch.setattr(cache, "_usable", lambda directory: False)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert cache.resolve(None, URL, SERVICE) is None
        assert cache.resolve(None, URL, SERVICE) is None
    assert caplog.text.count("memory only") == 1
    assert cache.write(None, TOKEN, body()) is False and cache.read(None, TOKEN, SERVICE) == (None, "absent")


# --- write and read -------------------------------------------------------------------------------


def test_round_trip_and_no_token_on_disk(tmp_path):
    path = where(tmp_path)
    assert cache.write(path, TOKEN, body(3))
    read, status = cache.read(path, TOKEN, SERVICE)
    assert status == "ok" and read["policy_version"] == 3 and read["policy"] == {"deny": ["wipe"]}
    assert TOKEN not in path.read_text()
    assert set(read) >= {"v", "service", "policy_version", "rows_hash", "rows", "served_at", "written_at", "key_id"}


def test_an_older_version_never_overwrites_a_newer_one(tmp_path):
    path = where(tmp_path)
    assert cache.write(path, TOKEN, body(5))
    assert cache.write(path, TOKEN, body(4)) is False
    assert cache.read(path, TOKEN, SERVICE)[0]["policy_version"] == 5
    assert cache.write(path, TOKEN, body(5)) and cache.write(path, TOKEN, body(6))


def test_no_temp_files_are_left_behind_even_on_a_failed_write(tmp_path, monkeypatch):
    path = where(tmp_path)
    monkeypatch.setattr(cache.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert cache.write(path, TOKEN, body()) is False
    assert [p.name for p in path.parent.iterdir()] == []


# --- integrity ---------------------------------------------------------------------------------------


def test_a_hand_edited_cache_is_rejected(tmp_path, caplog):
    path = where(tmp_path)
    cache.write(path, TOKEN, body(policy={"deny": ["wipe"], "require_approval": ["wipe"]}))
    data = json.loads(path.read_text())
    data["policy"]["require_approval"] = []  # approval_required: false, by hand
    path.write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert cache.read(path, TOKEN, SERVICE) == (None, "rejected")
        cache.read(path, TOKEN, SERVICE)
    assert caplog.text.count("ignoring the policy cache") == 1  # said once


def test_another_tokens_cache_another_service_garbage_and_truncation_are_rejected(tmp_path):
    path = where(tmp_path)
    cache.write(path, TOKEN, body())
    assert cache.read(path, "another-token", SERVICE)[1] == "rejected"
    assert cache.read(path, TOKEN, "another-service")[1] == "rejected"
    good = path.read_bytes()
    path.write_bytes(good[: len(good) // 2])
    assert cache.read(path, TOKEN, SERVICE)[1] == "rejected"
    path.write_bytes(b"\x00\xffnot json")
    assert cache.read(path, TOKEN, SERVICE)[1] == "rejected"
    path.write_bytes(b"")
    assert cache.read(path, TOKEN, SERVICE)[1] == "rejected"
    path.unlink()
    assert cache.read(path, TOKEN, SERVICE)[1] == "absent"


def _writer(path, rounds, ident):
    for i in range(rounds):
        cache.write(path, TOKEN, body(1000 * ident + i, policy={"deny": [f"tool-{ident}-{i}"] * 200}))


def _reader(path, rounds, out):
    torn = ok = 0
    for _ in range(rounds):
        _, status = cache.read(path, TOKEN, SERVICE)
        torn += status == "rejected"
        ok += status == "ok"
    out.put((torn, ok))


def test_four_writers_and_four_readers_give_no_torn_reads(tmp_path):
    from pathlib import Path

    path = where(tmp_path)
    cache.write(path, TOKEN, body(1))
    ctx = multiprocessing.get_context("spawn" if sys.platform == "darwin" else "fork")
    out = ctx.Queue()
    writers = [ctx.Process(target=_writer, args=(path, 80, n + 1)) for n in range(4)]
    readers = [ctx.Process(target=_reader, args=(path, 400, out)) for _ in range(4)]
    for process in writers + readers:
        process.start()
    for process in writers + readers:
        process.join(60)
    totals = [out.get(timeout=10) for _ in readers]
    assert sum(t for t, _ in totals) == 0, totals  # not one torn read
    assert sum(k for _, k in totals) > 0


# --- what a worker does with it ----------------------------------------------------------------------


class Client:
    def __init__(self):
        self.key_state = "unknown"

    def policy(self, service):
        return {"version": 7, "policy": {"deny": ["wipe"]}}

    def hello(self, payload):
        return None

    def approvals(self, *a, **k):
        return None, None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def remote(tmp_path, clock=None, **config):
    cfg = RunboundConfig(token=TOKEN, control_plane_url=URL, service=SERVICE, cache_dir=str(tmp_path), **config)
    cfg.validate()
    return RemoteState(Client(), None, cfg, **({} if clock is None else {"now": clock}))


def test_a_new_worker_enforces_the_cached_policy_before_any_hello(tmp_path):
    cache.write(where(tmp_path), TOKEN, body(9))
    shared = remote(tmp_path)
    assert shared.policy() == {"deny": ["wipe"]} and shared.policy_version == 9
    assert shared.status().applied_policy_version == 9


def test_a_policy_served_by_the_plane_is_written_to_the_cache(tmp_path):
    shared = remote(tmp_path)
    shared.apply_hello(HelloReply(policy_version=7))
    read, status = cache.read(where(tmp_path), TOKEN, SERVICE)
    assert status == "ok" and read["policy_version"] == 7 and read["policy"] == {"deny": ["wipe"]}


def test_a_cache_with_a_different_token_is_not_loaded(tmp_path):
    cache.write(where(tmp_path), "someone-elses-token", body(9))
    assert remote(tmp_path).policy() is None


CONSEQUENTIAL = [{"financial"}, {"destructive"}, {"privileged"}, None, set()]
OTHERS = [{"read"}, {"external"}, {"write", "external"}]
BARE_WRITE = [{"write"}, {"read", "write"}]  # no tag besides write: unclassified for fail mode (K8)


@pytest.mark.parametrize("effects", BARE_WRITE)
def test_a_bare_write_is_unclassified_for_fail_mode_so_it_refuses_when_the_policy_is_unavailable(tmp_path, effects):
    clock = Clock()
    shared = remote(tmp_path, clock)  # no cache, no hello
    assert shared.policy_state() == "unavailable" and shared.plane_refusal(frozenset(effects))
    cache.write(where(tmp_path), TOKEN, body(1))
    sealed = remote(tmp_path, clock)  # a valid sealed cache: not unavailable, so a write with no tag runs as it always did
    assert sealed.policy_state() == "cached" and sealed.plane_refusal(frozenset(effects)) is None


@pytest.mark.parametrize("effects", OTHERS)
def test_read_write_external_run_on_the_cache_in_every_state(tmp_path, effects):
    clock = Clock()
    shared = remote(tmp_path, clock)  # no cache, no hello, connecting
    assert shared.plane_refusal(frozenset(effects)) is None
    clock.t += 3600  # an hour of silence: unavailable
    assert shared.link_state() == "unavailable" and shared.plane_refusal(frozenset(effects)) is None


@pytest.mark.parametrize("effects", CONSEQUENTIAL)
def test_consequential_tools_follow_the_grace_period_and_the_cache(tmp_path, effects):
    effects = frozenset(effects or ())
    clock = Clock()
    shared = remote(tmp_path, clock)
    assert shared.plane_refusal(effects)  # connecting, no cache: refuse until the first hello

    cache.write(where(tmp_path), TOKEN, body(1))
    shared = remote(tmp_path, clock)
    assert shared.plane_refusal(effects) is None  # connecting, cache confirmed just now: trusted
    clock.t += 59
    assert shared.plane_refusal(effects) is None
    clock.t += 2  # 60 s since start with no hello: unavailable
    assert shared.plane_refusal(effects) and shared.link_state() == "unavailable"

    shared.apply_hello(HelloReply())  # a hello answered: back
    assert shared.plane_refusal(effects) is None and shared.link_state() == "connected"
    clock.t += 59.9
    assert shared.plane_refusal(effects) is None  # 3 missed hellos would be degraded, still everything runs
    clock.t += 0.2
    assert shared.plane_refusal(effects) and shared.link_state() == "unavailable"  # 60 s since the last answered hello
    shared.apply_hello(HelloReply())
    assert shared.plane_refusal(effects) is None


def test_a_cache_confirmed_more_than_15_minutes_ago_is_not_trusted_by_consequential_tools_at_boot(tmp_path, monkeypatch):
    cache.write(where(tmp_path), TOKEN, body(1))
    later = time.time() + cache.FRESH_FOR_CONSEQUENTIAL_S + 5
    monkeypatch.setattr("runbound.shared.time.time", lambda: later)
    assert remote(tmp_path).plane_refusal(frozenset({"financial"}))
    assert remote(tmp_path).plane_refusal(frozenset({"read"})) is None


def test_consequential_grace_s_is_configurable_between_15_and_60(tmp_path):
    clock = Clock()
    shared = remote(tmp_path, clock, consequential_grace_s=15)
    shared.apply_hello(HelloReply())
    clock.t += 14
    assert shared.plane_refusal(frozenset({"financial"})) is None
    clock.t += 2
    assert shared.plane_refusal(frozenset({"financial"}))
    assert shared.link_state() == "connected"  # unavailable is still 60 s; the grace only moves the refusal earlier
    for bad in (14, 61, "x", True, None):
        with pytest.raises(ValueError):
            RunboundConfig(consequential_grace_s=bad).validate()


def test_the_four_states_and_their_transitions(tmp_path):
    clock = Clock()
    shared = remote(tmp_path, clock)
    assert shared.status().mode == "connecting"
    shared.apply_hello(HelloReply())
    assert shared.status().mode == "connected"
    for _ in range(DEGRADE_AFTER - 1):
        shared.record_miss()
    assert shared.status().mode == "connected"  # two missed hellos are not an outage
    shared.record_miss()
    assert shared.status().mode == "degraded" and shared.status().reason == "heartbeat failures"
    shared.apply_hello(HelloReply())
    assert shared.status().mode == "connected" and shared.status().consecutive_failures == 0
    clock.t += UNAVAILABLE_AFTER_S
    assert shared.status().mode == "unavailable"  # time, not a count: no miss was counted at all
    shared.apply_hello(HelloReply())
    assert shared.status().mode == "connected"


def test_connecting_counts_misses_toward_degraded(tmp_path):
    shared = remote(tmp_path, Clock())
    for _ in range(DEGRADE_AFTER):
        shared.record_miss()
    assert shared.status().mode == "degraded"


def test_one_fleet_state_unavailable_reply_is_degraded(tmp_path):
    shared = remote(tmp_path, Clock())
    shared.apply_hello(HelloReply(fleet_state="unavailable"))
    assert shared.status().mode == "degraded" and shared.status().reason == "fleet state unavailable"
    shared.apply_hello(HelloReply())
    assert shared.status().mode == "connected"


def test_in_degraded_everything_runs_on_the_cache_but_new_approvals_refuse(tmp_path):
    shared = remote(tmp_path, Clock())
    shared.apply_hello(HelloReply())
    assert shared.approval_refusal() is None
    for _ in range(DEGRADE_AFTER):
        shared.record_miss()
    assert shared.link_state() == "degraded"
    assert shared.plane_refusal(frozenset({"destructive"})) is None  # still running on the cache
    assert shared.approval_refusal() and "degraded" in shared.approval_refusal()
    assert remote(tmp_path, Clock()).approval_refusal()  # connecting: no new approval either


def test_the_poller_reports_missed_hellos_and_jitters_its_wait(monkeypatch):
    from runbound.plane import Poller

    seen, waits = [], []

    class Dead:
        def hello(self, payload):
            return None

    poller = Poller(Dead(), 5.0, lambda reply: None, lambda: {}, on_miss=lambda: seen.append(1))
    poller._tick()
    poller._tick()
    assert seen == [1, 1]

    monkeypatch.setattr(poller._stopped, "wait", lambda seconds: waits.append(seconds) or len(waits) >= 50)
    poller.run()
    assert len(waits) == 50 and all(4.5 <= w <= 5.5 for w in waits) and len(set(waits)) > 10


def test_plane_status_reports_a_stale_cache_after_a_day_and_warns_once(tmp_path, monkeypatch, caplog):
    cache.write(where(tmp_path), TOKEN, body(1))
    later = time.time() + cache.STALE_AFTER_S + 60
    monkeypatch.setattr("runbound.shared.time.time", lambda: later)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        shared = remote(tmp_path)
        remote(tmp_path)
    status = shared.status()
    assert status.stale and status.cache_age_s > cache.STALE_AFTER_S
    assert caplog.text.count("policy cache is") == 1


def test_through_the_decorator_a_consequential_tool_refuses_and_a_read_tool_runs(tmp_path):
    runbound.init(control_plane_url="http://127.0.0.1:9", token=TOKEN, service=SERVICE, cache_dir=str(tmp_path),
                  control_plane_poll_s=60)

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop():
        return "dropped"

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "found"

    from runbound import shared as shared_module

    api._SHARED._started_at -= shared_module.UNAVAILABLE_AFTER_S  # sixty seconds of silence since start
    with pytest.raises(runbound.ExecutionRefused) as caught:
        drop()  # unavailable: refused
    assert caught.value.reason == "plane_unavailable" and caught.value.decision.level == "fleet"
    with pytest.raises(runbound.ExecutionRefused) as early:
        look()  # F3: nothing handshaken and no sealed cache, so even a read tool runs nothing yet
    assert early.value.reason == "policy_unavailable" and early.value.retryable
    api._SHARED._sealed_cache_loaded = True  # a worker restarted on a sealed cache: the read tool runs on what it holds
    assert look() == "found"
    assert runbound.tool_results()[0]["outcome"] == "refused"


def test_no_plane_configured_means_no_plane_gate():
    runbound.init()

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop():
        return "dropped"

    assert drop() == "dropped"


def test_code_declared_rules_are_never_in_the_cache(tmp_path):
    runbound.init(control_plane_url="http://127.0.0.1:9", token=TOKEN, service=SERVICE, cache_dir=str(tmp_path),
                  control_plane_poll_s=60)

    @runbound.tool(effects={"read"}, blocked=True)
    def wipe():
        return 1

    api._SHARED.apply_hello(HelloReply(policy_version=2))  # nothing served: nothing to write, and no code rule in it
    read, _ = cache.read(where(tmp_path), TOKEN, SERVICE)
    assert read is None or "blocked" not in json.dumps(read)
    with pytest.raises(runbound.ExecutionRefused):
        wipe()  # the code rule applies with or without the cache


# --- the validity window follows the service (Apollo's ruling on TR2-O1) ------------------------------------------------


@pytest.mark.parametrize("effects", CONSEQUENTIAL)
def test_without_enforced_defaults_a_service_keeps_the_per_class_cache_trust(tmp_path, effects):
    """Today's rule, unchanged: a sealed cache older than 15 minutes is not trusted by a consequential tool at boot, and the grace applies."""
    effects = frozenset(effects or ())
    cache.write(where(tmp_path), TOKEN, body(1))
    clock = Clock()
    shared = remote(tmp_path, clock)
    assert shared._defaults_enforced is False and shared.plane_refusal(effects) is None  # connecting, cache just confirmed
    clock.t += 61  # past consequential_grace_s with no hello
    assert shared.plane_refusal(effects)  # the old grace still refuses; nothing was loosened


@pytest.mark.parametrize("effects", CONSEQUENTIAL)
def test_with_enforced_defaults_one_24_hour_window_replaces_the_grace(tmp_path, effects):
    """The single window applies only to a service that has adopted enforced defaults (TR2-O3 sets the flag from the served policy)."""
    effects = frozenset(effects or ())
    cache.write(where(tmp_path), TOKEN, body(1))
    clock = Clock()
    shared = remote(tmp_path, clock)
    shared._defaults_enforced = True
    clock.t += 3600  # an hour of silence: cached, so the policy is enforced in full and fail mode does not apply
    assert shared.policy_state() == "cached" and shared.plane_refusal(effects) is None
    shared._cache_written_at -= cache.STALE_AFTER_S + 5  # the policy held is now past its 24 h window
    assert shared.policy_state() == "unavailable" and shared.plane_refusal(effects)
