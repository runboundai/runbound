"""Tests for Wave 24, T59a: what happens to a model with no known price.

Three layers, tested from the bottom up:

1. ``pricing.price_for``/``pricing.price_call`` — pure functions, no engine.
2. ``GuardrailConfig`` validation of the five new fields ``on_unpriced_model``
   cares about.
3. The API surface: ``_record_llm_call`` (what ``record_call()``,
   ``@runbound.llm`` and a wrapped client all funnel through),
   ``_Hooks.before`` refusing at the door, and ``_Hooks.abandoned`` recording
   a partial call — the last because it shares the same ``priced``/``partial``
   event-field machinery this module is about, even though its dedicated
   integration test lives with T60's stream work.
"""

import asyncio
import logging

import pytest

import runbound
from runbound import api, pricing
from runbound.config import GuardrailConfig
from runbound.exceptions import GuardrailTripped
from runbound.pricing import price_call, price_for


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def unwarned():
    """A process that has not yet used up its one-time unpriced warnings."""
    before_zero, before_refuse = pricing._WARNED_MODELS, pricing._REFUSE_RECORDED_MODELS
    pricing._WARNED_MODELS = set()
    pricing._REFUSE_RECORDED_MODELS = set()
    yield
    pricing._WARNED_MODELS = before_zero
    pricing._REFUSE_RECORDED_MODELS = before_refuse


class EventRecorder:
    """A detector that records every event the engine hands it."""

    name = "recorder"

    def __init__(self) -> None:
        self.events = []

    def check(self, state, event, config):
        self.events.append(event)
        return None


def recorder() -> EventRecorder:
    spy = EventRecorder()
    api._ENGINE.detectors.insert(0, spy)
    return spy


class RecordingObserver:
    def __init__(self) -> None:
        self.sent = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- pricing.price_for / price_call -----------------------------------------


def test_price_for_finds_a_static_price():
    # T139: gpt-4o now publishes a cached-input rate, so price_for returns
    # the 3-tuple (in, out, cached_in) rather than a bare (in, out) pair.
    assert price_for("gpt-4o") == (2.50, 10.00, 1.25)


def test_price_for_prefers_custom_prices():
    assert price_for("gpt-4o", {"gpt-4o": (1.0, 2.0)}) == (1.0, 2.0)


@pytest.mark.parametrize("model", [None, "", "totally-unknown-model", 42])
def test_price_for_is_none_for_unpriced_or_invalid(model):
    assert price_for(model) is None


def test_price_call_prices_a_known_model_exactly_and_is_not_estimated(unwarned):
    cost, estimated = price_call("gpt-4o", 1_000_000, 1_000_000)
    assert cost == pytest.approx(12.50)
    assert estimated is False


def test_price_call_prices_cached_tokens_at_the_cached_rate(unwarned):
    # 1000 of 1200 input tokens cached (T139's own hand-computed example).
    cost, estimated = price_call("gpt-4o", 1200, 50, tokens_cached_in=1000)
    price_in, price_out, price_cached_in = price_for("gpt-4o")
    expected = (200 / 1e6) * price_in + (1000 / 1e6) * price_cached_in + (50 / 1e6) * price_out
    assert cost == pytest.approx(expected)
    assert estimated is False


def test_price_call_prices_a_cache_write_at_its_own_premium(unwarned):
    # 200 fresh + 1000 cache-read + 300 cache-write, 80 out.
    cost, estimated = price_call(
        "claude-sonnet-4-5", 1500, 80, tokens_cached_in=1000, tokens_cache_write_in=300
    )
    price_in, price_out, price_cached_in, price_write_in = price_for("claude-sonnet-4-5")
    expected = (
        (200 / 1e6) * price_in
        + (1000 / 1e6) * price_cached_in
        + (300 / 1e6) * price_write_in
        + (80 / 1e6) * price_out
    )
    assert cost == pytest.approx(expected)
    assert estimated is False


