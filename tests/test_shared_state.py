"""Tests for shared session state — the SDK's half of the control plane.

No sockets and no sleeping: the plane is an in-memory :class:`FakePlane` with
the same methods as :class:`~runbound.plane.PlaneClient`, and both clocks are
fakes. Every test here is about one promise — the payload carries hashes and
counts, a slow or broken plane costs the caller nothing, and a plane we have
not heard from stops being believed.
"""

import threading
import time

import pytest

from runbound.circuit import CircuitBreaker
from runbound.config import GuardrailConfig
from runbound.events import Anomaly
from runbound.plane_types import EntryDecision, ExitDelta, HelloReply, key_hash
from runbound.shared import (
    DEGRADE_AFTER,
    DECISION_TTL_S,
    RETRY_EVERY_S,
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
        decision: EntryDecision | None = None,
        reply: HelloReply | None = None,
        policy_body: dict | None = None,
        ok: bool = True,
        explodes: bool = False,
        delay_s: float = 0.0,
    ) -> None:
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.key_state = "unknown"
        self.timeout_s = 0.15
        self.consecutive_failures = 0
        self.last_success: float | None = None
        self.decision = decision
        self.reply = reply
        self.policy_body = policy_body
        self.ok = ok
        self.explodes = explodes
        self.delay_s = delay_s
        self.calls: list[tuple[str, object, str]] = []
        self._lock = threading.Lock()

    # --- bookkeeping ------------------------------------------------------

    def _record(self, name: str, payload) -> None:
        with self._lock:
            self.calls.append((name, payload, threading.current_thread().name))
        if self.explodes:
            raise RuntimeError("the plane exploded")

    def _timed_out(self) -> bool:
        """Block like a socket would, and say whether the deadline passed.

        A real client waits ``timeout_s`` and then gives up; a fake that slept
        the whole delay would be testing the fake, not the SDK.
        """
        if not self.delay_s:
            return False
        time.sleep(min(self.delay_s, self.timeout_s))
        return self.delay_s > self.timeout_s

    def names(self) -> list[str]:
        with self._lock:
            return [name for name, _payload, _thread in self.calls]

    def payloads(self, name: str) -> list:
        with self._lock:
            return [payload for called, payload, _thread in self.calls if called == name]

    # --- the client's methods --------------------------------------------

    def hello(self, payload: dict):
        self._record("hello", payload)
        if self._timed_out():
            return None
        return self.reply

    def enter(self, payload: dict):
        self._record("enter", payload)
        if self._timed_out():
            return None
        return self.decision if self.ok else None

    def trip(self, report) -> bool:
        self._record("trip", report)
        if self._timed_out():
            return False
        return self.ok

    def events(self, batch: dict) -> bool:
        self._record("events", batch)
        return self.ok

    def policy(self, service: str):
        self._record("policy", service)
        return self.policy_body

    def clear(self, key_hash_value: str):
        self._record("clear", key_hash_value)
        return 1 if self.ok else None


class RecordingExporter:
    """The observer half of the exporter, recording instead of posting."""

    def __init__(self) -> None:
        self.events: list = []
        self.anomalies: list = []
        self.exits: list = []
        self.circuits: list = []
        self.started = 0
        self.stopped = 0

    def on_event(self, session, event) -> None:
        self.events.append((session, event))

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.anomalies.append((session, anomaly, reacted))

    def on_exit(self, delta) -> None:
        self.exits.append(delta)

    def on_circuit(self, label, state, failures, cooldown_s) -> None:
        self.circuits.append((label, state, failures, cooldown_s))

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


