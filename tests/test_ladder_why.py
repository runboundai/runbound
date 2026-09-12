"""Tests for the abuse ladder's ``why`` and ``history`` — it explains itself.

``session_status(key)`` already reports the ladder's *number* (``level``,
``strikes``, ``allowance_left``...); this module is about the *story* behind
it: which call moved the level, when it was last limited or closed, how many
times it has healed, and the trail of transitions that got it there.

Like ``tests/test_ladder_api.py``, the climb itself is driven with fake
durations handed to ``_record_llm_call`` by hand, never real time. The
``at_s_ago``/``*_s_ago`` fields, though, are timed by ``state.py`` with its
own ``time.monotonic()`` — independent of the engine's fake-clock hook used
elsewhere for cooldown countdowns — so this module asserts them as "small and
non-negative" (or "unchanged" / "not shrinking") rather than pinning exact
numbers. The one test that must outlive a cooldown (crossing a second
rollover) uses the same fake engine clock ``test_ladder_api.py`` does, purely
to skip the wait; it never touches the why/history timestamps.
"""

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound.exceptions import GuardrailTripped

KEY = "user:9901"

NORMAL_SECONDS = 2.0
#: Far past spike_factor (10x default) x the baseline, so a run of spikes
#: stays abnormal even as it drags the session's own held median along.
SPIKE_SECONDS = 400.0
COOLDOWN = 100.0

#: Normal turns before any abuse: a user who has been chatting for a while.
BASELINE_CALLS = 15

#: The detector's trailing confirm window (SPIKE_CONFIRM_MAX in detectors.py).
FLAG_WINDOW = 5


class FakeClock:
    """Stands in for the engine's ``time`` module; moved by hand.

    Only needed by the one test that crosses a cooldown to reach a second
    rollover — everything else here never needs to skip real time.
    """

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


def configure(**overrides) -> None:
    """The ladder, wound tight enough to climb in a handful of calls."""
    settings = dict(
        on_spike="limit",
        on_anomaly="raise",
        spike_warmup_calls=4,
        spike_confirm=2,
        spike_limit_calls=2,
        spike_cooldown_seconds=COOLDOWN,
        spike_max_strikes=2,
        spike_min_duration_s=1.0,
    )
    settings.update(overrides)
    runbound.init(**settings)


def turn(key: str = KEY, duration: float = NORMAL_SECONDS) -> None:
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


def heal(key: str = KEY, tries: int = FLAG_WINDOW + 2) -> dict:
    """Keep taking normal turns on a limited session until it heals to level 1."""
    for _ in range(tries):
        turn(key)
        status = runbound.session_status(key)
        if status["level"] == 1:
            return status
    raise AssertionError("the session never healed")


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


def assert_small_nonneg(value: float) -> None:
    """A real wall-clock delta timed within one fast test: sane, not exact."""
    assert isinstance(value, float)
    assert 0.0 <= value < 5.0


def trigger_core(trigger: dict | None) -> dict | None:
    """A trigger's identity, minus ``at_s_ago``.

    ``at_s_ago`` is recomputed against "now" on every read, so it is never
    equal between two separate ``session_status`` calls even when the
    underlying raw instant (and everything else about the trigger) did not
    change. Comparing this instead is how a test asserts "the same call is
    still the trigger" without pinning a timestamp.
    """
    return None if trigger is None else {k: v for k, v in trigger.items() if k != "at_s_ago"}


# --- blank before anything happens -------------------------------------------


def test_why_and_history_are_blank_before_any_spike():
    configure()
    turn()  # one ordinary call, nowhere near warmup

    status = runbound.session_status(KEY)

    assert status["why"] == {
        "trigger": None,
        "limited_at_s_ago": 0.0,
        "allowance_start": None,
        "healed_times": 0,
        "closed_at_s_ago": 0.0,
        "baseline": None,
    }
    assert status["history"] == []


# --- the climb, rung by rung --------------------------------------------------


