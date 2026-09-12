"""Self-hosted and self-deployed models: T35.

Four things, none of which a hosted-only SDK needs: a circuit keyed by the
*endpoint* rather than by the client's shape (a dead vLLM box must not refuse
calls to OpenAI proper), generic hooks for inference that never goes over
HTTP at all, an opt-in token estimator for servers that report no usage, and
a cap on how many calls may be in flight at once — because on your own GPUs
concurrency, not dollars, is the scarce thing.

Nothing from ``openai``/``anthropic`` is imported here: every client is a
duck-typed fake, exactly as the wrappers see them.
"""

import asyncio
import logging
import math
import threading

import pytest

import runbound
from runbound import api
from runbound.exceptions import CircuitOpen, GuardrailTripped
from runbound.wrappers import anthropic_wrapper, openai_wrapper, provider_label


# --- fakes ------------------------------------------------------------------


class Failure(Exception):
    """An SDK-shaped error carrying an HTTP status."""

    def __init__(self, status_code=503):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class Box:
    """An attribute bag: the shape every SDK response duck-types to."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class RecordingHooks:
    """The wrapper-facing hooks interface, recorded instead of enforced."""

    def __init__(self, estimate_tokens=False):
        self.before_calls: list[str] = []
        self.successes: list[str] = []
        self.errors: list[tuple] = []
        self.releases: list[str] = []
        self.estimate_tokens = estimate_tokens

    def before(self, provider):
        self.before_calls.append(provider)

    def success(self, provider):
        self.successes.append(provider)

    def error(self, model, exc, duration_s, provider):
        self.errors.append((model, exc, duration_s, provider))

    def release(self, provider):
        self.releases.append(provider)

    def tool_request(self, name, args_hash):
        return None


class Reports(list):
    """A ``report`` callback that keeps every call it was handed."""

    def __call__(self, model, tokens_in, tokens_out, duration_s=0.0, reasoning=0):
        self.append((model, tokens_in, tokens_out))


class FakeCompletions:
    """``chat.completions``: answers, fails, streams or blocks on demand."""

    def __init__(self, error=None, stream=None, response=None, gate=None, started=None):
        self.error = error
        self.stream = stream
        self.response = response
        self.gate = gate
        self.started = started
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if self.stream is not None:
            return self.stream
        if self.response is not None:
            return self.response
        return Box(model="local-model", usage=Box(prompt_tokens=10, completion_tokens=5))


class AsyncFakeCompletions(FakeCompletions):
    async def create(self, **kwargs):
        return super().create(**kwargs)


class FakeOpenAI:
    """An OpenAI-shaped client, with a ``base_url`` when one is given."""

    def __init__(self, completions=None, base_url=None, responses=None, **kwargs):
        self.chat = Box(completions=completions or FakeCompletions(**kwargs))
        if base_url is not None:
            self.base_url = base_url
        if responses is not None:
            self.responses = responses

    @property
    def completions(self):
        return self.chat.completions


class FakeMessages:
    def __init__(self, response=None, stream=None):
        self.response = response
        self.stream = stream
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.stream is not None:
            return self.stream
        if self.response is not None:
            return self.response
        return Box(model="claude", usage=Box(input_tokens=3, output_tokens=4))


class FakeAnthropic:
    def __init__(self, base_url=None, **kwargs):
        self.messages = FakeMessages(**kwargs)
        if base_url is not None:
            self.base_url = base_url


class FakeStream:
    """A synchronous stream of the chunks it was built with."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class Url:
    """An ``httpx.URL``-shaped object: only ``str()`` says anything useful."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class Hostile:
    """A client whose ``base_url`` raises the moment it is read."""

    @property
    def base_url(self):
        raise RuntimeError("no base_url for you")


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture(autouse=True)
def _fresh_estimate_warning():
    """The estimator warns once per process; let every test see it."""
    from runbound import wrappers

    wrappers._ESTIMATED_TOKENS_WARNED = False
    yield
    wrappers._ESTIMATED_TOKENS_WARNED = False


# --- 1. the provider label --------------------------------------------------


def test_the_label_is_the_shape_and_the_endpoints_host():
    client = FakeOpenAI(base_url="http://localhost:11434/v1")
    assert provider_label("openai", client) == "openai@localhost:11434"


def test_an_sdk_url_object_is_read_through_str():
    client = FakeOpenAI(base_url=Url("https://api.openai.com/v1"))
    assert provider_label("openai", client) == "openai@api.openai.com"


def test_a_client_with_no_base_url_is_the_default_endpoint():
    assert provider_label("anthropic", FakeAnthropic()) == "anthropic@default"


def test_an_unreadable_base_url_is_the_default_endpoint():
    assert provider_label("openai", Hostile()) == "openai@default"
    assert provider_label("openai", FakeOpenAI(base_url="")) == "openai@default"
    assert provider_label("openai", FakeOpenAI(base_url=object())) == "openai@default"


def test_the_host_is_lowercased_and_keeps_its_port():
    client = FakeOpenAI(base_url="http://GPU-Box.Internal:8000/v1")
    assert provider_label("openai", client) == "openai@gpu-box.internal:8000"


def test_the_wrapper_reports_the_endpoint_label_to_the_hooks():
    hooks = RecordingHooks()
    client = FakeOpenAI(base_url="http://localhost:11434/v1")
    openai_wrapper.install(client, Reports(), hooks=hooks)

    client.chat.completions.create(model="local-model")

    assert hooks.before_calls == ["openai@localhost:11434"]
    assert hooks.successes == ["openai@localhost:11434"]


def test_the_anthropic_wrapper_labels_its_endpoint_too():
    hooks = RecordingHooks()
    client = FakeAnthropic(base_url="https://api.anthropic.com")
    anthropic_wrapper.install(client, Reports(), hooks=hooks)

    client.messages.create(model="claude")

    assert hooks.successes == ["anthropic@api.anthropic.com"]


# --- 2. one circuit per endpoint --------------------------------------------


def test_two_endpoints_of_one_shape_have_independent_circuits():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    dead = runbound.wrap(
        FakeOpenAI(base_url="http://localhost:9/v1", error=Failure(503))
    )
    alive = runbound.wrap(FakeOpenAI(base_url="http://localhost:11434/v1"))

    with pytest.raises(Failure):
        dead.chat.completions.create(model="m")
    with pytest.raises(CircuitOpen) as excinfo:
        dead.chat.completions.create(model="m")

    assert excinfo.value.provider == "openai@localhost:9"
    assert runbound.circuit_state("openai@localhost:9") == "open"
    assert runbound.circuit_state("openai@localhost:11434") == "closed"

    alive.chat.completions.create(model="m")  # the healthy box is untouched
    assert alive.completions.calls == 1


def test_a_shape_prefix_resolves_to_the_worst_of_its_endpoints():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    dead = runbound.wrap(
        FakeOpenAI(base_url="http://localhost:9/v1", error=Failure(503))
    )
    runbound.wrap(FakeOpenAI(base_url="http://localhost:11434/v1"))

    assert runbound.circuit_state("openai") == "closed"
    with pytest.raises(Failure):
        dead.chat.completions.create(model="m")

    assert runbound.circuit_state("openai") == "open"  # the worst one wins
    assert runbound.circuit_state("anthropic") == "closed"


def test_a_half_open_endpoint_beats_a_closed_one_but_loses_to_an_open_one():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    clock = _FakeClock()
    api._ENGINE.circuit._now = clock
    first = runbound.wrap(FakeOpenAI(base_url="http://a:1/v1", error=Failure(503)))
    second = runbound.wrap(FakeOpenAI(base_url="http://b:2/v1", error=Failure(503)))

    with pytest.raises(Failure):
        first.chat.completions.create(model="m")
    clock.advance(31.0)
    assert runbound.circuit_state("openai") == "half_open"

    with pytest.raises(Failure):
        second.chat.completions.create(model="m")
    assert runbound.circuit_state("openai") == "open"


class _FakeClock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_an_opened_circuit_names_the_endpoint_in_its_alert():
    alerts = []
    runbound.init(
        circuit_failure_threshold=1, on_anomaly="callback", callback=alerts.append
    )
    api._ENGINE.observers = [
        Box(
            on_event=lambda session, event: None,
            on_anomaly=lambda session, anomaly, reacted: alerts.append(anomaly),
        )
    ]
    client = runbound.wrap(
        FakeOpenAI(base_url="http://gpu-box:8000/v1", error=Failure(503))
    )

    with pytest.raises(Failure):
        client.chat.completions.create(model="m")

    circuit = [a for a in alerts if a.detector == "circuit"]
    assert circuit, alerts
    assert circuit[0].details["provider"] == "openai@gpu-box:8000"
    assert circuit[0].details["host"] == "gpu-box:8000"
    assert "openai@gpu-box:8000" in circuit[0].message


def test_circuit_state_is_closed_for_an_endpoint_nobody_has_called():
    runbound.init()
    assert runbound.circuit_state("openai@nowhere:1") == "closed"
    assert runbound.circuit_state("openai") == "closed"


# --- 3. record_call and @llm ------------------------------------------------


def test_record_call_counts_tokens_against_the_session():
    runbound.init()

    runbound.record_call("local-llama", 100, 50, 1.5)

    state = runbound.current_session()
    assert state.total_tokens == 150


def test_record_call_is_inert_before_init():
    runbound.record_call("local-llama", 100, 50)  # must not raise


def test_record_call_trips_a_budget_like_any_other_call():
    runbound.init(max_total_tokens=100, on_anomaly="raise")

    runbound.record_call("local-llama", 60, 0)
    with pytest.raises(GuardrailTripped) as excinfo:
        runbound.record_call("local-llama", 60, 0)

    assert excinfo.value.anomaly.detector == "budget"


def test_record_call_with_an_error_feeds_the_storm_and_the_circuit():
    runbound.init(error_storm_limit=None, circuit_failure_threshold=2)

    runbound.record_call(None, 0, 0, 0.2, provider="vllm@gpu:8000", error=Failure(503))
    assert runbound.circuit_state("vllm@gpu:8000") == "closed"
    runbound.record_call(None, 0, 0, 0.2, provider="vllm@gpu:8000", error=Failure(503))

    assert runbound.circuit_state("vllm@gpu:8000") == "open"
    assert runbound.current_session().consecutive_errors == 2


def test_record_call_errors_can_trip_the_error_storm():
    runbound.init(error_storm_limit=1, on_anomaly="raise")

    runbound.record_call(None, 0, 0, error=Failure(503))
    with pytest.raises(GuardrailTripped) as excinfo:
        runbound.record_call(None, 0, 0, error=Failure(503))

    assert excinfo.value.anomaly.detector == "error_storm"


def test_the_llm_decorator_records_a_local_inference():
    runbound.init()

    @runbound.llm(model="llama-3.1-8b", tokens=lambda result: (len(result), 2))
    def infer(prompt):
        return "hello"

    assert infer("hi") == "hello"
    state = runbound.current_session()
    assert state.total_tokens == 7  # 5 in, 2 out


def test_the_llm_decorator_without_a_tokens_callback_records_a_step():
    runbound.init(max_steps=1, on_anomaly="raise")

    @runbound.llm
    def infer():
        return "hello"

    infer()
    with pytest.raises(GuardrailTripped) as excinfo:
        infer()

    assert excinfo.value.anomaly.detector == "steps"
    assert runbound.current_session().total_tokens == 0


def test_a_broken_tokens_callback_never_breaks_the_call():
    runbound.init()

    @runbound.llm(tokens=lambda result: 1 / 0)
    def infer():
        return "hello"

    assert infer() == "hello"
    assert runbound.current_session().total_tokens == 0


def test_the_llm_decorator_records_a_failure_and_re_raises_it():
    runbound.init(circuit_failure_threshold=1, error_storm_limit=None)
    boom = Failure(503)

    @runbound.llm(provider="vllm@gpu:8000")
    def infer():
        raise boom

    with pytest.raises(Failure) as excinfo:
        infer()

    assert excinfo.value is boom
    assert runbound.circuit_state("vllm@gpu:8000") == "open"


def test_the_llm_decorator_refuses_a_call_when_the_circuit_is_open():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    ran = []

    @runbound.llm(provider="vllm@gpu:8000")
    def infer():
        ran.append(1)
        raise Failure(503)

    with pytest.raises(Failure):
        infer()
    with pytest.raises(CircuitOpen) as excinfo:
        infer()

    assert excinfo.value.provider == "vllm@gpu:8000"
    assert len(ran) == 1  # the body never ran the second time


def test_the_async_llm_decorator_records_and_re_raises():
    runbound.init(circuit_failure_threshold=1, error_storm_limit=None)

    @runbound.llm(model="llama", tokens=lambda result: (1, 2))
    async def infer():
        return "ok"

    assert asyncio.iscoroutinefunction(infer)
    assert asyncio.run(infer()) == "ok"
    assert runbound.current_session().total_tokens == 3

    @runbound.llm(provider="vllm@gpu:8000")
    async def broken():
        raise Failure(503)

    async def call():
        with pytest.raises(Failure):
            await broken()

    asyncio.run(call())
    assert runbound.circuit_state("vllm@gpu:8000") == "open"


def test_the_llm_decorator_is_inert_before_init():
    @runbound.llm(tokens=lambda result: (1, 2))
    def infer():
        return "ok"

    assert infer() == "ok"


# --- 4. the opt-in token estimator ------------------------------------------


def chat_request(chars=40):
    return {"model": "local-model", "messages": [{"role": "user", "content": "a" * chars}]}


def test_no_usage_stays_zero_when_the_estimator_is_off():
    reports = Reports()
    client = FakeOpenAI(response=Box(model="local-model", usage=None))
    openai_wrapper.install(client, reports, hooks=RecordingHooks())

    client.chat.completions.create(**chat_request())

    assert reports == [("local-model", 0, 0)]


def test_the_estimator_reads_chat_messages_and_the_answer():
    reports = Reports()
    response = Box(
        model="local-model",
        usage=None,
        choices=[Box(message=Box(content="b" * 20))],
    )
    client = FakeOpenAI(response=response)
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.chat.completions.create(**chat_request(40))

    assert reports == [("local-model", 10, 5)]


def test_the_estimator_reads_multimodal_message_parts():
    reports = Reports()
    request = {
        "model": "local-model",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "c" * 8}, {"image": 1}]},
            {"role": "assistant", "content": None},
        ],
    }
    client = FakeOpenAI(response=Box(model="local-model", usage=None, choices=[]))
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.chat.completions.create(**request)

    assert reports == [("local-model", 2, 0)]


def test_the_estimator_reads_the_responses_api():
    reports = Reports()
    responses = FakeCompletions(
        response=Box(model="local-model", usage=None, output_text="d" * 12)
    )
    client = FakeOpenAI(responses=responses)
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.responses.create(model="local-model", input="e" * 16)

    assert reports == [("local-model", 4, 3)]


def test_the_estimator_reads_responses_input_items():
    reports = Reports()
    responses = FakeCompletions(
        response=Box(
            model="local-model",
            usage=None,
            output=[Box(content=[Box(text="f" * 4)])],
        )
    )
    client = FakeOpenAI(responses=responses)
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.responses.create(
        model="local-model", input=[{"role": "user", "content": [{"text": "g" * 8}]}]
    )

    assert reports == [("local-model", 2, 1)]


def test_the_estimator_reads_anthropic_messages_and_the_system_prompt():
    reports = Reports()
    client = FakeAnthropic(
        response=Box(model="claude", usage=None, content=[Box(type="text", text="h" * 8)])
    )
    anthropic_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.messages.create(
        model="claude",
        system="s" * 12,
        messages=[{"role": "user", "content": "i" * 20}],
    )

    assert reports == [("claude", 8, 2)]


def test_the_estimator_accumulates_a_chat_stream():
    reports = Reports()
    chunks = [
        Box(model="local-model", usage=None, choices=[Box(delta=Box(content="jjjj"))]),
        Box(model="local-model", usage=None, choices=[Box(delta=Box(content="kkkk"))]),
    ]
    client = FakeOpenAI(stream=FakeStream(chunks))
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    list(client.chat.completions.create(stream=True, **chat_request(40)))

    assert reports == [("local-model", 10, 2)]


def test_the_estimator_accumulates_an_anthropic_stream():
    reports = Reports()
    chunks = [
        Box(type="message_start", message=Box(model="claude", usage=None)),
        Box(type="content_block_delta", delta=Box(type="text_delta", text="l" * 8)),
    ]
    client = FakeAnthropic(stream=FakeStream(chunks))
    anthropic_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    list(
        client.messages.create(
            model="claude", stream=True, messages=[{"role": "user", "content": "m" * 4}]
        )
    )

    assert reports == [("claude", 1, 2)]


def test_the_estimator_accumulates_a_responses_stream():
    reports = Reports()
    chunks = [
        Box(type="response.output_text.delta", delta="n" * 8),
        Box(type="response.completed", response=Box(model="local-model", usage=None)),
    ]
    responses = FakeCompletions(stream=FakeStream(chunks))
    client = FakeOpenAI(responses=responses)
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    list(client.responses.create(model="local-model", input="o" * 4, stream=True))

    assert reports == [("local-model", 1, 2)]


def test_real_usage_is_never_overridden_by_an_estimate():
    reports = Reports()
    client = FakeOpenAI()  # 10 in, 5 out, and a 40-char prompt
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.chat.completions.create(**chat_request(400))

    assert reports == [("local-model", 10, 5)]


def test_the_estimate_is_announced_once_per_process(caplog):
    reports = Reports()
    client = FakeOpenAI(response=Box(model="local-model", usage=None, choices=[]))
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    with caplog.at_level(logging.WARNING, logger="runbound"):
        client.chat.completions.create(**chat_request())
        client.chat.completions.create(**chat_request())

    estimated = [r for r in caplog.records if "usage estimated" in r.getMessage()]
    assert len(estimated) == 1
    assert "local-model" in estimated[0].getMessage()


def test_the_config_flag_reaches_the_wrappers():
    runbound.init(estimate_tokens=True)
    client = runbound.wrap(
        FakeOpenAI(response=Box(model="local-model", usage=None, choices=[]))
    )

    client.chat.completions.create(**chat_request(40))

    assert runbound.current_session().total_tokens == 10


def test_the_config_flag_is_off_by_default():
    runbound.init()
    client = runbound.wrap(
        FakeOpenAI(response=Box(model="local-model", usage=None, choices=[]))
    )

    client.chat.completions.create(**chat_request(40))

    assert runbound.current_session().total_tokens == 0


def test_an_unreadable_request_estimates_nothing_and_still_reports():
    reports = Reports()
    client = FakeOpenAI(response=Box(model="local-model", usage=None, choices=[]))
    openai_wrapper.install(client, reports, hooks=RecordingHooks(estimate_tokens=True))

    client.chat.completions.create(model="local-model", messages=object())

    assert reports == [("local-model", 0, 0)]


# --- 5. the in-flight cap ---------------------------------------------------


def test_a_second_concurrent_call_is_refused_before_it_reaches_the_provider():
    runbound.init(max_inflight_calls=1)
    gate, started = threading.Event(), threading.Event()
    completions = FakeCompletions(gate=gate, started=started)
    client = runbound.wrap(FakeOpenAI(completions=completions, base_url="http://gpu:1/v1"))
    worker = threading.Thread(target=lambda: client.chat.completions.create(model="m"))
    worker.start()
    try:
        assert started.wait(5)
        assert runbound.inflight_calls("openai") == 1

        with pytest.raises(GuardrailTripped) as excinfo:
            client.chat.completions.create(model="m")
    finally:
        gate.set()
        worker.join(5)

    anomaly = excinfo.value.anomaly
    assert anomaly.detector == "inflight"
    assert anomaly.severity == "critical"
    assert anomaly.details["provider"] == "openai@gpu:1"
    assert anomaly.details["host"] == "gpu:1"
    assert (anomaly.details["count"], anomaly.details["limit"]) == (1, 1)
    assert completions.calls == 1  # the refused call never reached the provider


def test_the_slot_is_given_back_when_the_call_finishes():
    runbound.init(max_inflight_calls=1)
    client = runbound.wrap(FakeOpenAI(base_url="http://gpu:1/v1"))

    for _ in range(3):
        client.chat.completions.create(model="m")

    assert client.completions.calls == 3
    assert runbound.inflight_calls("openai") == 0


def test_the_slot_is_given_back_when_a_call_fails():
    runbound.init(max_inflight_calls=1, error_storm_limit=None)
    client = runbound.wrap(FakeOpenAI(base_url="http://gpu:1/v1", error=Failure(503)))

    for _ in range(2):
        with pytest.raises(Failure):
            client.chat.completions.create(model="m")

    assert runbound.inflight_calls("openai") == 0


def test_a_stream_holds_its_slot_until_it_ends():
    runbound.init(max_inflight_calls=1)
    chunks = [Box(model="local-model", usage=None, choices=[])]
    client = runbound.wrap(
        FakeOpenAI(stream=FakeStream(chunks), base_url="http://gpu:1/v1")
    )

    stream = client.chat.completions.create(model="m", stream=True)
    assert runbound.inflight_calls("openai") == 1
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="m")

    list(stream)
    assert runbound.inflight_calls("openai") == 0
    client.chat.completions.create(model="m")  # allowed again


def test_a_closed_stream_gives_its_slot_back_too():
    runbound.init(max_inflight_calls=1)
    chunks = [Box(model="local-model", usage=None, choices=[])]
    client = runbound.wrap(
        FakeOpenAI(stream=FakeStream(chunks), base_url="http://gpu:1/v1")
    )

    stream = client.chat.completions.create(model="m", stream=True)
    stream.close()

    assert runbound.inflight_calls("openai") == 0


def test_a_stream_that_ends_and_is_closed_gives_its_slot_back_once():
    runbound.init(max_inflight_calls=2)
    chunks = [Box(model="local-model", usage=None, choices=[])]
    client = runbound.wrap(
        FakeOpenAI(stream=FakeStream(chunks), base_url="http://gpu:1/v1")
    )

    stream = client.chat.completions.create(model="m", stream=True)
    list(stream)
    stream.close()

    assert runbound.inflight_calls("openai") == 0  # never negative


def test_a_refused_call_never_took_a_slot():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1,
                    max_inflight_calls=2, error_storm_limit=None)
    client = runbound.wrap(FakeOpenAI(base_url="http://gpu:1/v1", error=Failure(503)))

    with pytest.raises(Failure):
        client.chat.completions.create(model="m")
    for _ in range(3):
        with pytest.raises(CircuitOpen):
            client.chat.completions.create(model="m")

    assert runbound.inflight_calls("openai") == 0


def test_the_cap_is_enforced_whatever_on_anomaly_says_and_latches_nothing():
    runbound.init(max_inflight_calls=1, on_anomaly="warn")
    gate, started = threading.Event(), threading.Event()
    completions = FakeCompletions(gate=gate, started=started)
    client = runbound.wrap(FakeOpenAI(completions=completions, base_url="http://gpu:1/v1"))
    worker = threading.Thread(target=lambda: client.chat.completions.create(model="m"))
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(GuardrailTripped):
            client.chat.completions.create(model="m")
    finally:
        gate.set()
        worker.join(5)

    assert runbound.is_tripped() is None  # refusing is not latching
    client.chat.completions.create(model="m")
    assert completions.calls == 2


def test_the_cap_is_per_endpoint_and_inflight_calls_sums_a_prefix():
    runbound.init(max_inflight_calls=1)
    gate, started = threading.Event(), threading.Event()
    first = runbound.wrap(
        FakeOpenAI(completions=FakeCompletions(gate=gate, started=started),
                   base_url="http://gpu-a:1/v1")
    )
    second = runbound.wrap(FakeOpenAI(base_url="http://gpu-b:2/v1"))
    worker = threading.Thread(target=lambda: first.chat.completions.create(model="m"))
    worker.start()
    try:
        assert started.wait(5)
        assert runbound.inflight_calls("openai@gpu-a:1") == 1
        assert runbound.inflight_calls("openai@gpu-b:2") == 0
        assert runbound.inflight_calls("openai") == 1
        second.chat.completions.create(model="m")  # a different box, its own cap
    finally:
        gate.set()
        worker.join(5)

    assert second.completions.calls == 1
    assert runbound.inflight_calls("openai") == 0


def test_no_cap_means_no_counting_and_no_refusals():
    runbound.init()
    gate, started = threading.Event(), threading.Event()
    completions = FakeCompletions(gate=gate, started=started)
    client = runbound.wrap(FakeOpenAI(completions=completions, base_url="http://gpu:1/v1"))
    worker = threading.Thread(target=lambda: client.chat.completions.create(model="m"))
    worker.start()
    try:
        assert started.wait(5)
        client.chat.completions.create(model="m")
    finally:
        gate.set()
        worker.join(5)

    assert completions.calls == 2


def test_the_cap_alerts_once_per_endpoint():
    alerts = []
    runbound.init(max_inflight_calls=1)
    api._ENGINE.observers = [
        Box(
            on_event=lambda session, event: None,
            on_anomaly=lambda session, anomaly, reacted: alerts.append(anomaly),
        )
    ]
    gate, started = threading.Event(), threading.Event()
    completions = FakeCompletions(gate=gate, started=started)
    client = runbound.wrap(FakeOpenAI(completions=completions, base_url="http://gpu:1/v1"))
    worker = threading.Thread(target=lambda: client.chat.completions.create(model="m"))
    worker.start()
    try:
        assert started.wait(5)
        for _ in range(3):
            with pytest.raises(GuardrailTripped):
                client.chat.completions.create(model="m")
    finally:
        gate.set()
        worker.join(5)

    assert [a.detector for a in alerts] == ["inflight"]


def test_the_decorator_takes_and_gives_back_a_slot():
    runbound.init(max_inflight_calls=1)

    @runbound.llm(provider="vllm@gpu:8000")
    def infer():
        assert runbound.inflight_calls("vllm") == 1
        return "ok"

    assert infer() == "ok"
    assert runbound.inflight_calls("vllm") == 0
    assert infer() == "ok"


def test_init_and_reset_forget_the_in_flight_counters():
    runbound.init(max_inflight_calls=1)
    api._INFLIGHT["openai@gpu:1"] = 1

    runbound.reset()
    assert runbound.inflight_calls("openai") == 0

    api._INFLIGHT["openai@gpu:1"] = 1
    runbound.init(max_inflight_calls=1)
    assert runbound.inflight_calls("openai") == 0


def test_inflight_calls_reads_zero_before_init():
    assert runbound.inflight_calls("openai") == 0


def test_a_negative_configuration_is_refused_at_init():
    with pytest.raises(ValueError, match="max_inflight_calls"):
        runbound.init(max_inflight_calls=0)


def test_the_estimator_ceils_rather_than_truncates():
    from runbound.wrappers import estimated_tokens

    assert estimated_tokens(0) == 0
    assert estimated_tokens(1) == 1
    assert estimated_tokens(4) == 1
    assert estimated_tokens(5) == 2
    assert estimated_tokens(9) == math.ceil(9 / 4)