def config(**kwargs) -> GuardrailConfig:
    fields = {
        "control_plane_url": "https://plane.example",
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    fields.update(kwargs)
    return GuardrailConfig(**fields)


def state(key: str = "user-9", **kwargs) -> SessionState:
    session = SessionState("sess-1", key=key, tags={"plan": "free"})
    for name, value in kwargs.items():
        setattr(session, name, value)
    return session


def remote(plane: FakePlane, clock: MovableClock, exporter=None, **kwargs):
    return RemoteState(plane, exporter, config(**kwargs), now=clock)


# --- LocalState -------------------------------------------------------------


def test_local_state_answers_nothing_and_does_nothing():
    local = LocalState()
    session = state()

    assert local.enter("user-9", session, config()) is None
    assert local.halted() is False
    assert local.policy() is None
    assert local.fleet_status("user-9") is None
    assert local.observers() == []
    # None of these may touch anything.
    local.exit("user-9", session, ExitDelta(key_hash="x", seq=1))
    local.trip("user-9", session, Anomaly("budget", "critical", "over", {}), None, False)
    local.clear("user-9")
    local.circuit("openai", "open", 5, 30.0)
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
    assert isinstance(build(GuardrailConfig()), LocalState)


def test_build_returns_a_remote_state_with_a_plane_url():
    shared = build(config(export_events=False))

    assert isinstance(shared, RemoteState)
    assert shared.observers() == []


def test_build_gives_a_remote_state_an_exporter_when_export_is_on():
    shared = build(config(export_events=True))

    assert len(shared.observers()) == 1


# --- the entry payload ------------------------------------------------------


def test_the_entry_payload_carries_hashes_and_counts_only():
    plane = FakePlane(decision=EntryDecision(fleet_spend_usd=1.0))
    shared = remote(plane, MovableClock(), budget_usd=5.0)
    session = state(key="user-9")
    session.total_cost_usd = 0.25
    session.total_tokens = 400

    shared.enter("user-9", session, shared._config)

    payload = plane.payloads("enter")[0]
    assert payload == {
        "key_hash": key_hash("user-9"),
        "tags": {"plan": "free"},
        "service": "checkout",
        "worker_id": "host-1:42",
        "budget_usd": 5.0,
        "local_spend_usd": 0.25,
        "local_total_tokens": 400,
    }
    assert "user-9" not in repr(payload)


def test_the_entry_payload_carries_the_raw_key_only_when_asked_to():
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, MovableClock(), send_session_keys=True)

    shared.enter("user-9", state(), shared._config)

    assert plane.payloads("enter")[0]["key"] == "user-9"


def test_entry_tags_are_scrubbed_and_capped():
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, MovableClock())
    session = state()
    session.tags = {"plan": "x" * 200, "n": 3, 7: "dropped"}

    shared.enter("user-9", session, shared._config)

    tags = plane.payloads("enter")[0]["tags"]
    assert len(tags["plan"]) == 64
    assert tags["n"] == "3"
    assert 7 not in tags


def test_entry_returns_the_planes_decision():
    decision = EntryDecision(fleet_spend_usd=4.8, strikes=2, generation=7)
    shared = remote(FakePlane(decision=decision), MovableClock())

    assert shared.enter("user-9", state(), shared._config) == decision


# --- the decision cache -----------------------------------------------------


def test_a_decision_is_cached_for_five_seconds():
    clock = MovableClock()
    plane = FakePlane(decision=EntryDecision(fleet_spend_usd=2.0))
    shared = remote(plane, clock)

    first = shared.enter("user-9", state(), shared._config)
    clock.advance(DECISION_TTL_S - 0.01)
    second = shared.enter("user-9", state(), shared._config)

    assert first is second
    assert plane.names().count("enter") == 1


def test_a_cached_decision_expires():
    clock = MovableClock()
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, clock)

    shared.enter("user-9", state(), shared._config)
    clock.advance(DECISION_TTL_S + 0.01)
    shared.enter("user-9", state(), shared._config)

    assert plane.names().count("enter") == 2


def test_the_cache_is_per_key():
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, MovableClock())

    shared.enter("user-9", state(key="user-9"), shared._config)
    shared.enter("user-8", state(key="user-8"), shared._config)

    assert plane.names().count("enter") == 2


def test_fleet_status_reports_the_cached_decision():
    clock = MovableClock()
    decision = EntryDecision(fleet_spend_usd=4.5, fleet_tokens=900, strikes=1, generation=3)
    shared = remote(FakePlane(decision=decision), clock)

    assert shared.fleet_status("user-9") is None
    shared.enter("user-9", state(), shared._config)
    clock.advance(1.0)
    status = shared.fleet_status("user-9")

    assert status["fleet_spend_usd"] == 4.5
    assert status["fleet_tokens"] == 900
    assert status["strikes"] == 1
    assert status["generation"] == 3
    assert status["age_s"] == pytest.approx(1.0)


def test_fleet_status_forgets_a_stale_decision():
    clock = MovableClock()
    shared = remote(FakePlane(decision=EntryDecision()), clock)

    shared.enter("user-9", state(), shared._config)
    clock.advance(DECISION_TTL_S + 1)

    assert shared.fleet_status("user-9") is None


