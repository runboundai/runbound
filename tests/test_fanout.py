"""Tests for the fan-out limits: how deep sessions nest, how many children a
session may open, and how many may be open at once.

The blunder these guard against is a cascade — one agent spawning sub-agents
that spawn sub-agents — which costs money in proportion to a number nobody
ever looked at. They are explicit limits (``None`` by default), so like the
caps they are enforced whatever ``on_anomaly`` says, and unlike a trip they
latch nothing: the shape of the run was wrong, not the end-user.
"""

import threading

import pytest

import runbound
from runbound import api
from runbound.events import Anomaly
from runbound.exceptions import GuardrailTripped


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class RecordingObserver:
    """An observer that just remembers the anomalies it was told about."""

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


def recording() -> RecordingObserver:
    """Watch what an initialized engine reports, without any real I/O."""
    observer = RecordingObserver()
    api._ENGINE.observers = [observer]
    return observer


# --- lineage ----------------------------------------------------------------


def test_nested_sessions_carry_depth_parent_and_child_counts():
    runbound.init()

    with runbound.session("a") as a, runbound.session("b") as b:
        with runbound.session("c") as c:
            assert (a.depth, b.depth, c.depth) == (0, 1, 2)
            assert (a.parent_key, b.parent_key, c.parent_key) == (None, "a", "b")
            assert (a.children, b.children, c.children) == (1, 1, 0)


def test_a_top_level_session_has_no_parent():
    runbound.init()

    with runbound.session("a") as a:
        pass

    assert (a.depth, a.parent_key, a.children) == (0, None, 0)


def test_re_entering_the_same_child_does_not_re_count_it():
    runbound.init()

    with runbound.session("a") as a:
        for _ in range(3):
            with runbound.session("b") as b:
                pass

    assert a.children == 1
    assert (b.depth, b.parent_key) == (1, "a")


def test_a_child_re_entered_under_another_parent_keeps_its_first_lineage():
    """Lineage is where a session was born, not where it is used again."""
    runbound.init()

    with runbound.session("a") as a:
        with runbound.session("b") as b:
            pass
    with runbound.session("z") as z:
        with runbound.session("b"):
            pass

    assert (b.depth, b.parent_key) == (1, "a")
    assert (a.children, z.children) == (1, 0)


def test_the_default_session_is_depth_zero_with_no_lineage():
    runbound.init()

    default = runbound.current_session()

    assert (default.depth, default.children, default.parent_key) == (0, 0, None)


# --- depth ------------------------------------------------------------------


def test_nesting_past_max_session_depth_is_refused_before_the_body_runs():
    runbound.init(max_session_depth=1)
    ran = []

    with runbound.session("a"):
        with runbound.session("b"):
            with pytest.raises(GuardrailTripped) as excinfo:
                with runbound.session("c"):
                    ran.append("body")

    assert ran == []
    anomaly = excinfo.value.anomaly
    assert anomaly.detector == "fanout"
    assert anomaly.severity == "critical"
    assert anomaly.details["rule"] == "depth"
    assert (anomaly.details["count"], anomaly.details["limit"]) == (2, 1)
    assert anomaly.details["key"] == "c"
    assert "'c'" in anomaly.message


def test_the_sessions_inside_the_limit_keep_working():
    runbound.init(max_session_depth=1)

    with runbound.session("a") as a:
        with runbound.session("b") as b:
            assert runbound.current_session() is b
        assert runbound.current_session() is a


def test_a_depth_refusal_latches_nothing():
    runbound.init(max_session_depth=1)

    with runbound.session("a"), runbound.session("b"):
        with pytest.raises(GuardrailTripped):
            with runbound.session("c"):
                pass

    assert runbound.is_tripped("c") is None
    assert runbound.is_tripped("b") is None


def test_depth_is_enforced_even_under_on_anomaly_warn():
    """An explicit limit is a limit: the customer stated a number."""
    runbound.init(max_session_depth=1, on_anomaly="warn")

    with runbound.session("a"), runbound.session("b"):
        with pytest.raises(GuardrailTripped):
            with runbound.session("c"):
                pass


def test_no_depth_limit_means_any_nesting():
    runbound.init()

    with runbound.session("a"), runbound.session("b"), runbound.session("c") as c:
        assert c.depth == 2


# --- children ---------------------------------------------------------------


