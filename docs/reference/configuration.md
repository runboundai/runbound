# Configuration reference

[← Docs](../README.md)

Every row below is a keyword argument to `runbound.init()`. `init(**kwargs)` builds a
`RunboundConfig` from them, so these are all the parameters there are: an unknown name raises
`TypeError`, and a bad value raises `ValueError` at `init()` time, not in the middle of a run.
`tests/test_docs_configuration_reference.py` checks this page against `RunboundConfig`: every
parameter appears in exactly one row, no row names a parameter that does not exist, and each
Default is the field's own default.

Every parameter works with no plane at all. The **Plane can tighten** column says whether a
connected control plane can make the setting stricter. Only `tool_policy` is: the policy the plane
serves is merged with yours, and the merge can only remove freedom (bans are unioned, `allow` lists
intersected, a per-session limit becomes the lower of the two). The dashboard's per-tool rows fold
onto the rules on `@runbound.tool` the same way; they are not an `init()` parameter. See
[Action policy](../guides/policy.md) and [Free and connected](../concepts/free-and-connected.md).

The groups: [the plane](#the-plane), [rules and hooks](#rules-and-hooks),
[approvals](#approvals), [results](#results), [telemetry and privacy](#telemetry-and-privacy),
[local](#local).

## The plane

How this process finds a control plane, and what it does when the plane halts the fleet or goes
away. With neither `token` nor `control_plane_url` (nor their environment variables), the process
is local and opens no socket to a plane.

| Name | Type | Default | Plane can tighten | Meaning |
|---|---|---|---|---|
| `token` | `str \| None` | `None` | no | The plane's API key, sent as `Authorization: Bearer`. `RUNBOUND_TOKEN` is read when unset. A token with no URL means the hosted plane; there is none yet, so it logs one warning and the process stays local. `token=""` is a self-hosted plane that needs no auth. |
| `control_plane_url` | `str \| None` | `None` | no | A self-hosted plane's base URL. `RUNBOUND_PLANE_URL` is read when unset; `""` keeps the process local whatever the environment says. Set here with no `token`, `init()` raises `ValueError`; set only in the environment with no token, it warns once and stays local. |
| `service` | `str` | `"default"` | no | What the plane files this worker under. It also names the policy cache file, is part of every approval seal, and is the span resource's `service.name`. |
| `worker_id` | `str \| None` | `None` | no | This worker's name in the fleet. Unset, it is `<hostname>:<pid>:<6 hex chars>`, fixed for the life of the process. In production set it to a stable name (a pod or host name) so a restart is recognised as the same worker; the plane tells the processes apart by their own boot id. |
| `control_plane_poll_s` | `float` | `5.0` | no | Seconds between hellos (±10% jitter), in every link state. The plane's reply can set a different interval. Must be positive. |
| `on_halt` | `str` | `"raise"` | no | What a fleet halt does: no longer anything. A halt refuses every tool (`reason="halt"`) under either value, and the setting is kept so existing code still runs. A halt holds until the plane lifts it, and is kept in the policy cache so it survives a restart. |
| `consequential_grace_s` | `float` | `60.0` | no | How long after the last answered hello (or after start, with none) `financial`, `destructive`, `privileged` and unclassified tools keep running on the cached policy before they refuse with `reason="plane_unavailable"`. Between 15 and 60, so it never outlasts a halt. See [Plane loss](../guides/plane-loss.md). |
| `cache_dir` | `str \| None` | `None` | no | Where the last policy the plane served is kept, so a restarted worker enforces it before its first hello. Unset: `RUNBOUND_CACHE_DIR`, else the platform cache directory, else the temp directory, else memory only; never `XDG_RUNTIME_DIR`. |

## Rules and hooks

What a tool call must pass before its body runs. The order is fixed: pre hooks, argument
validation, the tool policy, the capability class rules and the action cap, approval, gate hooks,
then the body.

| Name | Type | Default | Plane can tighten | Meaning |
|---|---|---|---|---|
| `tool_policy` | `ToolPolicy \| dict \| None` | `None` | yes | Allow and deny lists, per-tool `max_calls`, `constraints`, `require_approval` with its `approval_callback`, and `on_violation` (`"block"`, `"block_and_latch"`, `"dry_run"`). A rule on `@runbound.tool` wins over one here for the same tool, with a warning. See [Action policy](../guides/policy.md). |
| `capabilities` | `dict \| None` | `None` | no | A verdict per capability class: `"allow"`, `"approve"` or `"deny"` for `read`, `write`, `external`, `financial`, `destructive`, `privileged`. Unstated classes allow. A tool is judged by the strictest of its classes; once any class is narrowed, a tool that declares no class is refused. `"approve"` refuses too, saying the approval is what is missing. Refusals raise `CapabilityRefused`. |
| `require_rules` | `bool` | `False` | no | Every `@runbound.tool` must state a rule (`blocked`, `max_calls`, `constraint`, `require_approval`) or `reviewed=True`, and a tool with a consequential class needs a rule that can refuse it. A tool that fails raises `ValueError` at decoration, or at `init()` if it was decorated earlier. |
| `max_actions_per_run` | `int \| None` | `None` | no | A cap on the tool calls a session actually executes; the next one is refused (`reason="blast_radius"`). At least 1. |
| `pre_hooks` | `tuple \| list` | `()` | no | Run on every call before any check, ahead of the tool's own `pre_hooks`. Each takes a `ToolCall` and returns `hooks.allow()`, `hooks.refuse(reason)` or `hooks.rewrite(args, kwargs)`; this is the only place arguments may change. |
| `gate_hooks` | `tuple \| list` | `()` | no | Run on every call after all the built-in checks, ahead of the tool's own `gate_hooks`. They may only allow or refuse. |
| `hook_timeout_s` | `float \| None` | `5.0` | no | A user hook still running after this many seconds counts as one that raised; `None` waits forever. The hook's thread cannot be killed and keeps running. A hook that raises or times out refuses (`reason="hook_error"`) a tool with a consequential class or no class, and is logged and skipped for `read`, `write` and `external`. |

## Approvals

Settings for `@runbound.tool(approval="required")`. See [Approvals](../guides/approvals.md).

| Name | Type | Default | Plane can tighten | Meaning |
|---|---|---|---|---|
| `approval_secret` | `str \| None` | `None` | no | The per-service secret that seals deferred approvals (HMAC-SHA256 over service, tool, request id, arguments and expiry), so any replica holding it can run an approved request and a changed argument is caught. `RUNBOUND_APPROVAL_SECRET` is read when unset. Never sent to the plane. Without it, only the worker that asked can run the request. |
| `on_deferred_result` | `Callable[[dict], None] \| None` | `None` | no | Called with `{request_id, tool, outcome, result \| error}` for each deferred approval this worker executes or refuses to execute (`executed`, `failed`, `tampered`, `clock_skew`). An exception it raises is logged and ignored. |

## Results

What happens to a tool's return value before the caller sees it: redact, truncate, scan, then the
post hooks.

| Name | Type | Default | Plane can tighten | Meaning |
|---|---|---|---|---|
| `post_hooks` | `tuple \| list` | `()` | no | Run on every result after redact, truncate and scan, ahead of the tool's own `post_hooks`. Each takes a `hooks.ToolResult` and returns `hooks.allow()`, `hooks.replace(result)` or `hooks.withhold(reason)`. One that raises or times out withholds the output of a tool with a consequential class or no class, and is tagged and skipped for `read`, `write` and `external`. |
| `redact_keys` | `tuple \| list` | `()` | no | More dict keys whose values become `"[REDACTED]"` in results (exact, case-insensitive, recursive), on top of the 37 built in. Plain strings are not redacted. |
| `result_limits` | `dict \| None` | `None` | no | How far a result is truncated, keeping JSON valid: `max_chars` (16000, the serialized size), `max_str` (200), `max_items` (20), `max_depth` (6). Each value an int of at least 1. `@runbound.tool(max_result_chars=)` overrides `max_chars` for one tool. |

## Telemetry and privacy

What leaves the process, and where to. See [Privacy](privacy.md).

| Name | Type | Default | Plane can tighten | Meaning |
|---|---|---|---|---|
| `export_events` | `bool` | `True` | no | Post events, refusals and runtime changes to the plane's `/v1/events`. `False` posts none of them; the hello still goes. |
| `send_session_keys` | `bool` | `False` | no | Leave the raw session key in an exported anomaly's message and details instead of replacing it with a short hash. Every `key_hash` field stays a hash either way. |
| `otel_endpoint` | `str \| None` | `None` | no | An OTLP/HTTP collector base URL (`http://host:4318`). When no OpenTelemetry provider is configured, one `execute_tool {tool}` span per call is posted to `/v1/traces` as gzipped OTLP/JSON, with `token` as the Bearer key, and the per-call events stop going to `/v1/events`. Must be an `http(s)` URL. See [OpenTelemetry](../guides/opentelemetry.md). |

## Local

In-process callbacks and sizing.

| Name | Type | Default | Plane can tighten | Meaning |
|---|---|---|---|---|
| `on_event` | `Callable[[dict], None] \| None` | `None` | no | Called in-process with every local record as it happens: anomalies and refusals, decisions, runtime changes and `tool_result` records. One that raises is logged and ignored. |
| `max_sessions` | `int` | `10000` | no | How many keyed sessions are kept; past it the least recently used goes first. At least 1. |

---
