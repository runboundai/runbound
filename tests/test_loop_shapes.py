"""The three loop shapes beside "repeat" (sequence, retry, stall), and
the config knobs and details keys that come with them.

Design note: "repeat" counts a
hash's occurrences *anywhere* in the trailing window, so for any genuine
period-k cycle (k >= 2) built from the *same* arguments every cycle, an
individual hash inside that cycle always reaches ``loop_threshold`` strictly
before the full cycle can complete ``loop_threshold`` times — "repeat" would
always win the race. "sequence" is therefore a *tool-name* rotation, not an
exact-hash rotation: the edit-test-edit cycle an agent actually falls into
edits and tests *different* things each time (different arguments, different
hashes) while repeating the same rotation of tools. That is also why the
default ``loop_shapes=("repeat", "sequence", "retry")`` order — "repeat"
checked first — never actually starves "sequence": they detect different
signals and only compete when arguments truly repeat too, in which case
"repeat" is the more specific classification of the same evidence.
"""

import pytest

import runbound
from runbound import api, shared as shared_module
from runbound import detectors as detectors_module
from runbound.config import LOOP_SHAPES, GuardrailConfig
from runbound.detectors import LoopDetector
from runbound.events import Event
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import HelloReply
from runbound.state import SessionState
from spike_test_helpers import spike_controls_body
from test_shared_state import FakePlane

CENTS = {"m": (1_000_000, 0)}  # $1 per input token, $0 output -- round dollars

PLANE_URL = "https://plane.example"


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
    """This suite's own coverage of the plane-delivered path:
    "sequence", "retry" and "stall" are also real, local ``init()``
    keywords (see ``test_loop_shapes_and_its_knobs_are_real_local_fields_again``
    below), but this fixture drives them through the plane's
    ``/v1/controls`` ``loop_shapes`` bundle instead, delivered on a
    heartbeat exactly like ``tests/test_controls_delivery.py``'s own
    harness (this fixture is that same pattern, local to this file so the
    shape tests below stay about the shapes, not the delivery plumbing)."""
    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url = url
        fake.token = token
        fake.service = service
        fake.worker_id = worker_id
        fake.timeout_s = timeout_s
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    return fake


def init_with_shapes(plane: FakePlane, shapes, max_period=6, stall_turns=5, **kwargs) -> None:
    """``runbound.init(**kwargs)`` connected to ``plane``, with a
    ``loop_shapes`` bundle already delivered within the same call — the
    plane-delivered alternative to ``runbound.init(loop_shapes=..., ...)``."""
    fields = {
        "control_plane_url": PLANE_URL,
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
        "control_plane_poll_s": 3600.0,
        "export_events": False,
        "auto_wrap": False,
        # These tests are about what each shape *detects*. Since the graded
        # default (PS-1f) answers a loop in rungs instead of tripping at the
        # threshold, they pin the legacy "break" policy, which trips on the
        # first anomaly, so a shape's detection is what they observe.
        "on_loop": "break",
    }
    fields.update(kwargs)
    runbound.init(**fields)
    plane.controls_body = {
        "version": 1,
        "controls": {
            "loop_shapes": {"shapes": list(shapes), "max_period": max_period, "stall_turns": stall_turns}
        },
    }
    api._SHARED.apply_hello(HelloReply(controls_version=1))


# --- config validation --------------------------------------------------


def test_loop_shapes_and_its_knobs_are_real_local_fields_again():
    """`loop_shapes`,
    `loop_max_period` and `loop_stall_turns` are real `GuardrailConfig`
    fields again, with `loop_shapes` defaulting to `("repeat", "sequence",
    "retry")` -- their 0.3.x meanings. See `runbound.controls_merge.
    effective_loop_shapes` for how a plane can still add "stall" (or
    narrow `stall_turns`/widen `max_period`) on top."""
    cfg = GuardrailConfig()
    assert cfg.loop_shapes == ("repeat", "sequence", "retry")
    assert cfg.loop_max_period == 6
    assert cfg.loop_stall_turns == 5


def test_a_plane_less_engine_checks_the_local_default_shapes():
    from runbound.engine import Engine

    cfg = GuardrailConfig()
    cfg.validate()
    engine = Engine(cfg)
    assert engine._effective_loop_shapes() == ("repeat", "sequence", "retry")


def test_a_plane_less_engine_with_loop_shapes_off_only_checks_repeat():
    from runbound.engine import Engine

    cfg = GuardrailConfig(loop_shapes=("repeat",))
    cfg.validate()
    engine = Engine(cfg)
    assert engine._effective_loop_shapes() == ("repeat",)