# --- degrading --------------------------------------------------------------


def test_three_failures_in_a_row_degrade_the_link():
    plane = FakePlane(ok=False)
    shared = remote(plane, MovableClock())

    for _ in range(DEGRADE_AFTER):
        assert shared.enter("user-9", state(), shared._config) is None

    assert shared.status().mode == "degraded"
    assert shared.status().consecutive_failures == DEGRADE_AFTER


def test_a_degraded_link_stops_calling_and_answers_locally():
    clock = MovableClock()
    plane = FakePlane(ok=False)
    shared = remote(plane, clock)

    for _ in range(DEGRADE_AFTER + 5):
        clock.advance(1.0)
        shared.enter("user-9", state(), shared._config)

    assert plane.names().count("enter") == DEGRADE_AFTER


def test_a_degraded_link_retries_after_thirty_seconds():
    clock = MovableClock()
    plane = FakePlane(ok=False)
    shared = remote(plane, clock)

    for _ in range(DEGRADE_AFTER):
        shared.enter("user-9", state(), shared._config)
    clock.advance(RETRY_EVERY_S + 0.1)
    shared.enter("user-9", state(), shared._config)

    assert plane.names().count("enter") == DEGRADE_AFTER + 1


def test_a_successful_retry_reconnects_the_link():
    clock = MovableClock()
    plane = FakePlane(ok=False)
    shared = remote(plane, clock)

    for _ in range(DEGRADE_AFTER):
        shared.enter("user-9", state(), shared._config)
    plane.ok = True
    plane.decision = EntryDecision()
    clock.advance(RETRY_EVERY_S + 0.1)
    shared.enter("user-9", state(), shared._config)

    assert shared.status().mode == "connected"
    assert shared.status().consecutive_failures == 0


def test_the_heartbeats_failures_degrade_the_link_too():
    """A poller that cannot reach the plane spares the next entry the wait."""
    plane = FakePlane()
    plane.consecutive_failures = DEGRADE_AFTER
    shared = remote(plane, MovableClock())

    assert shared.status().mode == "degraded"
    assert shared.enter("user-9", state(), shared._config) is None
    assert plane.names() == []


def test_a_rejected_key_degrades_at_once_and_stops_calling():
    plane = FakePlane()
    plane.key_state = "invalid"
    shared = remote(plane, MovableClock())

    assert shared.enter("user-9", state(), shared._config) is None
    assert shared.status().mode == "degraded"
    assert plane.names() == []


# --- halts ------------------------------------------------------------------


def test_hello_sets_and_clears_the_halt():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)

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


def test_an_entry_decision_carries_a_halt_too():
    clock = MovableClock()
    shared = remote(FakePlane(decision=EntryDecision(halt=True)), clock)

    shared.enter("user-9", state(), shared._config)

    assert shared.halted() is True


# --- policy -----------------------------------------------------------------


def test_hello_fetches_the_policy_when_the_version_changes():
    plane = FakePlane(
        policy_body={"version": 4, "dry_run": False, "policy": {"deny": ["wire"]}}
    )
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=4))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_version == 4
    assert shared.policy_dry_run is False


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
    plane = FakePlane(policy_body={"deny": ["wire"], "dry_run": True})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=2))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_dry_run is True
    assert shared.policy_version == 2


def test_a_policy_fetch_that_fails_leaves_the_old_policy_alone():
    plane = FakePlane(policy_body={"version": 1, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=1))

    plane.policy_body = None
    shared.apply_hello(HelloReply(policy_version=2))

    assert shared.policy() == {"deny": ["wire"]}
    assert shared.policy_version == 1


# --- fleet circuits ---------------------------------------------------------


def test_hello_opens_and_closes_fleet_circuits():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    breaker = CircuitBreaker(3, 60.0, 10.0, now=clock)
    shared.breaker = breaker

    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open", "until_s": 30}}))
    assert breaker.state("openai@api") == "open"

    clock.advance(5)
    shared.apply_hello(HelloReply(circuits={"openai@api": "closed"}))
    assert breaker.state("openai@api") == "closed"


def test_a_circuit_already_open_is_not_reopened():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    calls: list = []

    class SpyBreaker:
        def state(self, label):
            return "open"

        def force_open(self, label, until_s=None):
            calls.append(("open", label, until_s))

        def force_close(self, label):
            calls.append(("close", label))

    shared.breaker = SpyBreaker()
    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open"}}))
    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open"}}))

    assert calls == []


