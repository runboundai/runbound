"""The graded loop policy: log, then alert, then contain and recover.

``on_loop`` ``None`` (the default) or ``"graded"``: a repeated call is answered
by how many times it has repeated. With ``loop_threshold`` 3, ``loop_alert_threshold``
6 and ``loop_contain_threshold`` 9 (the defaults' 1x, 2x and 3x): the third
repeat is a line in the log, the sixth pages a person, the ninth hands the loop
to the spike ladder, which restricts the session, spends its allowance on each
further repeat and closes it. 3 is worth a line, 6 is worth a person, 9 is a
runaway.
"""

import pytest

import runbound
from runbound import api, local_events
from runbound.config import GuardrailConfig, LOOP_ALERT_MULTIPLE, LOOP_CONTAIN_MULTIPLE
from runbound.detectors import LoopDetector
from runbound.engine import Engine
from runbound.events import Event
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState

KEY = "call:1"


class Observer:
    def __init__(self) -> None:
        self.seen: list[tuple] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.seen.append((anomaly, reacted))

    def rungs(self) -> list[tuple]:
        return [(a.severity, a.details.get("rung"), a.details.get("action"), reacted) for a, reacted in self.seen]


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def tool_event(step: int, args_hash: str = "h1", tool: str = "issue_refund") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name=tool, args_hash=args_hash)


def llm_event(step: int, duration: float = 2.0, tokens_out: int = 100) -> Event:
    return Event(kind="llm_call", ts=float(step), step=step, tokens_out=tokens_out, duration_s=duration, model="gpt-4o")


def build(key: str | None = KEY, **overrides):
    fields = dict(loop_threshold=3, on_anomaly="raise", on_spike="limit", spike_limit_calls=2,
                  spike_cooldown_seconds=30.0)
    fields.update(overrides)
    config = GuardrailConfig(**fields)
    config.validate()
    observer = Observer()
    engine = Engine(config, observers=[observer])
    state = SessionState("s1", key=key, loop_window=config.loop_window)
    return engine, state, observer


def repeat(engine, state, n, start=1, args_hash="h1"):
    for step in range(start, start + n):
        engine.process(state, tool_event(step, args_hash))


# --- the constants and knobs -------------------------------------------------------


def test_the_rungs_are_one_two_and_three_times_the_loop_threshold():
    config = GuardrailConfig()

    assert (LOOP_ALERT_MULTIPLE, LOOP_CONTAIN_MULTIPLE) == (2, 3)
    assert (config.loop_threshold, config.alert_loop_threshold(), config.contain_loop_threshold()) == (3, 6, 9)
    assert GuardrailConfig(loop_threshold=5, loop_window=30).contain_loop_threshold() == 15


def test_explicit_thresholds_win():
    config = GuardrailConfig(loop_alert_threshold=4, loop_contain_threshold=7)
    config.validate()

    assert (config.alert_loop_threshold(), config.contain_loop_threshold()) == (4, 7)


def test_derived_rungs_never_pass_the_window():
    """A customer with a big threshold and a small window has a valid config
    today; the default rungs clamp to the window rather than break it."""
    config = GuardrailConfig(loop_threshold=10, loop_window=12)
    config.validate()

    assert (config.alert_loop_threshold(), config.contain_loop_threshold()) == (12, 12)


@pytest.mark.parametrize("kwargs, message", [
    ({"loop_alert_threshold": 3}, "loop_alert_threshold"),  # not above loop_threshold
    ({"loop_alert_threshold": 2}, "loop_alert_threshold"),
    ({"loop_alert_threshold": 6, "loop_contain_threshold": 6}, "loop_contain_threshold"),
    ({"loop_contain_threshold": 3}, "loop_contain_threshold"),
    ({"loop_contain_threshold": 21}, "loop_window"),  # a window cannot hold more repeats than its size
    ({"loop_alert_threshold": 25}, "loop_window"),
    ({"loop_alert_threshold": 4.5}, "loop_alert_threshold"),
    ({"loop_contain_threshold": True}, "loop_contain_threshold"),
])
def test_bad_thresholds_are_refused_at_init(kwargs, message):
    with pytest.raises(ValueError, match=message):
        GuardrailConfig(**kwargs).validate()