def test_loop_shapes_is_the_full_set():
    assert set(LOOP_SHAPES) == {"repeat", "sequence", "retry", "stall"}


# --- "sequence": a period-k cycle of tool names -------------------------


def test_period_3_sequence_at_threshold(plane):
    """Three distinct tools, different arguments every cycle, rotated 3x."""
    init_with_shapes(plane, ("repeat", "sequence", "retry"), on_anomaly="raise")

    @runbound.tool
    def edit(text):
        return "ok"

    @runbound.tool
    def test_(text):
        return "ok"

    @runbound.tool
    def review(text):
        return "ok"

    with pytest.raises(GuardrailTripped) as excinfo:
        for i in range(3):
            edit(f"file-{i}")
            test_(f"run-{i}")
            review(f"note-{i}")

    anomaly = excinfo.value.anomaly
    assert anomaly.detector == "loop"
    assert anomaly.details["shape"] == "sequence"
    assert anomaly.details["period"] == 3
    assert anomaly.details["period_tools"] == ["edit", "test_", "review"]
    assert anomaly.details["repeats"] == 3
    assert anomaly.details["started_turn"] == 0  # no llm_call ever happened


def test_period_1_repeat_is_unchanged():
    """Same tool, same arguments, still just "repeat" -- the original shape."""
    runbound.init(on_anomaly="raise", on_loop="break", loop_threshold=3)

    @runbound.tool
    def search(query):
        return "result"

    search("weather")
    search("weather")
    with pytest.raises(GuardrailTripped) as excinfo:
        search("weather")

    anomaly = excinfo.value.anomaly
    assert anomaly.details["shape"] == "repeat"
    assert anomaly.details["period_tools"] == ["search"]
    assert anomaly.details["repeats"] == 3


def _run_period_7_fixture():
    """Three full period-7 cycles (21 calls, distinct args every call)."""
    names = [f"tool{i}" for i in range(7)]
    tools = {}
    for n in names:
        def make(name):
            @runbound.tool(name=name)
            def _fn(text):
                return "ok"
            return _fn
        tools[n] = make(n)

    for i in range(3):
        for n in names:
            tools[n](f"{n}-{i}")


def test_period_7_sequence_not_caught_at_the_default_max_period(plane):
    """``loop_max_period`` defaults to 6 once "sequence" is enabled at all; a
    genuine period-7 rotation is invisible.

    ``loop_window=30`` is wide enough to hold all 21 actions of the fixture
    (the default 20 is not: with only 20 slots, 21 actions never fit the
    window at all, which would make this pass for the wrong reason — the
    window evicting history, not the period cap excluding it). See the
    positive control right below: the same fixture *is* caught once
    ``loop_max_period`` covers 7.
    """
    init_with_shapes(plane, ("repeat", "sequence"), on_anomaly="raise", loop_window=30)

    _run_period_7_fixture()

    assert runbound.is_tripped() is None


def test_period_7_sequence_is_caught_once_loop_max_period_covers_it(plane):
    """The positive control for the test above: same fixture, wider cap."""
    init_with_shapes(
        plane, ("repeat", "sequence"), max_period=7, on_anomaly="raise", loop_window=30
    )

    with pytest.raises(GuardrailTripped) as excinfo:
        _run_period_7_fixture()

    anomaly = excinfo.value.anomaly
    assert anomaly.details["shape"] == "sequence"
    assert anomaly.details["period"] == 7


def test_a_polling_tool_inside_a_sequence_still_catches_the_other_tool(plane):
    init_with_shapes(plane, ("repeat", "sequence", "retry"), on_anomaly="raise")

    @runbound.tool(polling=True)
    def poll():
        return "pending"

    @runbound.tool
    def check():
        return "ok"

    poll()
    check()
    poll()
    check()
    poll()  # a 3rd poll(): must not be the one that raises
    with pytest.raises(GuardrailTripped) as excinfo:
        check()  # the 3rd check(): this one must be the one that raises

    anomaly = excinfo.value.anomaly
    assert anomaly.details["shape"] == "repeat"  # poll's hash never enters the window
    assert anomaly.details["tool_name"] == "check"


def test_sequence_requires_at_least_two_distinct_tool_names(plane):
    """A single tool spammed with different arguments is not a "sequence"."""
    init_with_shapes(plane, ("repeat", "sequence"), max_period=6, on_anomaly="raise", loop_threshold=3)

    @runbound.tool
    def search(query):
        return "ok"

    for i in range(12):
        search(f"q{i}")  # every hash distinct: never "repeat", never a real cycle

    assert runbound.is_tripped() is None


