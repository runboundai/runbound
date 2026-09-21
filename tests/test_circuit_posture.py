"""Engine-level tests for retry_after, circuit_posture, slow calls, and
the fleet circuit's own refusal contract (never refuse a worker that only
asked to be told).

These go through the real admission path (``Engine.admit`` / ``record_llm_
success``) rather than poking ``CircuitBreaker`` directly, because the whole
point of the things tested here is how the engine, the config and (for the
fleet cases) the shared state cooperate.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import CircuitOpen
from runbound.plane_types import HelloReply
from runbound.state import SessionState
from test_controls_engine import FakeControlsPlane
from test_shared_state import FakePlane, MovableClock, remote


class FakeClock:
    """A monotonic clock moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Failure(Exception):
    def __init__(self, status_code=None):
        super().__init__(f"status {status_code}")
        if status_code is not None:
            self.status_code = status_code


def open_engine(clock: FakeClock, *, controls: dict | None = None, **config_kwargs) -> Engine:
    """A bare :class:`Engine`, its breaker on ``clock``.

    ``controls``: rate mode, ``circuit_posture`` and the fleet fold are
    real, local ``init()`` knobs too, but a
    :class:`FakeControlsPlane` body (``{"circuit_rate": {...}}``) is how
    this file turns them on instead -- this suite's own coverage of the
    plane-delivered path.
    """
    engine = Engine(
        GuardrailConfig(circuit_failure_threshold=2, **config_kwargs),
        shared=FakeControlsPlane(controls),
    )
    engine.circuit._now = clock
    return engine


# --- retry_after: snapshotted at raise time, read at two different moments --
#
# retry_after must not depend on api._ENGINE
# still pointing at the engine that raised it -- a caught exception whose
# meaning silently changes after a later runbound.init()/reset() is
# surprising. It is snapshotted (the breaker's own clock, and how many
# seconds were left) the instant CircuitOpen is built, and decays from that,
# with no engine lookup at all -- see CircuitOpen.retry_after's docstring.


def test_circuit_open_carries_a_provider_and_a_retry_after():
    clock = FakeClock()
    engine = open_engine(clock, on_provider_failure="open")
    state = SessionState("s1")
    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    with pytest.raises(CircuitOpen) as excinfo:
        engine.admit(state, provider="openai")
    first = excinfo.value.retry_after
    clock.advance(10.0)
    second = excinfo.value.retry_after

    assert first == pytest.approx(30.0)
    assert second == pytest.approx(20.0)
    assert first != second  # the mutation this guards against: a frozen number


def test_retry_after_needs_no_live_engine_at_all():
    """Unlike the lazy form this replaced, retry_after never consults
    api._ENGINE -- it works identically whether or not one is attached,
    before init() included."""
    clock = FakeClock()
    engine = open_engine(clock, on_provider_failure="open")
    state = SessionState("s1")
    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")

    with pytest.raises(CircuitOpen) as excinfo:
        engine.admit(state, provider="openai")

    assert excinfo.value.retry_after == pytest.approx(30.0)


def test_retry_after_survives_a_reinit_that_swaps_the_engine():
    """The scenario the second review round asked about directly: a
    runbound.init()/reset() that installs a *different* engine (a fresh
    breaker, or none at all for this provider) after the exception was
    raised must not change what an already-caught CircuitOpen reports."""
    from runbound import api

    clock = FakeClock()
    engine = open_engine(clock, on_provider_failure="open")
    state = SessionState("s1")
    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    with api._LOCK:
        api._ENGINE = engine
    try:
        with pytest.raises(CircuitOpen) as excinfo:
            engine.admit(state, provider="openai")

        # A different engine, with its own real clock and no history for
        # "openai" at all -- what a plain runbound.init()/reset() installs.
        with api._LOCK:
            api._ENGINE = Engine(GuardrailConfig(on_provider_failure="open"))

        clock.advance(5.0)
        still_correct = excinfo.value.retry_after
    finally:
        with api._LOCK:
            api._ENGINE = None

    assert still_correct == pytest.approx(25.0)  # the ORIGINAL breaker's cooldown


def test_retry_after_is_none_for_a_bare_circuit_open_built_by_hand():
    """No snapshot at all (a test double, or hand-built anomaly) -- None,
    never a crash."""
    from runbound.events import Anomaly

    exc = CircuitOpen(Anomaly("circuit", "critical", "open", {"provider": "openai"}))

    assert exc.retry_after is None


# --- circuit_posture: half-open narrows, closing lifts only its own source --


def test_half_open_sets_restricted_when_circuit_posture_is_on():
    clock = FakeClock()
    engine = open_engine(
        clock, on_provider_failure="notify", controls={"circuit_posture": True}
    )
    state = SessionState("s1")
    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    assert engine.circuit.state("openai") == "open"
    assert engine.process_posture() is None

    clock.advance(31.0)  # cooldown elapses: half-open
    engine.admit(state, provider="openai")  # the real path notices it

    posture = engine.process_posture()
    assert posture is not None
    assert posture.name == "restricted"
    assert posture.source == "circuit"


