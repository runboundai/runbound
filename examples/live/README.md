# Live verification — runbound against a real model, for free

Thirteen scenarios run against a **real** LLM through a **real** `openai` client.
Nothing is simulated: every token count, duration and model-requested tool call
that runbound reacts to was measured off the wire. The model runs locally
under [Ollama](https://ollama.com), so the whole run costs **$0.00** and takes
about **40 seconds** on a laptop.

```
examples/live/ollama_verify.py   the runner: thirteen scenarios, PASS/INFO/FAIL, exit 1 on any FAIL
examples/live/last_report.md     the scorecard from the most recent run
```

This is the counterpart to `tests/test_real_sdk.py`, which puts the same real
SDK on a mock HTTP transport. That file proves runbound reads the SDK's
shapes correctly; this one proves the guards fire against a model nobody
scripted.

---

## Run it

```bash
ollama serve                       # in its own terminal, if it is not already running
ollama pull qwen2.5:1.5b           # ~1 GB, tool-calling capable
.venv/bin/pip install openai       # runbound itself needs nothing
.venv/bin/python examples/live/ollama_verify.py
```

`python -m examples.live.ollama_verify` works too, from the repo root.

Expected tail:

```
**13 PASS, 0 INFO, 0 FAIL** of 13 scenarios.
```

The exit code is `1` if anything FAILed, `0` otherwise. An `INFO` never fails
the run — see "Why some results are INFO" below.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUNBOUND_BASE_URL` | `http://localhost:11434/v1` | any OpenAI-compatible endpoint |
| `RUNBOUND_MODEL` | `qwen2.5:1.5b` | the model to drive |
| `RUNBOUND_API_KEY` | `ollama` | Ollama ignores it; a real endpoint will not |

Point it somewhere else — vLLM, LM Studio, Groq, Together, OpenRouter, or
OpenAI itself:

```bash
RUNBOUND_MODEL=llama3.1:8b .venv/bin/python examples/live/ollama_verify.py

RUNBOUND_BASE_URL=https://api.openai.com/v1 \
RUNBOUND_API_KEY=$OPENAI_API_KEY \
RUNBOUND_MODEL=gpt-4o-mini \
  .venv/bin/python examples/live/ollama_verify.py
```

Against a hosted endpoint the run costs real money — a few cents at
`gpt-4o-mini` rates — and the thresholds below are tuned for a slow local
1.5B model, so a fast hosted model may report scenario 3 as INFO (see below).
Nothing else changes: `runbound.wrap()` does not know or care which endpoint
is behind the client.

---

## What each scenario proves

| # | Scenario | The promise it checks |
| --- | --- | --- |
| 1 | token budget | `max_total_tokens=300` stops the session once the tokens are actually spent — counted from the provider's own `usage`, call after call. |
| 2 | per-call output cap | `max_tokens_out_per_call=40` needs no baseline and no warmup: **one** oversized answer is enough, and it is the `spike` detector reporting a `cap`, not a learned threshold. |
| 3 | spike watch | A call unlike this session's others (0.1s becomes 6s) is **reported and still served**. A behavior change is a notice, never a stop — that is the default, and it is deliberate. |
| 4 | tool loop, decorated | The model drives a real function-calling loop on `@runbound.tool get_weather`; the third identical request is refused, and the decorated body never runs a third time. |
| 5 | tool loop, undecorated | The same loop with **nothing decorated** — the agent dispatches by hand and runbound never sees a `@tool`. The wrapper reads the tool calls out of the response, so `Loop detected: model requested tool ...` fires anyway, with the response in hand and the dispatch still ahead of you. |
| 6 | policy deny | A denied tool is refused **before its body runs** — the assertion is that the function never executed, not that it was reported afterwards. |
| 7 | error storm + circuit | A second client points at `http://localhost:9` (a dead port). Three connection errors open the circuit; the fourth call raises `CircuitOpen` **without a request going out**. |
| 8 | wall-clock timeout | `max_session_seconds=0.5` stops a session that has simply been running too long, whatever it was doing. |
| 9 | fan-out depth | `max_session_depth=1` refuses the third nested `session()` block at the door, before its body runs. |
| 10 | abuse ladder | `on_spike="limit"` walks one key up the rungs — watching, limited, closed — as it repeatedly asks for 250-word essays. The trail printed as `L1a-s0 > L2a2s0 > L2a1s0 > L3a0s0` is `session_status()` after every turn: level, allowance left, strikes. |
| 11 | GPU cost budget | `custom_prices={"qwen2.5:1.5b": (1000.0, 1000.0)}` — $1 per 1k tokens, the shape of "my GPU-hour divided by the tokens it produces" — makes `budget_usd=0.30` trip on a model no vendor sells. Dollar limits work on self-hosted models the moment you price your own hardware. |
| 12 | in-flight cap | `max_inflight_calls=1`: one thread holds a stream open mid-flight while another calls the same endpoint. The second call raises `GuardrailTripped` with detector `inflight` **before a request goes out** — the guard for a GPU that can serve one call at a time. |
| 13 | per-endpoint circuits | Two clients, one shape, two boxes. Two connection errors put `openai@localhost:9` in `open` while `openai@localhost:11434` stays `closed` and answers normally — a dead box opens its own circuit and nobody else's. `circuit_state("openai")` reads the worst of the two. |

Scenarios 1–2, 6–9 and 11–13 are pure arithmetic and hold on any model.
Scenarios 3–5 and 10 depend on the model behaving like a model.

## Why some results are INFO

Three scenarios cannot be made deterministic by a guard, because what they
check is the *model's* behavior:

- **3 (spike watch)** needs the odd call to actually be odd — more than
  10x the session's median duration *and* at least 2 seconds slower. On a
  local 1.5B model, 0.1s versus 6s clears that easily; on a fast hosted model
  it may not, and the scenario reports INFO with the measured durations
  instead of failing.
- **4 and 5 (tool loops)** need the model to keep asking for the same tool.
  `qwen2.5:1.5b` gives up after one `unknown` unless it is nudged, so the
  harness appends a user turn saying "call `get_weather` for Paris again" —
  which is exactly the shape of the real bug this catches, an agent that keeps
  re-asking because the last answer was not good enough. A model that stops
  asking anyway is reported INFO.
- **10 (abuse ladder)** needs enough abnormal calls in a row to spend the
  allowance. It reports INFO with the observed rung trail if the model's
  answers were too uniform to be flagged.

INFO means "it ran, and here is what happened". Only FAIL — a guard that did
not do what it promises — sets the exit code.

## Honest caveats

- **The thresholds are demo numbers.** `max_total_tokens=300`,
  `max_tokens_out_per_call=40`, `max_session_seconds=0.5` and
  `spike_limit_calls=2` are chosen so each wall is reached in seconds. Real
  configurations are orders of magnitude larger.
- **Cost is $0.00 and so is the reported spend.** runbound has no price for
  `qwen2.5:1.5b`, so `budget_usd` cannot see a local model at all — which is
  why scenario 1 uses `max_total_tokens` rather than dollars. (Set
  `budget_usd` against an unpriced model and runbound says so, once:
  "no price known for model ... its cost is counted as $0.00".) Pass
  `custom_prices={"qwen2.5:1.5b": (0.20, 0.20)}` — your own cents per 1M
  tokens for the hardware — to make dollar limits work on a self-hosted
  endpoint. Scenario 11 does exactly that, with an absurd price so the wall is
  reached in nine calls.
- **Every scenario calls `runbound.init()` again.** That is supported —
  `init()` reconfigures the SDK and starts a fresh session and an empty keyed
  registry — and it is why scenario 7's open circuit does not affect
  scenarios 8–10.
- **openai 3.x builds its default HTTP client on `httpx2`**, whose macOS trust
  store shim fails outright on some systems. If that happens the harness says
  so once and falls back to an explicit `httpx` client. Install `httpx` if you
  see the SDK refuse to build a client at all.
