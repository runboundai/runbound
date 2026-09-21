# Works with

[← Docs](../README.md)

`wrap()` recognizes clients by **shape, not type** — runbound never imports
`openai` or `anthropic`:

- `.chat.completions.create` and/or `.responses.create` → the OpenAI path
  (every surface present on the client is guarded, and `wrap()` logs which)
- `.messages.create` → the Anthropic path

OpenAI and Anthropic are supported and tested against the real SDKs.
OpenAI-compatible servers are supported through that same shape-matched
`wrap()` and proven live on Ollama; other compatible providers — vLLM, Groq,
OpenRouter, Azure OpenAI, LM Studio — share that code path but are not
individually tested yet. Native adapters for Gemini, Bedrock, Mistral and
Cohere are not built; use `record_call()` / `@runbound.llm` there. See the
[compatibility matrix](#compatibility-matrix) below for exactly what is
proven versus expected-by-shape. Sync and async clients, streamed or not, all
go through that one call — see [Async and streaming](../guides/streams.md#async-and-streaming).
For framework-driven agents there is a [LangChain / LangGraph
handler](#langchain--langgraph).

## Compatibility matrix

Filled in only where a test proves it — a checkmark with no footnote is a
claim we are not making. An empty cell means "probably works by shape, not
proven here," not "does not work."

**Which versions.** CI runs the real-SDK suite on Python 3.10 and 3.13
against `openai` 1.66.5 and latest, and `anthropic` 1.0.0 and latest — eight
combinations, with "latest" deliberately unpinned so a release nobody has
seen yet breaks the job rather than a customer's agent. `openai` 1.66.5 is a
genuine floor: 1.65.0 has no Responses API at all, and 1.66.0–1.66.3 return
`input_tokens_details` as a plain dict. **The `anthropic` 1.0.0 pin is not
a floor** — it is an artefact of testing both providers in one interpreter.
Modern `openai` pulls in `httpx2`, and pre-1.0 `anthropic` type-rejects an
`httpx2` client; on its own, `anthropic` 0.125.0 passes the same suite. If
you run only Anthropic, older versions are fine.

| Provider | sync | async | stream | tool calls | usage | live-tested |
|---|---|---|---|---|---|---|
| OpenAI | yes [^sdk] | yes [^sdk] | yes [^stream] | yes [^sdk] | yes [^sdk] | yes [^live] |
| Anthropic | yes [^sdk] | yes [^conformance] | yes [^stream] | yes [^sdk] | yes [^sdk] | yes [^live] |
| Azure OpenAI | | | | | | |
| Ollama / vLLM / OpenAI-compatible | yes [^ollama] | | yes [^ollama] | yes [^ollama] | yes [^ollama] | yes [^ollama] |
| Gemini | | | | | | |
| Bedrock | | | | | | |
| Mistral / Cohere | | | | | | |
| LangChain | yes [^lc] | | | yes [^lc] | yes [^lc] | |

[^sdk]: Sync/tool-calls/usage cells for OpenAI and Anthropic, and the async
    cell for OpenAI, are proven against the real `openai` and `anthropic` SDK
    classes, over a fake HTTP transport (no live network) —
    `tests/test_real_sdk.py`.
[^conformance]: Anthropic async — plain, tool-calling, extended-thinking,
    cached-prompt and error responses — is proven against responses recorded
    from the real `anthropic` SDK and replayed through the wrapper, in a
    private conformance kit in the development monorepo (recorded against the
    real SDK; not part of this repository), alongside the fake-async-client
    tests in `tests/test_async_wrappers.py`. Recorded fixtures prove the
    wrapper reads a real recorded response correctly; they are not a live
    end-to-end run — the live-tested column cites [^live] for that, not this.
[^stream]: Sync for both providers, async for OpenAI — including
    abandoned-stream accounting: `tests/test_streaming.py`,
    `tests/test_streams_abandoned.py`. Async Anthropic streaming is covered
    too, by recorded event streams from the real `anthropic` SDK replayed
    through the wrapper in the same private conformance kit (recorded against
    the real SDK; not part of this repository) — recorded, not live.
[^live]: Live, over the network, against the real hosted Anthropic API on
    `claude-haiku-4-5`, 2026-09-14: nineteen scenarios, every one a PASS with
    the numbers it observed — cost accounting against the response's own
    usage object, sync and async, streamed and abandoned mid-stream,
    model-requested and decorated tool loops, policy deny, a budget trip and
    the refusal at the door after it, `max_steps` as turns, a spike watched
    and still served, the in-flight cap against a second concurrent stream,
    an invalid model name that does *not* count against the provider's
    circuit, extended thinking read and priced, a cache hit priced at the
    cached rate, `record_call` parity, and a latch healing on its TTL. The
    run cost $0.031. And against the real hosted OpenAI API on `gpt-4o-mini`
    (`o4-mini` for reasoning), 2026-09-14: the same nineteen plus a
    twentieth — a stream sent without `stream_options={"include_usage":
    True}` records zero tokens and warns exactly once — every one a PASS,
    with reasoning tokens read as a subset of the output and priced once, and
    a cache hit read as a slice of the prompt and priced at the cached rate.
    That run cost $0.013 and called `chat.completions.create` only: the
    Responses API is proven by recorded responses replayed through the
    wrapper, not live. Like the recorded fixtures above, both runs live in a
    private conformance kit in the development monorepo and are not part of
    this repository.
[^ollama]: Live, over the network, against a real running Ollama server —
    sync only, includes a streaming scenario (`max_inflight_calls` while a
    stream is in flight), a decorated and an undecorated tool loop, and real
    token usage — `examples/live/ollama_verify.py` (13 scenarios; not part of
    `pytest` — run manually against a local Ollama server). Ollama is reached
    through the OpenAI-compatible surface, not a distinct SDK, so this row
    stands in for vLLM / Groq / Together / OpenRouter / LM Studio too, by
    shape, but only Ollama has actually been run live.
[^lc]: The LangChain callback handler, with `langchain_core` faked out (no
    real network call) — `tests/test_langchain.py`.

Azure OpenAI, Gemini, Bedrock, and Mistral/Cohere have no test naming them
anywhere in this repo; Azure is expected to work through the OpenAI shape
(same client class, different `base_url`) but that expectation is untested
here. Gemini, Bedrock, Mistral and Cohere use their own SDK shapes and are not
wrapped at all today — see [Paths that are not guarded
today](#paths-that-are-not-guarded-today).

**Priced model families** (USD per 1M tokens, list prices that drift — override
with `custom_prices` when you need exact numbers):

- OpenAI: `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1`, `gpt-4.1-mini`,
  `gpt-4.1-nano`, `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `gpt-3.5-turbo`,
  `o3`, `o3-mini`, `o4-mini`
- Anthropic: `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5`,
  `claude-opus-4-1`, `claude-opus-4`, `claude-sonnet-4`, `claude-3-7-sonnet`,
  `claude-3-5-sonnet`, `claude-3-5-haiku`, `claude-3-opus`, `claude-3-haiku`

Dated releases resolve by longest prefix, so `gpt-4o-mini-2024-07-18` prices as
`gpt-4o-mini`, not `gpt-4o`.

**These prices were last checked on `runbound.pricing.as_of()`** —
`"2026-09-12"` today. A price that changed after that date is wrong until
this table is updated; `custom_prices` always wins over it, which is how you
fix a stale number without waiting for a release.

### Cached input tokens

A repeated system prompt, a long few-shot block, an agent's growing message
history — anything the provider recognizes as a repeat of something it just
saw — is billed as a **cache hit**, at a discount runbound now reads and
prices correctly: OpenAI at 50% of its input rate, Anthropic at 10%. Before
this, runbound had no reader for either field and priced every cached token
at the full input rate — an agent with a large cached system prompt was
over-charged on nearly every call, so `budget_usd` stopped a session **at
less real spend than the customer allowed** — the safe direction to be wrong
in, and still wrong.

Nothing to configure: every guarded OpenAI and Anthropic call already reads
its own cache fields (`prompt_tokens_details.cached_tokens` /
`input_tokens_details.cached_tokens` for OpenAI, `cache_read_input_tokens`
for Anthropic) and prices the discount automatically, streamed or not. Whether
the provider caches at all is its decision, not runbound's: measured on
`gpt-4o-mini` on 2026-09-14, chat completions served a repeated
8,000-character system prompt from cache with no option set, while the
Responses API reported `cached_tokens: 0` until the request set
`prompt_cache_key`. A
model with no published cached rate (`gpt-4-turbo`, `gpt-3.5-turbo` — both
predate prompt caching) prices every token at the full input rate instead of
guessing a discount that was never published.

Anthropic also bills a **cache write** (`cache_creation_input_tokens`) at a
125% premium — writing a new cache entry costs more than an ordinary input
token, not less. runbound counts a cache write inside `tokens_in` (so
`total_tokens` stays honest) *and* prices it at its own published rate
(50%/10% for a read, 125% for a write are the two directions a cache-pricing
mistake can run, and only one of them is safe: under-counting a read trips
`budget_usd` early, at less real spend than you set — annoying, but safe;
under-counting a write does the opposite, letting a customer spend past a
budget they set, which this product cannot afford). A model with no
published cache-write rate falls back to the plain input rate, same as an
unpublished read rate — never a guessed number, in either direction.

`budget_admission`'s pre-call estimate (below) has no way to know before a
call how many of its tokens will be cache hits, so it estimates every call
at the plain input rate — conservative, the same direction an unpriced
model's estimate already leans.

**Local and free models: use token limits, not dollars** (or price your own
hardware — see [Self-hosted models](../guides/self-hosted-models.md#self-hosted-models)). By default an
unknown model prices at `$0.00` (`on_unpriced_model="zero"`, with a
once-per-model warning), so `budget_usd` will never trip on Ollama or a
self-hosted vLLM on its own. Guard those runs with `max_total_tokens` and
`tokens_per_minute_limit`, which are model-agnostic:

```python
runbound.init(max_total_tokens=500_000, tokens_per_minute_limit=60_000,
                max_steps=100, on_anomaly="raise")
```

For a hosted model runbound does not have a price for, supply one:

```python
runbound.init(budget_usd=5.0, on_anomaly="raise",
                custom_prices={"my-gateway/llama-3.3-70b": (0.60, 0.60)})
```

**Or change what "no price" means.** `on_unpriced_model` decides what happens
to a dollar budget when neither the static table nor `custom_prices` has an
answer: `"zero"` (the default, above) counts it as free and warns once;
`"estimate"` prices it from a `unpriced_price_per_1m_usd` fallback pair
instead, marking the number `priced="estimated"`; `"refuse"` stops the call at
the door — before it goes out — rather than let an unpriced model spend
against a budget silently, whatever `on_anomaly` says:

```python
runbound.init(budget_usd=5.0, on_anomaly="raise",
                on_unpriced_model="refuse")   # an unknown model never runs at all
```

---
