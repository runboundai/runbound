"""A `stopped` posture actually stops model calls.

Before this, only a tool's declared capability classes were judged against
the posture (``Engine._admit_action``); a model call had no posture check at
all, so a process narrowed to ``stopped`` by hand still let the provider be
called. This is the reproduction: put the process (or the session) in
``stopped`` and prove a wrapped client's fake provider records zero calls,
across every calling convention, while every other posture keeps serving
model calls exactly as the postures table says.
"""

import asyncio

import pytest

import runbound
from runbound import api
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import GuardrailTripped
from runbound.state import SessionState, make_posture_state


@pytest.fixture(autouse=True)
def _pristine():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def engine(config: GuardrailConfig | None = None) -> Engine:
    config = config or GuardrailConfig()
    config.validate()
    return Engine(config, observers=[])


# --- Engine.admit, in isolation ---------------------------------------------


def test_admit_refuses_a_model_call_under_a_manual_stopped_posture():
    eng = engine()
    session = SessionState("s1")
    session.posture = make_posture_state("stopped", "manual", "manual")

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(session, "openai@default", "gpt-4o", None)

    assert excinfo.value.anomaly.detector == "safe_mode"
    assert excinfo.value.decision.boundary == "posture"
    assert excinfo.value.decision.verdict == "deny"


def test_admit_never_touches_the_circuit_or_pricing_before_the_posture_check():
    """The posture stage runs first: a call refused by it never reaches the
    circuit or unpriced-model checks, so an unknown/unpriced model or an open
    circuit changes nothing about a stopped run being refused for being
    stopped."""
    eng = engine(GuardrailConfig(on_unpriced_model="refuse"))
    session = SessionState("s1")
    session.posture = make_posture_state("stopped", "manual", "manual")

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(session, "openai@default", "totally-unknown-model", None)

    # If unpriced ran first this would be detector="budget"; it is not.
    assert excinfo.value.anomaly.detector == "safe_mode"


@pytest.mark.parametrize("posture_name", ["restricted", "read_only", "no_side_effects", "full"])
def test_admit_serves_a_model_call_under_every_other_posture(posture_name):
    eng = engine()
    session = SessionState("s1")
    if posture_name != "full":
        session.posture = make_posture_state(posture_name, "manual", "manual")

    eng.admit(session, "openai@default", "gpt-4o", None)  # must not raise


def test_a_ladder_closed_stopped_posture_latches():
    """The ladder's closed rung already latches the session (its own critical
    anomaly goes through the ordinary react/latch path); this stage latching
    too, for a ``source="ladder"`` stopped posture, is consistent with that —
    not a new kind of latch."""
    eng = engine()
    session = SessionState("s1")
    session.posture = make_posture_state("stopped", "closed: allowance spent", "ladder")

    with pytest.raises(GuardrailTripped):
        eng.admit(session, "openai@default", "gpt-4o", None)

    assert session.tripped_by is not None


def test_a_manual_stopped_posture_never_latches():
    eng = engine()
    session = SessionState("s1")
    session.posture = make_posture_state("stopped", "manual", "manual")

    with pytest.raises(GuardrailTripped):
        eng.admit(session, "openai@default", "gpt-4o", None)

    assert session.tripped_by is None


def test_a_process_level_stopped_posture_is_read_too():
    """The bug's own reproduction: the *process* narrowed by
    ``enter_safe_mode``, not the session."""
    eng = engine()
    session = SessionState("s1")
    eng.enter_safe_mode("manual", "stopped")

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.admit(session, "openai@default", "gpt-4o", None)

    assert excinfo.value.anomaly.details["decision"]["evaluation"]["source"] == "manual"


def test_a_bug_reading_the_posture_lets_a_model_call_through(monkeypatch, caplog):
    eng = engine()
    session = SessionState("s1")

    def boom(self, session):
        raise RuntimeError("boom")

    monkeypatch.setattr(Engine, "effective_posture", boom)
    eng.admit(session, "openai@default", "gpt-4o", None)  # must not raise


# --- end to end: sync, async, stream, @llm, a fake provider records zero ----


class FakeUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=10):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeResponse:
    def __init__(self, model=None):
        self.model = model
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(model=kwargs.get("model"))


class FakeOpenAISync:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions()


class FakeAsyncCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(model=kwargs.get("model"))


class FakeOpenAIAsync:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeAsyncCompletions()


class FakeStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        pass

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


def test_sync_wrapped_call_records_zero_calls_while_stopped():
    runbound.init(on_anomaly="raise")
    client = runbound.wrap(FakeOpenAISync())
    runbound.enter_safe_mode("manual", "stopped")

    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    assert client.chat.completions.calls == 0


def test_async_wrapped_call_records_zero_calls_while_stopped():
    runbound.init(on_anomaly="raise")
    client = runbound.wrap(FakeOpenAIAsync())
    runbound.enter_safe_mode("manual", "stopped")

    async def run():
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    with pytest.raises(GuardrailTripped):
        asyncio.run(run())

    assert client.chat.completions.calls == 0


def test_streamed_call_records_zero_calls_while_stopped():
    runbound.init(on_anomaly="raise")
    client = runbound.wrap(FakeOpenAIStreaming())
    runbound.enter_safe_mode("manual", "stopped")

    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
        )

    assert client.chat.completions.calls == 0


def test_llm_decorated_function_never_runs_its_body_while_stopped():
    runbound.init(on_anomaly="raise")
    calls = []

    @runbound.llm(model="local-model", provider="custom")
    def generate(prompt: str) -> str:
        calls.append(prompt)
        return "reply"

    runbound.enter_safe_mode("manual", "stopped")

    with pytest.raises(GuardrailTripped):
        generate("hi")

    assert calls == []


def test_async_llm_decorated_function_never_runs_its_body_while_stopped():
    runbound.init(on_anomaly="raise")
    calls = []

    @runbound.llm(model="local-model", provider="custom")
    async def generate(prompt: str) -> str:
        calls.append(prompt)
        return "reply"

    runbound.enter_safe_mode("manual", "stopped")

    async def run():
        await generate("hi")

    with pytest.raises(GuardrailTripped):
        asyncio.run(run())

    assert calls == []


def test_no_side_effects_still_serves_model_calls():
    """§2.3's own table: ``no_side_effects`` denies every tool but still
    admits model calls -- only ``stopped`` refuses them."""
    runbound.init(on_anomaly="raise")
    client = runbound.wrap(FakeOpenAISync())
    runbound.enter_safe_mode("manual", "no_side_effects")

    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    assert client.chat.completions.calls == 1


def test_exiting_safe_mode_lets_model_calls_through_again():
    runbound.init(on_anomaly="raise")
    client = runbound.wrap(FakeOpenAISync())
    runbound.enter_safe_mode("manual", "stopped")
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    runbound.exit_safe_mode()
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    assert client.chat.completions.calls == 1
