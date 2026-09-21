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
It has two ways of deciding when to open. Both are free and local: **count
mode** is the default, and **rate mode** (`circuit_mode="rate"`) is a real
`init()` keyword too. A connected plane's own Controls can only tighten
whichever mode you configured further, or turn rate mode on from nothing
if you left `circuit_mode` at its `"count"` default — never loosen what
you set.

## Two modes

```python
runbound.init(circuit_failure_threshold=5, circuit_window_seconds=60.0,
                circuit_cooldown_seconds=30.0)   # count mode, the free default
```

**Count** (the default, and everything this circuit did before rate mode
existed): `circuit_failure_threshold` (5) failures inside
`circuit_window_seconds` (60) opens it for `circuit_cooldown_seconds` (30).
Five failures in a ten-thousand-call minute opens it; a provider that answers
everything, just slowly — 40 seconds a call, nothing ever raising — never
does, because nothing here is a failure.

**Rate** is [resilience4j](https://resilience4j.readme.io/docs/circuitbreaker)'s
sliding-window model, and the answer to exactly that gap — `circuit_mode="rate"`
on `init()`, free and local.
Every call in the trailing window — success, slow success, or failure — is
kept. Below `circuit_min_calls` in the window, nothing is judged
at all: a provider that has only answered twice cannot trip a rate meant for a
busier one. At or above it, two independent lines are checked against every
call in the window:

- the fraction that **failed**, against `circuit_failure_rate` — the same
  two failure classes as count mode (`provider`, `transport`; see the table
  below);
- the fraction slower than `circuit_slow_call_seconds` (off, `None`, unless
  you set one), against `circuit_slow_rate`.

Either one read **strictly above** its line opens the circuit — at exactly
the line, it stays closed. A slow-call rate crossing its line opens the
circuit with **zero errors**, which failure-counting alone can never do. The
minimum call count, both rates and the slow-call threshold are all real
`init()` keywords, with sensible defaults; a connected plane can only
tighten whichever ones you set (or add a slow-call threshold you left
`None`), never loosen them.

### What "slow" measures, for a streamed call and a non-streamed one

`circuit_slow_call_seconds` means something different depending on the shape
of the call, and it is worth being exact about which:

- **A non-streamed call**: request out, response in. Its whole duration —
  the same number that reaches `session_status()` and the spike detector.
- **A streamed call**: request out, **first chunk** in — not the whole
  stream. A stream has answered as soon as its first token arrives; how long
  it then keeps generating is the length of the answer, not whether the
  provider was responsive. Measuring the full stream here would open a
  healthy, chatty provider's circuit for producing long answers — exactly
  the workloads that stream in the first place (chat UIs, long-form
  generation) — which is the opposite of what this knob is for. Everything
  else about the call — its full duration for usage and spike detection,
  its token counts — is completely unaffected; only what the circuit's
  slow-call check reads is different.
- **A call that fails, streamed or not — before or after any chunk**: never
  "slow" at all. A failure counts toward `circuit_failure_rate`, on the same
  two fault classes as always (`provider`, `transport`); duration plays no
  part in that decision, whatever it was.
- **A stream abandoned before ever being read** (dropped, never exhausted,
  closed or exited): reported as `hooks.abandoned`, which the circuit — in
  either mode — never sees at all. It never happened as far as the circuit
  is concerned, in either direction: not a failure, not a slow success, not
  a healthy one.

An ordinary (non-slow) success in rate mode only adds one more good data
point to the window — it does **not** wipe the window the way a success does
in count mode. Tolerating some failures without amnesia is the entire point
of a rate; a provider recovering from one failure with one success and then
failing again is still judged on all three calls, not reset to zero after the
middle one.

Count mode's own behavior — everything above the `"rate"` section — is
unchanged, byte for byte, whether or not rate mode exists in your version.

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
count toward either mode:

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

## What an open circuit does

Your explicit choice, whatever the mode:

```python
import runbound

runbound.init(on_provider_failure="notify")   # the default: count and alert
runbound.init(on_provider_failure="open")     # also refuse calls while it is open
```

Under `"notify"` nothing is ever blocked. Failures (or, in rate mode, a
crossed rate) are counted, and the moment the line is crossed you get one
alert per outage — not one per session that ran into it — and every call
still goes out.

Under `"open"` the wrapped client raises `CircuitOpen` **before** the request
leaves your process, so a refusal costs a microsecond instead of a timeout.
After the cooldown, `circuit_half_open_calls` (default `1`, the original
single-probe rule) probes are let through; the first one to succeed closes
the circuit, and a failure starts the cooldown over. A connected plane can
only tighten this further (fewer probes), never raise it past what you set.

```python
from runbound import CircuitOpen

try:
    reply = client.chat.completions.create(model="gpt-4o", messages=msgs)
except CircuitOpen as outage:
    print(outage.provider)                # 'openai@api.openai.com'
    print(outage.retry_after)              # seconds left on the cooldown, right now
    reply = other_provider(msgs)           # your fallback, your decision
```

`outage.retry_after` is a snapshot: the moment `CircuitOpen` is raised, it
records how many seconds were left on the cooldown and a reference reading
of the breaker's own clock, then decays from that on every access — never a
number frozen once and reused, so a retry loop that reads it again a few
seconds later sees it actually count down, the way a real `Retry-After`
should. Deliberately *not* a live lookup against whatever engine happens to
exist when you ask: a caught exception's meaning must not change if a later
`runbound.init()` or `reset()` swaps the engine (and its breaker) out from
under it — this keeps counting down against the breaker it actually opened
under. `None` only when the exception carries no snapshot at all (built by
hand, as a test double might).

`CircuitOpen` is a subclass of `GuardrailTripped`, so a host that already
catches that keeps working unchanged — the call simply fails fast instead of
joining the storm.

## Narrowing the process while a circuit heals

**`circuit_posture=True`, free and local (opt-in, default `False`).**
While a circuit sits half-open — cooldown elapsed, waiting to find out if
the provider recovered — this narrows the whole process to posture
`restricted` (model calls keep serving; every non-read `@runbound.tool`
refuses). A connected plane can only turn this on if you left it off,
never turn it off if you enabled it. The narrowing lifts, and
only it, the moment the circuit fully closes; a manual narrowing, the
ladder's, or one the control plane stated directly all stand on their own
and are untouched (`runbound.state.POSTURE_SOURCES`'s `"circuit"` entry). A
failed probe re-opens the circuit rather than staying half-open, and the
restriction simply stays set through that — it is never re-entered on every
retry, which is what keeps it from flapping.

There is no per-provider scoping of *tools* in this SDK — a `@runbound.tool`
declares capability classes (`effects=`), never a provider — so "restricted
while this provider heals" is honoured the only way the posture model
actually can: process-wide, the same mechanism a manual `enter_safe_mode()`
call and the spike ladder both use. Off — the only state with no plane
connected — leaves postures exactly as they were before this control
existed: a circuit never touches one.

## `prevented`: calls actually turned away

Every call `on_provider_failure="open"` refuses without it ever leaving the
process — while a circuit is open, and every probe past `circuit_half_open_
calls` while it is half-open — is one more against that provider's own
`prevented` count. It resets to zero the moment the circuit fully closes; a
failed probe reopening it is still the same incident, so `prevented` keeps
counting through that.

## Fleet-wide: one circuit, not one per worker (fleet mode)

With a [control plane](../guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane)
connected, a provider going down is one outage for the fleet, not one per
worker rediscovering it on its own. Each worker's own transitions — opened,
half-opened, closed — are reported to the plane, which folds them per
service and per label and hands the current picture back on every worker's
next heartbeat. A worker that connects while the fleet's circuit for a
provider is already open **starts open** — including on its very first
hello, before it has ever made a call to that provider itself.

That fleet instruction is applied to every worker's local breaker the same
way a natural opening is — the same cooldown, the same half-open probes — but
it never forces a *refusal* into a worker that only asked to be told:
`on_provider_failure` is each worker's own choice, read after the breaker's
state, not before it. A worker configured `"notify"` sees the breaker read
`"open"` (`circuit_state()` reports it honestly) and keeps calling anyway,
exactly as it would for an outage it discovered itself; only a worker
configured `"open"` actually refuses. Joining an already-open fleet circuit
is the same contract as discovering the outage locally — it is `on_provider_
failure`, not the fleet, that decides whether anything is ever refused.

Whether a worker takes part in this fold at all — `circuit_fleet`, default
`True` — is a real, local `init()` keyword: `circuit_fleet=False` opts a
worker out. Opted out, a worker's circuit
stays purely local — its own transitions never leave the process, and a
fleet instruction is never forced onto its breaker either, including that
starting state on connect. Irrelevant, and never consulted, with no plane
connected — a bare `control_plane_url` and `token` are all `init()` needs
here:

```python
runbound.init(control_plane_url="...", token="...")
```

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
  without waiting for `circuit_failure_threshold` failures (count mode) or a
  rate crossing its line (rate mode). `remaining` is read as the *smallest*
  count across every bucket the provider publishes, because the tightest one
  is what will refuse the next call. It is reported as the usual `circuit`
  anomaly with `details["reason"] == "quota"` (a failure-driven opening
  carries `"failures"`).

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

The state is counted under **both** `on_provider_failure` modes and **both**
circuit modes, so you can run on `"notify"`, watch `circuit_state()` on your
health endpoint, and switch to `"open"` when you trust the numbers. It reads
`"closed"` before `init()` and whenever the answer cannot be read — a health
check must never be the thing that breaks.

Whatever the mode, when a provider call fails **the provider's own exception is
what your code catches.** The one deliberate exception: if recording that
failure is what makes a retry storm, the `GuardrailTripped` replaces it — you
have been retrying a wall and being told again what you already know is worth
less than being told to stop.

---
