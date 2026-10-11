"""Tests for the plane link's state — the SDK's half of the control plane.

No sockets and no sleeping: the plane is an in-memory :class:`FakePlane` with
the same methods as :class:`~runbound.plane.PlaneClient` (hello, events,
policy), and the clock is a fake. Every test here is about one promise — the
payload carries hashes and counts, a slow or broken plane costs the caller
nothing, and a plane we have not heard from stops being believed.
"""

import threading
import time

import pytest

import runbound
from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply
from runbound.shared import (
    DEGRADE_AFTER,
    STALE_HALT_S,
    LocalState,
    RemoteState,
    build,
)
from runbound.state import SessionState


class MovableClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakePlane:
    """An in-memory control plane with :class:`PlaneClient`'s methods.

    Records every call (with the thread that made it), answers whatever the
    test told it to, and can be made to explode or to reject the key.
    """

    def __init__(
        self,
        reply: HelloReply | None = None,
        policy_body: dict | None = None,
        ok: bool = True,
        explodes: bool = False,
    ) -> None:
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.key_state = "unknown"
        self.timeout_s = 0.15
        self.consecutive_failures = 0
        self.last_success: float | None = None
        self.reply = reply
        self.policy_body = policy_body
        self.ok = ok
        self.explodes = explodes
        self.calls: list[tuple[str, object, str]] = []
        self._lock = threading.Lock()

    def _record(self, name: str, payload) -> None:
        with self._lock:
            self.calls.append((name, payload, threading.current_thread().name))
        if self.explodes:
            raise RuntimeError("the plane exploded")

    def names(self) -> list[str]:
        with self._lock:
            return [name for name, _payload, _thread in self.calls]

    def payloads(self, name: str) -> list:
        with self._lock:
            return [payload for called, payload, _thread in self.calls if called == name]

    def hello(self, payload: dict):
        self._record("hello", payload)
        return self.reply if self.ok else None

    def events(self, batch: dict) -> bool:
        self._record("events", batch)
        return self.ok

    def policy(self, service: str):
        self._record("policy", service)
        return self.policy_body if self.ok else None


class RecordingExporter:
    """The observer half of the exporter, recording instead of posting."""

    def __init__(self) -> None:
        self.events: list = []
        self.anomalies: list = []
        self.started = 0
        self.stopped = 0

    def on_event(self, session, event) -> None:
        self.events.append((session, event))

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.anomalies.append((session, anomaly, reacted))

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakePoller:
    """Stands in for the heartbeat thread: records the interval it was told."""

    def __init__(self) -> None:
        self.intervals: list[float] = []

    def set_interval(self, poll_s: float) -> None:
        self.intervals.append(poll_s)


