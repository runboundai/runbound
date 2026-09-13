"""Tests for T136 — the opt-in admission budget check (``budget_admission``).

Three layers, bottom-up:

1. ``Engine.admit`` in isolation — table-driven over (remaining budget,
   request chars, output cap, price known), plus the properties the launch
   plan calls out by name: a refusal never latches, leaves totals unchanged,
   reaches observers with ``reacted="door"``, and alerts once per session.
2. The wrapper-level readers T136 adds: ``request_output_cap`` in both
   provider modules, and ``call_before``'s new ``request`` parameter and its
   tolerance for hooks that predate it.
3. End to end through ``runbound.wrap()`` — sync, async and streamed calls —
   proving the refusal happens *before* the underlying client is touched at
   all, and that turning ``budget_admission`` off leaves every one of those
   paths exactly as it was (the existing suites are that proof; this module
   adds one direct check per calling convention).
"""

import asyncio
import logging

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import CircuitOpen, GuardrailTripped
from runbound.state import SessionState
from runbound.wrappers import anthropic_wrapper, call_before, openai_wrapper


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class RecordingObserver:
    def __init__(self) -> None:
        self.events = []
        self.anomalies = []

    def on_event(self, session, event) -> None:
        self.events.append(event)

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.anomalies.append((anomaly, reacted))


def engine(config: GuardrailConfig, observers=None) -> Engine:
    config.validate()
    return Engine(config, observers=list(observers) if observers else [])


def messages(chars: int) -> list[dict]:
    return [{"role": "user", "content": "x" * chars}]


# --- Engine.admit: circuit and unpriced-model, ported unchanged -------------


def test_admit_raises_circuit_open_when_the_circuit_is_open():
    config = GuardrailConfig(on_provider_failure="open", circuit_failure_threshold=1)
    eng = engine(config)
    session = SessionState("s1")
    eng.circuit.record_failure("openai@default")

    with pytest.raises(CircuitOpen):
        eng.admit(session, "openai@default", "gpt-4o", None)


def test_admit_refuses_an_unpriced_model_under_refuse():
    config = GuardrailConfig(on_unpriced_model="refuse")
    eng = engine(config)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(session, "openai@default", "mystery-model", None)

    assert excinfo.value.anomaly.detector == "budget"
    assert excinfo.value.anomaly.details["reason"] == "unpriced_model"


def test_admit_is_a_noop_with_a_known_model_circuit_closed_and_admission_off():
    config = GuardrailConfig()
    eng = engine(config)
    session = SessionState("s1")

    eng.admit(session, "openai@default", "gpt-4o", {"messages": messages(1000)})  # no raise


# --- budget_admission: off by default, opt-in --------------------------------


def test_budget_admission_defaults_to_off():
    assert GuardrailConfig().budget_admission is False


def test_admission_output_tokens_defaults_to_1024():
    assert GuardrailConfig().admission_output_tokens == 1024


def test_with_admission_off_a_call_that_would_never_fit_is_still_admitted():
    """The default: budget_admission=False skips the estimate entirely."""
    config = GuardrailConfig(budget_usd=0.0001)  # already effectively zero
    eng = engine(config)
    session = SessionState("s1")

    eng.admit(
        session, "openai@default", "gpt-4o", {"messages": messages(1_000_000)}
    )  # no raise: the estimate never runs


def test_admission_is_a_noop_without_budget_usd_even_when_enabled():
    config = GuardrailConfig(budget_admission=True)  # no budget_usd at all
    eng = engine(config)
    session = SessionState("s1")

    eng.admit(
        session, "openai@default", "gpt-4o", {"messages": messages(1_000_000)}
    )  # nothing to estimate against


# --- table-driven: (remaining, chars, output cap, price known) --------------

# gpt-4o: (2.50, 10.00) usd per 1M (input, output) tokens.
_GPT4O_IN, _GPT4O_OUT = 2.50, 10.00


