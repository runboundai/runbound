# What the SDK actually sees — and what it never sees

[← Docs](../README.md)

`runbound.init()` stores your configuration, builds the engine, starts a
session, and — with `auto_wrap` left at its default `True` — patches the OpenAI
and Anthropic SDK classes so clients built afterwards are guarded. By itself it
computes nothing. Everything runbound knows arrives through three sensors:

1. **A guarded client** — auto-wrapped at `init()`, or handed to
   `runbound.wrap()` — which sees each model call go out and come back.
2. **`@runbound.tool`**, which sees an action before its body runs and again
   when it returns or raises.
3. **`runbound.session(key)`**, which says whose work this is.

If none of them is in the path, nothing is guarded, and every detector reports
green because it has nothing to read. `runbound.coverage()` and
`runbound.assert_guarded()` exist so you never have to take that on faith.

## Where each number comes from

| Data point | Where it comes from | Blind if you skip… |
|---|---|---|
| Tokens in / out / reasoning / cached | `response.usage` on a guarded call — `prompt_tokens`/`input_tokens`, `completion_tokens`/`output_tokens`, `*_tokens_details.reasoning_tokens` (Anthropic's `output_tokens_details.thinking_tokens`), and the cached-input count (OpenAI's `prompt_tokens_details.cached_tokens`/`input_tokens_details.cached_tokens`, Anthropic's `cache_read_input_tokens` — see [Cached input tokens](../reference/compatibility.md#cached-input-tokens)). `estimate_tokens=True` only fills in when the server sends no usage at all, and never guesses a cache hit. | a guarded client. Use `record_call()` or `@runbound.llm` for calls runbound did not make — neither reports cached tokens, so those calls price every input token at the full rate. |
| Estimated cost | Those tokens × the static price table, or your `custom_prices` — cached tokens at the table's cached rate where it has one, else the full input rate. | anything that makes tokens blind — and it reads `$0.00` for a model with no price. |
| Call duration | A stopwatch around the guarded call. | a guarded client. Also blind under the LangChain handler, which reports no timing. |
| Provider errors → `error_storm`, circuits | The exception raised inside the guarded call. | any call runbound did not make. `record_call(..., error=exc)` reports one by hand. |
| Tool calls the **model asked for** → loops | Parsed off the response: `tool_calls`, Responses `function_call` items, Anthropic `tool_use` blocks. | a guarded client. |
| Tool name + argument hash **you executed** → loops, action policy, `max_calls` | `@runbound.tool`, or the LangChain handler. | any tool that is not decorated. |
| Which run this is → per-key limits, spike baselines, the ladder | `runbound.session(key)`. | without it every call lands in the one process-wide default session, so one key's spike is measured against everybody's traffic. |
| In-process inference (no HTTP call to read) | `@runbound.llm` or `runbound.record_call()`. | nothing else can see it. |

## What is exact and what is estimated

Determinism is the whole claim, so here is the line between what runbound
*knows* and what it *estimates*, in one table rather than in folklore.
Everything on the exact side is a count, a comparison or a set-membership
test over numbers this process holds. Everything on the estimated side is
either a number runbound made up because the provider gave it none, or a
fact about a fleet that is only true within a stated window. No detector
asks a model on either side. And "exact" is always exact about *what the
sensors saw*: an unguarded path is not counted at all, which is what
[`coverage()`](#how-to-be-sure) is for.

| Number | Exact or estimated | What that means, and what bounds it |
|---|---|---|
| Event and turn counts (`event_count`, `turns`) | **Exact** | Every recorded event is allocated its own step number in its session; `turns` counts `llm_call` events only. A streamed call is counted when the stream ends, and an abandoned one when Python collects it — so a count is exact about calls whose accounting has finished, not about calls still in flight. |
| Token counts, whenever the provider reports usage | **Exact** | Read verbatim off `response.usage` and never adjusted: `prompt`/`input`, `completion`/`output`, reasoning, and the cached counts. Anthropic states cache reads and cache writes as separate additive counts, so both are added into `tokens_in`. A field the provider omits reads `0`; runbound never fills a hole in a usage object with a guess. No usage at all is the `chars/4` row below. |
| Tool `deny` and `allow` | **Exact** | Set membership on the tool's name, evaluated before the function body runs. |
| `max_calls` | **Exact, per session, per process** | An integer tally of *attempts* — a call the policy refused still counts, because it was made. Fleet mode ships the org's rule to every worker but never the tally, so N workers each allow a `max_calls=1` tool once. |
| Latch state | **Exact in this process** | One flag in memory: the next `session()` entry (or the next guarded call) sees it. A latch set on another worker arrives within `control_plane_cache_s` plus one turn — see [INVARIANTS.md](../../INVARIANTS.md#latch). |
| Circuit state | **Exact in this process** | Failures counted inside `circuit_window_seconds` on the monotonic clock, per provider endpoint, with a cooldown that lets exactly one probe through. A fleet circuit is adopted on the next heartbeat, so it is shared rather than simultaneous. |
| Policy evaluation | **Exact** | `evaluate()` is a pure function of the policy, the call and the tally, applied in the fixed rule order (`deny`, `allow`, `max_calls`, `constraint`, `approval`) with no clock, no I/O and no model. Your own predicate is the one part runbound cannot speak for: one that raises refuses the call (fail-closed). |
| Token counts when the endpoint reports none (`estimate_tokens=True`) | **Estimated** | `ceil(chars / 4)` over the text runbound can read — a stand-in so a server that reports no usage is counted as something rather than as free traffic, never a tokenizer. You are told once per process in a WARNING; on the event itself only a partial (abandoned-stream) call carries `tokens_estimated`. |
| The dollar cost of a call | **Estimated** | Tokens × a static list-price table (`runbound.pricing.PRICES_AS_OF` dates it) or your `custom_prices`. Negotiated rates, batch discounts and any price published since that date are not modelled, and an unpriced model reads `$0.00` under the default `on_unpriced_model="zero"`. `budget_usd` is an exact comparison against a running total whose dollars are an estimate of your bill. |
| The admission check (`budget_admission=True`) | **Estimated, and opt-in** | Request characters / 4 at the model's input rate, plus the request's own output cap (or `admission_output_tokens`, 1024) at the output rate — always at the plain input rate, since nothing can know before the call how much of it will be a cache hit. It refuses a call before it goes out and never latches. Off by default: the wall that ships on is the post-call one. |
| What a spike *means* | **Estimated** | The arithmetic is exact — this call is over `spike_factor` × this session's median and over the absolute floor — but the reading of it is not. A spike is a behaviour-change signal, not proof of abuse. |
| Fleet totals inside the sync window | **Bounded, not exact** | `fleet_spend_offset_usd`, `fleet_tokens_offset` and fleet strikes are what the plane knew when this block opened, reused for `control_plane_cache_s` (5 s) while other workers' deltas arrive in roughly one-second batches. The worst that window can cost is stated as a bound in [INVARIANTS.md](../../INVARIANTS.md#budget), not hand-waved as "eventually". |
| An abandoned stream's record | **Partial, and estimated unless a chunk carried usage** | Its `tokens_out` is the provider's own count when any chunk carried usage (even a truthful zero) and `ceil(chars / 4)` of what streamed otherwise — `tokens_estimated` says which. Its `tokens_in` is the provider's count only when a chunk carried one: OpenAI sends usage on the final chunk alone, which an abandoned stream never reaches, so there it is `0` unless `estimate_tokens=True` estimated it from the request. Its duration covers up to the last chunk observed, not up to collection, and it is recorded when Python collects the stream, which is not necessarily promptly and never at interpreter exit. |

## What it never reads

Prompts and model replies are never read, stored, or sent — except the one
narrow case of `estimate_tokens=True`, which counts characters when the server
reported no usage. Tool arguments are hashed with sha256 and the digest is what
is kept; an [action policy](../guides/policy.md#action-policy--rules-for-what-your-agent-may-do)
hands your own callbacks the real arguments for the duration of that call and
stores none of them. Failures are recorded as the exception's class name.
Details are in [Privacy](../reference/privacy.md#privacy).

## Paths that are not guarded today

- Raw HTTP to a provider — `requests`, `httpx`, or your own transport.
- The Google Gemini / Vertex SDK, Bedrock via `boto3`, and the Mistral and
  Cohere SDKs.
- Any framework that reaches the provider through its own transport rather than
  through an OpenAI- or Anthropic-shaped client. (LangChain is covered by
  [its own handler](../guides/langchain.md#langchain--langgraph).)

For all of these, `runbound.record_call()` and `@runbound.llm` are the way
in: they take the same path a guarded client does, so budgets, spikes, limits
and circuits all see them.

## `wrap()` is canonical; `auto_wrap` is a convenience

`runbound.wrap(client)` — patching one client object you hand it — is the
mechanism the rest of the SDK is built on: it always works, on any client
whose shape it recognizes, whenever you call it. `auto_wrap` (on by default)
is a convenience layered on top: it patches the OpenAI and Anthropic SDK
*classes themselves* at `init()` time, so a client built afterwards is guarded
without a `wrap()` call anywhere in your code. Convenient, but class patching
is a guess about how your code constructs clients, and a few real situations
guess wrong:

- **Multiple SDK versions or code paths** — a class patched in one imported
  copy of `openai`/`anthropic` does nothing for a client built from a
  different copy (a vendored dependency, a plugin with its own pinned
  version).
- **Framework wrappers around the client** — some frameworks construct their
  own client internally and expose only their own call surface, so there is
  no client object left in your code for a class-level patch to reach through.
- **Test mocks** — a `unittest.mock` or a hand-rolled fake stands in for the
  real class in tests, so patching the real class guards nothing there (which
  is usually what you want in a test, but worth knowing rather than assuming).
- **Unusual init order** — a client built *before* `runbound.init()` runs
  (module-level construction, an import-time singleton) was built before the
  patch existed, so `auto_wrap` never touches it, ever.

`wrap()` on the actual client object sidesteps all four — there is no
guessing about which class or which import path, only the object in front of
you. When you are not certain `auto_wrap` reached everything, `wrap()` the
client by hand. `assert_guarded()`, next, only catches the *total* miss — a
provider SDK imported with zero guarded calls recorded; a client that is
guarded for some call sites and not others still passes it. The honest check
for a partial miss is `runbound.coverage()["guarded_calls"]` against the
call volume you actually expect, plus `wrap()` on the object you are unsure
about.

## How to be sure

```python
import runbound
from openai import OpenAI

runbound.init(budget_usd=5.0)

client = OpenAI()          # built after init(): auto_wrap already guards it

runbound.coverage()
# {'auto_wrapped': ['openai'], 'wrapped_clients': 0, 'decorated_tools': 3,
#  'guarded_calls': 12, 'tool_calls_seen': 7, 'keyed_sessions_seen': 4,
#  'providers_imported': ['openai'], 'providers_unguarded': [],
#  'last_guarded_call_age_s': 0.8, 'warnings': []}

runbound.assert_guarded()   # raises RuntimeError if a provider SDK is
                              # imported and no guarded call has been recorded
```

`coverage()` is the honest answer to "is this actually on?" — put
`assert_guarded()` in your startup path or your CI smoke test and a forgotten
`wrap()` fails loudly instead of quietly. You also get told without asking: if
a provider SDK is imported and no guarded call has landed 60 seconds after
`init()`, runbound logs a warning once. `coverage_check_seconds` moves that
deadline; `None` turns the check off.

---
