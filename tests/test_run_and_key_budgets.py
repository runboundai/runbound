"""Run and key budgets, named as such.

``budget_usd`` is the *key*'s budget -- an identity that can live for
months, reused across many ``session()`` blocks -- and now optionally has a
``budget_window`` governing when its cumulative spend resets. ``run_budget_usd``
and ``run_max_total_tokens`` are the *run*'s own budget -- one ``session()``
block, reset fresh on every entry, independent of the key's own history.
``Decision.level`` says which of the two actually bound a refused call.
"""

import calendar
import datetime as dt

import pytest

import runbound
from runbound import api
from runbound import engine as engine_module
from runbound import state as state_module
from runbound.config import GuardrailConfig
from runbound.exceptions import GuardrailTripped

KEY = "user:8842"


@pytest.fixture(autouse=True)
def _pristine():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class FakeClock:
    """Stands in for the ``time`` module the api, engine and state read.

    ``wall`` and ``monotonic`` advance together from independent starting
    points, exactly the offset a real process has between the two clocks --
    ``advance()`` moves both by the same amount, and ``set_wall`` jumps the
    wall clock alone (a customer's ``budget_window="day"`` cares about the
    calendar, not about how much monotonic time has passed to get there).
    """

    def __init__(self, wall: float = 1_700_000_000.0, monotonic: float = 1_000.0) -> None:
        self._wall = wall
        self._monotonic = monotonic

    def time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._monotonic += seconds

    def set_wall(self, wall: float) -> None:
        self._wall = wall


@pytest.fixture()
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(api, "time", fake)
    monkeypatch.setattr(state_module, "time", fake)
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


def _utc_midnight_epoch(year: int, month: int, day: int) -> float:
    return calendar.timegm(dt.datetime(year, month, day, tzinfo=dt.timezone.utc).timetuple())


class FakeUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeResponse:
    def __init__(self, model=None, usage=None):
        self.model = model
        self.usage = usage or FakeUsage()


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        # Actual usage matches the stated cap exactly, so the real spend
        # settles at exactly what the reservation estimated -- the fake
        # provider's "worst case" always happens, which is what lets these
        # tests spend an exact, predictable number of dollars per call.
        completion_tokens = kwargs.get("max_tokens", 0)
        return FakeResponse(
            model=kwargs.get("model"),
            usage=FakeUsage(prompt_tokens=0, completion_tokens=completion_tokens),
        )


class FakeOpenAI:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions()


PRICE = {"m": (0.0, 1_000_000.0)}  # $1 per output token, $0/input


def spend(client, dollars: float, key: str = KEY) -> None:
    """Spend exactly ``dollars`` in one call, keyed, under an envelope-off
    reservation (a stated cap at $1/token means the estimate always equals
    the actual, so admission and the actual spend agree to the cent)."""
    with runbound.session(key):
        client.chat.completions.create(model="m", messages=[], max_tokens=int(round(dollars)))


# --- config validation -------------------------------------------------------


@pytest.mark.parametrize("value", ["fortnight", -1, 0, True])
def test_budget_window_rejects_bad_values(value):
    with pytest.raises(ValueError, match="budget_window"):
        GuardrailConfig(budget_usd=10.0, budget_window=value).validate()


def test_budget_window_requires_a_real_budget_usd():
    with pytest.raises(ValueError, match="budget_window"):
        GuardrailConfig(budget_window="day").validate()


@pytest.mark.parametrize("value", ["hour", "day", "month", 3600.0, 1])
def test_budget_window_accepts_every_documented_value(value):
    GuardrailConfig(budget_usd=10.0, budget_window=value).validate()  # must not raise


@pytest.mark.parametrize("value", [0, -1.0])
def test_run_budget_usd_must_be_positive_or_none(value):
    # Same shape and the same generic checker (_POSITIVE_LIMITS) as
    # budget_usd's own validation -- not stricter, not looser.
    with pytest.raises(ValueError, match="run_budget_usd"):
        GuardrailConfig(run_budget_usd=value).validate()


@pytest.mark.parametrize("value", [0, -1])
def test_run_max_total_tokens_must_be_positive_or_none(value):
    with pytest.raises(ValueError, match="run_max_total_tokens"):
        GuardrailConfig(run_max_total_tokens=value).validate()


def test_run_budget_usd_accepts_a_positive_number():
    GuardrailConfig(run_budget_usd=5.0).validate()  # must not raise


def test_run_max_total_tokens_accepts_a_positive_number():
    GuardrailConfig(run_max_total_tokens=1000).validate()  # must not raise


