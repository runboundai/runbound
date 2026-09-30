"""Tests for the Engine — the piece that turns events into consequences.

Fakes, never mocks: a detector is anything with ``check(state, event, config)``
and an observer anything with ``on_event(session, event)`` and
``on_anomaly(session, anomaly, reacted)``, so the doubles here are the real
interfaces. There is no alerter interface any more: delivery is not the
engine's concern, and an observer — telemetry export among them — is how
the engine reports an anomaly.
"""

import logging

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

WARN = Anomaly("velocity", "warn", "going fast", {})
CRITICAL = Anomaly("budget", "critical", "out of money", {})


class StubDetector:
    """Returns a fixed anomaly (or None) every time it is checked."""

    def __init__(self, anomaly: Anomaly | None, name: str = "stub") -> None:
        self.anomaly = anomaly
        self.name = name
        self.checked = 0

    def check(self, state, event, config):
        self.checked += 1
        return self.anomaly


class BoomDetector:
    """A detector with a bug in it."""

    name = "boom"

    def __init__(self) -> None:
        self.checked = 0

    def check(self, state, event, config):
        self.checked += 1
        raise RuntimeError("detector is broken")


class RecordingObserver:
    """Records the anomalies it is told about, into a shared call log."""

    def __init__(self, log: list, label: str = "alert") -> None:
        self.log = log
        self.label = label

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.log.append((self.label, anomaly))


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash)


def session(loop_window: int = 20) -> SessionState:
    return SessionState("s1", loop_window=loop_window)


# --- record-then-detect ordering -------------------------------------------


def test_process_records_before_detecting_so_loop_counts_current_event():
    config = GuardrailConfig(loop_threshold=3, on_anomaly="raise", on_loop="break")
    engine = Engine(config)
    state = session()

    engine.process(state, tool_event(1))
    engine.process(state, tool_event(2))

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(state, tool_event(3))

    assert excinfo.value.anomaly.detector == "loop"
    assert excinfo.value.anomaly.details["count"] == 3
    assert state.step_count == 3


def test_process_updates_session_counters_even_when_nothing_trips():
    engine = Engine(GuardrailConfig())
    state = session()

    engine.process(state, Event(kind="llm_call", ts=1.0, step=1, tokens_in=10, tokens_out=5, cost_usd=0.25))

    assert (state.step_count, state.total_tokens) == (1, 15)
    assert state.total_cost_usd == pytest.approx(0.25)


# --- fail-open --------------------------------------------------------------


def test_buggy_detector_is_skipped_logged_and_others_still_run(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    good = StubDetector(CRITICAL)
    boom = BoomDetector()
    log: list = []
    engine = Engine(GuardrailConfig(), detectors=[boom, good], observers=[RecordingObserver(log)])

    engine.process(session(), tool_event(1))  # host survives

    assert boom.checked == 1
    assert good.checked == 1
    assert log == [("alert", CRITICAL)]
    assert "boom" in caplog.text


def test_callback_exception_is_swallowed_and_logged(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    seen: list[Anomaly] = []

    def callback(anomaly):
        seen.append(anomaly)
        raise RuntimeError("user callback is broken")

    config = GuardrailConfig(on_anomaly="callback", callback=callback)
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])

    engine.process(session(), tool_event(1))

    assert seen == [CRITICAL]
    assert "callback" in caplog.text


# --- reactions --------------------------------------------------------------


def test_warn_mode_logs_the_anomaly_message(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    engine = Engine(GuardrailConfig(on_anomaly="warn"), detectors=[StubDetector(CRITICAL)])

    engine.process(session(), tool_event(1))

    assert CRITICAL.message in caplog.text


def test_callback_mode_calls_the_user_callback():
    seen: list[Anomaly] = []
    config = GuardrailConfig(on_anomaly="callback", callback=seen.append)
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])

    engine.process(session(), tool_event(1))

    assert seen == [CRITICAL]


def test_raise_mode_reports_the_critical_anomaly_when_warn_also_fired():
    config = GuardrailConfig(on_anomaly="raise")
    engine = Engine(
        config,
        detectors=[StubDetector(WARN, "w"), StubDetector(CRITICAL, "c")],
    )

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.process(session(), tool_event(1))

    assert excinfo.value.anomaly is CRITICAL
    assert str(excinfo.value) == CRITICAL.message


def test_observers_hear_every_anomaly_before_the_exception_is_raised():
    """The removal of the alerter send loop must not reorder this: every
    observer still hears about every anomaly, in the same order, before the
    exception that stops the run leaves ``process()``."""
    log: list = []
    config = GuardrailConfig(on_anomaly="raise")
    engine = Engine(
        config,
        detectors=[StubDetector(WARN, "w"), StubDetector(CRITICAL, "c")],
        observers=[RecordingObserver(log, "one"), RecordingObserver(log, "two")],
    )

    with pytest.raises(GuardrailTripped):
        engine.process(session(), tool_event(1))

    assert log == [
        ("one", WARN),
        ("two", WARN),
        ("one", CRITICAL),
        ("two", CRITICAL),
    ]


def test_no_anomaly_means_no_notification_and_no_reaction():
    log: list = []
    config = GuardrailConfig(on_anomaly="raise")
    engine = Engine(config, detectors=[StubDetector(None)], observers=[RecordingObserver(log)])

    engine.process(session(), tool_event(1))

    assert log == []


# --- construction -----------------------------------------------------------


def test_default_detectors_are_fresh_instances_per_engine():
    first = Engine(GuardrailConfig())
    second = Engine(GuardrailConfig())

    assert [type(d) for d in first.detectors] == [type(d) for d in second.detectors]
    assert len(first.detectors) == 8  # includes EventsDetector
    assert all(a is not b for a, b in zip(first.detectors, second.detectors))




# --- warn severity never stops -----------------------------------------------


@pytest.mark.parametrize("mode", ["raise", "callback", "warn"])
def test_a_warn_severity_anomaly_never_stops_whatever_on_anomaly_says(mode, caplog):
    """``on_anomaly`` is the reaction to a *critical* anomaly (reactions.md).

    ``velocity`` is warn-severity and "never stops anything": under
    ``"raise"`` it must not raise, under ``"callback"`` it must not be handed
    to the kill switch, and in no mode may it latch the session --
    regression coverage for a bug where a velocity warning raised
    ``GuardrailTripped`` under ``"raise"``.
    """
    caplog.set_level(logging.WARNING, logger="runbound")
    seen: list[Anomaly] = []
    extra = {"callback": seen.append} if mode == "callback" else {}
    engine = Engine(GuardrailConfig(on_anomaly=mode, **extra), detectors=[StubDetector(WARN)])
    state = session()

    engine.process(state, tool_event(1))  # must not raise

    assert state.tripped_by is None
    assert seen == []
    assert WARN.message in caplog.text