def test_price_call_zero_mode_is_free_and_warns_once(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        first = price_call("mystery-model", 1_000, 1_000)
        second = price_call("mystery-model", 1_000, 1_000)

    assert first == (0.0, False)
    assert second == (0.0, False)
    assert len(_warnings(caplog)) == 1
    assert "mystery-model" in _warnings(caplog)[0]


def test_price_call_estimate_mode_prices_from_the_fallback_pair(unwarned):
    cost, estimated = price_call(
        "mystery-model",
        1_000_000,
        1_000_000,
        on_unpriced_model="estimate",
        unpriced_price_per_1m_usd=(1.0, 2.0),
    )
    assert cost == pytest.approx(3.0)
    assert estimated is True


def test_price_call_estimate_mode_without_a_pair_falls_back_to_zero(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        cost, estimated = price_call(
            "mystery-model", 1_000, 1_000, on_unpriced_model="estimate"
        )

    assert (cost, estimated) == (0.0, False)
    assert _warnings(caplog)  # the ordinary "zero" warning still fires


def test_price_call_refuse_mode_with_a_pair_estimates_and_warns_once(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        first = price_call(
            "mystery-model",
            1_000_000,
            1_000_000,
            on_unpriced_model="refuse",
            unpriced_price_per_1m_usd=(1.0, 2.0),
        )
        second = price_call(
            "mystery-model",
            1_000_000,
            1_000_000,
            on_unpriced_model="refuse",
            unpriced_price_per_1m_usd=(1.0, 2.0),
        )

    assert first == second == (3.0, True)
    assert len(_warnings(caplog)) == 1
    assert "refuse" in _warnings(caplog)[0]


def test_price_call_refuse_mode_without_a_pair_is_free_and_warns_once(unwarned, caplog):
    with caplog.at_level(logging.WARNING, logger="runbound"):
        cost, estimated = price_call("mystery-model", 1_000, 1_000, on_unpriced_model="refuse")

    assert (cost, estimated) == (0.0, False)
    assert len(_warnings(caplog)) == 1


def test_price_call_never_raises_on_bad_input():
    assert price_call(object(), "nope", None) == (0.0, False)  # type: ignore[arg-type]


# --- config validation -------------------------------------------------------


def test_on_unpriced_model_defaults_to_zero():
    assert GuardrailConfig().on_unpriced_model == "zero"


def test_on_unpriced_model_rejects_unknown_mode():
    with pytest.raises(ValueError, match="on_unpriced_model"):
        GuardrailConfig(on_unpriced_model="ignore").validate()


def test_estimate_mode_requires_a_fallback_pair():
    with pytest.raises(ValueError, match="unpriced_price_per_1m_usd"):
        GuardrailConfig(on_unpriced_model="estimate").validate()


def test_estimate_mode_accepts_a_valid_pair():
    GuardrailConfig(
        on_unpriced_model="estimate", unpriced_price_per_1m_usd=(1.0, 2.0)
    ).validate()


@pytest.mark.parametrize("mode", ["zero", "refuse"])
def test_zero_and_refuse_do_not_require_a_pair(mode):
    GuardrailConfig(on_unpriced_model=mode).validate()


@pytest.mark.parametrize(
    "pair", [(1.0,), (1.0, 2.0, 3.0), (-1.0, 2.0), ("a", "b"), (True, 2.0), [1.0, 2.0]]
)
def test_unpriced_price_pair_must_be_a_2_tuple_of_non_negative_numbers(pair):
    with pytest.raises(ValueError, match="unpriced_price_per_1m_usd"):
        GuardrailConfig(unpriced_price_per_1m_usd=pair).validate()


def test_unpriced_price_pair_none_is_fine_outside_estimate_mode():
    GuardrailConfig(unpriced_price_per_1m_usd=None).validate()


# --- _record_llm_call: zero / estimate marking --------------------------------


def test_zero_mode_records_a_free_call_with_no_priced_marker(unwarned):
    runbound.init()
    spy = recorder()

    runbound.record_call("mystery-model", 1_000, 1_000)

    event = spy.events[-1]
    assert event.cost_usd == 0.0
    assert event.priced is None


def test_estimate_mode_prices_and_marks_the_event(unwarned):
    runbound.init(on_unpriced_model="estimate", unpriced_price_per_1m_usd=(1.0, 2.0))
    spy = recorder()

    runbound.record_call("mystery-model", 1_000_000, 1_000_000)

    event = spy.events[-1]
    assert event.cost_usd == pytest.approx(3.0)
    assert event.priced == "estimated"


def test_estimate_mode_does_not_mark_a_priced_model(unwarned):
    runbound.init(on_unpriced_model="estimate", unpriced_price_per_1m_usd=(1.0, 2.0))
    spy = recorder()

    runbound.record_call("gpt-4o", 1_000_000, 1_000_000)

    event = spy.events[-1]
    assert event.cost_usd == pytest.approx(12.50)
    assert event.priced is None


# --- refuse: the door (_Hooks.before) ----------------------------------------


def test_before_refuses_an_unpriced_model_when_configured():
    runbound.init(on_unpriced_model="refuse")

    with pytest.raises(GuardrailTripped) as excinfo:
        api._HOOKS.before("openai", "mystery-model")

    assert excinfo.value.anomaly.detector == "budget"
    assert excinfo.value.anomaly.details == {
        "reason": "unpriced_model",
        "model": "mystery-model",
    }


def test_before_ignores_on_anomaly_and_never_latches():
    runbound.init(on_unpriced_model="refuse", on_anomaly="warn")

    with pytest.raises(GuardrailTripped):
        api._HOOKS.before("openai", "mystery-model")

    assert runbound.current_session().tripped_by is None
    # a later, priced call still goes through normally
    api._HOOKS.before("openai", "gpt-4o")


def test_before_does_not_refuse_a_priced_model():
    runbound.init(on_unpriced_model="refuse")
    api._HOOKS.before("openai", "gpt-4o")  # does not raise


@pytest.mark.parametrize("model", [None, ""])
def test_before_skips_the_refuse_check_without_a_model(model):
    runbound.init(on_unpriced_model="refuse")
    api._HOOKS.before("openai", model)  # does not raise: nothing to price yet


def test_before_still_works_when_called_with_one_argument():
    """Backward compatibility: a wrapper that has not been updated to pass
    `model=` yet must keep working — refuse simply cannot fire for it."""
    runbound.init(on_unpriced_model="refuse")
    api._HOOKS.before("openai")  # does not raise, and does not raise TypeError


def test_before_alerts_once_per_model_per_process():
    runbound.init(on_unpriced_model="refuse")
    observer = RecordingObserver()
    api._ENGINE.observers.append(observer)

    for _ in range(3):
        with pytest.raises(GuardrailTripped):
            api._HOOKS.before("openai", "mystery-model")

    with pytest.raises(GuardrailTripped):
        api._HOOKS.before("openai", "another-mystery-model")

    assert len(observer.sent) == 2  # one per distinct model, not per call


def test_before_refuses_regardless_of_on_anomaly_callback_mode():
    seen = []
    runbound.init(
        on_unpriced_model="refuse", on_anomaly="callback", callback=seen.append
    )

    with pytest.raises(GuardrailTripped):
        api._HOOKS.before("openai", "mystery-model")

    assert seen == []  # the callback is never invoked; the door raises directly


# --- refuse: @runbound.llm applies the same check before the body ---------


def test_llm_decorator_refuses_before_the_body_runs():
    runbound.init(on_unpriced_model="refuse")
    ran = []

    @runbound.llm(model="mystery-model")
    def infer():
        ran.append(1)
        return "should not happen"

    with pytest.raises(GuardrailTripped):
        infer()

    assert ran == []


def test_async_llm_decorator_refuses_before_the_body_runs():
    runbound.init(on_unpriced_model="refuse")
    ran = []

    @runbound.llm(model="mystery-model")
    async def infer():
        ran.append(1)
        return "should not happen"

    async def drive():
        with pytest.raises(GuardrailTripped):
            await infer()

    asyncio.run(drive())
    assert ran == []


def test_llm_decorator_runs_normally_for_a_priced_model_under_refuse():
    runbound.init(on_unpriced_model="refuse")

    @runbound.llm(model="gpt-4o", tokens=lambda r: (10, 5))
    def infer():
        return "ok"

    assert infer() == "ok"


# --- refuse: record_call() is after the fact ---------------------------------


def test_record_call_under_refuse_prices_zero_without_a_fallback_pair(unwarned, caplog):
    runbound.init(on_unpriced_model="refuse")
    spy = recorder()

    with caplog.at_level(logging.WARNING, logger="runbound"):
        runbound.record_call("mystery-model", 1_000, 1_000)
        runbound.record_call("mystery-model", 1_000, 1_000)

    event = spy.events[-1]
    assert event.cost_usd == 0.0
    assert event.priced is None
    assert len([w for w in _warnings(caplog) if "mystery-model" in w]) == 1


def test_record_call_under_refuse_prices_from_the_fallback_pair_when_given(unwarned):
    runbound.init(
        on_unpriced_model="refuse", unpriced_price_per_1m_usd=(1.0, 2.0)
    )
    spy = recorder()

    runbound.record_call("mystery-model", 1_000_000, 1_000_000)

    event = spy.events[-1]
    assert event.cost_usd == pytest.approx(3.0)
    assert event.priced == "estimated"


# --- abandoned(): the hook _Hooks.abandoned implements for T60's finalizer --


def test_abandoned_records_one_partial_call():
    runbound.init()
    spy = recorder()

    api._HOOKS.abandoned("gpt-4o", 100, 50, 1.5, "openai", False)

    event = spy.events[-1]
    assert event.kind == "llm_call"
    assert event.partial is True
    assert event.tokens_estimated is False
    assert (event.tokens_in, event.tokens_out) == (100, 50)


def test_abandoned_marks_estimated_tokens_when_told_to():
    runbound.init()
    spy = recorder()

    api._HOOKS.abandoned("gpt-4o", 10, 3, 0.2, "openai", True)

    assert spy.events[-1].tokens_estimated is True


def test_abandoned_trips_a_budget_and_latches_but_never_raises():
    runbound.init(budget_usd=0.0001, on_anomaly="raise")

    api._HOOKS.abandoned("gpt-4o", 1_000_000, 1_000_000, 1.0, "openai", False)  # no raise

    assert runbound.current_session().tripped_by is not None
    assert runbound.current_session().tripped_by.detector == "budget"


def test_abandoned_never_calls_success_or_error(monkeypatch):
    runbound.init()
    calls = []
    monkeypatch.setattr(api._HOOKS, "success", lambda provider: calls.append(("success", provider)))
    monkeypatch.setattr(api._HOOKS, "error", lambda *a: calls.append(("error", a)))

    api._HOOKS.abandoned("gpt-4o", 10, 5, 0.1, "openai", False)

    assert calls == []