def config(**kwargs) -> RunboundConfig:
    fields = {
        "control_plane_url": "https://plane.example",
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    fields.update(kwargs)
    return RunboundConfig(**fields)


def state(key: str = "user-9", **kwargs) -> SessionState:
    session = SessionState("sess-1", key=key, tags={"plan": "free"})
    for name, value in kwargs.items():
        setattr(session, name, value)
    return session


def remote(plane: FakePlane, clock: MovableClock, exporter=None, **kwargs):
    return RemoteState(plane, exporter, config(**kwargs), now=clock)


def fail_hello(shared: RemoteState, n: int = DEGRADE_AFTER) -> None:
    """Make ``n`` real calls to the plane that all fail."""
    for _ in range(n):
        shared._attempt(lambda: None, "hello")


# --- LocalState -------------------------------------------------------------


def test_local_state_answers_nothing_and_does_nothing():
    local = LocalState()

    assert local.halted() is False
    assert local.halt_mode() is None
    assert local.policy() is None
    assert local.observers() == []
    local.start(object())
    local.stop()


def test_local_state_says_it_is_not_a_fleet():
    assert LocalState().fleet is False
    assert RemoteState(FakePlane(), None, config()).fleet is True


def test_local_state_reports_local_mode():
    status = LocalState().status()

    assert status.mode == "local"
    assert status.last_contact_age_s is None
    assert status.consecutive_failures == 0


def test_build_returns_a_local_state_without_a_plane_url():
    assert isinstance(build(RunboundConfig()), LocalState)


def test_build_returns_a_remote_state_with_a_plane_url():
    shared = build(config(export_events=False))

    assert isinstance(shared, RemoteState)
    assert shared.observers() == []


def test_build_gives_a_remote_state_an_exporter_when_export_is_on():
    shared = build(config(export_events=True))

    assert len(shared.observers()) == 1


# --- degrading --------------------------------------------------------------


def test_three_missed_hellos_in_a_row_degrade_the_link():
    shared = remote(FakePlane(ok=False), MovableClock())

    for _ in range(DEGRADE_AFTER):
        shared.record_miss()

    assert shared.status().mode == "degraded"  # unavailable is a matter of time, not of a count
    assert shared.status().consecutive_failures == DEGRADE_AFTER
    assert shared.status().reason == "heartbeat failures"


def test_a_degraded_link_keeps_calling_there_is_no_separate_probe():
    clock = MovableClock()
    plane = FakePlane(ok=False)
    shared = remote(plane, clock)
    for _ in range(DEGRADE_AFTER):
        shared.record_miss()

    for _ in range(DEGRADE_AFTER + 5):
        clock.advance(1.0)
        shared._attempt(lambda: plane.hello({}), "hello")

    assert plane.names().count("hello") == DEGRADE_AFTER + 5  # every call goes out


def test_a_hello_that_comes_back_reconnects_the_link():
    plane = FakePlane(ok=False)
    shared = remote(plane, MovableClock())
    for _ in range(DEGRADE_AFTER):
        shared.record_miss()
    assert shared.status().mode == "degraded"

    shared.apply_hello(HelloReply())

    assert shared.status().mode == "connected"
    assert shared.status().consecutive_failures == 0


def test_a_rejected_key_stops_calling():
    plane = FakePlane()
    plane.key_state = "invalid"
    shared = remote(plane, MovableClock())

    assert shared._attempt(lambda: plane.hello({}), "hello") is None
    assert plane.names() == []


def test_a_policy_fetch_on_a_degraded_link_is_skipped():
    plane = FakePlane(ok=False, policy_body={"version": 4, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    fail_hello(shared)

    shared.apply_hello(HelloReply(policy_version=4))

    # apply_hello itself is a success (a reply arrived), so the link recovers
    # and the fetch is attempted; it fails and the old (empty) policy stays.
    assert shared.policy() is None


# --- the heartbeat's poll interval -----------------------------------------


def test_a_hello_reply_poll_s_changes_the_pollers_interval():
    shared = remote(FakePlane(), MovableClock())
    poller = FakePoller()
    shared._poller = poller

    shared.apply_hello(HelloReply(poll_s=12.5))

    assert poller.intervals == [12.5]


def test_a_hello_reply_without_poll_s_leaves_the_interval_alone():
    shared = remote(FakePlane(), MovableClock())
    poller = FakePoller()
    shared._poller = poller

    shared.apply_hello(HelloReply())

    assert poller.intervals == []


def test_poll_s_reaches_a_real_poller_through_start_and_stop():
    plane = FakePlane(reply=HelloReply(poll_s=7.0))
    shared = remote(plane, MovableClock(), control_plane_poll_s=3600.0)

    shared.start(object())
    try:
        deadline = time.monotonic() + 3.0
        while shared._poller._poll_s != 7.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert shared._poller._poll_s == 7.0
    finally:
        shared.stop()


# --- halts ------------------------------------------------------------------


def test_hello_sets_and_clears_the_halt():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(halt=True))
    assert shared.halted() is True

    shared.apply_hello(HelloReply(halt=False))
    assert shared.halted() is False


def test_a_stale_halt_fails_open():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S - 1)
    assert shared.halted() is True

    clock.advance(2)
    assert shared.halted() is False


def test_a_halt_is_believed_again_once_the_plane_answers():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)

    shared.apply_hello(HelloReply(halt=True))
    clock.advance(STALE_HALT_S + 1)
    assert shared.halted() is False

    shared.apply_hello(HelloReply(halt=True))
    assert shared.halted() is True


def test_status_reports_how_stale_an_enforced_halt_is():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True))
    clock.advance(10.0)

    assert shared.status().halt_stale_s == pytest.approx(10.0)


def test_status_has_no_halt_staleness_without_a_halt():
    shared = remote(FakePlane(), MovableClock())
    shared.apply_hello(HelloReply(halt=False))

    assert shared.status().halt_stale_s is None


def test_a_stop_halt_is_mode_stop():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(halt=True, halt_mode="stop"))

    assert shared.halt_mode() == "stop"


def test_a_narrow_halt_is_mode_narrow():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    assert shared.halt_mode() == "narrow"


def test_an_older_plane_that_never_sends_a_mode_means_stop():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(halt=True))

    assert shared.halt_mode() == "stop"


