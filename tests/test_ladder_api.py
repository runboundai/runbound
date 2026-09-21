"""Tests for the abuse ladder's api half — rollover, strikes, cooldown, status.

The detector closes a session and asks for a rollover; everything after that is
this module's subject. The end-user's app never learns any of it: the same
``session(key)`` block keeps working, the key quietly moves to a fresh session
with a tighter allowance, and the cooldown refusal it serves meanwhile looks
like any other :class:`GuardrailTripped`.

Nothing here measures real time: model-call durations are values handed to
``_record_llm_call`` and the latch clock is a fake moved by hand.
"""

import logging

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound import shared as shared_module
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import HelloReply
from spike_test_helpers import spike_controls_body
from test_shared_state import FakePlane

KEY = "user:8842"
OTHER = "user:0001"
PLANE_URL = "https://plane.example"

NORMAL_SECONDS = 2.0
#: Far past ``spike_factor`` x the baseline, so a run of spikes stays abnormal
#: even as it drags the session's own median along.
SPIKE_SECONDS = 400.0
COOLDOWN = 100.0

#: Normal turns before any abuse: a user who has been chatting for a while.
BASELINE_CALLS = 15

#: The ladder, wound tight enough to climb in a handful of calls --
#: delivered through the fake plane's Controls here (every knob is also
#: a real, local init() keyword; see configure() below).
_DEFAULT_SPIKE_KWARGS = dict(
    spike_warmup_calls=4,
    spike_confirm=2,
    spike_limit_calls=2,
    spike_cooldown_seconds=COOLDOWN,
    spike_max_strikes=2,
    spike_min_duration_s=1.0,
)


class FakeClock:
    """Stands in for the engine's ``time`` module; moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:  # pragma: no cover - never slept
        pass

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture(autouse=True)
def _plane(monkeypatch) -> FakePlane:
    """Every test in this module climbs the ladder through the plane's
    Controls (see ``configure()``'s own docstring for why, alongside the
    real local keywords) -- autouse so ``configure()`` (called by every
    single test here) can always reach a fake plane to deliver it
    through, exactly test_plane_baseline_restore.py's own harness."""
    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url = url
        fake.token = token
        fake.service = service
        fake.worker_id = worker_id
        fake.timeout_s = timeout_s
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    global _PLANE
    _PLANE = fake
    yield fake
    _PLANE = None


_PLANE: FakePlane | None = None


def configure(**overrides) -> None:
    """The ladder, wound tight enough to climb in a handful of calls --
    every one of the nine tuning knobs (and ``on_spike``) is a real,
    local ``init()`` keyword too. Still delivered on the fake plane's
    Controls, with the identical
    values, so the connected-worker path this module also exercises
    (strikes, generation, fleet cooldown) stays covered end to end and the
    plane's own merge is a confirming no-op rather than a conflict with
    what the code already asked for."""
    on_spike = overrides.pop("on_spike", "limit")
    spike_kwargs = dict(_DEFAULT_SPIKE_KWARGS)
    for name in list(overrides):
        if name in spike_kwargs:
            spike_kwargs[name] = overrides.pop(name)

    settings = dict(
        control_plane_url=PLANE_URL,
        token="k",
        service="checkout",
        worker_id="host-1:1",
        control_plane_poll_s=3600.0,
        export_events=False,
        auto_wrap=False,
        on_anomaly="raise",
        on_spike=on_spike,
        **spike_kwargs,
    )
    settings.update(overrides)
    runbound.init(**settings)

    _PLANE.controls_body = {"version": 1, "controls": spike_controls_body(on_spike, **spike_kwargs)}
    api._SHARED.apply_hello(HelloReply(controls_version=1))


def turn(key: str, duration: float = NORMAL_SECONDS) -> None:
    """One chatbot turn: enter the user's session, make one model call."""
    with runbound.session(key):
        api._record_llm_call("gpt-4o", 10, 100, duration_s=duration)


def spike(key: str = KEY) -> None:
    turn(key, SPIKE_SECONDS)


def warm(key: str = KEY) -> None:
    for _ in range(BASELINE_CALLS):
        turn(key)


def to_the_limit(key: str = KEY) -> dict:
    """Warm up, then spike until the session is limited (level 2)."""
    warm(key)
    for _ in range(2):
        spike(key)
    status = runbound.session_status(key)
    assert status["level"] == 2
    return status


def close_the_session(key: str = KEY, tries: int = 6):
    """Keep spiking a limited session until the closing call raises."""
    for _ in range(tries):
        try:
            spike(key)
        except GuardrailTripped as exc:
            return exc.anomaly
    raise AssertionError("the ladder never closed the session")


def refused(key: str = KEY):
    """Enter ``key``'s session expecting the door to be shut; the anomaly."""
    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session(key):
            pass
    return caught.value.anomaly


