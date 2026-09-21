"""Tests for what a wrapped client does when the provider fails.

Two levels. The wrapper level uses a recording hooks object — the whole
interface a wrapper has to the rest of runbound — against duck-typed fake
clients; nothing from openai/anthropic is imported here either. The end-to-end
level goes through ``runbound.wrap()`` with a fake client that raises, and
watches the circuit open, refuse, cool down and heal.
"""

import asyncio

import pytest

import runbound
from runbound import api
from runbound.exceptions import CircuitOpen, GuardrailTripped
from runbound.wrappers import anthropic_wrapper, openai_wrapper


class FakeClock:
    """A monotonic clock moved by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Failure(Exception):
    """An SDK-shaped error carrying an HTTP status."""

    def __init__(self, status_code=429):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class RecordingHooks:
    """The wrapper-facing hooks interface, recorded instead of enforced."""

    def __init__(self, refuse: bool = False) -> None:
        self.before_calls: list[str] = []
        self.successes: list[str] = []
        self.errors: list[tuple] = []
        self.requests: list[tuple] = []
        self.refuse = refuse

    def before(self, provider):
        self.before_calls.append(provider)
        if self.refuse:
            raise CircuitOpen(
                runbound.Anomaly("circuit", "critical", "open", {"provider": provider}),
                provider,
            )

    def success(self, provider):
        self.successes.append(provider)

    def error(self, model, exc, duration_s, provider):
        self.errors.append((model, exc, duration_s, provider))

    def tool_request(self, name, args_hash):
        self.requests.append((name, args_hash))


class FakeUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, model="gpt-4o"):
        self.model = model
        self.usage = FakeUsage(prompt_tokens=10, completion_tokens=5)


class FakeCompletions:
    """Shaped like openai.resources.chat.Completions; fails on demand."""

    def __init__(self, error=None, stream=None):
        self.error = error
        self.stream = stream
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.stream is not None:
            return self.stream
        return FakeResponse()


class AsyncFakeCompletions(FakeCompletions):
    async def create(self, **kwargs):
        return super().create(**kwargs)


class FakeOpenAI:
    def __init__(self, completions=None, **kwargs):
        self.chat = type("Chat", (), {})()
        self.chat.completions = completions or FakeCompletions(**kwargs)

    @property
    def completions(self):
        return self.chat.completions


class FakeMessages:
    """Shaped like anthropic.resources.Messages; fails on demand."""

    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FakeResponse(model="claude-sonnet-4-5")


class FakeAnthropic:
    def __init__(self, **kwargs):
        self.messages = FakeMessages(**kwargs)


class BoomStream:
    """A stream that yields one chunk and then dies mid-flight."""

    def __init__(self, exc):
        self.exc = exc
        self.chunks = iter([FakeUsage(model="gpt-4o", usage=None)])

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise self.exc from None


class GoodStream:
    def __init__(self):
        self.chunks = iter(
            [FakeUsage(model="gpt-4o", usage=FakeUsage(prompt_tokens=7, completion_tokens=3))]
        )

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.chunks)


def report(*args, **kwargs):
    """A ``report`` that counts nothing — these tests are about the hooks."""


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def circuit_clock():
    """Put the live engine's circuit on a clock this test moves by hand."""

    def install() -> FakeClock:
        clock = FakeClock()
        api._ENGINE.circuit._now = clock
        return clock

    return install


# --- the wrapper's contract with the hooks ----------------------------------


def test_a_failed_call_is_reported_once_and_the_original_error_re_raised():
    hooks = RecordingHooks()
    failure = Failure(503)
    client = FakeOpenAI(error=failure)
    openai_wrapper.install(client, report, hooks=hooks)

    with pytest.raises(Failure) as excinfo:
        client.chat.completions.create(model="gpt-4o")

    assert excinfo.value is failure
    assert hooks.before_calls == ["openai@default"]
    assert len(hooks.errors) == 1
    model, exc, duration_s, provider = hooks.errors[0]
    assert (model, exc, provider) == ("gpt-4o", failure, "openai@default")
    assert duration_s >= 0.0
    assert hooks.successes == []


def test_a_successful_call_reports_success():
    hooks = RecordingHooks()
    client = FakeOpenAI()
    openai_wrapper.install(client, report, hooks=hooks)

    client.chat.completions.create(model="gpt-4o")

    assert hooks.before_calls == ["openai@default"]
    assert hooks.successes == ["openai@default"]
    assert hooks.errors == []


