# runbound documentation

[← README](../README.md)

The SDK's manual. This repository manual is canonical: the site at
[runbound.co/docs](https://runbound.co/docs) renders it at build time, with the
snippets that tests execute, so a page here and its published twin cannot differ.

## Getting started

- [Start here: which door](which-door.md) — the SDK, the gateway or the action
  API, by the shape of your workload.

- [Getting started](getting-started.md) — install, run the demo, then build
  the same core loop yourself: a budget, a run and key pair, a decorated
  tool, a narrowed posture, a graded loop, a stop, handling the refusal, and a
  verify step.

## Concepts

- [Three levels of protection](concepts/three-levels.md) — runtime limits,
  identity limits, capability limits.
- [Why this exists](concepts/why.md) — the runaways this was built for, and
  what having the authority to pull the plug means.
- [How it works](concepts/how-it-works.md) — the path a guarded call takes,
  from the wrapper through the engine to the detectors.
- [What it controls](concepts/what-it-controls.md) — cost, execution,
  authorization, reliability and governance, knob by knob.
- [What the SDK actually sees — and what it never sees](concepts/what-it-sees.md)
  — the three sensors, where each number comes from, what is exact and what is
  estimated, and the paths that are not guarded today.
- [Free SDK, connected plane](concepts/free-and-connected.md) — the free
  four, and every advanced control that only turns on once a plane is
  connected.
- [Boundaries](concepts/boundaries.md) — what runbound deliberately does not
  do, what each exclusion costs and buys you, and what to use instead.

## Guides

- [Runs keyed by any id](guides/runs.md) — scoping every control to a run, a
  job, a tenant or a customer.
- [Fleet mode](guides/fleet-mode.md) — one truth across all your workers: a
  shared budget, a shared latch, shared strikes and org-wide action policy.
- [Attach: the gateway](guides/attach-gateway.md) — point `OPENAI_BASE_URL` or
  `ANTHROPIC_BASE_URL` at it; identity, refusals, fail mode, streams, what it
  cannot see.
- [Attach: the action API](guides/attach-action-api.md) — admit and report,
  the Decision, idempotency and held duplicates.
- [Attach with an agent](guides/attach-with-an-agent.md) — a prompt a coding agent
  follows to attach the SDK or the gateway, ending in `python -m runbound check`.
- [A runaway, start to finish](guides/a-runaway.md) — one incident told from
  `events()`: detected, contained, explained, recovered.
- [What changed](guides/what-changed.md) — the `runtime_change` record: the
  model, provider or policy version behind a call moved.
- [Spike detection](guides/spike-detection.md) — the zero-config ladder that
  learns what a session normally looks like and reacts when it changes.
- [Action policy](guides/policy.md) — rules for what your agent may do, and
  the algebra that merges an org policy with yours.
- [Handling refusals](guides/handling-refusals.md) — the typed exception, a
  retry predicate that actually knows when retrying could help, and
  FastAPI, Flask, background-job and streaming examples.
- [Self-hosted models](guides/self-hosted-models.md) — vLLM, Ollama, TGI and
  anything else behind an OpenAI-shaped endpoint.
- [LangChain / LangGraph](guides/langchain.md) — the callback handler, and
  what it does and does not see.
- [OpenTelemetry](guides/opentelemetry.md) — refusals, anomalies and posture
  changes as OpenTelemetry log records and three counters.
- [Async and streaming](guides/streams.md) — async clients, streamed calls
  and abandoned streams.

## Reference

- [Configuration reference](reference/configuration.md) — every keyword
  argument to `runbound.init()`.
- [API reference](reference/api.md) — the rest of runbound's public API,
  every call besides `init()`.
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