def rolled_over(key: str = KEY):
    """Climb the whole ladder once, then take the rollover on entry."""
    to_the_limit(key)
    close_the_session(key)
    return refused(key)


# --- the climb, seen from the app -------------------------------------------


def test_the_ladder_only_watches_and_limits_before_it_closes(clock):
    configure()
    warm()

    spike()  # level 1: a notice, nothing stopped
    assert runbound.session_status(KEY)["level"] == 1

    spike()  # level 2: limited, still nothing stopped
    status = runbound.session_status(KEY)
    assert (status["level"], status["allowance_left"]) == (2, 2)

    spike()  # one of the two allowed abnormal calls
    assert runbound.session_status(KEY)["allowance_left"] == 1


def test_the_call_that_spends_the_last_allowance_closes_the_session(clock):
    configure()
    to_the_limit()

    anomaly = close_the_session()

    assert anomaly.details["action"] == "rollover"
    assert anomaly.details["strikes"] == 1
    assert runbound.session_status(KEY)["level"] == 3


# --- the rollover ------------------------------------------------------------


def test_entering_after_the_close_rolls_the_key_over_into_a_cooldown(clock):
    configure()
    to_the_limit()
    close_the_session()
    before = runbound.session_status(KEY)

    anomaly = refused()

    assert anomaly.details["action"] == "rollover"
    status = runbound.session_status(KEY)
    assert status["generation"] == before["generation"] + 1
    assert status["strikes"] == 1
    assert status["tripped_by"] == "spike"
    assert status["cooldown_remaining_s"] == pytest.approx(COOLDOWN)
    assert status["level"] == 0  # a brand-new session, not the closed one


def test_the_rollover_is_logged_at_info(clock, caplog):
    caplog.set_level(logging.INFO, logger="runbound")
    configure()
    to_the_limit()
    close_the_session()

    refused()

    assert "rolled over" in caplog.text
    assert "strike 1 of 2" in caplog.text
    assert "cooldown 100s" in caplog.text


def test_the_session_is_replaced_not_reused(clock):
    configure()
    to_the_limit()
    close_the_session()
    closed = api._REGISTRY[KEY]

    refused()

    fresh = api._REGISTRY[KEY]
    assert fresh is not closed
    assert fresh.session_id != closed.session_id


def test_retrying_during_the_cooldown_costs_no_further_strike(clock):
    configure()
    rolled_over()

    for _ in range(5):
        assert refused().details["action"] == "rollover"

    status = runbound.session_status(KEY)
    assert status["strikes"] == 1
    assert status["generation"] == 1


def test_the_cooldown_counts_down_and_then_the_key_is_served_again(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN - 1)

    assert runbound.session_status(KEY)["cooldown_remaining_s"] == pytest.approx(1.0)
    refused()  # still inside the window

    clock.advance(2)

    turn(KEY)  # served: the cooldown is over
    status = runbound.session_status(KEY)
    assert status["tripped_by"] is None
    assert status["cooldown_remaining_s"] == 0.0
    assert status["level"] == 0
    assert status["strikes"] == 1


def test_the_session_after_a_rollover_gets_half_the_allowance(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN + 1)

    status = to_the_limit()

    assert status["allowance_left"] == 1  # spike_limit_calls 2, halved by one strike


def test_one_abnormal_call_past_the_halved_limit_closes_it_again(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN + 1)
    to_the_limit()

    anomaly = close_the_session()

    assert anomaly.details["strikes"] == 2


# --- the last strike: blocked until clear() ----------------------------------


def test_the_last_strike_blocks_the_key_permanently(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN + 1)
    to_the_limit()
    close_the_session()

    anomaly = refused()

    assert anomaly.details["action"] == "blocked"
    assert anomaly.details["level"] == 4
    assert anomaly.details["strikes"] == 2
    assert anomaly.details["key"] == KEY
    assert anomaly.detector == "spike"
    assert anomaly.severity == "critical"
    assert KEY in anomaly.message


def test_a_blocked_key_never_heals_on_its_own(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN + 1)
    to_the_limit()
    close_the_session()
    refused()

    clock.advance(10_000.0)

    assert refused().details["action"] == "blocked"
    status = runbound.session_status(KEY)
    assert status["cooldown_remaining_s"] == 0.0
    assert status["tripped_by"] == "spike"
    assert status["strikes"] == 2


def test_clear_forgives_the_strikes_too(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN + 1)
    to_the_limit()
    close_the_session()
    refused()  # blocked

    runbound.clear(KEY)

    assert runbound.session_status(KEY) is None  # forgotten entirely
    turn(KEY)  # served again
    assert runbound.session_status(KEY)["strikes"] == 0
    assert to_the_limit()["allowance_left"] == 2  # the full allowance is back