def _estimate(chars: int, output_tokens: int) -> float:
    input_tokens = -(-chars // 4)  # ceil(chars / 4), the estimator's own rule
    return (input_tokens / 1_000_000) * _GPT4O_IN + (output_tokens / 1_000_000) * _GPT4O_OUT


@pytest.mark.parametrize(
    "budget_usd, chars, output_cap, expect_refused",
    [
        # Plenty of budget, small request, explicit cap: fits.
        (10.0, 40, 100, False),
        # Tiny budget, large request, explicit cap: does not fit.
        (0.001, 40_000, 4000, True),
        # Tiny budget, small request, no cap at all (falls back to
        # admission_output_tokens=1024 at $10/1M = $0.01024): does not fit.
        (0.001, 40, None, True),
        # Generous budget, no cap: the 1024-token fallback still fits.
        (1.0, 40, None, False),
        # Exactly at the boundary: admitted (estimate > remaining refuses,
        # not >=).
        (None, 40, 100, False),  # budget_usd filled in by the test below
    ],
)
def test_admission_table(budget_usd, chars, output_cap, expect_refused):
    if budget_usd is None:
        budget_usd = _estimate(chars, output_cap)  # exactly the estimate
    config = GuardrailConfig(budget_usd=budget_usd, budget_admission=True)
    observer = RecordingObserver()
    eng = engine(config, observers=[observer])
    session = SessionState("s1")
    request = {"messages": messages(chars)}
    if output_cap is not None:
        request["max_tokens"] = output_cap

    if expect_refused:
        with pytest.raises(GuardrailTripped) as excinfo:
            eng.admit(session, "openai@default", "gpt-4o", request)
        anomaly = excinfo.value.anomaly
        assert anomaly.detector == "budget"
        assert anomaly.details["rule"] == "admission"
        assert anomaly.details["estimated_cost_usd"] > anomaly.details["remaining_usd"]
        assert observer.anomalies == [(anomaly, "door")]
    else:
        eng.admit(session, "openai@default", "gpt-4o", request)  # no raise
        assert observer.anomalies == []


# --- a refused admission: never latches, totals unchanged, unlatched -------


def test_a_refused_admission_never_latches():
    config = GuardrailConfig(
        budget_usd=0.0001, budget_admission=True, on_anomaly="raise", on_trip="latch"
    )
    eng = engine(config)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        eng.admit(session, "openai@default", "gpt-4o", {"messages": messages(4000)})

    assert session.tripped_by is None
    assert session.tripped_at is None


def test_a_refused_admission_leaves_totals_unchanged():
    config = GuardrailConfig(budget_usd=0.0001, budget_admission=True)
    eng = engine(config)
    session = SessionState("s1")
    session.total_cost_usd = 0.00005  # some prior, legitimate spend

    with pytest.raises(GuardrailTripped):
        eng.admit(session, "openai@default", "gpt-4o", {"messages": messages(4000)})

    assert session.total_cost_usd == 0.00005
    assert session.total_tokens == 0


def test_a_cheaper_call_fits_right_after_a_refusal():
    """The whole point of never latching: a cheaper call may still go out."""
    config = GuardrailConfig(budget_usd=0.001, budget_admission=True)
    eng = engine(config)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        eng.admit(
            session,
            "openai@default",
            "gpt-4o",
            {"messages": messages(40_000), "max_tokens": 100},
        )

    eng.admit(
        session, "openai@default", "gpt-4o", {"messages": messages(4), "max_tokens": 1}
    )  # fits, no raise


# --- alerted once per session -------------------------------------------


def test_an_admission_refusal_is_alerted_once_per_session():
    config = GuardrailConfig(budget_usd=0.0001, budget_admission=True)
    observer = RecordingObserver()
    eng = engine(config, observers=[observer])
    session = SessionState("s1")

    for _ in range(3):
        with pytest.raises(GuardrailTripped):
            eng.admit(session, "openai@default", "gpt-4o", {"messages": messages(4000)})

    assert len(observer.anomalies) == 1  # refused every time; alerted once
    assert observer.anomalies[0][1] == "door"


def test_an_admission_refusal_and_a_postcall_budget_trip_both_alert():
    """The dedup key includes ``rule``, so admission and the post-call
    ``budget`` detector never shadow each other in the same session."""
    config = GuardrailConfig(budget_usd=0.0001, budget_admission=True, on_anomaly="warn")
    observer = RecordingObserver()
    eng = engine(config, observers=[observer])
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped):
        eng.admit(session, "openai@default", "gpt-4o", {"messages": messages(4000)})

    from runbound.events import Event

    eng.process(session, Event(kind="llm_call", ts=1.0, step=1, cost_usd=1.0))

    assert [reacted for _, reacted in observer.anomalies] == ["door", "warn"]
    assert [a.detector for a, _ in observer.anomalies] == ["budget", "budget"]


