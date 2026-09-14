# Self-hosted models

[← Docs](../README.md)

If you run the model yourself — vLLM, TGI, Ollama, llama.cpp, LM Studio,
LiteLLM, a fine-tune on your own GPUs — the runaway is the same and the
currency is not. **Nobody sends you an invoice, so nothing stops.** A looping
agent on a rented H100 costs you the hour either way; what it actually burns is
*capacity* — the queue every other request is waiting in.

**It already works.** Every OpenAI-compatible server is guarded by shape —
by `auto_wrap` or by an ordinary `wrap()` — with no adapter and no configuration:

```python
import openai, runbound

runbound.init(max_total_tokens=500_000, max_inflight_calls=8, on_anomaly="raise")
client = runbound.wrap(openai.OpenAI(base_url="http://gpu-box:8000/v1", api_key="x"))
```

That exact setup is what `examples/live/ollama_verify.py` runs against a real
local model, run by hand — thirteen scenarios, real tokens, real durations,
`$0.00`.

**The knobs that protect capacity.** Dollars are the wrong meter here; these
are the right ones:

| Knob | What it protects |
|---|---|
| `max_inflight_calls` | The GPU itself. Calls to one endpoint that may be in flight at once; the next one is refused **before it goes out** (detector `inflight`), rather than joining a queue that makes every user slower. Set it to the concurrency your server is actually sized for. |
| `tokens_per_minute_limit` | Sustained throughput — one session hogging the batch. |
| `max_tokens_out_per_call` | The output explosion: a model that will not stop generating. |
| `max_call_seconds`, `max_session_seconds` | Latency and the run that never ends. |
| `max_active_sessions`, `max_session_depth`, `max_child_sessions` | Fan-out — the sub-agent cascade that fills the queue with work nobody asked for. |
| `max_total_tokens`, `max_steps`, `max_events` | The session-wide walls that need no prices at all. |

**Want dollars anyway? Price your own hardware.** `custom_prices` is
`model -> (usd per 1M input tokens, usd per 1M output tokens)`, and nothing
says those have to be a vendor's numbers — GPU-hour cost divided by the tokens
an hour produces is a perfectly good internal price, and `budget_usd`,
`max_cost_per_call_usd` and the cost spike detector all start working the
moment you supply one:

```python
runbound.init(budget_usd=25.0, on_anomaly="raise",
                custom_prices={"qwen2.5:7b": (0.05, 0.05)})   # your cents, your GPU
```

**Servers that report no usage.** Some self-hosted stacks answer without a
`usage` object at all, and a guard that counts zero tokens reports green while
seeing nothing. `estimate_tokens=True` (opt-in, off by default) fills that gap
with `ceil(chars / 4)` over the request text and the answer — chat messages,
Responses input, Anthropic content blocks, and streams by accumulating text
deltas. It is used **only** when the response carried no usage; a server that
reports real numbers is always believed, and the estimate is announced once per
process so nobody mistakes it for measurement.

**Inference that never goes over HTTP.** In-process generation — `llama-cpp-python`,
`transformers`, a `torch` model in the same worker — has no client to wrap. Two
generic hooks put it on the same path everything else uses:

```python
@runbound.llm(model="llama-3.1-8b", provider="llama.cpp@local",
                tokens=lambda out: (out["usage"]["prompt_tokens"],
                                    out["usage"]["completion_tokens"]))
def generate(prompt: str) -> dict:
    return llama(prompt, max_tokens=256)

# or, when you would rather report by hand:
runbound.record_call("llama-3.1-8b", tokens_in=812, tokens_out=210,
                       duration_s=1.9, provider="llama.cpp@local")
runbound.record_call(None, 0, 0, provider="llama.cpp@local", error=exc)  # it failed
```

The decorator times the call, applies the circuit and the in-flight cap
**before** your body runs, records the tokens your callback reads, and on an
exception records the failure and re-raises it untouched. Sync and `async def`.
Budgets, loops, spikes, storms and circuits then treat local inference exactly
as they treat a hosted call.

**One circuit per endpoint.** The provider label is
`"{shape}@{host}"` — `"openai@gpu-box:8000"` — so your own box failing opens
its own circuit and refuses nothing else. This is the bug the label format
fixes: keyed by shape alone, a dead vLLM server would have failed fast on
OpenAI proper. `circuit_state("openai")` still answers for the whole shape (the
worst of its endpoints); `circuit_state("openai@gpu-box:8000")` answers for
that box.

**Honest limits.** The in-flight cap is **per process**, not per cluster —
eight replicas with `max_inflight_calls=8` allow 64 concurrent calls, and fleet
mode does not share it (a fleet-wide cap is on the [roadmap](../roadmap.md#roadmap)). A stream holds its slot until it
ends, is closed, **or is garbage collected** — an abandoned stream frees its
slot and is counted as one partial call the moment Python collects it (see
[Async and streaming](streams.md#async-and-streaming)); it does not hold the slot until
the process exits. The estimator is an
approximation, not a tokenizer: treat estimated dollars as an order of
magnitude, not a bill. And native, non-OpenAI-shaped SDKs — TGI's own client,
Bedrock, Vertex — are not wrapped yet; use `record_call` / `@runbound.llm`
around them today.

---
