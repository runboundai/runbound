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
from runbound.engine import Engine
from runbound.events import Anomaly
from runbound.plane_types import EntryDecision, ExitDelta, HelloReply, key_hash
from runbound.shared import (
    DEGRADE_AFTER,
    DECISION_TTL_S,
    ENTRY_WINDOW_S,
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
        controls_body: dict | None = None,
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
        #: What :meth:`controls` answers — the ``/v1/controls`` envelope
        #: (``{"version", "dry_run", "controls"}``), or a caller sets this
        #: per test.
        self.controls_body = controls_body
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

    def controls(self, service: str):
        self._record("controls", service)
        return self.controls_body

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


def enable_circuit_fleet(shared: RemoteState, plane: FakePlane, *, fleet: bool = True) -> None:
    """``circuit_fleet`` is a real, local ``init()`` keyword, defaulting
    ``True`` — this worker's
    own opt in/out of folding its breaker into the plane's fleet-wide
    circuit, read straight off ``config.circuit_fleet``
    (:meth:`~runbound.shared.RemoteState._circuit_fleet`), never a plane
    directive. ``plane`` is accepted (and unused) only so every existing
    call site here keeps its shape."""
    shared._config.circuit_fleet = fleet


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
    # An EntryDecision has no mode of its own -- it only ever means "stop"
    # (a Narrow halt never refuses at the door, so it never rides `/enter`).
    assert shared.halt_mode() == "stop"


# --- Halt mode and the halt's own narrow posture --------------------------


def test_a_stop_halt_has_no_narrow_posture():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)

    shared.apply_hello(HelloReply(halt=True, halt_mode="stop"))

    assert shared.halt_mode() == "stop"
    assert shared.halt_posture_directive() is None


def test_a_narrow_halt_states_restricted_from_source_halt():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)

    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    assert shared.halt_mode() == "narrow"
    posture = shared.halt_posture_directive()
    assert posture is not None
    assert posture.name == "restricted"
    assert posture.source == "halt"


def test_an_older_plane_that_never_sends_a_mode_means_stop():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)

    shared.apply_hello(HelloReply(halt=True))

    assert shared.halt_mode() == "stop"
    assert shared.halt_posture_directive() is None


def test_lifting_a_narrow_halt_clears_its_posture_and_mode():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    shared.apply_hello(HelloReply(halt=False))

    assert shared.halted() is False
    assert shared.halt_mode() is None
    assert shared.halt_posture_directive() is None


def test_narrowing_again_with_the_same_name_keeps_its_own_timestamp():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))
    first = shared.halt_posture_directive()

    clock.advance(1.0)
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))
    second = shared.halt_posture_directive()

    assert first.entered_at == second.entered_at


def test_a_stale_narrow_halt_fails_open():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    clock.advance(STALE_HALT_S + 1)

    assert shared.halt_mode() is None
    assert shared.halt_posture_directive() is None


def test_a_narrow_halt_holds_under_stale_halt_hold():
    clock = MovableClock()
    shared = remote(FakePlane(), clock, stale_halt="hold")
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    clock.advance(STALE_HALT_S + 1)

    assert shared.halt_mode() == "narrow"
    assert shared.halt_posture_directive() is not None


def test_a_narrow_halt_and_a_controls_posture_are_independent_slots():
    """Two plane-driven postures (the Controls-stated one and a Narrow
    halt) must never share storage -- lifting one must never
    clear the other, in either direction."""
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(posture="read_only", halt=True, halt_mode="narrow"))

    assert shared.posture_directive().name == "read_only"
    assert shared.halt_posture_directive().name == "restricted"

    # The halt lifts; the heartbeat still carries the same Controls posture.
    shared.apply_hello(HelloReply(posture="read_only", halt=False))

    assert shared.halt_posture_directive() is None
    assert shared.posture_directive() is not None
    assert shared.posture_directive().name == "read_only"


def test_local_state_has_no_halt_mode_or_posture():
    local = LocalState()
    assert local.halt_mode() is None
    assert local.halt_posture_directive() is None


def test_the_hello_payload_acks_a_halt_it_has_seen():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="narrow"))

    payload = shared._hello_payload()

    assert payload["halt_ack"] is True