def test_none_and_graded_both_select_the_graded_policy():
    assert GuardrailConfig().is_graded_loop() and GuardrailConfig(on_loop="graded").is_graded_loop()
    assert not any(GuardrailConfig(on_loop=v).is_graded_loop() for v in ("break", "throttle", "escalate"))


# --- rung 1: a line in the log ---------------------------------------------------------


def test_nothing_before_the_threshold_and_one_log_at_it():
    engine, state, observer = build()

    repeat(engine, state, 2)
    assert observer.seen == []
    repeat(engine, state, 1, start=3)

    assert observer.rungs() == [("warn", "log", None, "notify")]
    anomaly = observer.seen[0][0]
    assert anomaly.detector == "loop" and anomaly.details["repeats"] == 3
    assert anomaly.details["policy"] == "graded" and "issue_refund" in anomaly.message


def test_the_fourth_and_fifth_repeats_say_nothing_new():
    engine, state, observer = build()

    repeat(engine, state, 5)

    assert observer.rungs() == [("warn", "log", None, "notify")]


def test_the_log_does_not_stop_anything():
    engine, state, _observer = build()

    repeat(engine, state, 3)  # no GuardrailTripped

    assert state.tripped_by is None and state.posture is None and state.spike_level == 0


# --- rung 2: a person is paged -----------------------------------------------------


def test_the_sixth_repeat_is_a_critical_notify_and_nothing_is_stopped():
    engine, state, observer = build()

    repeat(engine, state, 6)

    assert observer.rungs() == [("warn", "log", None, "notify"), ("critical", "alert", None, "notify")]
    assert state.tripped_by is None and state.posture is None and state.spike_level == 0


def test_the_seventh_and_eighth_repeats_say_nothing_new():
    engine, state, observer = build()

    repeat(engine, state, 8)

    assert [r[1] for r in observer.rungs()] == ["log", "alert"]


# --- rung 3: the spike ladder --------------------------------------------------------


def test_the_ninth_repeat_limits_the_session_to_restricted():
    engine, state, observer = build()

    repeat(engine, state, 9)

    assert observer.rungs()[-1] == ("warn", "contain", "limit", "warn")
    limit = observer.seen[-1][0]
    assert limit.detector == "loop" and limit.details["level"] == 2 and limit.details["allowance"] == 2
    assert state.spike_level == 2 and state.posture.name == "restricted" and state.posture.source == "ladder"
    assert state.spike_allowance == 2
    assert state.tripped_by is None  # limited is not stopped


def test_each_further_repeat_spends_the_allowance_and_the_last_closes_the_session():
    engine, state, observer = build()
    repeat(engine, state, 9)

    engine.process(state, tool_event(10))  # 2 -> 1
    assert state.spike_allowance == 1 and state.spike_level == 2
    with pytest.raises(GuardrailTripped) as closed:
        engine.process(state, tool_event(11))  # 1 -> 0: closed

    anomaly = closed.value.anomaly
    assert (anomaly.detector, anomaly.severity) == ("loop", "critical")
    assert anomaly.details["action"] == "rollover" and anomaly.details["level"] == 3
    assert anomaly.details["strikes"] == 1 and anomaly.details["cooldown_seconds"] == 30.0
    assert anomaly.details["max_strikes"] == 3
    assert state.spike_level == 3 and state.posture.name == "stopped"
    assert state.tripped_by is anomaly
    assert observer.rungs()[-1] == ("critical", "contain", "rollover", "raise")


def test_a_closed_session_stays_closed_and_reapplies_its_stop():
    engine, state, _ = build()
    repeat(engine, state, 10)
    with pytest.raises(GuardrailTripped):
        engine.process(state, tool_event(11))

    with pytest.raises(GuardrailTripped):
        engine.process(state, tool_event(12, "other"))  # a latched session refuses whatever comes next


