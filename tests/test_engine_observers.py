"""Tests for the engine's fleet seam: observers and shared state.

The engine gained two collaborators in fleet mode — ``observers``, told what
happened after it has already happened, and ``shared``, asked what the rest of
the fleet knows. Both are doubles here, and both must be unable to change what
the agent sees: an observer that raises, or a shared state that explodes, costs
the host nothing.
"""

import logging

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import HALT_DETECTOR, Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolCall, ToolPolicy
from runbound.shared import LocalState
from runbound.state import SessionState

WARN = Anomaly("velocity", "warn", "going fast", {})
CRITICAL = Anomaly("budget", "critical", "out of money", {"spend": 6})


class StubDetector:
    """Returns a fixed anomaly (or None) every time it is checked."""

    def __init__(self, anomaly: Anomaly | None, name: str = "stub") -> None:
        self.anomaly = anomaly
        self.name = name

    def check(self, state, event, config):
        return self.anomaly


class RecordingObserver:
    """Records the engine's notifications, in order."""

    def __init__(self) -> None:
        self.events: list = []
        self.anomalies: list = []

    def on_event(self, session, event) -> None:
        self.events.append((session.session_id, event.kind, session.step_count))

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.anomalies.append((anomaly.detector, reacted))


class BoomObserver:
    """An observer with a bug in it."""

    def on_event(self, session, event) -> None:
        raise RuntimeError("observer is broken")

    def on_anomaly(self, session, anomaly, reacted) -> None:
        raise RuntimeError("observer is broken")


class RecordingShared(LocalState):
    """A shared state that records what it was told and answers a fixed policy."""

    fleet = True

    def __init__(self, remote_policy: dict | None = None, version: int = 0,
                 dry_run: bool = False) -> None:
        self.trips: list = []
        self.circuits: list = []
        self.remote_policy = remote_policy
        self.version = version
        self.dry_run = dry_run

    def trip(self, key, state, anomaly, ttl, door) -> None:
        self.trips.append((key, anomaly.detector, ttl, door))

    def circuit(self, label, state, failures, cooldown_s) -> None:
        self.circuits.append((label, state, failures, cooldown_s))

    def policy(self):
        return self.remote_policy

    @property
    def policy_version(self) -> int:
        return self.version

    @property
    def policy_dry_run(self) -> bool:
        return self.dry_run


class BoomShared(LocalState):
    """A shared state that breaks its never-raises contract."""

    def trip(self, key, state, anomaly, ttl, door):
        raise RuntimeError("shared is broken")

    def circuit(self, *args):
        raise RuntimeError("shared is broken")

    def policy(self):
        raise RuntimeError("shared is broken")


def session(key: str = "user-9") -> SessionState:
    return SessionState("sess-1", key=key)


def event(kind: str = "llm_call", step: int = 1, **fields) -> Event:
    return Event(kind=kind, ts=1000.0, step=step, **fields)


def call(name: str = "wire_transfer") -> ToolCall:
    return ToolCall(name=name, args=(), kwargs={}, session_key="user-9", tags={})


# --- on_event ---------------------------------------------------------------


def test_every_event_reaches_the_observers_after_it_is_recorded():
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(), detectors=[], observers=[observer])
    state = session()

    for step, kind in enumerate(("llm_call", "tool_call", "tool_error", "tool_request"), 1):
        engine.process(state, event(kind, step=step))

    assert [kind for _id, kind, _steps in observer.events] == [
        "llm_call",
        "tool_call",
        "tool_error",
        "tool_request",
    ]
    # Recorded first: the observer sees the session including this event.
    assert [steps for _id, _kind, steps in observer.events] == [1, 2, 3, 4]


def test_a_failed_model_call_reaches_the_observers_too():
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(), detectors=[], observers=[observer])

    engine.record_llm_error(session(), "gpt-4o", RuntimeError("boom"), 1.0, "openai@api")

    assert [kind for _id, kind, _steps in observer.events] == ["llm_error"]


def test_an_observer_that_raises_never_reaches_the_host(caplog):
    engine = Engine(
        GuardrailConfig(), detectors=[], observers=[BoomObserver()]
    )

    with caplog.at_level(logging.WARNING, logger="runbound"):
        engine.process(session(), event())

    assert "observer" in caplog.text.lower()


def test_a_second_observer_still_hears_about_the_event():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(),
        detectors=[],
        observers=[BoomObserver(), observer],
    )

    engine.process(session(), event())

    assert len(observer.events) == 1


def test_no_observers_is_the_default():
    assert Engine(GuardrailConfig()).observers == []


# --- on_anomaly and what the engine did about it ----------------------------


def test_an_anomaly_that_stops_the_session_is_reported_as_a_raise():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(on_anomaly="raise"),
        detectors=[StubDetector(CRITICAL)],
        observers=[observer],
    )

    with pytest.raises(GuardrailTripped):
        engine.process(session(), event())

    assert observer.anomalies == [("budget", "raise")]


def test_an_anomaly_under_warn_is_reported_as_a_warn():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(on_anomaly="warn"),
        detectors=[StubDetector(CRITICAL)],
        observers=[observer],
    )

    engine.process(session(), event())

    assert observer.anomalies == [("budget", "warn")]