def test_the_hello_payload_does_not_ack_when_there_is_no_halt():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=False))

    payload = shared._hello_payload()

    assert payload["halt_ack"] is False


def test_the_hello_payload_stops_acking_once_a_stale_halt_releases():
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    shared.apply_hello(HelloReply(halt=True, halt_mode="stop"))

    clock.advance(STALE_HALT_S + 1)

    assert shared._hello_payload()["halt_ack"] is False


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


# --- Controls ---------------------------------------------------------------


def test_hello_fetches_controls_when_the_version_changes():
    plane = FakePlane(
        controls_body={"version": 4, "dry_run": False, "controls": {"envelope": True}}
    )
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(controls_version=4))

    assert shared.controls_directive() == {"envelope": True}
    assert shared.controls_version == 4


def test_controls_are_not_refetched_for_the_same_version():
    plane = FakePlane(controls_body={"version": 4, "controls": {"envelope": True}})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(controls_version=4))
    shared.apply_hello(HelloReply(controls_version=4))

    assert plane.names().count("controls") == 1


def test_a_new_controls_version_refetches():
    plane = FakePlane(controls_body={"version": 4, "controls": {"envelope": True}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(controls_version=4))

    plane.controls_body = {"version": 5, "controls": {"envelope": False}}
    shared.apply_hello(HelloReply(controls_version=5))

    assert shared.controls_directive() == {"envelope": False}
    assert shared.controls_version == 5


def test_a_dry_run_controls_row_is_fetched_but_never_directed():
    """Carried and shown (the version still advances), never enforced —
    the same "shadow observes, enforce acts" rule the plane's own rollout
    state applies elsewhere."""
    plane = FakePlane(
        controls_body={"version": 4, "dry_run": True, "controls": {"envelope": True}}
    )
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(controls_version=4))

    assert shared.controls_directive() is None
    assert shared.controls_version == 4


def test_no_served_controls_is_no_directive():
    plane = FakePlane(controls_body={"version": 0, "controls": None})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(controls_version=0))

    assert shared.controls_directive() is None


