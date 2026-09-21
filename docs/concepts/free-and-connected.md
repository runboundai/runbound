# Free SDK, connected plane

[← Docs](../README.md)

**Open-source runtime. Cloud control plane.**

Runbound's SDK is yours. The fleet intelligence is ours. This page states
the line and what falls on each side of it.

## Five principles

1. **Every local deterministic safety or control feature is free forever.**
   If it can be decided correctly from local state, it is in the
   open-source SDK, configurable in code, with no account, no token, no
   cloud dependency and no telemetry sent to us by default.
2. **No feature is made artificially worse in the SDK to create a
   paywall.** No "detected but will not stop", no trial timers, no plan
   checks.
3. **The paid product sells coordination, authoritative state, central
   control, evidence and operations**, not the ability to perform a local
   `if`. You can enforce a budget locally for free; you pay when that
   boundary has to stay true across your fleet and be managed centrally.
4. **A free cloud tier exists as the activation path**, limited by scale
   and history, never by crippled semantics.
5. **Pricing follows protected infrastructure and coordination scale**
   (protected agents, synced workers, history), not individual safety
   events.

**Architecture rules that follow:** plan gating happens on the plane, never
in the SDK — `grep -rn "plan" runbound-sdk/runbound` finds no branch on a
plan name. Entitlements and Controls are separate objects: entitlements say
what your org's plan includes (fleet policy, simulation, retention,
agents, workers); Controls say what a run may do, and the control engine
never reads an entitlement. Local and central configuration merge
tighten-only, one function: your code states a control, and a connected
plane can only narrow it, or state one from scratch where your code leaves
it unconfigured — never loosen what you already set. Local telemetry is
free: `runbound.events()`/`runbound.decisions()` read an in-memory ring of
this process's own anomalies, refusals, posture transitions and Decisions,
with no account and nothing sent anywhere.

## Every control is free and local

Every `init()` keyword in the
[configuration reference](../reference/configuration.md) is free, forever,
including six that a connected plane can additionally coordinate across
your fleet: circuit rate mode and posture-narrowing, loop shapes beyond
"repeat", the budget soft line, `max_actions_per_run`, class-rule
capabilities, and spike detection with the abuse ladder. Configure any of
them in code and they run with no account at all — the plane's own version
of each is a *tightening*, never a requirement.

| Feature | Free, open-source SDK (local) | Runbound Cloud (coordination, state, control, evidence, ops) |
|---|---|---|
| Dollar and token budgets, reservation, per-call ceilings | yes | fleet-shared counters, fleet reservation, windows that survive restarts |
| Steps, events, run time | yes | fleet configuration |
| Concurrency, fan-out, actions per run, blast radius | yes, per process | fleet blast radius |
| Loop and cycle shapes, retry and error storms | yes | cross-worker history |
| Spike detection, the ladder | yes | baselines across restarts, peer baselines |
| Provider circuit, count and rate modes | yes | shared circuit state |
| Tool rules, capability classes, class rules, constraints | yes | org and agent policy, versioning, rollout |
| Postures and safe mode | **yes, locally configured** | fleet-wide posture, Narrow, automatic rollout, incident history |
| Approvals | local callback and same-process approve | remote approvals, the queue, the audit line |
| MCP client enforcement | yes | central governance of MCP tools |
| Dry run / shadow | yes, locally | centralized simulation and policy impact preview |
| Decisions, local events, local audit trail | yes | durable centralized records, timelines, export |
| Coverage, control surface, CI gate | yes | fleet control surface, drift over time |
| Hierarchy (process, agent, key, run, tag scopes) | yes, inside one process | org, service and agent scopes across workers |
| Refusal profiles, pricing, custom prices | yes | fleet-managed |
| Kill switch | local manual stop | fleet Stop and Narrow with convergence |
| Remote latch, alerts routing, dashboard, roles, API keys, retention, SSO, RBAC, SLA | none | all |

## The mechanism

A connected plane delivers a `Controls` payload on its heartbeat to your
worker, tightened against your own `init()` configuration by one merge
function (`runbound.controls_merge`) — the same algebra whichever control
it touches. The SDK's enforcement code ships in the open-source package
and runs from `init()` on, with or without a plane; a connected plane can
only narrow a value you already set, or hand you one from scratch where
you left a field unconfigured. `coverage()["fleet"]` says whether a plane
is coordinating right now (`"connected"`) or not
(`"local protection active; fleet coordination: not connected"`) — the one
nudge, never a per-call log line or a warning at `init()`.

The SDK itself never refuses anything for plan reasons: entitlements are a
separate object, carrying only coordination and scale facts (protected
agents, synced workers, history), never a control name — the control
engine reads no entitlement at all.

---