def test_a_warning_that_still_stops_the_run_is_reported_as_a_raise():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(on_anomaly="raise"),
        detectors=[StubDetector(WARN)],
        observers=[observer],
    )

    # A "warn"-severity anomaly latches nothing, but under on_anomaly="raise"
    # it does stop this call — and that is what the fleet is told.
    with pytest.raises(GuardrailTripped):
        engine.process(session(), event())

    assert observer.anomalies == [("velocity", "raise")]


def test_a_watching_spike_is_reported_as_a_warn_even_under_raise():
    observer = RecordingObserver()
    watching = Anomaly("spike", "warn", "unusual call", {"level": 1})
    engine = Engine(
        GuardrailConfig(on_anomaly="raise"),
        detectors=[StubDetector(watching, name="spike")],
        observers=[observer],
    )

    engine.process(session(), event())

    assert observer.anomalies == [("spike", "warn")]


def test_a_callback_reaction_is_reported_as_a_callback():
    observer = RecordingObserver()
    config = GuardrailConfig(on_anomaly="callback", callback=lambda anomaly: None)
    engine = Engine(
        config, detectors=[StubDetector(CRITICAL)], observers=[observer]
    )

    engine.process(session(), event())

    assert observer.anomalies == [("budget", "callback")]


def test_a_door_refusal_is_reported_as_a_door():
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(), detectors=[], observers=[observer])
    anomaly = Anomaly("fanout", "critical", "too many sessions", {"rule": "depth"})

    engine.notify_door(session(), anomaly)

    assert observer.anomalies == [("fanout", "door")]


def test_a_door_refusal_is_reported_once_per_session():
    observer = RecordingObserver()
    engine = Engine(GuardrailConfig(), detectors=[], observers=[observer])
    anomaly = Anomaly(HALT_DETECTOR, "critical", "halted", {})
    state = session()

    engine.notify_door(state, anomaly)
    engine.notify_door(state, anomaly)

    assert len(observer.anomalies) == 1


def test_an_observer_hears_nothing_when_the_alert_is_deduped():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(on_anomaly="warn"),
        detectors=[StubDetector(CRITICAL)],
        observers=[observer],
    )
    state = session()

    engine.process(state, event(step=1))
    engine.process(state, event(step=2))

    assert len(observer.anomalies) == 1


def test_an_observer_that_raises_on_an_anomaly_never_stops_the_reaction():
    engine = Engine(
        GuardrailConfig(on_anomaly="raise"),
        detectors=[StubDetector(CRITICAL)],
        observers=[BoomObserver()],
    )

    with pytest.raises(GuardrailTripped):
        engine.process(session(), event())


def test_a_session_served_its_wall_again_is_reported_once_as_blocked():
    observer = RecordingObserver()
    engine = Engine(
        GuardrailConfig(on_anomaly="warn", on_trip="latch"),
        detectors=[],
        observers=[observer],
    )
    state = session()
    with state.lock:
        state.tripped_by = CRITICAL
        state.tripped_at = 1000.0

    for step in (1, 2, 3):
        engine.process(state, event(step=step))

    assert observer.anomalies == [("budget", "blocked")]


# --- trips reach the fleet --------------------------------------------------


def test_a_latch_tells_the_fleet():
    shared = RecordingShared()
    engine = Engine(
        GuardrailConfig(on_anomaly="raise", latch_ttl_seconds=120.0),
        detectors=[StubDetector(CRITICAL)],
        shared=shared,
    )

    with pytest.raises(GuardrailTripped):
        engine.process(session(), event())

    assert shared.trips == [("user-9", "budget", 120.0, False)]


def test_a_session_that_was_already_latched_is_not_reported_twice():
    shared = RecordingShared()
    engine = Engine(
        GuardrailConfig(on_anomaly="raise", on_trip="latch"),
        detectors=[StubDetector(CRITICAL)],
        shared=shared,
    )
    state = session()

    for step in (1, 2):
        with pytest.raises(GuardrailTripped):
            engine.process(state, event(step=step))

    assert len(shared.trips) == 1


def test_nothing_is_reported_when_nothing_latched():
    shared = RecordingShared()
    engine = Engine(
        GuardrailConfig(on_anomaly="raise", on_trip="once"),
        detectors=[StubDetector(CRITICAL)],
        shared=shared,
    )

    with pytest.raises(GuardrailTripped):
        engine.process(session(), event())

    assert shared.trips == []


def test_a_shared_state_that_explodes_never_reaches_the_host(caplog):
    engine = Engine(
        GuardrailConfig(on_anomaly="raise"),
        detectors=[StubDetector(CRITICAL)],
        shared=BoomShared(),
    )

    with caplog.at_level(logging.WARNING, logger="runbound"):
        with pytest.raises(GuardrailTripped):
            engine.process(session(), event())


def test_local_state_is_the_default_shared_state():
    assert isinstance(Engine(GuardrailConfig()).shared, LocalState)


# --- circuits reach the fleet -----------------------------------------------


