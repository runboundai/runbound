"""Tests for the latch: once a session's policy says ``block_and_latch``, it stays stopped.

The bug this closes: a chatbot that catches ``PolicyViolation`` and keeps
serving would let a "blocked" user carry on. A latched session refuses *every*
later tool call with the stored anomaly, whichever tool it is, until the
session is reset; other keys are untouched.
"""

import pytest

import runbound
from runbound import api
from runbound.config import RunboundConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Decision, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolCall, ToolPolicy
from runbound.state import SessionState

CRITICAL = Anomaly("policy", "critical", "out of bounds", {"tool": "wire_money", "rule": "deny"})
DENY = Decision(verdict="deny", kind="action", boundary="policy", detector="policy")


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class RecordingObserver:
    """Remembers every anomaly it was told about."""

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []
        self.reactions: list[str] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)
        self.reactions.append(reacted)


def tool_event(step: int, name: str = "search") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name=name, args_hash="h1")


def call(name: str = "wire_money") -> ToolCall:
    return ToolCall(name, (), {}, None, {})


def latching_engine(observers=None) -> Engine:
    policy = ToolPolicy(deny=["wire_money"], on_violation="block_and_latch")
    return Engine(RunboundConfig(tool_policy=policy), observers=observers or [])


def tools_with_a_latching_deny():
    runbound.init(tool_policy={"deny": ["wire_money"], "on_violation": "block_and_latch"})
    ran: list[str] = []

    @runbound.tool
    def wire_money(amount):
        ran.append("wire_money")
        return "ran"

    @runbound.tool
    def search(query):
        ran.append("search")
        return "ran"

    return wire_money, search, ran


# --- a blocked user stays blocked --------------------------------------------


def test_a_latched_user_is_refused_on_every_later_tool_call():
    wire_money, search, ran = tools_with_a_latching_deny()

    with runbound.session("user:abuser") as state:
        with pytest.raises(PolicyViolation):
            wire_money(1)
        trips = 0
        for _ in range(10):
            try:
                search("hats")
            except GuardrailTripped:
                trips += 1

    assert trips == 10
    assert ran == []  # not a single body ran after the breach
    assert state.tripped_by.detector == "policy"


def test_the_latched_refusal_carries_the_original_anomaly():
    wire_money, search, _ = tools_with_a_latching_deny()

    with runbound.session("user:abuser") as state:
        with pytest.raises(PolicyViolation) as first:
            wire_money(1)
        with pytest.raises(GuardrailTripped) as later:
            search("x")

    assert later.value.anomaly is state.tripped_by
    assert later.value.anomaly.anomaly_id == first.value.anomaly.anomaly_id
    assert later.value.anomaly.details["rule"] == "deny"


def test_the_latch_is_per_key_and_other_keys_keep_working():
    wire_money, search, ran = tools_with_a_latching_deny()

    with runbound.session("user:abuser"):
        with pytest.raises(PolicyViolation):
            wire_money(1)
    with runbound.session("user:innocent") as other:
        assert search("hats") == "ran"

    assert other.tripped_by is None
    assert ran == ["search"]


def test_a_latched_key_stays_latched_when_its_session_is_entered_again():
    wire_money, search, _ = tools_with_a_latching_deny()

    with runbound.session("user:abuser"):
        with pytest.raises(PolicyViolation):
            wire_money(1)
    with runbound.session("user:abuser"):  # entering is free; the first tool call is refused
        with pytest.raises(GuardrailTripped):
            search("x")


def test_a_latched_session_still_counts_its_attempts():
    wire_money, search, _ = tools_with_a_latching_deny()

    with runbound.session("user:abuser"):
        with pytest.raises(PolicyViolation):
            wire_money(1)
        for _ in range(3):
            with pytest.raises(GuardrailTripped):
                search("x")

    assert runbound.tool_calls("user:abuser") == {"wire_money": 1, "search": 3}


def test_a_blocked_attempt_without_latch_leaves_the_session_running():
    runbound.init(tool_policy={"deny": ["wire_money"], "on_violation": "block"})

    @runbound.tool
    def wire_money(amount):
        return "ran"

    @runbound.tool
    def search(query):
        return "ran"

    with runbound.session("user:a") as state:
        with pytest.raises(PolicyViolation):
            wire_money(1)
        assert search("x") == "ran"

    assert state.tripped_by is None


# --- engine level ---------------------------------------------------------------


def test_block_and_latch_stores_the_anomaly_on_the_session():
    engine = latching_engine()
    session = SessionState("s1")

    with pytest.raises(PolicyViolation) as caught:
        engine.enforce_policy(session, call())

    assert session.tripped_by is caught.value.anomaly
    assert session.tripped_at is not None


def test_a_latched_session_still_records_its_events():
    engine = Engine(RunboundConfig())
    session = SessionState("s1")
    session.tripped_by = CRITICAL

    for step in (1, 2):
        with pytest.raises(GuardrailTripped):
            engine.process(session, tool_event(step))

    assert session.event_count == 2
    assert session.tool_calls == {"search": 2}


def test_re_application_never_re_alerts():
    observer = RecordingObserver()
    engine = latching_engine([observer])
    session = SessionState("s1")

    with pytest.raises(PolicyViolation):
        engine.enforce_policy(session, call())
    for step in range(1, 12):
        with pytest.raises(GuardrailTripped):
            engine.process(session, tool_event(step))

    # One page for the refusal that latched; the retries are served the stored
    # anomaly without telling the observers again.
    assert [a.detector for a in observer.sent] == ["policy"]
    assert observer.reactions == ["blocked"]


def test_the_first_anomaly_is_the_one_that_is_kept():
    """The latch records why the session was stopped, not what came later."""
    engine = Engine(RunboundConfig())
    session = SessionState("s1")
    later = Anomaly("policy", "critical", "second", {})

    with pytest.raises(GuardrailTripped):
        engine.refuse(session, CRITICAL, DENY, latch=True)
    with pytest.raises(GuardrailTripped):
        engine.refuse(session, later, DENY, latch=True)

    assert session.tripped_by.message == CRITICAL.message


def test_a_warn_severity_notice_never_latches():
    """A dry run reports and lets the call through: nothing latches."""
    policy = ToolPolicy(deny=["wire_money"], on_violation="dry_run")
    engine = Engine(RunboundConfig(tool_policy=policy))
    session = SessionState("s1")

    engine.enforce_policy(session, call())

    assert session.tripped_by is None


# --- reset ------------------------------------------------------------------


def test_reset_clears_the_default_sessions_latch():
    wire_money, search, _ = tools_with_a_latching_deny()
    with pytest.raises(PolicyViolation):
        wire_money(1)
    with pytest.raises(GuardrailTripped):
        search("x")

    runbound.reset()

    assert runbound.current_session().tripped_by is None
    assert search("x") == "ran"  # and the guard works again


def test_reset_forgets_every_keyed_latch():
    wire_money, search, _ = tools_with_a_latching_deny()
    with runbound.session("user:abuser"):
        with pytest.raises(PolicyViolation):
            wire_money(1)

    runbound.reset()

    with runbound.session("user:abuser") as state:
        assert search("x") == "ran"
    assert state.tripped_by is None


# --- state ------------------------------------------------------------------


def test_tripped_by_defaults_to_none():
    assert SessionState("s").tripped_by is None
