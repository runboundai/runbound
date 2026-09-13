"""Tests for T135: explicit anomaly precedence.

Before this, a tie among critical anomalies co-firing on the same event was
decided by the order ``detectors.DEFAULT_DETECTORS`` happened to list them
in — invisible, and an accident of history rather than a stated decision.
``events.PRIORITY`` is now the one place the order is stated, and
``Engine._winner`` resolves ties against it instead of iteration order. The
test that matters most: reversing ``DEFAULT_DETECTORS`` must not change which
anomaly wins.
"""

import logging

import pytest

from runbound.config import GuardrailConfig
from runbound.detectors import DEFAULT_DETECTORS
from runbound.engine import Engine
from runbound.events import PRIORITY, Anomaly, Event
from runbound.state import SessionState

CRITICAL_LOW = Anomaly("budget", "critical", "budget critical", {})
CRITICAL_HIGH = Anomaly("velocity", "critical", "velocity critical (hypothetically)", {})
WARN = Anomaly("spike", "warn", "spike warn", {})


class StubDetector:
    """Returns a fixed anomaly (or None) every time it is checked."""

    def __init__(self, anomaly: Anomaly | None, name: str) -> None:
        self.anomaly = anomaly
        self.name = name

    def check(self, state, event, config):
        return self.anomaly


def event(step: int = 1) -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name="t")


def session() -> SessionState:
    return SessionState("s1")


# --- events.PRIORITY is the single stated order ------------------------------


def test_priority_declares_the_fixed_order_highest_first():
    assert list(PRIORITY.keys()) == [
        "policy",
        "budget",
        "loop",
        "error_storm",
        "steps",
        "events",
        "timeout",
        "spike",
        "velocity",
    ]
    # Strictly increasing: "highest first" is a real ordering, not a set.
    ranks = list(PRIORITY.values())
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


# --- Engine._winner: severity first, then PRIORITY, regardless of list order -


def test_critical_always_beats_warn_whatever_the_priority_table_says():
    engine = Engine(GuardrailConfig(), detectors=[])
    winner = engine._winner([WARN, CRITICAL_HIGH])
    assert winner is CRITICAL_HIGH


@pytest.mark.parametrize("order", [[CRITICAL_LOW, CRITICAL_HIGH], [CRITICAL_HIGH, CRITICAL_LOW]])
def test_among_criticals_the_lower_priority_number_wins_regardless_of_list_order(order):
    """budget (rank 1) outranks velocity (rank 8) whichever came first."""
    engine = Engine(GuardrailConfig(), detectors=[])
    assert engine._winner(order) is CRITICAL_LOW


def test_an_unknown_detector_sorts_last_and_never_beats_a_known_one():
    engine = Engine(GuardrailConfig(), detectors=[])
    known = Anomaly("timeout", "critical", "known", {})
    unknown = Anomaly("customers_own_detector", "critical", "unknown", {})

    assert engine._winner([unknown, known]) is known
    assert engine._winner([known, unknown]) is known


def test_an_unknown_detector_warns_exactly_once_per_engine(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    engine = Engine(GuardrailConfig(), detectors=[])
    unknown = Anomaly("customers_own_detector", "critical", "unknown", {})

    engine._winner([unknown])
    engine._winner([unknown])
    engine._winner([unknown])

    warnings = [r for r in caplog.records if "customers_own_detector" in r.getMessage()]
    assert len(warnings) == 1


def test_an_unranked_detector_never_crashes_the_winner_selection():
    """A customer's own detector, with a name PRIORITY has never heard of,
    must still produce a winner — never raise."""
    engine = Engine(GuardrailConfig(), detectors=[])
    only_unknown = Anomaly("mystery", "critical", "?", {})

    assert engine._winner([only_unknown]) is only_unknown


# --- the real acceptance test: reversing DEFAULT_DETECTORS ------------------


def _fired_anomalies(config: GuardrailConfig, detector_classes: list) -> list[Anomaly]:
    """Record one llm_call that can trip budget, steps, events and timeout at
    once, then ask every detector in ``detector_classes`` what it thinks."""
    state = SessionState("s1")
    state.run_started_at = 0.0
    engine = Engine(config, detectors=[cls() for cls in detector_classes])
    evt = Event(
        kind="llm_call", ts=100_000.0, step=1, cost_usd=1.0, tokens_out=100, model="gpt-4o"
    )
    state.record(evt)
    return engine._detect(state, evt)


def test_reversing_default_detectors_produces_the_same_winner_budget_case():
    config = GuardrailConfig(
        budget_usd=0.01, max_steps=0, max_events=0, max_session_seconds=1.0
    )

    forward = _fired_anomalies(config, DEFAULT_DETECTORS)
    reversed_ = _fired_anomalies(config, list(reversed(DEFAULT_DETECTORS)))

    # Sanity: this event really does trip more than one critical detector.
    assert {a.detector for a in forward} >= {"budget", "steps", "events", "timeout"}

    engine = Engine(config, detectors=[])
    assert engine._winner(forward).detector == "budget"
    assert engine._winner(reversed_).detector == "budget"


def test_reversing_default_detectors_produces_the_same_winner_steps_case():
    """With budget out of the running, steps (rank 4) beats events (rank 5)
    and timeout (rank 6) — again regardless of DEFAULT_DETECTORS' own order."""
    config = GuardrailConfig(max_steps=0, max_events=0, max_session_seconds=1.0)

    forward = _fired_anomalies(config, DEFAULT_DETECTORS)
    reversed_ = _fired_anomalies(config, list(reversed(DEFAULT_DETECTORS)))

    assert {a.detector for a in forward} >= {"steps", "events", "timeout"}

    engine = Engine(config, detectors=[])
    assert engine._winner(forward).detector == "steps"
    assert engine._winner(reversed_).detector == "steps"