def test_before_runs_ahead_of_the_provider_call():
    hooks = RecordingHooks(refuse=True)
    client = FakeOpenAI()
    openai_wrapper.install(client, report, hooks=hooks)

    with pytest.raises(CircuitOpen):
        client.chat.completions.create(model="gpt-4o")

    assert client.completions.calls == 0


def test_a_trip_raised_by_the_error_hook_replaces_the_provider_error():
    class Tripping(RecordingHooks):
        def error(self, model, exc, duration_s, provider):
            super().error(model, exc, duration_s, provider)
            raise GuardrailTripped(
                runbound.Anomaly("error_storm", "critical", "storm", {})
            )

    hooks = Tripping()
    client = FakeOpenAI(error=Failure(429))
    openai_wrapper.install(client, report, hooks=hooks)

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o")

    assert excinfo.value.anomaly.detector == "error_storm"
    assert len(hooks.errors) == 1


def test_the_anthropic_wrapper_reports_the_same_way():
    hooks = RecordingHooks()
    failure = Failure(529)
    client = FakeAnthropic(error=failure)
    anthropic_wrapper.install(client, report, hooks=hooks)

    with pytest.raises(Failure):
        client.messages.create(model="claude-sonnet-4-5")

    assert hooks.before_calls == ["anthropic@default"]
    assert hooks.errors[0][3] == "anthropic@default"


def test_an_async_client_reports_the_same_way():
    hooks = RecordingHooks()
    failure = Failure(503)
    client = FakeOpenAI(completions=AsyncFakeCompletions(error=failure))
    openai_wrapper.install(client, report, hooks=hooks)

    async def call():
        with pytest.raises(Failure):
            await client.chat.completions.create(model="gpt-4o")

    asyncio.run(call())

    assert hooks.before_calls == ["openai@default"]
    assert len(hooks.errors) == 1
    assert hooks.errors[0][3] == "openai@default"


def test_an_async_success_reports_success():
    hooks = RecordingHooks()
    client = FakeOpenAI(completions=AsyncFakeCompletions())
    openai_wrapper.install(client, report, hooks=hooks)

    asyncio.run(client.chat.completions.create(model="gpt-4o"))

    assert hooks.successes == ["openai@default"]


def test_a_stream_that_dies_mid_flight_reports_one_error():
    hooks = RecordingHooks()
    failure = Failure(503)
    client = FakeOpenAI(completions=FakeCompletions(stream=BoomStream(failure)))
    openai_wrapper.install(client, report, hooks=hooks)

    stream = client.chat.completions.create(model="gpt-4o", stream=True)
    chunks = []
    with pytest.raises(Failure):
        for chunk in stream:
            chunks.append(chunk)

    assert len(chunks) == 1
    assert len(hooks.errors) == 1
    assert hooks.errors[0][3] == "openai@default"
    assert hooks.successes == []


def test_a_stream_that_ends_cleanly_reports_success():
    hooks = RecordingHooks()
    client = FakeOpenAI(completions=FakeCompletions(stream=GoodStream()))
    openai_wrapper.install(client, report, hooks=hooks)

    list(client.chat.completions.create(model="gpt-4o", stream=True))

    assert hooks.successes == ["openai@default"]
    assert hooks.errors == []


def test_installing_without_hooks_still_works():
    """The two-argument install() predates the hooks and must keep working."""
    seen = []
    client = FakeOpenAI()
    openai_wrapper.install(client, lambda *args: seen.append(args))

    client.chat.completions.create(model="gpt-4o")

    assert len(seen) == 1

    failing = FakeOpenAI(error=Failure(503))
    anthropic = FakeAnthropic()
    openai_wrapper.install(failing, report)
    anthropic_wrapper.install(anthropic, report)
    with pytest.raises(Failure):
        failing.chat.completions.create(model="gpt-4o")
    anthropic.messages.create(model="claude-sonnet-4-5")


# --- end to end through wrap() ----------------------------------------------