# --- everyone else keeps being served ----------------------------------------


def test_another_key_is_untouched_by_the_whole_climb(clock):
    configure()
    turn(OTHER)
    rolled_over()
    clock.advance(COOLDOWN + 1)
    to_the_limit()
    close_the_session()
    refused()  # KEY is blocked

    for _ in range(5):
        turn(OTHER)

    status = runbound.session_status(OTHER)
    assert status == {
        "level": 0,
        "strikes": 0,
        "allowance_left": None,
        "cooldown_remaining_s": 0.0,
        "tripped_by": None,
        "generation": 0,
        "why": {
            "trigger": None,
            "limited_at_s_ago": 0.0,
            "allowance_start": None,
            "healed_times": 0,
            "closed_at_s_ago": 0.0,
            "baseline": {
                "duration_s": NORMAL_SECONDS,
                "output_tokens": 100.0,
                "source": "local",  # this session learned it itself
            },
            "service_baseline": None,  # a plane is connected but never states one
        },
        "history": [],
        "posture": None,
        "budget": None,  # no budget_usd configured
        "actions": {"requested": 0, "admitted": 0, "executed": 0, "refused": 0},
    }


# --- session_status ----------------------------------------------------------


def test_session_status_is_none_before_init():
    assert runbound.session_status(KEY) is None


def test_session_status_is_none_for_an_unknown_key_and_creates_nothing(clock):
    configure()

    assert runbound.session_status("user:nobody") is None
    assert "user:nobody" not in api._REGISTRY


def test_session_status_does_not_disturb_the_lru_order(clock):
    configure(max_sessions=2)
    turn(KEY)
    turn(OTHER)

    runbound.session_status(KEY)  # not a use of KEY's session
    turn("user:third")

    assert KEY not in api._REGISTRY  # still the least recently used


def test_session_status_survives_a_broken_registry(clock, monkeypatch, caplog):
    configure()
    turn(KEY)
    monkeypatch.setattr(api, "_latched", _explode)

    assert runbound.session_status(KEY) is None


# --- strikes outlive eviction ------------------------------------------------


def test_evicting_a_rolled_over_session_does_not_forget_its_strikes(clock):
    configure(max_sessions=2)
    rolled_over()

    turn(OTHER)
    turn("user:third")  # KEY is the least recently used and is dropped
    assert runbound.session_status(KEY) is None

    turn(KEY)  # a fresh session for a key with a history

    assert runbound.session_status(KEY)["strikes"] == 1
    assert to_the_limit()["allowance_left"] == 1


# --- the other spike modes never roll anything over --------------------------


def test_notify_mode_never_climbs_or_rolls_over(clock):
    configure(on_spike="notify")
    warm()

    for _ in range(6):
        spike()  # a confirmed spike notifies; nothing stops

    status = runbound.session_status(KEY)
    assert status["level"] == 0
    assert status["strikes"] == 0
    assert status["generation"] == 0


def test_trip_mode_latches_without_rolling_over(clock):
    configure(on_spike="trip")
    warm()
    spike()
    with pytest.raises(GuardrailTripped) as caught:
        spike()
    assert caught.value.anomaly.details.get("action") is None
    latched = api._REGISTRY[KEY]

    anomaly = refused()  # entry is refused, as it always was

    assert anomaly.details.get("action") is None
    assert api._REGISTRY[KEY] is latched  # the same session, not a rollover
    status = runbound.session_status(KEY)
    assert status["generation"] == 0
    assert status["strikes"] == 0


# --- fail-open ---------------------------------------------------------------


def _explode(*args, **kwargs):
    raise RuntimeError("boom")


def test_a_rollover_that_fails_leaves_the_key_exactly_as_it_was(
    clock, monkeypatch, caplog
):
    configure()
    to_the_limit()
    close_the_session()
    closed = api._REGISTRY[KEY]
    monkeypatch.setattr(api, "_new_session", _explode)

    anomaly = refused()  # the old session's latch still answers

    assert anomaly.details["action"] == "rollover"
    assert api._REGISTRY[KEY] is closed
    assert api._GENERATIONS.get(KEY, 0) == 0
    assert api._STRIKES.get(KEY, 0) == 0
    assert "roll" in caplog.text.lower()


def test_callback_mode_rolls_the_key_over_without_refusing_the_block(clock):
    """Only ``"raise"`` refuses at the door; every mode still rolls over."""
    seen = []
    configure(on_anomaly="callback", callback=seen.append)
    to_the_limit()
    for _ in range(2):
        spike()  # callback mode: the closing call never raises

    ran = False
    with runbound.session(KEY):
        ran = True

    assert ran is True
    status = runbound.session_status(KEY)
    assert status["strikes"] == 1
    assert status["cooldown_remaining_s"] == pytest.approx(COOLDOWN)
