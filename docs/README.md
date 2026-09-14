# runbound documentation

[← README](../README.md)

The SDK's manual. Every page here is also published, with executed snippets,
at [runbound.co/docs](https://runbound.co/docs).

## Getting started

- [Getting started](getting-started.md) — install, wire the three sensors
  around one agent run, check what is actually guarded, and catch the trip.

## Concepts

- [Three levels of protection](concepts/three-levels.md) — guard the model
  calls, one session per run, refuse actions before they run.
- [Why this exists](concepts/why.md) — the runaways this was built for, and
  what having the authority to pull the plug means.
- [How it works](concepts/how-it-works.md) — the path a guarded call takes,
  from the wrapper through the engine to the detectors.
- [What it controls](concepts/what-it-controls.md) — cost, execution,
  authorization, reliability and governance, knob by knob.
- [What the SDK actually sees — and what it never sees](concepts/what-it-sees.md)
  — the three sensors, where each number comes from, what is exact and what is
  estimated, and the paths that are not guarded today.

## Guides

- [Runs keyed by any id](guides/runs.md) — scoping every control to a run, a
  job, a tenant or a customer.
- [Fleet mode](guides/fleet-mode.md) — one truth across all your workers: a
  shared budget, a shared latch, shared strikes and org-wide action policy.
- [Spike detection](guides/spike-detection.md) — the zero-config ladder that
  learns what a session normally looks like and reacts when it changes.
- [Action policy](guides/policy.md) — rules for what your agent may do, and
  the algebra that merges an org policy with yours.
- [Self-hosted models](guides/self-hosted-models.md) — vLLM, Ollama, TGI and
  anything else behind an OpenAI-shaped endpoint.
- [LangChain / LangGraph](guides/langchain.md) — the callback handler, and
  what it does and does not see.
- [Async and streaming](guides/streams.md) — async clients, streamed calls
  and abandoned streams.

## Reference

- [Configuration reference](reference/configuration.md) — every keyword
  argument to `runbound.init()`.
- [The detectors](reference/detectors.md) — how uncontrolled execution is
  prevented, which anomaly wins a tie, and the opt-in admission check.
- [What happens when something trips](reference/reactions.md) — every choice,
  in one place: `on_anomaly`, `on_trip`, alerting, and what the caller sees.
- [Retry storms and the provider circuit breaker](reference/circuit-breaker.md)
  — per-endpoint circuits and the provider's own rate-limit headers.
- [Model-requested tool calls](reference/model-requested-tools.md) — loops
  caught before your code dispatches them, without `@tool`.
- [Time and fan-out limits](reference/limits.md) — call and session
  timeouts, session depth, child sessions and in-flight caps.
- [Works with](reference/compatibility.md) — the compatibility matrix, the
  priced model families, and cached input tokens.
- [Privacy](reference/privacy.md) — what the telemetry reveals, and what it
  never reveals.
- [Guarantees and limitations](reference/guarantees.md) — what is promised,
  what is not, and where the edges are.

## Roadmap

- [Roadmap](roadmap.md) — shipped, still open, and explicitly out of scope.