def test_a_controls_fetch_that_fails_leaves_the_old_body_alone():
    plane = FakePlane(controls_body={"version": 1, "controls": {"envelope": True}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(controls_version=1))

    plane.controls_body = None
    shared.apply_hello(HelloReply(controls_version=2))

    assert shared.controls_directive() == {"envelope": True}
    assert shared.controls_version == 1


def test_a_controls_directive_goes_stale_like_the_halt():
    plane = FakePlane(controls_body={"version": 1, "controls": {"envelope": True}})
    clock = MovableClock()
    shared = remote(plane, clock)
    shared.apply_hello(HelloReply(controls_version=1))
    assert shared.controls_directive() is not None

    clock.advance(STALE_HALT_S + 1.0)

    assert shared.controls_directive() is None


def test_a_controls_directive_holds_while_stale_halt_is_hold():
    plane = FakePlane(controls_body={"version": 1, "controls": {"envelope": True}})
    clock = MovableClock()
    shared = remote(plane, clock, stale_halt="hold")
    shared.apply_hello(HelloReply(controls_version=1))

    clock.advance(STALE_HALT_S + 1.0)

    assert shared.controls_directive() == {"envelope": True}


def test_local_state_has_no_controls_directive():
    assert LocalState().controls_directive() is None


# --- fleet circuits ---------------------------------------------------------


def test_hello_opens_and_closes_fleet_circuits():
    plane = FakePlane()
    clock = MovableClock()
    shared = remote(plane, clock)
    breaker = CircuitBreaker(3, 60.0, 10.0, now=clock)
    shared.breaker = breaker
    enable_circuit_fleet(shared, plane)

    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open", "until_s": 30}}))
    assert breaker.state("openai@api") == "open"

    clock.advance(5)
    shared.apply_hello(HelloReply(circuits={"openai@api": "closed"}))
    assert breaker.state("openai@api") == "closed"


def test_circuit_fleet_is_on_by_default_once_connected():
    """``circuit_fleet`` defaults ``True`` -- a freshly connected worker
    folds into the fleet
    circuit from its very first hello, with no plane directive needed to
    turn it on."""
    clock = MovableClock()
    shared = remote(FakePlane(), clock)
    breaker = CircuitBreaker(3, 60.0, 10.0, now=clock)
    shared.breaker = breaker

    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open", "until_s": 30}}))

    assert breaker.state("openai@api") == "open"


def test_circuit_fleet_false_never_applies_a_fleet_instruction():
    """An opted-out worker's local breaker is untouched by the fleet, even a
    fresh worker's very first hello into an already-open circuit."""
    plane = FakePlane()
    clock = MovableClock()
    shared = remote(plane, clock)
    breaker = CircuitBreaker(3, 60.0, 10.0, now=clock)
    shared.breaker = breaker
    enable_circuit_fleet(shared, plane, fleet=False)

    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open", "until_s": 30}}))

    assert breaker.state("openai@api") == "closed"


def test_circuit_fleet_false_never_reports_a_local_transition():
    plane = FakePlane()
    shared = remote(plane, MovableClock())
    enable_circuit_fleet(shared, plane, fleet=False)
    exporter = RecordingExporter()
    shared._exporter = exporter

    shared.circuit("openai", "open", 5, 30.0)

    assert exporter.circuits == []


def test_circuit_fleet_enabled_by_the_plane_applies_and_reports():
    plane = FakePlane()
    clock = MovableClock()
    shared = remote(plane, clock)
    breaker = CircuitBreaker(3, 60.0, 10.0, now=clock)
    shared.breaker = breaker
    exporter = RecordingExporter()
    shared._exporter = exporter
    enable_circuit_fleet(shared, plane)

    shared.apply_hello(HelloReply(circuits={"openai@api": {"state": "open", "until_s": 30}}))
    shared.circuit("openai", "open", 5, 30.0)

    assert breaker.state("openai@api") == "open"
    assert exporter.circuits == [("openai", "open", 5, 30.0)]


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
    plane = FakePlane()
    exporter = RecordingExporter()
    shared = remote(plane, MovableClock(), exporter=exporter)
    enable_circuit_fleet(shared, plane)

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
    plane.controls_body = {"version": 1, "controls": {}}
    shared.apply_hello(HelloReply(controls_version=1))

    payload = shared._hello_payload()

    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert payload["policy_version_seen"] == 0
    assert payload["controls_version_seen"] == 1
    assert payload["circuits"] == {"openai@api": "open"}
    assert isinstance(payload["sdk_version"], str)
    assert payload["active"] == 0
    # No engine attached (``start`` was never called) -- "we do not know
    # yet", never rendered by the plane as "cannot stop".
    assert payload["can_stop"] is None
    assert "controls_refused" not in payload


def test_the_hello_payload_reports_can_stop_from_the_attached_engine():
    plane = FakePlane()
    shared = remote(plane, MovableClock(), on_anomaly="raise")
    eng = Engine(shared._config)
    shared.start(eng)
    try:
        assert shared._hello_payload()["can_stop"] is True
    finally:
        shared.stop()


def test_the_hello_payload_reports_can_stop_false_under_warn_mode():
    plane = FakePlane()
    shared = remote(plane, MovableClock(), on_anomaly="warn")
    eng = Engine(shared._config)
    shared.start(eng)
    try:
        assert shared._hello_payload()["can_stop"] is False
    finally:
        shared.stop()


def test_the_hello_payload_carries_controls_refused_from_the_engine():
    plane = FakePlane(
        controls_body={
            "version": 1,
            "controls": {"limits": {"org": {"budget_usd": 100.0}}},
        }
    )
    shared = remote(plane, MovableClock(), budget_usd=10.0)
    eng = Engine(shared._config, shared=shared)
    shared.start(eng)
    try:
        shared.apply_hello(HelloReply(controls_version=1))

        payload = shared._hello_payload()

        assert payload["controls_refused"] == [
            {"path": "limits.budget_usd", "base": 10.0, "candidate": 100.0}
        ]
    finally:
        shared.stop()


def test_the_hello_payload_carries_the_envelope_alongside_posture_and_budget():
    """The heartbeat carries the same object ``runbound.envelope()``
    returns, so the plane can show a worker's own picture without a call
    the customer's own process has to make."""
    import runbound
    from runbound import api

    plane = FakePlane()
    shared = remote(plane, MovableClock())
    api._teardown_for_tests()
    try:
        runbound.init(budget_usd=5.0)

        envelope = shared._hello_payload()["envelope"]

        assert envelope["posture"] == "full"
        assert envelope["budget"] == {"remaining": 5.0, "reserved": 0.0, "max_request": 5.0}
        assert "capabilities" in envelope
        assert "execution" in envelope
    finally:
        api._teardown_for_tests()


def test_the_hello_payload_omits_the_envelope_before_any_init(monkeypatch):
    """Fail-open: no engine at all (``runbound.init()`` never ran in this
    process) is exactly what ``_envelope(None)`` already reports ``None``
    for -- the heartbeat then leaves the key out entirely, the same
    conditional-key shape ``controls_refused`` already has above."""
    from runbound import api

    plane = FakePlane()
    shared = remote(plane, MovableClock())
    monkeypatch.setattr(api, "_ENGINE", None)

    assert "envelope" not in shared._hello_payload()


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


# --- the rolling one-minute window over entry outcomes -----------------------
#
# A link can answer every heartbeat and still be having most session entries
# decided locally: a plane slow enough to miss control_plane_timeout_s on the
# hot ``/v1/enter`` path, but fast enough to answer the background heartbeat.
# The consecutive-failure count never catches that, because a heartbeat's own
# success resets it long before three entry timeouts land in a row. This
# window is a second, independent signal: how the last minute's entries were
# actually decided -- by the plane, from the cache, or locally -- so a
# customer watching plane_status() or coverage() sees the truth even while
# every heartbeat keeps succeeding.


def test_the_entry_window_starts_at_zero():
    shared = remote(FakePlane(), MovableClock())

    status = shared.status()

    assert status.entries_window == {
        "plane": 0, "cache": 0, "local": 0,
        "local_causes": {"timeout": 0, "plane_loss": 0, "plane_unavailable": 0, "error": 0},
    }
    assert status.entries_local_share == 0.0


def test_a_plane_answer_is_counted_in_the_window():
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, MovableClock())

    shared.enter("user-9", state(), shared._config)

    assert shared.status().entries_window == {
        "plane": 1, "cache": 0, "local": 0,
        "local_causes": {"timeout": 0, "plane_loss": 0, "plane_unavailable": 0, "error": 0},
    }