def test_more_children_than_max_child_sessions_is_refused():
    runbound.init(max_child_sessions=2)

    with runbound.session("a"):
        with runbound.session("b"):
            pass
        with runbound.session("c"):
            pass
        with pytest.raises(GuardrailTripped) as excinfo:
            with runbound.session("d"):
                pass

    anomaly = excinfo.value.anomaly
    assert anomaly.details["rule"] == "children"
    assert (anomaly.details["count"], anomaly.details["limit"]) == (3, 2)
    assert anomaly.details["key"] == "d"


def test_the_same_child_re_entered_never_reaches_the_child_limit():
    runbound.init(max_child_sessions=1)

    with runbound.session("a"):
        for _ in range(5):
            with runbound.session("b"):
                pass


# --- active -----------------------------------------------------------------


def test_active_sessions_counts_entries_and_exits():
    runbound.init()

    assert runbound.active_sessions() == 0
    with runbound.session("a"):
        assert runbound.active_sessions() == 1
        with runbound.session("b"):
            assert runbound.active_sessions() == 2
        assert runbound.active_sessions() == 1
    assert runbound.active_sessions() == 0


def test_reset_puts_the_active_count_back_to_zero():
    """A restarted world starts empty, and the blocks still unwinding from the
    old one cannot drive the count below zero."""
    runbound.init()

    with runbound.session("a"):
        assert runbound.active_sessions() == 1
        runbound.reset()
        assert runbound.active_sessions() == 0

    assert runbound.active_sessions() == 0


def test_active_sessions_is_zero_before_init():
    assert runbound.active_sessions() == 0


def test_the_default_session_is_never_counted_as_active():
    runbound.init()

    assert runbound.current_session() is not None
    assert runbound.active_sessions() == 0


def test_more_concurrent_sessions_than_max_active_sessions_is_refused():
    runbound.init(max_active_sessions=2)

    with runbound.session("a"), runbound.session("b"):
        with pytest.raises(GuardrailTripped) as excinfo:
            with runbound.session("c"):
                pass

    anomaly = excinfo.value.anomaly
    assert anomaly.details["rule"] == "active"
    assert (anomaly.details["count"], anomaly.details["limit"]) == (3, 2)


def test_a_refused_session_is_not_counted_as_active():
    runbound.init(max_active_sessions=2)

    with runbound.session("a"), runbound.session("b"):
        with pytest.raises(GuardrailTripped):
            with runbound.session("c"):
                pass
        assert runbound.active_sessions() == 2
    assert runbound.active_sessions() == 0


def test_active_sessions_is_decremented_when_the_body_raises():
    runbound.init()

    with pytest.raises(RuntimeError):
        with runbound.session("a"):
            raise RuntimeError("the agent blew up")

    assert runbound.active_sessions() == 0


def test_the_active_limit_holds_across_threads():
    runbound.init(max_active_sessions=2)
    holding, release = threading.Event(), threading.Event()
    entered: list[str] = []

    def hold(key: str) -> None:
        with runbound.session(key):
            entered.append(key)
            if len(entered) == 2:
                holding.set()
            release.wait(timeout=5)

    workers = [threading.Thread(target=hold, args=(key,)) for key in ("a", "b")]
    for worker in workers:
        worker.start()
    assert holding.wait(timeout=5)

    try:
        assert runbound.active_sessions() == 2
        with pytest.raises(GuardrailTripped) as excinfo:
            with runbound.session("c"):
                pass
        assert excinfo.value.anomaly.details["rule"] == "active"
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)

    assert runbound.active_sessions() == 0


# --- alerting ---------------------------------------------------------------


def test_a_refusal_alerts_once_however_often_it_is_retried():
    runbound.init(max_session_depth=1)
    alerter = recording()

    with runbound.session("a"), runbound.session("b"):
        for _ in range(3):
            with pytest.raises(GuardrailTripped):
                with runbound.session("c"):
                    pass

    assert [(a.detector, a.details["rule"]) for a in alerter.sent] == [
        ("fanout", "depth")
    ]


# --- fail-open --------------------------------------------------------------


def test_a_malformed_parent_never_stops_the_block(caplog):
    runbound.init(max_session_depth=1)

    class Odd:
        key = "odd"
        depth = "not a number"
        children = 0
        session_id = "odd"

        @property
        def lock(self):
            raise RuntimeError("this state is broken")

    token = api._CURRENT.set(Odd())
    try:
        with runbound.session("child") as child:
            assert child is not None
    finally:
        api._CURRENT.reset(token)


def test_a_broken_engine_never_stops_the_block(monkeypatch):
    runbound.init(max_session_depth=1)

    def boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(api, "_fanout_anomaly", boom)

    with runbound.session("a"), runbound.session("b"), runbound.session("c") as c:
        assert c is not None
