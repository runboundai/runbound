# Retry storms and the provider circuit breaker

[← Docs](../README.md)

The failures themselves are free. The retry loop around them is not: a provider
answering 429 while your SDK retries, your framework retries, and your own
`for attempt in range(5)` retries — every attempt costing latency and quota,
and every one that half-succeeds costing real dollars, with nothing that could
possibly work happening. runbound counts failed calls in two places, because
they are two different facts.

**Per session — the `error_storm` detector.** A wrapped client that raises
records an `llm_error` event, and a failing `@runbound.tool` already records
a `tool_error`. More than `error_storm_limit` (default **10**) of them in the
trailing 60 seconds is a `critical` anomaly that follows `on_anomaly` and
`on_trip` like any other detector — this is your *agent's* behavior, and the
session it belongs to is the thing to stop. Set `error_storm_limit=None` to
turn it off.

**Per provider — the circuit.** The circuit is process-wide and keyed by
provider, because a provider is down for everyone, not for one unlucky caller.
`circuit_failure_threshold` (5) failures inside `circuit_window_seconds` (60)
opens it for `circuit_cooldown_seconds` (30).

The key is the client's shape **and the endpoint it points at** —
`"openai@api.openai.com"`, `"openai@localhost:11434"`,
`"anthropic@api.anthropic.com"`, `"openai@default"` for a client that names no
`base_url`. Two OpenAI-compatible endpoints in one process are therefore two
circuits: a dead vLLM box opens its own and never refuses a call to OpenAI
proper. `circuit_state()` takes either form — a full label for that endpoint,
or a bare shape (`"openai"`) for the **worst** state among its endpoints, so a
health check written against `circuit_state("openai")` keeps meaning what it
meant.

Which failures count is a four-way classification, and only two of the four
open a circuit:

| Class | What it is | Counts? |
| --- | --- | --- |
| `provider` | 408, 425, 429, any 5xx, and every flavour of timeout | **yes** |
| `transport` | connection reset or refused, DNS, TLS — the request never arrived | **yes** |
| `application` | `TypeError`, `ValueError`, `KeyError`, pydantic `ValidationError`, and any other 4xx (400, 401, 404) | no |
| `cancel` | your code cancelled the call | no |

`provider` and `transport` both count because both say the *next* call cannot
succeed either, which is the only question a breaker asks. The other two never
do: a bad request or a bad key is a bug in your code, a `TypeError` in your own
callback is not the provider's fault, and opening a circuit over either would
stop your calls to a provider that is answering perfectly. The alert an open
circuit raises names the class in `details["fault"]`, because "the provider is
answering 503" and "we cannot reach the provider" are different pages in a
runbook.

The classifier reads `exc.status_code` when the exception carries one — the
provider answered, and its own verdict beats any guess of ours — and otherwise
the exception's class name, walking its base classes. Provider SDK types
(`openai.APIConnectionError`, `anthropic.APITimeoutError`) are matched **by
name**: runbound imports neither package, so the answer is the same whether or
not they are installed. An exception runbound cannot place is `application` —
it will not stop your calls over something it does not understand.

What an open circuit *does* is your explicit choice:

```python
import runbound

runbound.init(on_provider_failure="notify")   # the default: count and alert
runbound.init(on_provider_failure="open")     # also refuse calls while it is open
```

Under `"notify"` nothing is ever blocked. Failures are counted, and the moment
the threshold is crossed you get one alert per outage — not one per session
that ran into it — and every call still goes out.

Under `"open"` the wrapped client raises `CircuitOpen` **before** the request
leaves your process, so a refusal costs a microsecond instead of a timeout.
After the cooldown, exactly one probe is allowed through; if it works the
circuit closes, if it fails the cooldown starts again.

```python
from runbound import CircuitOpen

try:
    reply = client.chat.completions.create(model="gpt-4o", messages=msgs)
except CircuitOpen as outage:
    reply = other_provider(msgs)     # your fallback, your decision
    print(outage.provider)           # 'openai@api.openai.com'
```

`CircuitOpen` is a subclass of `GuardrailTripped`, so a host that already
catches that keeps working unchanged — the call simply fails fast instead of
joining the storm.

## Reading the provider's own rate-limit headers (opt-in)

Both providers already tell you how much quota is left and when it comes back
— Anthropic on twelve `anthropic-ratelimit-*` headers, OpenAI on
`x-ratelimit-remaining-requests` / `-tokens` and their resets — and a 429 adds
`Retry-After`. `circuit_reads_quota=True` lets the circuit act on that instead
of only counting failures:

```python
runbound.init(on_provider_failure="open", circuit_reads_quota=True)
```

Two things change, and nothing else:

- **A 429's `Retry-After` sets that opening's cooldown** instead of
  `circuit_cooldown_seconds`. The provider said when to come back; guessing 30
  seconds over that is worse.
- **A bucket at zero opens the circuit pre-emptively**, until its reset,
  without waiting for `circuit_failure_threshold` failures. `remaining` is read
  as the *smallest* count across every bucket the provider publishes, because
  the tightest one is what will refuse the next call. It is reported as the
  usual `circuit` anomaly with `details["reason"] == "quota"` (a failure-driven
  opening carries `"failures"`).

No header may hold a circuit shut for longer than **one hour**, whatever it
says: your `circuit_cooldown_seconds` is the floor, that ceiling is the cap,
and a reset a provider or a proxy states in days cannot wedge your agent shut.

**The honest limit — a plain successful call carries no headers at all.** Both
SDKs hand your code a parsed model (`anthropic.types.Message`,
`openai.types.…`) with no `.headers` anywhere on it, and runbound will not
change how your call is made to get at them: making it raw would change what
your code receives, and no header is worth that. So a pre-emptive opening
happens in exactly two situations:

1. from an **error** response — every `APIStatusError` keeps
   `.response.headers`, which is where a 429's `Retry-After` lives; or
2. from a call **your own code** already made through `with_raw_response` /
   `.parse()`, so the object in hand has `.headers` on it.

On an ordinary successful call the check is one attribute lookup that misses,
and nothing happens. Streaming is out of scope entirely.

It is off by default because a header your gateway, proxy or LLM router
rewrites should not stop your traffic by surprise. Everything about it is
fail-open: a header that cannot be read says *nothing* — an unreadable
`remaining` is never treated as zero, so an agent behind a proxy that strips
rate-limit headers keeps calling a provider that is answering perfectly. With
the option off, the circuit behaves exactly as it did before it existed.

Nothing a header *said* ever leaves your process: the anomaly carries the
derived numbers (the cooldown now in force, `reason`) and no header name or
value.

**We open the circuit and hand you the signal; we never route.** Picking a
fallback provider is an application decision with your keys, your prices and
your quality bar in it — the same boundary as never writing your bot's replies.

Ask at any time:

```python
runbound.circuit_state("openai")                  # worst of the openai@* endpoints
runbound.circuit_state("openai@localhost:11434")  # that one box
```

The state is counted under **both** modes, so you can run on `"notify"`,
watch `circuit_state()` on your health endpoint, and switch to `"open"` when
you trust the numbers. It reads `"closed"` before `init()` and whenever the
answer cannot be read — a health check must never be the thing that breaks.

Whatever the mode, when a provider call fails **the provider's own exception is
what your code catches.** The one deliberate exception: if recording that
failure is what makes a retry storm, the `GuardrailTripped` replaces it — you
have been retrying a wall and being told again what you already know is worth
less than being told to stop.

---
