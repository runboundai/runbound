# Free SDK, connected plane

[← Docs](../README.md)

The SDK is free and runs with no account. A control plane adds the rules a person sets in a
console, and the things only a shared service can do: count across workers, ask a person, stop a
fleet. This page says which is which.

## How a process connects

```python
import runbound

runbound.init()  # no plane: everything below "Free" works; nothing leaves the process
```

A self-hosted plane needs both a URL and a token: `init(control_plane_url=..., token=...)`, or
`RUNBOUND_PLANE_URL` and `RUNBOUND_TOKEN` in the environment. A token with no URL logs one warning
and the process stays local (there is no hosted plane yet). A URL from the environment with no
token also stays local, with a warning. `control_plane_url=""` pins a process local whatever the
environment says. `runbound.plane_status().mode` is `"local"` with no plane configured.

## Free: no account, no plane

| What | Where |
|---|---|
| Code rules: `blocked=`, `max_calls=`, `constraint=`, `require_approval=` on `@runbound.tool`, and `init(tool_policy=...)` | [Per-tool policy](../guides/policy.md) |
| Capability class rules (`init(capabilities=...)`), `max_actions_per_run`, `require_rules` | [Configuration](../reference/configuration.md) |
| Argument validation against the signature and type hints | [How it works](how-it-works.md) |
| User hooks: `pre_hooks`, `gate_hooks`, `post_hooks` | [How it works](how-it-works.md) |
| The post chain: redact, truncate, scan | [How it works](how-it-works.md#after-the-body) |
| Sessions and keys: `runbound.session(key)`, per-session counts | [How it works](how-it-works.md#sessions-and-keys) |
| Spans: one per call, to your OpenTelemetry provider or to `init(otel_endpoint=...)` as OTLP/JSON | [OpenTelemetry](../guides/opentelemetry.md) |
| Local records: `runbound.events()`, `decisions()`, `tool_results()`, `init(on_event=...)` | [API](../reference/api.md) |
| Self-checks: `runbound.coverage()`, `tools()`, `check()`, `assert_guarded()`, `python -m runbound check` | [Boundaries](boundaries.md#it-guards-only-what-is-decorated) |
| The LangChain callback handler | [LangChain](../guides/langchain.md) |

`otel_endpoint` needs no plane: spans go straight to the collector you name, with `token` (if
set) as the Bearer key.

## Connected: needs a plane

| What | With no plane |
|---|---|
| **Console rows**: per-tool `blocked`, `max_calls_per_session`, `approval_required`, `risk_class`, `fail_mode`, `quota`, and `dry_run` rollout, folded with the code rules (most restrictive wins) | no rows; the code rules alone apply |
| **Org tool policy**, merged with yours (it can only remove freedom) | your policy alone |
| **Quotas**: a row's limit per fixed UTC window, shared by every worker of the service | none |
| **Approvals**: `@runbound.tool(approval="required")`, sync or deferred, and a row's `approval_required` | refused at once: `reason="approval_unavailable"`; the body never runs |
| **Halt**: the plane stops every tool of the fleet (`on_halt="raise"` refuses with `reason="halt"`) | nothing to halt |
| **Policy cache**: the last served policy and rows on disk, enforced by a restarted worker before its first heartbeat | nothing cached; code rules come from code |
| **Fleet view**: each worker's tool report (names, parameters, classes, rules; never arguments) and its exported refusals and changes (`export_events`), in one console | records stay in the process |

Approvals with no plane are refused rather than run: a call that needs a person is never run
because nobody could be asked. A `capabilities={...: "approve"}` class rule refuses too
(`CapabilityRefused`), plane or not.

## What connecting adds to the call path

With a plane configured, a tool carrying `financial`, `destructive` or `privileged`, or no class,
is refused (`plane_unavailable`, retryable) once the plane has not answered for
`consequential_grace_s` (60 s by default), and while the first heartbeat is still pending unless
the policy cache was confirmed in the last 15 minutes. `read`, `write` and `external` tools keep
running on the cache. A new approval needs a connected link; a quota past its lease refuses
`quota_plane_unavailable` while the row's `fail_mode` is `closed`. A process with no plane
configured has none of this. See [Plane loss](../guides/plane-loss.md).

Hellos, the policy fetch and event export run on background threads, never on a tool call. The
plane is reached during a tool call only to file an approval (and, for a sync one, wait for the
answer) and to take a quota unit when the worker holds no lease for it (`POST /v1/quota/take`; a
refusal is then cached until the window ends).

## Related

- [The plane link](../guides/plane-link.md): the wire the SDK speaks.
- [Privacy](../reference/privacy.md): what leaves the process, connected or not.
