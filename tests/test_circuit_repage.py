"""SDK-6: one page per OUTAGE, not one per process.

``Engine._alerted`` remembers that a provider's circuit has already paged, so a storm of retries pages once. It used to remember
that for the life of the process: a second outage of the same provider, hours later in the same long-lived worker, was never reported
(a demo scorecard re-run found it). The memory is now forgotten when the circuit CLOSES, by a successful call or by the fleet closing it.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import CircuitOpen
from runbound.state import SessionState


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Failure(Exception):
    status_code = 503


class Pages:
    """An observer that keeps every circuit anomaly the engine tells it about."""

    def __init__(self):
        self.anomalies = []

    def on_anomaly(self, session, anomaly, reacted=None):
        if anomaly.detector == "circuit":
            self.anomalies.append(anomaly)

    def on_event(self, session, event):
        pass


@pytest.fixture
def rig():
    clock, pages = Clock(), Pages()
    engine = Engine(GuardrailConfig(circuit_failure_threshold=2, on_provider_failure="open"))
    engine.circuit._now = clock
    engine.observers.append(pages)
    return engine, SessionState("s1"), clock, pages


def fail(engine, state, times=2, provider="openai@dead:1"):
    for _ in range(times):
        engine.record_llm_error(state, "gpt-4o", Failure(), 0.1, provider)


def refuse_once(engine, state, provider="openai@dead:1"):
    with pytest.raises(CircuitOpen):
        engine.admit(state, provider=provider)


def test_open_close_open_again_pages_twice(rig):
    engine, state, clock, pages = rig
    fail(engine, state)
    assert len(pages.anomalies) == 1  # the first outage pages
    clock.advance(31)  # past the cooldown: half-open, the next call is the probe
    engine.record_llm_success("openai@dead:1")  # the provider is back: the circuit closes
    assert engine.circuit.state("openai@dead:1") == "closed"
    fail(engine, state)
    assert len(pages.anomalies) == 2  # the second outage pages again


def test_retries_during_one_outage_still_page_once(rig):
    engine, state, clock, pages = rig
    fail(engine, state, times=2)
    for _ in range(5):
        refuse_once(engine, state)  # the retry storm while it is open
    fail(engine, state, times=3)  # more failures on the same open circuit
    assert len(pages.anomalies) == 1


def test_a_failed_probe_does_not_forget_the_page(rig):
    engine, state, clock, pages = rig
    fail(engine, state)
    clock.advance(31)
    engine.record_llm_error(state, "gpt-4o", Failure(), 0.1, "openai@dead:1")  # the half-open probe fails: still the same outage
    clock.advance(31)
    engine.record_llm_error(state, "gpt-4o", Failure(), 0.1, "openai@dead:1")
    assert len(pages.anomalies) == 1


def test_closing_one_providers_circuit_forgets_only_its_own_page(rig):
    engine, state, clock, pages = rig
    fail(engine, state, provider="openai@a:1")
    fail(engine, state, provider="openai@b:1")
    assert len(pages.anomalies) == 2
    clock.advance(31)
    engine.record_llm_success("openai@a:1")
    fail(engine, state, provider="openai@a:1")  # a's second outage pages
    fail(engine, state, provider="openai@b:1", times=3)  # b is still the same outage: no new page
    assert [a.details["provider"] for a in pages.anomalies] == ["openai@a:1", "openai@b:1", "openai@a:1"]


def test_the_fleet_closing_the_circuit_forgets_the_page_too(rig):
    engine, state, clock, pages = rig
    fail(engine, state)
    engine.circuit.force_close("openai@dead:1")  # what a fleet instruction to close does (shared.py)
    fail(engine, state)
    assert len(pages.anomalies) == 2


def test_a_failing_close_callback_never_breaks_the_close(rig):
    engine, state, clock, pages = rig
    fail(engine, state)

    def broken(provider):
        raise RuntimeError("boom")

    engine.circuit.on_close = broken
    engine.circuit.force_close("openai@dead:1")
    assert engine.circuit.state("openai@dead:1") == "closed"


def test_the_old_behaviour_is_what_the_test_would_catch(rig):
    """Without the close hook the second outage pages nothing: the can-fail for this file."""
    engine, state, clock, pages = rig
    engine.circuit.on_close = None  # the pre-SDK-6 engine
    fail(engine, state)
    clock.advance(31)
    engine.record_llm_success("openai@dead:1")
    fail(engine, state)
    assert len(pages.anomalies) == 1  # one page for two outages: the bug
