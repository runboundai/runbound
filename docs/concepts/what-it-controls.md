# What it controls

[← Docs](../README.md)

Five areas, each a runtime control — it changes what the agent is allowed to
do, not just what you can see about it:

| Area | Controls |
|---|---|
| **Cost** | `budget_usd` / `max_total_tokens` budgets, `max_cost_per_call_usd` / `max_tokens_out_per_call` per-call caps, `tokens_per_minute_limit` velocity, `on_unpriced_model` unknown-model policy |
| **Execution** | `max_steps`, four loop shapes — `repeat`, `sequence`, `retry`, `stall` (`loop_shapes`, `loop_threshold`, `loop_window`, `loop_max_period`, `loop_stall_turns`; `polling=True` / `loop_ignore_tools` for tools meant to repeat), all free and local — `max_call_seconds` / `max_session_seconds` timeouts, `max_active_sessions` / `max_session_depth` / `max_child_sessions` fan-out, `max_inflight_calls` concurrency, spike detection (`on_spike`, notify by default, free and local) |
| **Authorization** | `tool_policy` deny/allow, `max_calls`, `constraints`, `require_approval` + `approval_callback`, org-wide action policy with `dry_run` rollout (fleet mode) |
| **Reliability** | per-endpoint provider circuits (`on_provider_failure`, `circuit_*`), fleet halt (`on_halt`), `on_plane_loss` and `stale_halt` policy, fail-open on every internal error |
| **Governance** | central policy versions (fleet mode), refused-actions ledger, estimated exposure prevented, `plane_status()` / `fleet_status()` fleet state, `refusals` refusal profiles |

## Capabilities and postures

A tool declares the capability classes it carries; a **posture** says which
classes may run right now, so a run that gets hot loses the right to act
without losing the right to think. `runbound.posture()` names the one in
force, and `runbound.enter_safe_mode(posture=…)` sets it by hand.

| Posture | read | write | external | financial | destructive | privileged |
|---|---|---|---|---|---|---|
| `full` | allow | allow | allow | allow | allow | allow |
| `restricted` | allow | allow | deny | deny | deny | deny |
| `read_only` | allow | deny | deny | deny | deny | deny |
| `no_side_effects` | deny | deny | deny | deny | deny | deny |
| `stopped` | deny | deny | deny | deny | deny | deny |

`no_side_effects` refuses every tool and still lets model calls out; `stopped`
is the run being over. Safe mode is `posture() != "full"`. See [Classify by
capability](../guides/policy.md#classify-by-capability) for what moves a
posture and what a refusal says.

[Configuration reference](../reference/configuration.md#configuration-reference) has every knob;
[What happens when something
trips](#what-happens-when-something-trips--every-choice-in-one-place) has the
full reactions table.

---