def test_the_ladder_history_records_the_loop_as_the_reason():
    engine, state, _ = build()
    repeat(engine, state, 9)

    reasons = [reason for *_rest, reason in state.ladder_history]
    assert reasons == ["loop"]
    assert state.spike_trigger["metric"] == "loop_repeats" and state.spike_trigger["value"] == 9.0


def test_a_loop_keeps_its_own_detector_name_all_the_way_up():
    engine, state, observer = build()
    repeat(engine, state, 9)

    assert {a.detector for a, _ in observer.seen} == {"loop"}


def test_only_the_ladders_close_stops_the_session_never_the_rungs_before_it():
    engine, state, _ = build()
    for count in range(1, 11):
        engine.process(state, tool_event(count))
        assert state.tripped_by is None


# --- the tool is refused before its body runs -------------------------------------


def test_a_restricted_loop_refuses_the_financial_tool_before_it_runs_and_lets_a_write_through():
    """The call that carries the loop to the contain rung is itself refused
    (the session is restricted before its body is admitted): only the refunds
    before it ran. A write is still allowed."""
    runbound.init(on_anomaly="raise", loop_threshold=3, on_spike="limit", spike_limit_calls=2)
    ran = []

    @runbound.tool(effects={"financial"})
    def issue_refund(amount):
        ran.append(("refund", amount))
        return "refunded"

    @runbound.tool(effects={"write"})
    def book(slot):
        ran.append(("book", slot))
        return "booked"

    with runbound.session(KEY):
        for _ in range(8):
            issue_refund(25)
        with pytest.raises(runbound.PolicyViolation):
            issue_refund(25)  # the ninth: restricted, refused before it runs
        with pytest.raises(runbound.PolicyViolation):
            issue_refund(25)
        book("10am")

    assert [what for what, _ in ran] == ["refund"] * 8 + ["book"]  # neither refused refund ran


# --- re-arming ----------------------------------------------------------------------


def test_a_loop_that_ends_and_starts_again_logs_again():
    engine, state, observer = build()
    repeat(engine, state, 3)
    for step in range(4, 4 + 20):  # the window rolls over with other work
        engine.process(state, tool_event(step, f"other-{step}", tool="lookup"))
    repeat(engine, state, 3, start=30)

    logs = [r for r in observer.rungs() if r[1] == "log"]
    assert len(logs) == 2
    assert [a.details["episode"] for a, _ in observer.seen] == [1, 2]


def test_two_different_calls_looping_each_have_their_own_rungs():
    engine, state, observer = build()

    for step in range(1, 4):
        engine.process(state, tool_event(step * 2 - 1, "a", tool="search"))
        engine.process(state, tool_event(step * 2, "b", tool="lookup"))

    assert [(a.details["tool_name"], a.details["rung"]) for a, _ in observer.seen] == [
        ("search", "log"), ("lookup", "log")]


# --- a normal model call does not heal a loop-limited session ---------------------


def _warm(engine, state, n=4):
    for i in range(n):
        engine.process(state, llm_event(100 + i))


def test_normal_model_calls_between_the_repeats_do_not_heal_a_loop_limited_session():
    engine, state, _ = build(spike_warmup_calls=4)
    _warm(engine, state)
    repeat(engine, state, 9)
    assert state.spike_level == 2 and state.loop_active

    engine.process(state, llm_event(200))  # an ordinary model call, in the middle of the loop

    assert state.spike_level == 2 and state.posture.name == "restricted"
    assert state.spike_allowance == 2  # and the allowance was not forgotten by a heal


def test_once_the_loop_is_over_a_normal_call_heals_as_it_always_did():
    engine, state, _ = build(spike_warmup_calls=4)
    _warm(engine, state)
    repeat(engine, state, 9)
    for step in range(30, 50):  # the loop ends: the window fills with other calls
        engine.process(state, tool_event(step, f"other-{step}", tool="lookup"))
    assert not state.loop_active

    engine.process(state, llm_event(300))

    assert state.spike_level == 1 and state.posture is None  # healed back to watching, unrestricted


# --- when the ladder cannot act ---------------------------------------------------------