# --- "retry": one tool failing and being re-attempted -------------------


def test_retry_storm_on_one_tool(plane):
    """The trip is discovered while recording the *failure* (a tool_error
    event), and runbound always prefers the tool's own exception over
    GuardrailTripped there (``api._record_tool_error``'s documented
    swallow-and-warn) — the same rule ``error_storm`` already lives under.
    The session is latched regardless, so the very next call surfaces
    GuardrailTripped through the latch instead.
    """
    init_with_shapes(plane, ("repeat", "retry"), on_anomaly="raise", loop_threshold=3)

    @runbound.tool
    def flaky(x):
        raise RuntimeError("boom")

    for i in range(1, 4):
        with pytest.raises(RuntimeError):
            flaky(f"try-{i}")  # varying args: never trips "repeat" on the way in

    assert runbound.is_tripped().details["shape"] == "retry"

    with pytest.raises(GuardrailTripped) as excinfo:
        flaky("try-4")  # latched: raises before the body runs this time

    anomaly = excinfo.value.anomaly
    assert anomaly.details["shape"] == "retry"
    assert anomaly.details["tool_name"] == "flaky"
    assert anomaly.details["repeats"] == 3
    assert anomaly.details["period_tools"] == ["flaky"]


def test_retry_storm_is_distinct_from_error_storm(plane):
    """error_storm counts every failure in a time window; retry is per-tool, per-action-count."""
    init_with_shapes(
        plane, ("repeat", "retry"), on_anomaly="raise", loop_threshold=10, error_storm_limit=None
    )

    @runbound.tool
    def flaky(x):
        raise RuntimeError("boom")

    for i in range(1, 4):
        with pytest.raises(RuntimeError):
            flaky(f"try-{i}")

    assert runbound.is_tripped() is None  # below loop_threshold=10, and error_storm is off


def test_retryable_tool_survives_the_ordinary_threshold(plane):
    """``retryable=True`` is a *grace*, not a no-op — a retryable
    tool's retries "do not count toward the retry
    shape until loop_threshold", i.e. the ordinary bar is not enough; the
    shape only fires at twice loop_threshold for a retryable tool.
    """
    init_with_shapes(plane, ("repeat", "retry"), on_anomaly="raise", loop_threshold=2)

    @runbound.tool(retryable=True)
    def flaky(x):
        raise RuntimeError("boom")

    for i in range(1, 3):  # exactly loop_threshold failures
        with pytest.raises(RuntimeError):
            flaky(f"try-{i}")

    assert runbound.is_tripped() is None  # the grace is not spent yet


def test_retryable_tool_trips_at_double_the_threshold(plane):
    init_with_shapes(plane, ("repeat", "retry"), on_anomaly="raise", loop_threshold=2)

    @runbound.tool(retryable=True)
    def flaky(x):
        raise RuntimeError("boom")

    for i in range(1, 5):  # 2 * loop_threshold failures
        with pytest.raises(RuntimeError):
            flaky(f"try-{i}")

    anomaly = runbound.is_tripped()
    assert anomaly.details["shape"] == "retry"
    assert anomaly.details["repeats"] == 4
    assert anomaly.details["threshold"] == 4


def test_a_non_retryable_tool_is_unaffected_by_the_grace(plane):
    """The grace is opt-in: a plain tool still trips at the ordinary threshold."""
    init_with_shapes(plane, ("repeat", "retry"), on_anomaly="raise", loop_threshold=2)

    @runbound.tool
    def flaky(x):
        raise RuntimeError("boom")

    for i in range(1, 3):  # exactly loop_threshold failures
        with pytest.raises(RuntimeError):
            flaky(f"try-{i}")

    anomaly = runbound.is_tripped()
    assert anomaly.details["shape"] == "retry"
    assert anomaly.details["repeats"] == 2
    assert anomaly.details["threshold"] == 2


# --- "stall": consecutive turns with nothing new -------------------------


def test_a_stall_of_n_turns(plane):
    init_with_shapes(
        plane,
        ("stall",),
        stall_turns=3,
        on_anomaly="raise",
        custom_prices=CENTS,
    )

    @runbound.tool
    def search(query):
        return "ok"

    runbound.record_call("m", 1, 0, 0.0)  # turn 1
    search("only-call")  # new hash at turn 1
    runbound.record_call("m", 1, 0, 0.0)  # turn 2, stalled=1
    runbound.record_call("m", 1, 0, 0.0)  # turn 3, stalled=2
    with pytest.raises(GuardrailTripped) as excinfo:
        runbound.record_call("m", 1, 0, 0.0)  # turn 4, stalled=3 -> fires

    anomaly = excinfo.value.anomaly
    assert anomaly.details["shape"] == "stall"
    assert anomaly.details["repeats"] == 3
    assert anomaly.details["started_turn"] == 2
    assert anomaly.details["period_tools"] == []