def test_circuits_are_ignored_without_a_breaker():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(circuits={"openai@api": "open"}))  # must not raise


# --- exits, trips, circuits, clear ------------------------------------------


def test_exit_hands_the_delta_to_the_exporter():
    exporter = RecordingExporter()
    shared = remote(FakePlane(), MovableClock(), exporter=exporter)
    delta = ExitDelta(key_hash="abc", seq=3, spend_delta_usd=0.5)

    shared.exit("user-9", state(), delta)

    assert exporter.exits == [delta]


def test_a_trip_is_posted_to_the_plane_synchronously():
    plane = FakePlane()
    exporter = RecordingExporter()
    shared = remote(plane, MovableClock(), exporter=exporter)
    anomaly = Anomaly("budget", "critical", "over", {"spend": 6})
    session = state()
    session.strikes = 2
    session.fleet_generation = 4

    shared.trip("user-9", session, anomaly, 60.0, False)

    report = plane.payloads("trip")[0]
    assert report.key_hash == key_hash("user-9")
    assert report.anomaly["detector"] == "budget"
    assert report.anomaly["reacted"] == "raise"
    assert report.latch_ttl_s == 60.0
    assert report.strikes == 2
    assert report.generation == 4
    assert report.refused_at_door is False
    assert exporter.anomalies == []


def test_a_trip_the_plane_refused_falls_back_to_the_exporter():
    exporter = RecordingExporter()
    shared = remote(FakePlane(ok=False), MovableClock(), exporter=exporter)
    anomaly = Anomaly("budget", "critical", "over", {})

    shared.trip("user-9", state(), anomaly, None, True)

    _session, sent, reacted = exporter.anomalies[0]
    assert sent is anomaly
    assert reacted == "door"


def test_a_circuit_transition_goes_to_the_exporter():
    exporter = RecordingExporter()
    shared = remote(FakePlane(), MovableClock(), exporter=exporter)

    shared.circuit("openai@api", "open", 5, 30.0)

    assert exporter.circuits == [("openai@api", "open", 5, 30.0)]


def test_clear_forwards_the_key_hash():
    plane = FakePlane()
    shared = remote(plane, MovableClock())

    shared.clear("user-9")

    assert plane.payloads("clear") == [key_hash("user-9")]


def test_clear_also_forgets_the_cached_decision():
    shared = remote(FakePlane(decision=EntryDecision()), MovableClock())
    shared.enter("user-9", state(), shared._config)

    shared.clear("user-9")

    assert shared.fleet_status("user-9") is None


# --- status -----------------------------------------------------------------


def test_status_reports_the_age_of_the_last_contact():
    clock = MovableClock()
    shared = remote(FakePlane(decision=EntryDecision()), clock)

    shared.enter("user-9", state(), shared._config)
    clock.advance(2.5)
    status = shared.status()

    assert status.mode == "connected"
    assert status.last_contact_age_s == pytest.approx(2.5)


def test_status_carries_the_planes_notice():
    shared = remote(FakePlane(), MovableClock())

    shared.apply_hello(HelloReply(notice="plan expires in 3 days"))

    assert shared.status().notice == "plan expires in 3 days"


# --- fail-open --------------------------------------------------------------


def test_nothing_raises_when_the_plane_explodes(caplog):
    plane = FakePlane(explodes=True)
    exporter = RecordingExporter()
    shared = remote(plane, MovableClock(), exporter=exporter)
    session = state()

    with caplog.at_level("WARNING"):
        assert shared.enter("user-9", session, shared._config) is None
        shared.trip("user-9", session, Anomaly("budget", "critical", "over", {}), 1.0, False)
        shared.clear("user-9")
        shared.apply_hello(HelloReply(policy_version=3))

    assert shared.status().mode in ("connected", "degraded")


def test_nothing_raises_when_the_exporter_explodes():
    class BoomExporter:
        def on_exit(self, delta):
            raise RuntimeError("boom")

        def on_circuit(self, *args):
            raise RuntimeError("boom")

        def on_anomaly(self, *args):
            raise RuntimeError("boom")

    shared = remote(FakePlane(ok=False), MovableClock(), exporter=BoomExporter())

    shared.exit("user-9", state(), ExitDelta(key_hash="x"))
    shared.circuit("openai", "open", 3, 30.0)
    shared.trip("user-9", state(), Anomaly("budget", "critical", "over", {}), None, False)