def test_lifting_a_narrow_halt_clears_its_mode():
    shared = remote(FakePlane(), MovableClock())
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    shared.apply_hello(HelloReply(halt=False))

    assert shared.halted() is False
    assert shared.halt_mode() is None


def test_a_stale_narrow_halt_fails_open():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    clock.advance(STALE_HALT_S + 1)

    assert shared.halt_mode() is None


def test_local_state_has_no_halt_mode():
    assert LocalState().halt_mode() is None


def test_the_hello_payload_acks_a_halt_it_has_seen():
    shared = remote(FakePlane(), MovableClock())
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    assert shared._hello_payload()["halt_ack"] is True


def test_the_hello_payload_does_not_ack_when_there_is_no_halt():
    shared = remote(FakePlane(), MovableClock())
    shared.apply_hello(HelloReply(halt=False))

    assert shared._hello_payload()["halt_ack"] is False


def test_the_hello_payload_stops_acking_once_a_stale_halt_releases():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="stop"))

    clock.advance(STALE_HALT_S + 1)

    assert shared._hello_payload()["halt_ack"] is False


# --- policy -----------------------------------------------------------------


def test_hello_fetches_the_policy_when_the_version_changes():
    plane = FakePlane(policy_body={"version": 4, "dry_run": False, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=4))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_version == 4
    assert shared.policy_dry_run is False
    assert plane.payloads("policy") == ["checkout"]


def test_the_policy_is_not_refetched_for_the_same_version():
    plane = FakePlane(policy_body={"version": 4, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=4))
    shared.apply_hello(HelloReply(policy_version=4))

    assert plane.names().count("policy") == 1


def test_a_new_version_refetches_the_policy():
    plane = FakePlane(policy_body={"version": 4, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=4))

    plane.policy_body = {"version": 5, "dry_run": True, "policy": {"deny": ["refund"]}}
    shared.apply_hello(HelloReply(policy_version=5))

    assert shared.policy() == {"deny": ["refund"]}
    assert shared.policy_version == 5
    assert shared.policy_dry_run is True


def test_a_bare_policy_body_is_taken_as_the_policy_itself():
    plane = FakePlane(policy_body={"deny": ["wire"]})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=2))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_version == 2


def test_a_policy_fetch_that_fails_leaves_the_old_policy_alone():
    plane = FakePlane(policy_body={"version": 1, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=1))

    plane.policy_body = None
    shared.apply_hello(HelloReply(policy_version=2))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_version == 1


def test_a_policy_fetch_that_fails_is_retried_on_the_next_heartbeat():
    plane = FakePlane(policy_body=None)
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=3))
    assert shared.policy() is None

    plane.policy_body = {"version": 3, "policy": {"deny": ["wire"]}}
    shared.apply_hello(HelloReply(policy_version=3))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_version == 3


# --- status -----------------------------------------------------------------


def test_status_reports_the_age_of_the_last_contact():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)

    shared.apply_hello(HelloReply())
    clock.advance(2.5)
    status = shared.status()

    assert status.mode == "connected"
    assert status.last_contact_age_s == pytest.approx(2.5)


def test_status_carries_the_planes_notice():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(notice="plan expires in 3 days"))

    assert shared.status().notice == "plan expires in 3 days"


# --- fail-open --------------------------------------------------------------


def test_nothing_raises_when_the_plane_explodes():
    plane = FakePlane(explodes=True, policy_body={"version": 3, "policy": {}})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=3))

    assert shared.status().mode in ("connected", "degraded")
    assert shared.policy() is None


def test_nothing_raises_when_the_reply_is_garbage():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(object())
    shared.apply_hello(None)

    assert shared.halted() is False


def test_start_and_stop_are_safe_with_a_plane_thread():
    exporter = RecordingExporter()
    shared = remote(FakePlane(), MovableClock(), exporter=exporter, control_plane_poll_s=3600.0)

    shared.start(object())
    try:
        assert exporter.started == 1
        assert shared._poller is not None
    finally:
        shared.stop()
    assert exporter.stopped == 1
    shared.stop()  # idempotent


# --- the hello payload ------------------------------------------------------


def test_the_hello_payload_describes_this_worker():
    shared = remote(FakePlane(policy_body={"version": 2, "policy": {}}), MovableClock())
    shared.apply_hello(HelloReply(policy_version=2))

    payload = shared._hello_payload()

    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert payload["policy_version_seen"] == 2
    assert isinstance(payload["sdk_version"], str)
    assert payload["active"] == 0
    assert isinstance(payload["tools_hash"], str)