# --- run_budget_usd: fresh every session() entry ----------------------------


def test_run_budget_usd_resets_on_every_session_entry():
    runbound.init(run_budget_usd=2.0, custom_prices=PRICE, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())

    with runbound.session(KEY):
        client.chat.completions.create(model="m", messages=[], max_tokens=2)  # spends $2, at the run cap
        with pytest.raises(GuardrailTripped) as excinfo:
            client.chat.completions.create(model="m", messages=[], max_tokens=1)
        assert excinfo.value.decision.level == "run"

    with runbound.session(KEY):  # a fresh run: the $2 cap is available again
        client.chat.completions.create(model="m", messages=[], max_tokens=2)  # must not raise

    assert client.chat.completions.calls == 2


def test_run_budget_usd_and_budget_usd_are_independent_controls():
    """A key budget alone still behaves exactly as it always has; a run
    budget works with no budget_usd configured at all."""
    runbound.init(run_budget_usd=1.0, custom_prices=PRICE, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())

    with runbound.session(KEY):
        with pytest.raises(GuardrailTripped) as excinfo:
            client.chat.completions.create(model="m", messages=[], max_tokens=2)
    assert excinfo.value.decision.level == "run"
    assert excinfo.value.decision.evaluation["limit"] == 1.0


# --- Decision.level: run vs key ----------------------------------------------


def test_decision_level_names_the_tighter_budget(clock):
    runbound.init(
        run_budget_usd=5.0, budget_usd=20.0, budget_window="day",
        custom_prices=PRICE, on_anomaly="raise",
    )
    client = runbound.wrap(FakeOpenAI())

    with runbound.session(KEY):
        # $5 run cap is tighter than $20 (fresh) key cap: refused at "run".
        with pytest.raises(GuardrailTripped) as excinfo:
            client.chat.completions.create(model="m", messages=[], max_tokens=6)
    assert excinfo.value.decision.level == "run"


# --- the acceptance scenario: four runs spend the key dry, the fifth is
# refused at the key, and UTC midnight heals it ------------------------------


def test_run_budget_usd_5_key_budget_20_daily_window_acceptance(clock):
    runbound.init(
        run_budget_usd=5.0, budget_usd=20.0, budget_window="day",
        custom_prices=PRICE, on_anomaly="raise",
    )
    client = runbound.wrap(FakeOpenAI())
    clock.set_wall(_utc_midnight_epoch(2026, 6, 15) + 3600.0)  # 01:00 UTC, June 15

    # Four runs, each spending its own fresh $5 -- the key accumulates to $20.
    for _ in range(4):
        spend(client, 5.0)

    view = runbound.budget(KEY)
    assert view.spent == pytest.approx(20.0)
    assert view.remaining == pytest.approx(0.0)

    # The fifth run starts with a fresh $5 of its own (run_budget_usd is
    # unconditional on session() entry)...
    with runbound.session(KEY):
        # ...but the key has nothing left: refused at the key, not the run.
        with pytest.raises(GuardrailTripped) as excinfo:
            client.chat.completions.create(model="m", messages=[], max_tokens=1)
    assert excinfo.value.decision.level == "key"
    assert excinfo.value.decision.evaluation["limit"] == 20.0

    # UTC midnight: the key's window rolls over and heals, with no restart
    # and no clear() -- the customer's own $20 is back, and a sixth run's
    # fresh $5 stacks on top of it exactly as the first run's did.
    clock.set_wall(_utc_midnight_epoch(2026, 6, 16) + 1.0)
    view = runbound.budget(KEY)
    assert view.spent == pytest.approx(0.0)
    assert view.remaining == pytest.approx(20.0)

    spend(client, 5.0)  # the sixth run: must not raise
    # 4 successful spends before the window rolled, the 5th run's attempt
    # refused before it ever reached the fake, and this 6th run's own spend:
    # 5 calls the provider actually saw.
    assert client.chat.completions.calls == 5


# --- budget_window: the clock-edge table -------------------------------------