# --- unknown price: skip admission, warn once per model ---------------------


def test_an_unpriced_model_skips_admission_and_warns_once(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(budget_usd=0.0001, budget_admission=True)
    eng = engine(config)
    session = SessionState("s1")
    request = {"messages": messages(1_000_000)}  # would refuse if priced

    eng.admit(session, "openai@default", "mystery-model", request)  # no raise
    eng.admit(session, "openai@default", "mystery-model", request)  # still no raise

    warnings = [r for r in caplog.records if "mystery-model" in r.getMessage()]
    assert len(warnings) == 1


def test_an_unpriced_model_warns_once_per_model_not_globally(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(budget_usd=0.0001, budget_admission=True)
    eng = engine(config)
    session = SessionState("s1")

    eng.admit(session, "openai@default", "mystery-a", {"messages": messages(1000)})
    eng.admit(session, "openai@default", "mystery-b", {"messages": messages(1000)})

    assert any("mystery-a" in r.getMessage() for r in caplog.records)
    assert any("mystery-b" in r.getMessage() for r in caplog.records)


def test_custom_prices_are_used_for_the_admission_estimate():
    config = GuardrailConfig(
        budget_usd=0.0005,
        budget_admission=True,
        custom_prices={"my-model": (1000.0, 1000.0)},  # deliberately expensive
    )
    eng = engine(config)
    session = SessionState("s1")

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(session, "openai@default", "my-model", {"messages": messages(40)})

    assert excinfo.value.anomaly.details["rule"] == "admission"


# --- fleet offsets fold into "remaining", like the post-call check ----------


def test_the_fleet_spend_offset_counts_toward_remaining():
    config = GuardrailConfig(budget_usd=0.01, budget_admission=True)
    eng = engine(config)
    session = SessionState("s1")
    session.spend_offset_usd = 0.0099  # almost nothing left, fleet-wide

    with pytest.raises(GuardrailTripped):
        eng.admit(session, "openai@default", "gpt-4o", {"messages": messages(4000)})


# --- request_output_cap: both providers' own readers ------------------------


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, None),
        ({"max_tokens": 500}, 500),
        ({"max_completion_tokens": 300}, 300),
        ({"max_output_tokens": 200}, 200),
        ({"max_tokens": 0}, None),  # non-positive: not a real cap
        ({"max_tokens": -5}, None),
        ({"max_tokens": "not a number"}, None),
        ({"max_tokens": None, "max_completion_tokens": 42}, 42),
    ],
)
def test_openai_request_output_cap(kwargs, expected):
    assert openai_wrapper.request_output_cap(kwargs) == expected


def test_openai_request_output_cap_prefers_max_tokens_first():
    kwargs = {"max_tokens": 111, "max_completion_tokens": 222, "max_output_tokens": 333}
    assert openai_wrapper.request_output_cap(kwargs) == 111


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, None),
        ({"max_tokens": 1024}, 1024),
        ({"max_tokens": 0}, None),
        ({"max_tokens": -1}, None),
        ({"max_tokens": "nope"}, None),
        # Anthropic never carries the OpenAI-only fields; this reader must
        # not be fooled into using them.
        ({"max_completion_tokens": 500}, None),
    ],
)
def test_anthropic_request_output_cap(kwargs, expected):
    assert anthropic_wrapper.request_output_cap(kwargs) == expected


# --- call_before: passes the request through, tolerant of older hooks ------


class RequestCapturingHooks:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def before(self, provider, model=None, request=None) -> None:
        self.calls.append((provider, model, request))


class ModelOnlyHooks:
    """A hook written before T136 added ``request``."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def before(self, provider, model=None) -> None:
        self.calls.append((provider, model))


class ProviderOnlyHooks:
    """A hook written before the model parameter even existed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def before(self, provider) -> None:
        self.calls.append(provider)


def test_call_before_passes_the_request_when_hooks_accept_it():
    hooks = RequestCapturingHooks()
    request = {"messages": [{"role": "user", "content": "hi"}]}

    call_before(hooks, "openai@test", "gpt-4o", request)

    assert hooks.calls == [("openai@test", "gpt-4o", request)]


