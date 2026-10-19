# runbound documentation

[← README](../README.md)

The SDK's manual. This repository manual is canonical. runbound is a tool
boundary: `@runbound.tool` puts a policy check in front of every tool an agent can
call.

- [Getting started](getting-started.md): install, guard a tool, handle the
  refusal, check what is guarded, connect a plane.

## Concepts

- [How it works](concepts/how-it-works.md): the hook chain, in order.
- [Free SDK, connected plane](concepts/free-and-connected.md): what needs an account.
- [Boundaries](concepts/boundaries.md): what runbound deliberately does not do.

## Guides

- [Per-tool policy](guides/policy.md): code rules, console rows, dry run, quotas.
- [Approvals](guides/approvals.md): sync and deferred, the seal, idempotency.
- [Plane loss](guides/plane-loss.md): the four modes, the cache, what refuses.
- [Upgrading from 0.11.x](guides/upgrading-from-0-11.md): console rules need 1.x; a 0.x worker ignores them.
- [Handling refusals](guides/handling-refusals.md): the typed exception and every reason.
- [OpenTelemetry](guides/opentelemetry.md): one span per tool call.
- [LangChain / LangGraph](guides/langchain.md): the opt-in reference integration (tested combinations, limits, migration) and the callback handler.
- [The plane link](guides/plane-link.md): the wire the SDK speaks.

## Reference

- [Configuration](reference/configuration.md): every `init()` parameter.
- [API](reference/api.md) and [API.md](../API.md): every public name.
- [Privacy](reference/privacy.md): what leaves the process.
- [Guarantees and limitations](reference/guarantees.md): what is promised, what is not.
- [INVARIANTS.md](../INVARIANTS.md): each invariant and the test that asserts it.