def test_the_first_abnormal_call_sets_the_trigger_and_a_history_entry():
    configure()
    warm()

    spike()  # level 1: a notice

    status = runbound.session_status(KEY)
    assert status["level"] == 1
    trigger = status["why"]["trigger"]
    assert trigger["metric"] == "duration"
    assert trigger["value"] == pytest.approx(SPIKE_SECONDS)
    assert trigger["median"] == pytest.approx(NORMAL_SECONDS)
    assert trigger["factor"] == pytest.approx(10.0)  # config default
    assert_small_nonneg(trigger["at_s_ago"])
    assert len(status["history"]) == 1
    level_from, level_to, at_s_ago, reason = status["history"][0]
    assert (level_from, level_to, reason) == (0, 1, "first_abnormal")
    assert_small_nonneg(at_s_ago)
    # nothing limited or closed yet
    assert status["why"]["limited_at_s_ago"] == 0.0
    assert status["why"]["closed_at_s_ago"] == 0.0
    assert status["why"]["healed_times"] == 0
    assert status["why"]["allowance_start"] is None


def test_the_confirming_spike_moves_the_trigger_and_appends_confirmed():
    configure()
    warm()
    spike()  # level 1

    spike()  # level 2: confirmed

    status = runbound.session_status(KEY)
    assert status["level"] == 2
    why = status["why"]
    assert why["trigger"]["metric"] == "duration"
    assert why["trigger"]["value"] == pytest.approx(SPIKE_SECONDS)
    assert why["allowance_start"] == 2  # spike_limit_calls, no strikes yet
    assert_small_nonneg(why["limited_at_s_ago"])
    assert why["closed_at_s_ago"] == 0.0
    assert why["healed_times"] == 0
    reasons = [entry[3] for entry in status["history"]]
    assert reasons == ["first_abnormal", "confirmed"]
    levels = [(entry[0], entry[1]) for entry in status["history"]]
    assert levels == [(0, 1), (1, 2)]


def test_baseline_matches_the_session_s_own_held_baseline():
    configure()
    warm()
    spike()
    spike()

    status = runbound.session_status(KEY)
    held = api._REGISTRY[KEY].spike_baseline

    assert held is not None
    assert status["why"]["baseline"] == {
        "duration_s": held[0],
        "output_tokens": held[1],
    }


def test_an_allowance_spending_call_does_not_move_the_trigger():
    configure(spike_limit_calls=3)
    status = to_the_limit()
    before_trigger = status["why"]["trigger"]
    before_allowance = status["allowance_left"]
    assert before_allowance == 3

    spike()  # burns one allowance; does not close, does not re-confirm

    after = runbound.session_status(KEY)
    assert after["allowance_left"] == before_allowance - 1
    # this call didn't raise the level: the trigger is still the confirming call
    assert trigger_core(after["why"]["trigger"]) == trigger_core(before_trigger)
    assert after["history"][-1][3] == "allowance_spent"
    assert after["history"][-1][:2] == (2, 2)


def test_healing_increments_healed_times_and_records_the_transition():
    configure()
    to_the_limit()

    status = heal()

    assert status["level"] == 1
    assert status["why"]["healed_times"] == 1
    assert status["why"]["allowance_start"] is None  # forgotten on heal
    assert status["allowance_left"] is None
    assert status["history"][-1][3] == "healed"
    assert status["history"][-1][:2] == (2, 1)


def test_healing_twice_counts_twice():
    configure()
    to_the_limit()
    heal()

    for _ in range(2):
        spike()  # back to level 2
    assert runbound.session_status(KEY)["level"] == 2
    status = heal()

    assert status["why"]["healed_times"] == 2


# --- closing and rollover -----------------------------------------------


def test_closing_the_session_sets_closed_at_and_the_final_history_entry():
    configure()
    to_the_limit()

    anomaly = close_the_session()

    assert anomaly.details["action"] == "rollover"
    status = runbound.session_status(KEY)
    assert status["level"] == 3
    assert_small_nonneg(status["why"]["closed_at_s_ago"])
    assert status["history"][-1][3] == "allowance_spent"
    assert status["history"][-1][:2] == (2, 3)
    # the closing call itself is the trigger: it was the one that raised the level
    assert status["why"]["trigger"]["value"] == pytest.approx(SPIKE_SECONDS)