def test_a_cache_hit_is_counted_separately_from_a_plane_answer():
    clock = MovableClock()
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, clock)

    shared.enter("user-9", state(), shared._config)  # a real plane answer
    shared.enter("user-9", state(), shared._config)  # served from the cache

    assert shared.status().entries_window == {
        "plane": 1, "cache": 1, "local": 0,
        "local_causes": {"timeout": 0, "plane_loss": 0, "plane_unavailable": 0, "error": 0},
    }


def test_a_timed_out_entry_is_counted_as_local():
    plane = FakePlane(ok=False)
    shared = remote(plane, MovableClock())

    shared.enter("user-9", state(), shared._config)

    assert shared.status().entries_window == {
        "plane": 0, "cache": 0, "local": 1,
        "local_causes": {"timeout": 1, "plane_loss": 0, "plane_unavailable": 0, "error": 0},
    }


def test_a_skipped_call_on_an_already_degraded_link_is_also_local():
    """``_may_call`` returning False (the degraded-link retry gate) never
    opens a socket, but the entry is still decided on this worker's own
    numbers -- it belongs in the same bucket as a fresh timeout."""
    clock = MovableClock()
    plane = FakePlane(ok=False)
    shared = remote(plane, clock)
    for _ in range(DEGRADE_AFTER):
        shared.enter("user-9", state(), shared._config)  # spends the real attempts

    shared.enter("user-9", state(), shared._config)  # answered with no call at all

    assert plane.names().count("enter") == DEGRADE_AFTER
    assert shared.status().entries_window["local"] == DEGRADE_AFTER + 1