def test_a_broken_session_never_costs_the_caller_its_entry():
    class BrokenSession:
        key = "user-9"

        @property
        def tags(self):
            raise RuntimeError("no tags for you")

    shared = remote(FakePlane(decision=EntryDecision()), MovableClock())

    assert shared.enter("user-9", BrokenSession(), shared._config) is None


def test_start_and_stop_are_safe_without_a_plane_thread():
    exporter = RecordingExporter()
    shared = remote(FakePlane(), MovableClock(), exporter=exporter)

    class FakeEngine:
        circuit = CircuitBreaker(3, 60.0, 30.0)

    shared.start(FakeEngine())
    try:
        assert exporter.started == 1
        assert shared.breaker is not None
    finally:
        shared.stop()
    assert exporter.stopped == 1


def test_the_hello_payload_describes_this_worker():
    plane = FakePlane()
    shared = remote(plane, MovableClock())
    breaker = CircuitBreaker(3, 60.0, 30.0)
    breaker.force_open("openai@api", 30.0)
    shared.breaker = breaker

    payload = shared._hello_payload()

    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert payload["policy_version_seen"] == 0
    assert payload["circuits"] == {"openai@api": "open"}
    assert isinstance(payload["sdk_version"], str)
    assert payload["active"] == 0


def test_the_hello_payload_counts_this_processs_open_sessions():
    """The plane's heartbeat consumes this to see how busy a worker is.

    Read from :func:`runbound.api.active_sessions`, process-wide — not from
    anything this :class:`RemoteState` itself tracks — because it is a count
    of open ``runbound.session()`` blocks, keyed or not otherwise, and the
    fleet link is not where that bookkeeping lives.
    """
    import runbound

    plane = FakePlane()
    shared = remote(plane, MovableClock())
    runbound.init()

    assert shared._hello_payload()["active"] == 0
    with runbound.session("user-1"):
        assert shared._hello_payload()["active"] == 1
        with runbound.session("user-2"):
            assert shared._hello_payload()["active"] == 2
        assert shared._hello_payload()["active"] == 1
    assert shared._hello_payload()["active"] == 0


def test_the_hello_payload_fails_open_when_the_active_count_cannot_be_read(monkeypatch):
    """A heartbeat must never fail because the active count could not be read."""
    from runbound import api

    def _boom():
        raise RuntimeError("no")

    monkeypatch.setattr(api, "active_sessions", _boom)
    plane = FakePlane()
    shared = remote(plane, MovableClock())

    assert shared._hello_payload()["active"] == 0


def test_the_hello_payload_carries_a_coverage_block_with_the_right_shape():
    """The heartbeat reports the same four coverage numbers as ``runbound.coverage()``.

    Exact counts are not asserted — they depend on whatever else in the
    process has imported provider SDKs or decorated tools — only that the
    block has exactly these four keys, typed as the fleet expects them.
    """
    plane = FakePlane()
    shared = remote(plane, MovableClock())

    coverage = shared._hello_payload()["coverage"]

    assert set(coverage) == {
        "guarded_calls",
        "decorated_tools",
        "decorated_tool_names",
        "providers_imported",
        "providers_unguarded",
    }
    assert isinstance(coverage["guarded_calls"], int)
    assert isinstance(coverage["decorated_tools"], int)
    assert isinstance(coverage["decorated_tool_names"], list)
    assert isinstance(coverage["providers_imported"], list)
    assert isinstance(coverage["providers_unguarded"], list)
    assert all(isinstance(name, str) for name in coverage["decorated_tool_names"])
    assert all(isinstance(name, str) for name in coverage["providers_imported"])
    assert all(isinstance(name, str) for name in coverage["providers_unguarded"])


def test_the_hello_payload_fails_open_when_coverage_cannot_be_read(monkeypatch):
    """A heartbeat must never fail because its own coverage snapshot broke —
    and the rest of the payload must still be intact."""
    from runbound import _coverage

    def _boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(_coverage, "snapshot", _boom)
    plane = FakePlane()
    shared = remote(plane, MovableClock())
    breaker = CircuitBreaker(3, 60.0, 30.0)
    shared.breaker = breaker

    payload = shared._hello_payload()

    assert payload["coverage"] == {}
    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert payload["policy_version_seen"] == 0
    assert payload["circuits"] == {}
    assert isinstance(payload["sdk_version"], str)
    assert payload["active"] == 0