@pytest.mark.parametrize("overrides, why", [
    ({"on_spike": "notify"}, "spike ladder is off"),
    ({"on_anomaly": "warn"}, "on_anomaly"),
    ({"spike_detection": False}, "spike ladder is off"),
])
def test_when_the_ladder_cannot_act_the_ninth_repeat_is_a_critical_notice_that_says_so(overrides, why):
    engine, state, observer = build(**overrides)

    repeat(engine, state, 12)  # no GuardrailTripped, however long the loop goes

    last = observer.seen[-1][0]
    assert (last.severity, last.details["rung"], last.details["contained"]) == ("critical", "contain", False)
    assert why in last.details["why_not"]
    assert [r[1] for r in observer.rungs()] == ["log", "alert", "contain"]  # once
    assert state.posture is None and state.spike_level == 0 and state.tripped_by is None


def test_an_unkeyed_session_cannot_be_closed_so_it_is_only_ever_told():
    engine, state, observer = build(key=None)

    repeat(engine, state, 12)

    assert observer.seen[-1][0].details["contained"] is False
    assert "default session" in observer.seen[-1][0].details["why_not"]


def test_a_controls_notify_for_loops_keeps_the_ladder_out_of_it():
    config = GuardrailConfig(loop_threshold=3, on_anomaly="raise", on_spike="limit")
    config.loop_contain_allowed = False  # what Engine._effective_config sets from the plane's Controls
    state = SessionState("s1", key=KEY)
    detector = LoopDetector()
    last = None
    for step in range(1, 10):
        event = tool_event(step)
        state.record(event)
        last = detector.check(state, event, config) or last

    assert last.details["contained"] is False and "Controls" in last.details["why_not"]
    assert state.spike_level == 0


# --- the other shapes ----------------------------------------------------------------------


def cycle_events(cycles, start=1):
    step = start
    for cycle in range(cycles):
        for tool in ("edit", "test"):
            yield tool_event(step, f"{tool}-{cycle}", tool=tool)  # different arguments every cycle
            step += 1


def test_a_sequence_loop_climbs_the_same_rungs_by_cycles():
    engine, state, observer = build()

    for event in cycle_events(9):
        engine.process(state, event)

    assert [(a.details["shape"], a.details["rung"], a.details["repeats"]) for a, _ in observer.seen] == [
        ("sequence", "log", 3), ("sequence", "alert", 6), ("sequence", "contain", 9)]
    assert state.spike_level == 2


def test_a_retrying_tool_climbs_the_same_rungs_by_failures():
    engine, state, observer = build()

    for step in range(1, 10):
        engine.process(state, Event(kind="tool_error", ts=float(step), step=step, tool_name="flaky", error="boom"))

    assert [(a.details["shape"], a.details["rung"]) for a, _ in observer.seen] == [
        ("retry", "log"), ("retry", "alert"), ("retry", "contain")]


def test_a_retryable_tool_gets_its_grace_before_the_first_rung():
    engine, state, observer = build()

    for step in range(1, 6):
        engine.process(state, Event(kind="tool_error", ts=float(step), step=step, tool_name="flaky",
                                    error="boom", retryable=True))

    assert observer.seen == []  # 5 failures, the bar is 2 x loop_threshold = 6


# --- end to end: what the customer sees ----------------------------------------------------


def test_runbound_events_carry_each_rung_and_nothing_is_raised_before_the_close():
    local_events.clear_for_tests()
    runbound.init(on_anomaly="raise", loop_threshold=3, on_spike="limit", spike_limit_calls=2)

    @runbound.tool(effects={"read"})
    def lookup(n):
        return "ok"

    with runbound.session(KEY):
        for _ in range(9):
            lookup(1)

    loops = [(e["severity"], e["reacted"], e["details"].get("rung")) for e in runbound.events()
             if e.get("kind") == "anomaly" and e.get("detector") == "loop"]
    assert loops == [("warn", "notify", "log"), ("critical", "notify", "alert"), ("warn", "warn", "contain")]
    local_events.clear_for_tests()