@pytest.mark.parametrize(
    "local, other, expect_degraded",
    [
        (9, 0, False),   # under the 10-entry floor: never degrades, however
                         # lopsided the share is
        (5, 5, False),   # exactly at the 0.5 boundary: not "exceeds"
        (6, 4, True),    # just over the boundary: degrades
        (0, 10, False),  # a healthy window: no share problem at all
    ],
)
def test_the_window_arithmetic_is_table_tested(local, other, expect_degraded):
    """Isolates the window's own arithmetic from the older consecutive-
    failure mechanism: a heartbeat between every local outcome keeps
    resetting the failure count, exactly as a real fleet's does, so only
    the window decides whether this case degrades."""
    clock = MovableClock()
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, clock)

    for i in range(other):
        shared.enter(f"plane-{i}", state(key=f"plane-{i}"), shared._config)
    plane.ok = False
    for i in range(local):
        shared.apply_hello(HelloReply())
        shared.enter(f"local-{i}", state(key=f"local-{i}"), shared._config)

    status = shared.status()
    assert status.entries_window == {
        "plane": other, "cache": 0, "local": local,
        "local_causes": {"timeout": local, "plane_loss": 0, "plane_unavailable": 0, "error": 0},
    }
    if expect_degraded:
        assert status.mode == "degraded"
        assert status.reason == "entry timeouts"
    else:
        assert status.mode == "connected"
        assert status.reason is None


def test_an_entry_ages_out_of_the_window_after_a_minute():
    clock = MovableClock()
    plane = FakePlane(ok=False)
    shared = remote(plane, clock)

    shared.enter("user-9", state(), shared._config)
    assert shared.status().entries_window["local"] == 1

    clock.advance(ENTRY_WINDOW_S - 0.01)
    assert shared.status().entries_window["local"] == 1  # not stale yet

    clock.advance(0.02)
    assert shared.status().entries_window["local"] == 0  # a minute has passed


def test_a_partial_outage_degrades_the_link_with_an_entry_timeouts_reason():
    """The blind spot this window closes: the heartbeat keeps succeeding
    (so the older consecutive-failure count never reaches DEGRADE_AFTER),
    while every /v1/enter call sleeps past the timeout and is answered
    locally instead."""
    clock = MovableClock()
    plane = FakePlane(decision=EntryDecision())
    plane.delay_s = plane.timeout_s + 0.05  # every enter() call times out
    shared = remote(plane, clock)

    for i in range(12):
        shared.apply_hello(HelloReply())  # the heartbeat keeps succeeding
        shared.enter(f"user-{i}", state(key=f"user-{i}"), shared._config)

    status = shared.status()
    assert status.mode == "degraded"
    assert status.reason == "entry timeouts"
    assert status.consecutive_failures < DEGRADE_AFTER
    assert status.entries_local_share > 0.5
    assert status.entries_window["local"] >= 10

    # Recovers once /v1/enter answers again -- the window is dominated by
    # good answers again, well before the bad ones would even age out.
    plane.delay_s = 0.0
    shared.apply_hello(HelloReply())
    for i in range(20):
        shared.enter(f"user-recovered-{i}", state(key=f"user-recovered-{i}"), shared._config)

    recovered = shared.status()
    assert recovered.mode == "connected"
    assert recovered.reason is None


def test_the_total_outage_reason_is_heartbeat_failures():
    """Pins the total-outage behaviour already known to be correct: three
    failures in a row (the heartbeat included) degrade the link the old
    way, and the new ``reason`` field names that path too."""
    plane = FakePlane(ok=False)
    shared = remote(plane, MovableClock())

    for _ in range(DEGRADE_AFTER):
        shared.enter("user-9", state(), shared._config)

    status = shared.status()
    assert status.mode == "degraded"
    assert status.reason == "heartbeat failures"


# --- the local share on the heartbeat -----------------------------------------


def test_the_hello_payload_omits_the_local_share_with_too_few_entries():
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, MovableClock())

    for i in range(3):
        shared.enter(f"user-{i}", state(key=f"user-{i}"), shared._config)

    assert "entries_local_share" not in shared._hello_payload()


def test_the_hello_payload_carries_the_local_share_once_the_window_fills():
    clock = MovableClock()
    plane = FakePlane(decision=EntryDecision())
    shared = remote(plane, clock)
    for i in range(6):
        shared.enter(f"plane-{i}", state(key=f"plane-{i}"), shared._config)
    plane.ok = False
    for i in range(4):
        shared.enter(f"local-{i}", state(key=f"local-{i}"), shared._config)

    assert shared._hello_payload()["entries_local_share"] == pytest.approx(0.4)


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