def test_the_hello_payload_carries_no_removed_fields():
    payload = remote(FakePlane(), MovableClock())._hello_payload()

    for removed in ("circuits", "can_stop", "controls_version_seen", "envelope", "entries_local_share"):
        assert removed not in payload


def test_the_hello_payload_counts_this_processs_open_sessions():
    from runbound import api

    api._teardown_for_tests()
    shared = remote(FakePlane(), MovableClock())
    runbound.init()
    try:
        assert shared._hello_payload()["active"] == 0
        with runbound.session("user-1"):
            assert shared._hello_payload()["active"] == 1
            with runbound.session("user-2"):
                assert shared._hello_payload()["active"] == 2
            assert shared._hello_payload()["active"] == 1
        assert shared._hello_payload()["active"] == 0
    finally:
        api._teardown_for_tests()


def test_the_hello_payload_fails_open_when_the_active_count_cannot_be_read(monkeypatch):
    from runbound import api

    def _boom():
        raise RuntimeError("no")

    monkeypatch.setattr(api, "_active_count", _boom)
    shared = remote(FakePlane(), MovableClock())

    assert shared._hello_payload()["active"] == 0


def test_the_hello_payload_carries_a_coverage_block_with_the_right_shape():
    coverage = remote(FakePlane(), MovableClock())._hello_payload()["coverage"]

    assert set(coverage) == {
        "decorated_tools",
        "decorated_tool_names",
        "tool_calls_seen",
        "keyed_sessions_seen",
        "tools_by_class",
    }
    assert isinstance(coverage["decorated_tools"], int)
    assert isinstance(coverage["decorated_tool_names"], list)
    assert isinstance(coverage["tools_by_class"], dict)


def test_the_hello_payload_fails_open_when_coverage_cannot_be_read(monkeypatch):
    from runbound import _coverage

    def _boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(_coverage, "snapshot", _boom)
    payload = remote(FakePlane(), MovableClock())._hello_payload()

    assert payload["coverage"] == {}
    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert payload["policy_version_seen"] == 0
    assert isinstance(payload["sdk_version"], str)
    assert payload["active"] == 0


def test_the_hello_payload_fails_open_when_the_tool_report_cannot_be_built(monkeypatch):
    from runbound import _coverage

    def _boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(_coverage, "tool_report", _boom)
    payload = remote(FakePlane(), MovableClock())._hello_payload()

    assert "tools_hash" not in payload
    assert "tools" not in payload
    assert payload["service"] == "checkout"


# --- the tool report's hash ack on the heartbeat ------------------------------


def test_the_first_heartbeat_carries_the_tool_report_and_its_hash():
    payload = remote(FakePlane(), MovableClock())._hello_payload()

    assert "tools" in payload
    assert isinstance(payload["tools"], list)
    assert isinstance(payload["tools_hash"], str)


def test_an_acknowledged_report_is_not_resent_but_its_hash_is():
    shared = remote(FakePlane(), MovableClock())
    first = shared._hello_payload()
    shared.apply_hello(HelloReply())

    second = shared._hello_payload()

    assert second["tools_hash"] == first["tools_hash"]
    assert "tools" not in second


def test_a_report_whose_reply_never_came_is_resent():
    shared = remote(FakePlane(), MovableClock())
    shared._hello_payload()

    second = shared._hello_payload()

    assert "tools" in second


def test_tools_known_false_makes_the_next_heartbeat_resend_the_report():
    shared = remote(FakePlane(), MovableClock())
    shared._hello_payload()
    shared.apply_hello(HelloReply())
    assert "tools" not in shared._hello_payload()

    shared.apply_hello(HelloReply(tools_known=False))

    assert "tools" in shared._hello_payload()


def test_a_changed_tool_report_is_sent_again():
    shared = remote(FakePlane(), MovableClock())
    shared._hello_payload()
    shared.apply_hello(HelloReply())

    @runbound.tool
    def shared_state_new_tool_for_report(x: int) -> int:
        return x

    payload = shared._hello_payload()

    assert "tools" in payload


# --- the exporter's pending count, read the way plane_status() is -----------


def test_local_state_has_no_pending_events():
    assert LocalState().pending_events() is None


def test_remote_state_without_an_exporter_has_no_pending_events():
    shared = remote(FakePlane(), MovableClock(), exporter=None)

    assert shared.pending_events() is None


def test_remote_state_reports_the_exporters_pending_count():
    exporter = RecordingExporter()
    exporter.pending = 7
    shared = remote(FakePlane(), MovableClock(), exporter=exporter)

    assert shared.pending_events() == 7