@pytest.mark.parametrize(
    "advance_seconds, still_same_window",
    [
        (0.0, True),
        (3599.0, True),  # one second short of the hour: still inside it
        (3600.0, False),  # exactly the hour: the next bucket
        (7200.0, False),
    ],
)
def test_hourly_window_clock_edges(clock, advance_seconds, still_same_window):
    runbound.init(budget_usd=10.0, budget_window="hour", custom_prices=PRICE, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    clock.set_wall(_utc_midnight_epoch(2026, 6, 15))

    spend(client, 10.0)  # exhausts the hour's $10
    clock.advance(advance_seconds)

    view = runbound.budget(KEY)
    if still_same_window:
        assert view.spent == pytest.approx(10.0)
    else:
        assert view.spent == pytest.approx(0.0)


def test_a_rolling_seconds_window_heals_after_exactly_that_many_seconds(clock):
    runbound.init(budget_usd=10.0, budget_window=100.0, custom_prices=PRICE, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())

    spend(client, 10.0)
    assert runbound.budget(KEY).spent == pytest.approx(10.0)

    clock.advance(99.0)
    assert runbound.budget(KEY).spent == pytest.approx(10.0)  # not yet

    clock.advance(1.0)  # exactly 100s since the window's own start
    assert runbound.budget(KEY).spent == pytest.approx(0.0)


def test_a_restart_starts_a_fresh_window():
    """In memory only: a fresh Engine (what a process restart gives you) has
    no marker at all, so the very first read establishes one rather than
    inheriting whatever the previous process's window was mid-way through."""
    runbound.init(budget_usd=10.0, budget_window="day", custom_prices=PRICE, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    spend(client, 10.0)
    assert runbound.budget(KEY).spent == pytest.approx(10.0)

    runbound.init(budget_usd=10.0, budget_window="day", custom_prices=PRICE, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    assert runbound.budget(KEY) is None  # a fresh registry: the key itself is gone
    spend(client, 3.0)
    assert runbound.budget(KEY).spent == pytest.approx(3.0)


# --- the 24h-no-window warning ------------------------------------------------


def test_a_keyed_session_over_24h_old_with_no_window_warns_once(clock, caplog):
    import logging

    runbound.init(budget_usd=10.0, on_anomaly="raise")  # no budget_window
    caplog.set_level(logging.WARNING, logger="runbound")

    with runbound.session(KEY):
        pass
    assert not any("budget_window" in r.message for r in caplog.records)

    clock.advance(24 * 60 * 60.0 + 1.0)
    caplog.clear()
    with runbound.session(KEY):
        pass
    warnings = [r for r in caplog.records if "budget_window" in r.message]
    assert len(warnings) == 1

    caplog.clear()
    with runbound.session(KEY):  # only once
        pass
    assert not any("budget_window" in r.message for r in caplog.records)


def test_no_warning_with_a_window_configured(clock, caplog):
    import logging

    runbound.init(budget_usd=10.0, budget_window="day", on_anomaly="raise")
    caplog.set_level(logging.WARNING, logger="runbound")
    clock.advance(24 * 60 * 60.0 + 1.0)

    with runbound.session(KEY):
        pass
    assert not any("budget_window" in r.message for r in caplog.records)


def test_no_warning_for_the_default_unkeyed_session(clock, caplog):
    import logging

    runbound.init(budget_usd=10.0, on_anomaly="raise")
    caplog.set_level(logging.WARNING, logger="runbound")
    clock.advance(24 * 60 * 60.0 + 1.0)

    runbound.record_call("m", 0, 0)  # touches the default session, never a key
    assert not any("budget_window" in r.message for r in caplog.records)


# --- run_max_total_tokens: the post-call wall --------------------------------


def test_run_max_total_tokens_wall_fires_and_names_the_run_level():
    """Mirrors max_total_tokens's own post-call wall (budget_admission=False,
    or simply no stated cap): a run's own token ceiling, independent of any
    key-level max_total_tokens."""
    runbound.init(run_max_total_tokens=100, on_anomaly="raise")

    with runbound.session(KEY):
        with pytest.raises(GuardrailTripped) as excinfo:
            runbound.record_call("gpt-4o", 60, 60)  # 120 tokens, over the run's 100

    assert excinfo.value.anomaly.detector == "budget"
    assert excinfo.value.decision.level == "run"
    assert excinfo.value.decision.evaluation["limit"] == 100


def test_run_max_total_tokens_resets_every_entry_but_key_level_does_not():
    runbound.init(run_max_total_tokens=100, max_total_tokens=150, on_anomaly="raise")

    with runbound.session(KEY):
        runbound.record_call("gpt-4o", 40, 40)  # 80 tokens: under both caps
    with runbound.session(KEY):  # fresh run: 80 key-cumulative, 0 run-fresh
        # 80 more: the run's own fresh total is 80 (under its 100), but the
        # key's cumulative reaches 160 -- over the key's own 150.
        with pytest.raises(GuardrailTripped) as excinfo:
            runbound.record_call("gpt-4o", 40, 40)
    assert excinfo.value.decision.level == "key"