def test_call_before_falls_back_for_a_model_only_hook():
    hooks = ModelOnlyHooks()

    call_before(hooks, "openai@test", "gpt-4o", {"messages": []})

    assert hooks.calls == [("openai@test", "gpt-4o")]


def test_call_before_falls_back_all_the_way_for_a_provider_only_hook():
    hooks = ProviderOnlyHooks()

    call_before(hooks, "openai@test", "gpt-4o", {"messages": []})

    assert hooks.calls == ["openai@test"]


def test_call_before_works_with_no_request_at_all():
    """The @runbound.llm decorator has no kwargs dict to pass."""
    hooks = RequestCapturingHooks()

    call_before(hooks, "openai@test", "gpt-4o")

    assert hooks.calls == [("openai@test", "gpt-4o", None)]


# --- end to end: sync, async, stream — refused before the client is touched


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, model=None, usage=None):
        self.model = model
        self.usage = usage


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(
            model=kwargs.get("model"),
            usage=FakeUsage(prompt_tokens=10, completion_tokens=10),
        )


class FakeOpenAISync:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions()


class FakeAsyncCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(
            model=kwargs.get("model"),
            usage=FakeUsage(prompt_tokens=10, completion_tokens=10),
        )


class FakeOpenAIAsync:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeAsyncCompletions()


class FakeStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class FakeStreamingCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return FakeStream([])


class FakeOpenAIStreaming:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeStreamingCompletions()


_HUGE_REQUEST = [{"role": "user", "content": "x" * 4_000_000}]  # ~1M estimated tokens


def test_admission_refuses_a_sync_call_before_the_client_is_touched():
    runbound.init(budget_usd=0.01, budget_admission=True, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAISync())

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=_HUGE_REQUEST)

    assert excinfo.value.anomaly.details["rule"] == "admission"
    assert client.chat.completions.calls == 0  # the provider was never called


def test_admission_refuses_an_async_call_before_the_client_is_touched():
    runbound.init(budget_usd=0.01, budget_admission=True, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAIAsync())

    async def run():
        await client.chat.completions.create(model="gpt-4o", messages=_HUGE_REQUEST)

    with pytest.raises(GuardrailTripped) as excinfo:
        asyncio.run(run())

    assert excinfo.value.anomaly.details["rule"] == "admission"
    assert client.chat.completions.calls == 0


def test_admission_refuses_a_streamed_call_before_the_client_is_touched():
    runbound.init(budget_usd=0.01, budget_admission=True, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAIStreaming())

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(
            model="gpt-4o", messages=_HUGE_REQUEST, stream=True
        )

    assert excinfo.value.anomaly.details["rule"] == "admission"
    assert client.chat.completions.calls == 0


def test_admission_off_a_huge_sync_call_goes_through_unrefused():
    """Without ``budget_admission``, even a huge request reaches the client —
    admission never runs, so nothing at the door has a reason to refuse it.
    (This fake reports a small fixed usage regardless of request size, so the
    point here is arrival at the client, not a post-call trip.)
    """
    runbound.init(budget_usd=0.01, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAISync())

    client.chat.completions.create(model="gpt-4o", messages=_HUGE_REQUEST)  # no raise

    assert client.chat.completions.calls == 1


def test_admission_refusal_never_reserves_an_inflight_slot():
    """No leaked slot: admission runs, and refuses, before the in-flight cap
    reservation the api's own ``_Hooks.before`` makes right after it."""
    runbound.init(
        budget_usd=0.01,
        budget_admission=True,
        max_inflight_calls=1,
        on_anomaly="raise",
    )
    client = runbound.wrap(FakeOpenAISync())

    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=_HUGE_REQUEST)

    assert runbound.inflight_calls("openai") == 0


# --- config validation --------------------------------------------------


@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_budget_admission_must_be_a_bool(value):
    with pytest.raises(ValueError, match="budget_admission"):
        GuardrailConfig(budget_admission=value).validate()


@pytest.mark.parametrize("value", [0, -1, 1.5, "1024", True, False])
def test_admission_output_tokens_must_be_a_positive_int(value):
    with pytest.raises(ValueError, match="admission_output_tokens"):
        GuardrailConfig(admission_output_tokens=value).validate()


def test_admission_output_tokens_accepts_a_positive_int():
    GuardrailConfig(admission_output_tokens=1).validate()  # must not raise