def test_circuit_posture_off_by_default_never_narrows():
    clock = FakeClock()
    engine = open_engine(clock, on_provider_failure="notify")  # circuit_rate not enabled: no posture gate
    state = SessionState("s1")
    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    clock.advance(31.0)

    engine.admit(state, provider="openai")

    assert engine.process_posture() is None


def test_closing_the_circuit_lifts_only_its_own_posture_source():
    """A manual posture set separately must survive the circuit's own
    narrowing being lifted -- and the circuit's own narrowing must actually
    lift when the provider recovers."""
    clock = FakeClock()
    engine = open_engine(
        clock, on_provider_failure="notify", controls={"circuit_posture": True}
    )
    state = SessionState("s1")

    engine.enter_safe_mode("an operator's own call", "restricted", source="manual")

    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    clock.advance(31.0)
    engine.admit(state, provider="openai")
    assert engine.process_posture().source in ("manual", "circuit")

    engine.record_llm_success("openai")  # the probe succeeded: the circuit closes

    posture = engine.process_posture()
    assert posture is not None
    assert posture.source == "manual"  # the circuit's own entry is gone
    assert posture.name == "restricted"  # the manual one is untouched


def test_a_failed_probe_does_not_flap_the_posture_back_open():
    """Re-opening from a failed probe reads 'open', not 'half_open' -- the
    restriction set at half-open simply stays, it is never re-entered or
    lifted on 'open'."""
    clock = FakeClock()
    engine = open_engine(
        clock, on_provider_failure="notify", controls={"circuit_posture": True}
    )
    state = SessionState("s1")
    for _ in range(2):
        engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")
    clock.advance(31.0)
    engine.admit(state, provider="openai")  # half-open noticed, posture set
    assert engine.process_posture() is not None

    engine.record_llm_error(state, "gpt-4o", Failure(503), 0.1, "openai")  # the probe fails
    assert engine.circuit.state("openai") == "open"

    assert engine.process_posture() is not None  # still restricted
    assert engine.process_posture().source == "circuit"


# --- rate mode's slow calls, wired end to end through record_llm_success ----


def test_a_rate_mode_circuit_opens_on_slow_successes_alone():
    clock = FakeClock()
    engine = open_engine(
        clock,
        on_provider_failure="open",
        controls={
            "circuit_rate": {"min_calls": 3, "slow_call_seconds": 2.0, "slow_rate": 0.5}
        },
    )

    engine.record_llm_success("openai", duration_s=5.0)
    engine.record_llm_success("openai", duration_s=5.0)
    engine.record_llm_success("openai", duration_s=5.0)

    assert engine.circuit.state("openai") == "open"
    assert engine.circuit_allows("openai") is False


def test_count_mode_never_reads_duration_s_at_all():
    clock = FakeClock()
    engine = open_engine(clock, on_provider_failure="open")  # circuit_rate not enabled: count mode

    for _ in range(50):
        engine.record_llm_success("openai", duration_s=999.0)

    assert engine.circuit.state("openai") == "closed"
    assert engine.circuit_allows("openai") is True


# --- the fleet circuit: refuse only a worker whose own config would refuse --


def _joined_fleet_engine(on_provider_failure: str) -> Engine:
    """An engine that just heard, on its very first heartbeat, that the
    fleet's ``openai`` circuit is already open -- through the real
    ``RemoteState`` (see ``test_shared_state.py``), not a stand-in.

    ``circuit_fleet`` is a real, local ``init()`` knob too; the plane's
    own Controls turn fleet folding on for this worker here instead,
    exactly ``test_shared_state.py``'s own ``enable_circuit_fleet`` helper.
    """
    plane = FakePlane()
    engine = Engine(GuardrailConfig(on_provider_failure=on_provider_failure))
    shared = remote(plane, MovableClock())
    shared.breaker = engine.circuit  # what shared.start(engine) would attach, minus the thread
    engine.shared = shared
    plane.controls_body = {"version": 1, "controls": {"circuit_fleet": True}}
    shared.apply_hello(HelloReply(controls_version=1))
    shared.apply_hello(HelloReply(circuits={"openai": {"state": "open", "until_s": 30}}))
    return engine


def test_a_worker_joining_an_open_fleet_circuit_refuses_under_open():
    """on_provider_failure='open': the fleet's instruction is honoured, and
    this worker's first call to that provider is refused -- with zero local
    failures ever recorded on this worker."""
    engine = _joined_fleet_engine("open")
    state = SessionState("s1")

    assert engine.circuit.state("openai") == "open"
    with pytest.raises(CircuitOpen):
        engine.admit(state, provider="openai")


def test_the_same_fleet_circuit_only_notifies_a_notify_worker():
    """on_provider_failure='notify' (the default): the same fleet instruction
    still lands locally -- the breaker really is 'open' -- but this worker's
    own configuration only asked to be told, so its calls are never refused."""
    engine = _joined_fleet_engine("notify")
    state = SessionState("s1")

    assert engine.circuit.state("openai") == "open"
    engine.admit(state, provider="openai")  # does not raise
    assert engine.circuit_allows("openai") is True