def test_stall_is_opt_in_not_on_by_default():
    """No plane at all: "stall" (like every shape beyond "repeat") never
    runs, so a run of otherwise-stalling turns is never even evaluated for
    it -- the SDK's own default, not merely "stall" being left out
    of some tuple."""
    runbound.init(on_anomaly="raise", custom_prices=CENTS)

    for _ in range(5):
        runbound.record_call("m", 1, 0, 0.0)

    assert runbound.is_tripped() is None


def test_a_new_hash_resets_the_stall_clock(plane):
    init_with_shapes(
        plane,
        ("stall",),
        stall_turns=2,
        on_anomaly="raise",
        custom_prices=CENTS,
    )

    @runbound.tool
    def search(query):
        return "ok"

    runbound.record_call("m", 1, 0, 0.0)  # turn 1
    search("a")  # new hash, turn 1
    runbound.record_call("m", 1, 0, 0.0)  # turn 2, stalled=1 (< 2)
    search("b")  # a *different* hash: resets the clock, turn 2
    runbound.record_call("m", 1, 0, 0.0)  # turn 3, stalled=1 again
    assert runbound.is_tripped() is None


# --- usd_inside_loop: honest, bounded, never a wrong total ---------------


def test_usd_inside_loop_equals_spend_since_started_turn():
    runbound.init(on_anomaly="raise", on_loop="break", custom_prices=CENTS)

    @runbound.tool
    def search(query):
        return "ok"

    runbound.record_call("m", 2, 0, 0.0)  # turn 1, $2
    search("x")  # turn 1, first occurrence of h
    runbound.record_call("m", 3, 0, 0.0)  # turn 2, $3
    search("x")  # turn 2, count 2
    runbound.record_call("m", 5, 0, 0.0)  # turn 3, $5
    with pytest.raises(GuardrailTripped) as excinfo:
        search("x")  # turn 3, count 3 -> repeat fires

    anomaly = excinfo.value.anomaly
    assert anomaly.details["started_turn"] == 1
    assert anomaly.details["usd_inside_loop"] == pytest.approx(2 + 3 + 5)


def test_usd_inside_loop_is_bounded_by_the_spike_window_honestly(plane):
    """Older than spike_window turns: a documented lower bound, never a wrong total.

    ``spike_window`` is a real, local ``init()`` field, and
    ``recent_calls``'s maxlen follows it regardless of ``spike_enabled``
    (``Engine._effective_config`` sets it unconditionally) — this test
    delivers it through the fake plane's Controls instead, this suite's
    own coverage of that path.
    """
    runbound.init(
        control_plane_url=PLANE_URL, token="k", service="checkout",
        worker_id="host-1:42", control_plane_poll_s=3600.0,
        export_events=False, auto_wrap=False,
        on_anomaly="raise", on_loop="break", custom_prices=CENTS,
    )
    plane.controls_body = {"version": 1, "controls": spike_controls_body(spike_window=3, spike_warmup_calls=2)}
    api._SHARED.apply_hello(HelloReply(controls_version=1))
    runbound.reset()  # the default session was already sized before the hello above

    @runbound.tool
    def search(query):
        return "ok"

    runbound.record_call("m", 1, 0, 0.0)  # turn 1, $1 -- will be evicted
    search("x")  # started_turn will be 1
    runbound.record_call("m", 2, 0, 0.0)  # turn 2, $2
    search("x")  # count 2
    runbound.record_call("m", 3, 0, 0.0)  # turn 3, $3 (recent_calls now full: 1,2,3)
    runbound.record_call("m", 4, 0, 0.0)  # turn 4, $4 (evicts turn 1's $1)
    with pytest.raises(GuardrailTripped) as excinfo:
        search("x")  # turn 4, count 3 -> repeat fires

    anomaly = excinfo.value.anomaly
    assert anomaly.details["started_turn"] == 1
    # true total would be 1+2+3+4=10; the $1 fell out of recent_calls' window,
    # so the honest, bounded answer is only the trailing spike_window's worth.
    assert anomaly.details["usd_inside_loop"] == pytest.approx(2 + 3 + 4)


def test_usd_inside_loop_is_zero_with_no_model_calls_yet():
    state = SessionState("s1")
    assert detectors_module._usd_inside_loop(state, started_turn=0) == 0.0


# --- digests never appear in message -------------------------------------