def test_an_opening_circuit_is_reported_to_the_fleet():
    shared = RecordingShared()
    config = GuardrailConfig(circuit_failure_threshold=2, circuit_cooldown_seconds=30.0)
    engine = Engine(config, detectors=[], shared=shared)
    state = session()

    for _ in range(2):
        engine._mark_provider(state, TimeoutError("timeout"), "openai@api")

    assert shared.circuits == [("openai@api", "open", 2, 30.0)]


def test_a_closing_circuit_is_reported_to_the_fleet():
    shared = RecordingShared()
    config = GuardrailConfig(circuit_failure_threshold=1, circuit_cooldown_seconds=30.0)
    engine = Engine(config, detectors=[], shared=shared)
    engine._mark_provider(session(), TimeoutError("timeout"), "openai@api")

    engine.record_llm_success("openai@api")

    assert shared.circuits[-1] == ("openai@api", "closed", 0, 30.0)


def test_a_healthy_provider_reports_nothing():
    shared = RecordingShared()
    engine = Engine(GuardrailConfig(), detectors=[], shared=shared)

    engine.record_llm_success("openai@api")

    assert shared.circuits == []


def test_circuit_reporting_is_skipped_entirely_without_a_plane():
    engine = Engine(GuardrailConfig(), detectors=[])

    engine.record_llm_success("openai@api")  # must not raise or cost anything


# --- the merged policy ------------------------------------------------------


def test_the_org_policy_is_merged_into_the_local_one():
    shared = RecordingShared(remote_policy={"deny": ["wire_transfer"]}, version=1)
    config = GuardrailConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(config, detectors=[], shared=shared)

    policy = engine._policy()

    assert sorted(policy.deny) == ["delete_db", "wire_transfer"]


def test_the_merged_policy_is_cached_until_the_version_changes():
    shared = RecordingShared(remote_policy={"deny": ["wire_transfer"]}, version=1)
    engine = Engine(GuardrailConfig(), detectors=[], shared=shared)

    first = engine._policy()
    assert engine._policy() is first

    shared.remote_policy = {"deny": ["refund"]}
    shared.version = 2
    second = engine._policy()

    assert second is not first
    assert second.deny == ["refund"]


def test_a_local_policy_stands_alone_without_a_remote_one():
    engine = Engine(
        GuardrailConfig(tool_policy={"deny": ["delete_db"]}), detectors=[]
    )

    assert engine._policy().deny == ["delete_db"]


def test_a_remote_policy_that_cannot_be_merged_leaves_the_local_one(caplog):
    shared = RecordingShared(remote_policy={"require_approval": ["refund"]}, version=1)
    config = GuardrailConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(config, detectors=[], shared=shared)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        policy = engine._policy()

    assert policy.deny == ["delete_db"]
    assert "policy" in caplog.text.lower()


def test_a_shared_state_whose_policy_explodes_leaves_the_local_one():
    config = GuardrailConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(config, detectors=[], shared=BoomShared())

    assert engine._policy().deny == ["delete_db"]


# --- the org's dry run ------------------------------------------------------


def test_an_org_dry_run_violation_alerts_and_lets_the_call_run(caplog):
    log: list = []

    class Recorder:
        """A second observer that keeps the whole anomaly, details included."""

        def on_event(self, session, event) -> None:
            pass

        def on_anomaly(self, session, anomaly, reacted) -> None:
            log.append(anomaly)

    observer = RecordingObserver()
    shared = RecordingShared(
        remote_policy={"deny": ["wire_transfer"]}, version=1, dry_run=True
    )
    config = GuardrailConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(
        config, detectors=[], observers=[Recorder(), observer], shared=shared
    )

    with caplog.at_level(logging.WARNING, logger="runbound"):
        engine.enforce_policy(session(), call("wire_transfer"))

    assert log and log[0].severity == "warn"
    assert log[0].details["origin"] == "org"
    assert observer.anomalies == [("policy", "dry_run")]
    assert "dry-run" in caplog.text.lower()


def test_a_local_rule_still_blocks_while_the_org_policy_is_dry():
    observer = RecordingObserver()
    shared = RecordingShared(
        remote_policy={"deny": ["wire_transfer"]}, version=1, dry_run=True
    )
    config = GuardrailConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(
        config, detectors=[], observers=[observer], shared=shared
    )

    with pytest.raises(PolicyViolation):
        engine.enforce_policy(session(), call("delete_db"))

    assert observer.anomalies == [("policy", "blocked")]


def test_an_enforced_org_rule_blocks_like_any_other():
    shared = RecordingShared(remote_policy={"deny": ["wire_transfer"]}, version=1)
    engine = Engine(GuardrailConfig(), detectors=[], shared=shared)

    with pytest.raises(PolicyViolation):
        engine.enforce_policy(session(), call("wire_transfer"))


def test_a_local_dry_run_policy_still_only_logs():
    policy = ToolPolicy(deny=["wire_transfer"], on_violation="dry_run")
    engine = Engine(GuardrailConfig(tool_policy=policy), detectors=[])

    engine.enforce_policy(session(), call("wire_transfer"))
