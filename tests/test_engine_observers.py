"""Tests for the engine's fleet seam: observers and shared state.

The engine gained two collaborators in fleet mode — ``observers``, told what
happened after it has already happened, and ``shared``, asked what the rest of
the fleet knows. Both are doubles here, and both must be unable to change what
the agent sees: an observer that raises, or a shared state that explodes, costs
the host nothing.
"""

import logging

import pytest

from runbound.config import RunboundConfig
from runbound.engine import HALT_DETECTOR, Engine
from runbound.events import Anomaly, Decision, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolCall, ToolPolicy
from runbound.shared import LocalState
from runbound.state import SessionState

CRITICAL = Anomaly("policy", "critical", "wire_transfer is denied", {"tool": "wire_transfer"})
DENY = Decision(verdict="deny", kind="action", boundary="policy", detector="policy")


class RecordingObserver:
    """Records the engine's notifications, in order."""

    def __init__(self) -> None:
        self.events: list = []
        self.anomalies: list = []

    def on_event(self, session, event) -> None:
        self.events.append((session.session_id, event.kind, session.event_count))

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
        self.remote_policy = remote_policy
        self.version = version
        self.dry_run = dry_run

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

    def halted(self):
        raise RuntimeError("shared is broken")

    def policy(self):
        raise RuntimeError("shared is broken")


def session(key: str = "user-9") -> SessionState:
    return SessionState("sess-1", key=key)


def event(kind: str = "tool_call", step: int = 1, **fields) -> Event:
    return Event(kind=kind, ts=1000.0, step=step, **fields)


def call(name: str = "wire_transfer") -> ToolCall:
    return ToolCall(name=name, args=(), kwargs={}, session_key="user-9", tags={})


# --- on_event ---------------------------------------------------------------


def test_every_event_reaches_the_observers_after_it_is_recorded():
    observer = RecordingObserver()
    engine = Engine(RunboundConfig(), observers=[observer])
    state = session()

    for step, kind in enumerate(("tool_call", "tool_error", "tool_request", "tool_call"), 1):
        engine.process(state, event(kind, step=step))

    assert [kind for _id, kind, _steps in observer.events] == [
        "tool_call",
        "tool_error",
        "tool_request",
        "tool_call",
    ]
    # Recorded first: the observer sees the session including this event.
    assert [steps for _id, _kind, steps in observer.events] == [1, 2, 3, 4]


def test_an_observer_that_raises_never_reaches_the_host(caplog):
    engine = Engine(
        RunboundConfig(), observers=[BoomObserver()]
    )

    with caplog.at_level(logging.WARNING, logger="runbound"):
        engine.process(session(), event())

    assert "observer" in caplog.text.lower()


def test_a_second_observer_still_hears_about_the_event():
    observer = RecordingObserver()
    engine = Engine(
        RunboundConfig(),
        observers=[BoomObserver(), observer],
    )

    engine.process(session(), event())

    assert len(observer.events) == 1


def test_no_observers_is_the_default():
    assert Engine(RunboundConfig()).observers == []


# --- on_anomaly and what the engine did about it ----------------------------


def test_a_refusal_is_reported_to_the_observers_with_what_the_engine_did():
    observer = RecordingObserver()
    engine = Engine(RunboundConfig(), observers=[observer])

    with pytest.raises(GuardrailTripped):
        engine.refuse(session(), CRITICAL, DENY, reacted="blocked")

    assert observer.anomalies == [("policy", "blocked")]


def test_a_door_refusal_is_reported_as_a_door():
    observer = RecordingObserver()
    engine = Engine(RunboundConfig(max_actions_per_run=1), observers=[observer])
    state = session()
    state.mark_action_admitted()

    with pytest.raises(GuardrailTripped):
        engine.admit_action(state, "search")

    assert observer.anomalies == [("fanout", "door")]


def test_a_fleet_halt_is_reported_as_a_door_and_names_the_halt_detector():
    class Halted(LocalState):
        def halted(self):
            return True

        def halt_mode(self):
            return "stop"

    observer = RecordingObserver()
    engine = Engine(RunboundConfig(), observers=[observer], shared=Halted())

    with pytest.raises(GuardrailTripped):
        engine.admit_call(session(), ToolCall("search", (), {}, None, {}))

    assert observer.anomalies == [(HALT_DETECTOR, "door")]