def test_repeat_message_never_contains_the_args_hash():
    runbound.init(on_anomaly="raise", on_loop="break")

    @runbound.tool
    def search(query):
        return "ok"

    search("weather")
    search("weather")
    with pytest.raises(GuardrailTripped) as excinfo:
        search("weather")

    anomaly = excinfo.value.anomaly
    assert anomaly.details["args_hash"] not in anomaly.message


def test_sequence_message_never_contains_any_hash_in_the_window(plane):
    init_with_shapes(plane, ("repeat", "sequence"), on_anomaly="raise")

    @runbound.tool
    def edit(text):
        return "ok"

    @runbound.tool
    def test_(text):
        return "ok"

    with pytest.raises(GuardrailTripped) as excinfo:
        for i in range(3):
            edit(f"file-{i}")
            test_(f"run-{i}")

    anomaly = excinfo.value.anomaly
    detector = None
    for det in api._ENGINE.detectors:
        if isinstance(det, LoopDetector):
            detector = det
    session = runbound.current_session()
    window = detector._windows.get(session.session_id, [])
    for _, _, args_hash in window:
        assert args_hash not in anomaly.message


def test_retry_message_never_contains_a_hash(plane):
    init_with_shapes(plane, ("repeat", "retry"), on_anomaly="raise", loop_threshold=2)

    @runbound.tool
    def flaky(x):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        flaky("a")
    with pytest.raises(RuntimeError):
        flaky("b")

    # a retry anomaly carries no args_hash at all -- assert that directly,
    # which would fail the moment one was added and interpolated into message.
    anomaly = runbound.is_tripped()
    assert anomaly.details["shape"] == "retry"
    assert "args_hash" not in anomaly.details


# --- O(window) per call: an operation-count bound, not a timing one -----


class _CountingHash(str):
    """A str whose == is instrumented, to bound comparisons without timing."""

    calls = 0

    def __eq__(self, other):
        _CountingHash.calls += 1
        return str.__eq__(self, other)

    def __ne__(self, other):
        _CountingHash.calls += 1
        return str.__ne__(self, other)

    def __hash__(self):
        return str.__hash__(self)


def _sequence_event(step: int, tag: int) -> Event:
    return Event(
        kind="tool_call", ts=float(step), step=step, tool_name="tool",
        args_hash=_CountingHash(f"unique-{tag}"),
    )


def test_sequence_comparison_cost_per_call_does_not_grow_with_history():
    """Every hash here is distinct, so "sequence" never matches -- it always
    walks its full checked range (2..loop_max_period). That per-call walk is
    bounded by loop_max_period and loop_threshold alone: it must cost the
    same whether it is the 50th event this detector has seen or the 2000th,
    because the window it looks at (a deque with maxlen=loop_window) never
    grows past loop_window regardless of how many events preceded it.
    """
    config = GuardrailConfig(
        loop_threshold=3, loop_window=12, loop_shapes=("sequence",), loop_max_period=4,
        on_loop="break",  # the legacy policy: this measures detection, not the graded rungs
    )

    def per_call_costs(n_events: int) -> list[int]:
        detector = LoopDetector()
        state = SessionState("s", loop_window=12)
        costs = []
        for i in range(n_events):
            event = _sequence_event(i, i)
            state.record(event)
            before = _CountingHash.calls
            detector.check(state, event, config)
            costs.append(_CountingHash.calls - before)
        return costs

    short_run = per_call_costs(50)
    long_run = per_call_costs(2000)

    # steady state (once the window is full) costs exactly the same either way
    assert max(short_run[-10:]) == max(long_run[-10:])
    # and it is a small constant, not proportional to how many events ran
    assert max(long_run[-10:]) <= config.loop_threshold * config.loop_max_period ** 2


# --- fail-open ------------------------------------------------------------


def test_a_broken_usd_inside_loop_never_crashes_the_host(monkeypatch):
    """The engine already wraps every detector in try/except (fail-open);
    this proves it holds for the new helper: a real loop still exists, but a
    bug in usd_inside_loop must not turn into a crash for the host's call.
    """
    runbound.init(on_anomaly="raise", loop_threshold=3)

    def _boom(state, started_turn):
        raise RuntimeError("usd_inside_loop is broken")

    monkeypatch.setattr(detectors_module, "_usd_inside_loop", _boom)

    @runbound.tool
    def search(query):
        return "ok"

    search("weather")
    search("weather")
    # would raise GuardrailTripped(shape="repeat") if the helper worked;
    # instead the detector's exception is caught by the engine and this
    # call must still return normally.
    result = search("weather")
    assert result == "ok"