def test_rollover_carries_the_why_forward_onto_the_fresh_session():
    configure()
    to_the_limit()
    close_the_session()
    before = runbound.session_status(KEY)

    refused()

    status = runbound.session_status(KEY)
    assert status["level"] == 0  # a brand-new session
    # the story survives even though the session object was replaced
    assert status["why"]["closed_at_s_ago"] >= before["why"]["closed_at_s_ago"]
    assert_small_nonneg(status["why"]["closed_at_s_ago"])
    assert trigger_core(status["why"]["trigger"]) == trigger_core(before["why"]["trigger"])
    # the fresh session's own baseline and allowance start over
    assert status["why"]["baseline"] is None
    assert status["why"]["allowance_start"] is None
    # history is the old climb plus the rollover itself
    reasons = [entry[3] for entry in status["history"]]
    assert reasons == [entry[3] for entry in before["history"]] + ["rollover"]
    assert status["history"][-1][:2] == (3, 0)


def test_the_last_strike_records_blocked_not_rollover(clock):
    configure()
    rolled_over()
    clock.advance(COOLDOWN + 1)

    to_the_limit()
    close_the_session()
    refused()  # the final strike: blocked

    status = runbound.session_status(KEY)
    level_from, level_to, at_s_ago, reason = status["history"][-1]
    assert (level_from, level_to, reason) == (3, 0, "blocked")
    assert_small_nonneg(at_s_ago)


def test_history_is_bounded_to_ten_entries():
    configure(spike_confirm=1)  # confirm on the very first abnormal call
    warm()
    # climb to level 2 and heal back, repeatedly, past 10 transitions total
    for _ in range(6):
        spike()  # confirms immediately (spike_confirm=1): straight to level 2
        heal()

    status = runbound.session_status(KEY)
    # 6 iterations x (confirmed, healed) = 12 transitions; the oldest 2 are
    # dropped, so the surviving history starts at the second iteration's
    # climb to level 2, not the first.
    assert len(status["history"]) == 10
    assert status["history"][0][:2] == (1, 2)


# --- clear() --------------------------------------------------------------


def test_clear_records_a_cleared_transition_on_the_forgotten_session():
    configure()
    warm()
    spike()  # level 1
    old = api._REGISTRY[KEY]

    runbound.clear(KEY)

    assert old.ladder_history[-1][3] == "cleared"
    assert old.ladder_history[-1][:2] == (1, 0)
    # the key is forgotten: a fresh session shares none of the old why/history
    assert runbound.session_status(KEY) is None
    turn(KEY)
    fresh_status = runbound.session_status(KEY)
    assert fresh_status["why"]["trigger"] is None
    assert fresh_status["history"] == []


# --- fail-open ------------------------------------------------------------


def test_a_broken_ladder_history_write_fails_open_for_the_host(monkeypatch):
    """The engine already catches a broken detector (see test_engine.py); this
    pins that guarantee to the new bookkeeping specifically, not just to the
    principle. A customer's agent must keep running even if this feature has
    a bug in it.
    """
    configure()
    warm()

    def _explode(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api.SessionState, "record_ladder_transition", _explode)

    spike()  # must not raise, even though the ladder bookkeeping is broken

    # the rest of runbound is unaffected: status is still readable
    assert runbound.session_status(KEY) is not None


def test_session_status_survives_a_malformed_trigger(monkeypatch):
    configure()
    warm()
    spike()
    # simulate a corrupted in-memory trigger (should never happen in practice)
    api._REGISTRY[KEY].spike_trigger = {"unexpected": "shape"}

    assert runbound.session_status(KEY) is None


# --- never creates a session, unknown key stays None ----------------------


def test_session_status_is_none_for_an_unknown_key_and_creates_nothing():
    configure()

    assert runbound.session_status("user:nobody") is None
    assert "user:nobody" not in api._REGISTRY


def test_session_status_is_none_before_init():
    assert runbound.session_status(KEY) is None