def test_the_circuit_opens_and_fails_fast_without_calling_the_provider():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)
    failure = Failure(429)
    client = runbound.wrap(FakeOpenAI(error=failure))

    for _ in range(2):
        with pytest.raises(Failure) as excinfo:
            client.chat.completions.create(model="gpt-4o")
        assert excinfo.value is failure

    with pytest.raises(CircuitOpen) as excinfo:
        client.chat.completions.create(model="gpt-4o")

    assert excinfo.value.provider == "openai@default"
    assert isinstance(excinfo.value, GuardrailTripped)
    assert client.completions.calls == 2  # the third never reached the provider
    assert runbound.circuit_state("openai") == "open"


def test_the_probe_goes_through_after_the_cooldown_and_success_closes_it(circuit_clock):
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)
    completions = FakeCompletions(error=Failure(429))
    client = runbound.wrap(FakeOpenAI(completions=completions))
    clock = circuit_clock()

    for _ in range(2):
        with pytest.raises(Failure):
            client.chat.completions.create(model="gpt-4o")
    with pytest.raises(CircuitOpen):
        client.chat.completions.create(model="gpt-4o")

    clock.advance(31.0)
    assert runbound.circuit_state("openai") == "half_open"
    completions.error = None
    client.chat.completions.create(model="gpt-4o")  # the probe, and it works

    assert runbound.circuit_state("openai") == "closed"
    assert completions.calls == 3
    client.chat.completions.create(model="gpt-4o")
    assert completions.calls == 4


def test_notify_counts_the_outage_but_never_refuses_a_call():
    runbound.init(circuit_failure_threshold=2)  # on_provider_failure defaults to notify
    client = runbound.wrap(FakeOpenAI(error=Failure(429)))

    for _ in range(3):
        with pytest.raises(Failure):
            client.chat.completions.create(model="gpt-4o")

    assert client.completions.calls == 3  # every call still went out
    assert runbound.circuit_state("openai") == "open"


def test_a_bad_request_never_opens_the_circuit():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=2)
    client = runbound.wrap(FakeOpenAI(error=Failure(400)))

    for _ in range(5):
        with pytest.raises(Failure):
            client.chat.completions.create(model="gpt-4o")

    assert client.completions.calls == 5
    assert runbound.circuit_state("openai") == "closed"


def test_an_async_client_fails_fast_too():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    completions = AsyncFakeCompletions(error=Failure(503))
    client = runbound.wrap(FakeOpenAI(completions=completions))

    async def run():
        with pytest.raises(Failure):
            await client.chat.completions.create(model="gpt-4o")
        with pytest.raises(CircuitOpen):
            await client.chat.completions.create(model="gpt-4o")

    asyncio.run(run())

    assert completions.calls == 1


def test_the_providers_have_separate_circuits_end_to_end():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    openai_client = runbound.wrap(FakeOpenAI(error=Failure(503)))
    anthropic_client = runbound.wrap(FakeAnthropic())

    with pytest.raises(Failure):
        openai_client.chat.completions.create(model="gpt-4o")

    with pytest.raises(CircuitOpen):
        openai_client.chat.completions.create(model="gpt-4o")
    anthropic_client.messages.create(model="claude-sonnet-4-5")  # untouched
    assert runbound.circuit_state("anthropic") == "closed"


def test_a_storm_of_failures_trips_the_session():
    runbound.init(on_anomaly="raise", error_storm_limit=2)
    client = runbound.wrap(FakeOpenAI(error=Failure(503)))

    for _ in range(2):
        with pytest.raises(Failure):
            client.chat.completions.create(model="gpt-4o")

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o")
    assert excinfo.value.anomaly.detector == "error_storm"


# --- model-requested tool calls ---------------------------------------------


def test_three_identical_model_requests_are_a_loop():
    runbound.init(on_anomaly="raise", loop_threshold=3)

    api._HOOKS.tool_request("search", "req:abc")
    api._HOOKS.tool_request("search", "req:abc")
    with pytest.raises(GuardrailTripped) as excinfo:
        api._HOOKS.tool_request("search", "req:abc")

    assert excinfo.value.anomaly.detector == "loop"
    assert "model requested tool" in excinfo.value.anomaly.message


def test_requests_and_executions_are_counted_separately():
    """A request and the call it leads to are one action each, not two."""
    runbound.init(on_anomaly="raise", loop_threshold=3)

    @runbound.tool
    def search(query):
        return "ok"

    for _ in range(2):
        api._HOOKS.tool_request("search", "req:abc")
        search("weather")

    assert runbound.is_tripped() is None
    assert runbound.tool_calls()["search"] == 2
