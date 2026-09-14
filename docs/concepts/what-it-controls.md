# What it controls

[← Docs](../README.md)

Five areas, each a runtime control — it changes what the agent is allowed to
do, not just what you can see about it:

| Area | Controls |
|---|---|
| **Cost** | `budget_usd` / `max_total_tokens` budgets, `max_cost_per_call_usd` / `max_tokens_out_per_call` per-call caps, `tokens_per_minute_limit` velocity, `on_unpriced_model` unknown-model policy |
| **Execution** | `max_steps`, loop detection (`loop_threshold`, `loop_window`; `repeatable=True` / `loop_ignore_tools` for tools meant to repeat), `max_call_seconds` / `max_session_seconds` timeouts, `max_active_sessions` / `max_session_depth` / `max_child_sessions` fan-out, `max_inflight_calls` concurrency, spike detection (`on_spike`, notify by default) |
| **Authorization** | `tool_policy` deny/allow, `max_calls`, `constraints`, `require_approval` + `approval_callback`, org-wide action policy with `dry_run` rollout (fleet mode) |
| **Reliability** | per-endpoint provider circuits (`on_provider_failure`, `circuit_*`), fleet halt (`on_halt`), `on_plane_loss` and `stale_halt` policy, fail-open on every internal error |
| **Governance** | central policy versions (fleet mode), refused-actions ledger, estimated exposure prevented, `plane_status()` / `fleet_status()` fleet state, `refusals` refusal profiles |

[Configuration reference](../reference/configuration.md#configuration-reference) has every knob;
[What happens when something
trips](#what-happens-when-something-trips--every-choice-in-one-place) has the
full reactions table.

---