def test_every_refusal_is_reported_not_deduped():
    """A refusal is evidence: each one is told to the observers (up to the cap)."""
    observer = RecordingObserver()
    engine = Engine(RunboundConfig(tool_policy=ToolPolicy(deny=["wire_transfer"])), observers=[observer])
    state = session()

    for _ in range(3):
        with pytest.raises(PolicyViolation):
            engine.enforce_policy(state, call("wire_transfer"))

    assert observer.anomalies == [("policy", "blocked")] * 3


def test_an_observer_that_raises_on_an_anomaly_never_stops_the_refusal():
    engine = Engine(RunboundConfig(), observers=[BoomObserver()])

    with pytest.raises(GuardrailTripped):
        engine.refuse(session(), CRITICAL, DENY, reacted="blocked")


def test_a_latched_session_served_again_raises_without_paging_again():
    observer = RecordingObserver()
    engine = Engine(RunboundConfig(), observers=[observer])
    state = session()
    with state.lock:
        state.tripped_by = CRITICAL
        state.tripped_at = 1000.0

    for step in (1, 2, 3):
        with pytest.raises(GuardrailTripped):
            engine.process(state, event(step=step))

    assert observer.anomalies == []  # the on-call was paged when it latched


def test_a_shared_state_that_explodes_never_reaches_the_host(caplog):
    engine = Engine(RunboundConfig(), shared=BoomShared())

    with caplog.at_level(logging.WARNING, logger="runbound"):
        engine.admit_action(session(), "search")  # allowed: the plane is an optimization


def test_local_state_is_the_default_shared_state():
    assert isinstance(Engine(RunboundConfig()).shared, LocalState)


# --- the merged policy ------------------------------------------------------


def test_the_org_policy_is_merged_into_the_local_one():
    shared = RecordingShared(remote_policy={"deny": ["wire_transfer"]}, version=1)
    config = RunboundConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(config, shared=shared)

    policy = engine._policy()

    assert sorted(policy.deny) == ["delete_db", "wire_transfer"]


def test_the_merged_policy_is_cached_until_the_version_changes():
    shared = RecordingShared(remote_policy={"deny": ["wire_transfer"]}, version=1)
    engine = Engine(RunboundConfig(), shared=shared)

    first = engine._policy()
    assert engine._policy() is first

    shared.remote_policy = {"deny": ["refund"]}
    shared.version = 2
    second = engine._policy()

    assert second is not first
    assert second.deny == ["refund"]


def test_a_local_policy_stands_alone_without_a_remote_one():
    engine = Engine(
        RunboundConfig(tool_policy={"deny": ["delete_db"]})
    )

    assert engine._policy().deny == ["delete_db"]


def test_a_remote_policy_that_cannot_be_merged_leaves_the_local_one(caplog):
    shared = RecordingShared(remote_policy={"require_approval": ["refund"]}, version=1)
    config = RunboundConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(config, shared=shared)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        policy = engine._policy()

    assert policy.deny == ["delete_db"]
    assert "policy" in caplog.text.lower()


def test_a_shared_state_whose_policy_explodes_leaves_the_local_one():
    config = RunboundConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(config, shared=BoomShared())

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
    config = RunboundConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(
        config, observers=[Recorder(), observer], shared=shared
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
    config = RunboundConfig(tool_policy={"deny": ["delete_db"]})
    engine = Engine(
        config, observers=[observer], shared=shared
    )

    with pytest.raises(PolicyViolation):
        engine.enforce_policy(session(), call("delete_db"))

    assert observer.anomalies == [("policy", "blocked")]


def test_an_enforced_org_rule_blocks_like_any_other():
    shared = RecordingShared(remote_policy={"deny": ["wire_transfer"]}, version=1)
    engine = Engine(RunboundConfig(), shared=shared)

    with pytest.raises(PolicyViolation):
        engine.enforce_policy(session(), call("wire_transfer"))


def test_a_local_dry_run_policy_still_only_logs():
    policy = ToolPolicy(deny=["wire_transfer"], on_violation="dry_run")
    engine = Engine(RunboundConfig(tool_policy=policy))

    engine.enforce_policy(session(), call("wire_transfer"))
