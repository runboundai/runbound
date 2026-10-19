# runbound — invariants

The promises the SDK keeps today, one statement each, with the tests that assert them. Every
`Asserted by:` reference names a real test; `tests/test_invariants_doc.py` fails if one does not
exist. A promise no test covers yet is listed under [Not yet asserted](#not-yet-asserted), not
dropped. The public API is in [API.md](API.md); what each rule does is in [docs/](docs/README.md).

Terms: a **consequential** tool carries a `financial`, `destructive` or `privileged` class; an
**unclassified** tool declares no class; **the chain** is `Engine.admit_call` plus the post chain.

---

## The host application

1. **A decorated call raises only the tool's own exception or an `ExecutionRefused`** (or its
   subclasses `PolicyViolation`, `CapabilityRefused`). A runbound bug outside the chain (session
   lookup, event recording, an observer, the `on_event` callback, the plane client) is logged and
   the call goes ahead. Bad configuration raises `ValueError` at `init()` or at decoration instead.
   Asserted by: `tests/test_api.py::test_engine_failure_never_reaches_the_host`, `tests/test_policy_hook.py::test_a_broken_session_lookup_never_blocks_the_host`, `tests/test_engine_observers.py::test_an_observer_that_raises_never_reaches_the_host`, `tests/test_local_events.py::test_a_raising_on_event_callback_never_breaks_the_host_call`, `tests/test_session_sync.py::test_a_plane_that_explodes_never_reaches_the_host`, `tests/test_api.py::test_init_validates_config_loudly`

2. **The tool's own exception is re-raised unchanged**, after a `tool_error` event and an `error`
   `tool_result`; a cancelled async tool stays cancelled.
   Asserted by: `tests/test_api.py::test_tool_error_is_emitted_and_the_original_exception_re_raised`, `tests/test_post_chain.py::test_a_tool_that_raises_records_an_error_result_and_reraises_its_own_exception`, `tests/test_async_tool.py::test_a_cancelled_async_tool_is_recorded_and_stays_cancelled`

3. **Before `init()` a decorated tool is inert:** the body runs, nothing is recorded or enforced,
   and an `async def` tool is still a coroutine function.
   Asserted by: `tests/test_api.py::test_tool_works_and_records_nothing_before_init`, `tests/test_async_tool.py::test_async_tool_is_inert_before_init`, `tests/test_post_chain.py::test_the_chain_is_inert_before_init`, `tests/test_policy_hook.py::test_no_policy_is_enforced_before_init`, `tests/test_async_tool.py::test_decorated_async_function_is_still_a_coroutine_function`

4. **The removed API stays removed:** every name dropped from the old SDK raises `AttributeError`,
   its modules do not import, and `API.md` lists exactly `runbound.__all__`.
   Asserted by: `tests/test_public_api.py::test_removed_name_is_gone`, `tests/test_public_api.py::test_removed_modules_do_not_import`, `tests/test_public_api.py::test_api_md_lists_exactly_the_exported_names`

## Refusal happens before the body

5. **A refused call never runs its body**, sync or async.
   Asserted by: `tests/test_api.py::test_a_refused_call_never_runs_the_tool_body`, `tests/test_async_tool.py::test_a_refused_async_call_never_runs_its_body`, `tests/test_hooks_validation.py::test_a_sync_tool_is_refused_before_its_body_runs`, `tests/test_hooks_validation.py::test_an_async_tool_is_refused_before_its_body_runs`, `tests/test_policy_hook.py::test_denied_tool_is_refused_before_its_body_runs`

6. **Refusal precedence is fixed; the first refusal wins and nothing after it runs:** fleet halt,
   plane loss, user pre (rewrite) hooks, argument validation, policy rules (`deny` > `allow` >
   `max_calls` > `constraint`), row `blocked`, row session cap, capability class and action cap,
   quota, the policy's approval callback, a person's approval, user gate hooks, then the body.
   Asserted by: `tests/test_hooks_chain.py::test_the_order_is_rewrite_validate_policy_capability_approval_gate`, `tests/test_hooks_chain.py::test_gate_hooks_run_last_and_may_only_refuse`, `tests/test_hooks_chain.py::test_approval_runs_last_after_the_capability_check`, `tests/test_behaviour_golden.py::test_policy_rules_are_judged_before_the_capability_class`, `tests/test_policy_rows.py::test_blocked_is_judged_before_the_session_cap_before_quota_and_a_refused_call_takes_no_unit`, `tests/test_policy_core.py::test_deny_beats_every_other_rule`, `tests/test_policy_core.py::test_max_calls_beats_constraint_and_approval`, `tests/test_policy_core.py::test_approval_is_the_last_gate`

7. **A refused call takes no quota unit and asks no person.** The attempt counts in `tool_calls`
   but never as an executed action, and never against `max_calls`, which counts calls that cleared the chain.
   Asserted by: `tests/test_policy_rows.py::test_blocked_is_judged_before_the_session_cap_before_quota_and_a_refused_call_takes_no_unit`, `tests/test_engine.py::test_a_refused_attempt_does_not_count_against_the_action_cap`, `tests/test_behaviour_golden.py::test_a_refused_attempt_is_not_counted_as_executed`, `tests/test_coverage.py::test_every_attempted_tool_call_is_counted_even_a_refused_one`, `tests/test_max_calls_counts_admitted.py::test_refused_attempts_do_not_use_up_max_calls`

8. **Arguments are validated, never coerced.** A refusal (`invalid_arguments`) names the parameter
   and the types, never the value.
   Asserted by: `tests/test_hooks_validation.py::test_the_case_table`, `tests/test_hooks_validation.py::test_validation_runs_without_a_policy_and_never_coerces`, `tests/test_hooks_validation.py::test_a_reason_names_the_parameter_and_the_types_never_the_value`

9. **Only pre hooks rewrite.** The rewritten arguments are re-validated and are what policy sees;
    the caller's containers are never mutated. A rewrite from a gate hook, a framework adapter or
    during an approved deferred execution (after the seal) is a hook error.
    Asserted by: `tests/test_hooks_chain.py::test_a_rewrite_is_visible_to_policy`, `tests/test_hooks_chain.py::test_a_rewrite_is_revalidated_and_never_mutates_the_callers_arguments`, `tests/test_hooks_chain.py::test_a_gate_hook_cannot_rewrite`, `tests/test_hooks_chain.py::test_a_framework_adapter_cannot_apply_a_rewrite`, `tests/integration/test_approvals_plane.py::test_a_rewrite_after_the_seal_is_a_hook_error`

## Failures fail by capability class

10. **A user hook that raises, returns nonsense or outlives `hook_timeout_s`** refuses with
    `hook_error` for a consequential or unclassified tool; for a `read`, `write` or `external` tool
    it is logged once, skipped and tagged `hook_error:<name>` on the `tool_result`.
    Asserted by: `tests/test_hooks_chain.py::test_a_raising_user_hook_refuses_a_consequential_or_unclassified_tool`, `tests/test_hooks_chain.py::test_a_raising_user_hook_is_logged_once_and_a_read_write_or_external_tool_continues`, `tests/test_hooks_chain.py::test_a_hook_that_outlives_the_timeout_counts_as_raising`, `tests/test_hooks_chain.py::test_a_hook_returning_nonsense_is_a_hook_error`, `tests/test_post_chain.py::test_a_skipped_pre_hook_failure_is_tagged_on_the_result_event`

11. **A built-in that raises refuses every class:** the validator, policy evaluation, a policy
    constraint or approval callback. Its exception text is not in the refusal.
    Asserted by: `tests/test_hooks_chain.py::test_a_built_in_that_raises_refuses_even_a_read_tool`, `tests/test_engine.py::test_enforce_policy_fails_closed_when_evaluation_breaks`, `tests/test_policy_core.py::test_a_constraint_that_raises_blocks_the_call_fail_closed`, `tests/test_policy_core.py::test_an_approval_callback_that_raises_blocks_the_call_fail_closed`, `tests/test_policy_core.py::test_a_policy_the_engine_cannot_evaluate_refuses_the_call`

12. **The post chain never passes an unprocessed result on:** a redaction that raises withholds
    the output for every class; a user post hook that raises or times out withholds it for a
    consequential or unclassified tool and is tagged and skipped otherwise; a failed truncation
    falls back to the plain size cap; a failed scan leaves the result untagged. Exception text
    reaches neither the model nor the event.
    Asserted by: `tests/test_post_chain.py::test_a_raising_redactor_withholds_the_output_for_every_class`, `tests/test_post_chain.py::test_a_raising_user_post_hook_withholds_a_consequential_or_unclassified_result`, `tests/test_post_chain.py::test_a_raising_user_post_hook_is_tagged_and_skipped_for_read_write_external`, `tests/test_post_chain.py::test_a_post_hook_that_outlives_the_timeout_counts_as_raising`, `tests/test_results.py::test_a_failed_truncation_falls_back_to_the_plain_size_cap`, `tests/test_post_chain.py::test_a_failed_scan_tags_and_passes_the_result_through`, `tests/test_post_chain.py::test_the_exception_text_never_reaches_the_model_or_the_event`

## Results

13. **Redact, then truncate, then scan, then user post hooks.** Secret-named keys are redacted
    exactly, case-insensitively, at any depth; plain strings are not. A truncated JSON result
    stays valid JSON with markers. A non-JSON result passes through untouched.
    Asserted by: `tests/test_results.py::test_process_redacts_then_truncates_then_scans`, `tests/test_results.py::test_secrets_are_redacted_at_depth_case_insensitively_keeping_the_key`, `tests/test_results.py::test_matching_is_exact_not_substring`, `tests/test_results.py::test_a_plain_string_is_not_redacted_in_v1`, `tests/test_results.py::test_an_oversized_result_comes_back_truncated_as_valid_json_with_markers`, `tests/test_results.py::test_a_non_json_value_passes_through_untouched`, `tests/test_post_chain.py::test_a_post_hook_can_replace_the_result_after_redact_and_truncate`

14. **The injection scan tags and never blocks**, and reports pattern names, never matched text.
    Asserted by: `tests/test_post_chain.py::test_the_scan_tags_the_event_and_never_blocks_the_result`, `tests/test_results.py::test_scan_reports_names_never_the_matched_text`

15. **Every decorated call ends in one `tool_result` record** (`ok`, `error`, `refused`, `withheld`
    or `pending_approval`) with `duration_s` and `outcome` and never the result, even in a latched
    session, in its own ring apart from `events()`.
    Asserted by: `tests/test_post_chain.py::test_the_event_carries_duration_and_outcome_ok`, `tests/test_post_chain.py::test_a_refused_call_records_a_refused_result_even_in_a_latched_session`, `tests/test_post_chain.py::test_tool_results_is_its_own_ring_and_events_stays_for_refusals`, `tests/test_post_chain.py::test_on_event_receives_a_tool_result_for_an_ok_call_a_flagged_result_and_a_withheld_one`

## Policy only tightens

16. **An org policy merged onto the local one never permits a call either side refuses**, never
    loosens the mode, and a dry-run org policy never changes the local mode. Constraints and the
    approval callback stay local.
    Asserted by: `tests/test_policy_merge_properties.py::test_the_merge_never_permits_a_call_either_input_refuses`, `tests/test_policy_merge_properties.py::test_the_merged_mode_is_never_looser_than_either_input`, `tests/test_policy_merge.py::test_a_dry_run_remote_never_changes_the_local_mode`, `tests/test_policy_merge.py::test_constraints_and_the_approval_callback_stay_local`

17. **A dashboard row folds onto the code's rules, most restrictive wins, field by field:**
    `blocked` OR, the lower session cap, `approval_required` OR, the code's `approval_mode` when it
    declares one, the lower timeout, the stricter `risk_class` (it can only raise the tool's
    class), `fail_mode` `closed`. A malformed row is skipped.
    Asserted by: `tests/test_policy_rows.py::test_a_row_can_only_add_never_loosen_what_the_code_requires`, `tests/test_policy_rows.py::test_a_dashboard_row_blocks_a_tool_the_code_allows`, `tests/test_policy_rows.py::test_a_row_session_cap_and_the_codes_cap_the_lower_wins`, `tests/test_policy_rows.py::test_code_wins_on_approval_mode_and_the_timeout_is_the_lower`, `tests/test_policy_rows.py::test_a_rows_risk_class_raises_the_tools_class_and_never_lowers_it`, `tests/test_policy_rows.py::test_fail_mode_closed_wins_and_malformed_rows_refuse_their_tool`

18. **Dry run refuses nothing.** A `dry_run` row or a `dry_run` policy records what it would have
    refused (`would_refuse`) and lets the call run; a dry-run approval row asks nobody.
    Asserted by: `tests/test_policy_rows.py::test_a_dry_run_row_refuses_nothing_and_records_would_refuse`, `tests/test_policy_rows.py::test_a_dry_run_approval_row_asks_nobody`, `tests/test_policy_rows.py::test_a_dry_run_quota_is_counted_and_records_would_refuse_without_refusing`, `tests/test_policy_core.py::test_dry_run_logs_and_never_raises`

19. **Rows are replaced only when `rows_hash` changes**; a `policy_version` bump alone changes nothing.
    Asserted by: `tests/test_policy_rows.py::test_rows_replace_only_when_their_hash_changes_and_a_version_bump_alone_changes_nothing`, `tests/integration/test_rows_quota_plane.py::test_an_unchanged_rows_hash_replaces_nothing_even_when_the_version_moves`

20. **A capability class rule refuses a decorated tool with `CapabilityRefused`** and never
    latches; a call the decorator did not wrap skips the class rules.
    Asserted by: `tests/test_engine.py::test_a_capability_class_rule_refuses_a_decorated_tool`, `tests/test_engine.py::test_an_undecorated_call_skips_the_class_rules`, `tests/test_decision_level.py::test_a_tool_refused_by_a_class_rule_is_a_process_decision`

21. **Only `block_and_latch` stops a session:** every later call in it is refused with the first
    anomaly; other session keys keep working; `block` and `dry_run` never latch.
    Asserted by: `tests/test_latch.py::test_a_latched_user_is_refused_on_every_later_tool_call`, `tests/test_latch.py::test_the_latch_is_per_key_and_other_keys_keep_working`, `tests/test_policy_core.py::test_block_does_not_latch_the_session`, `tests/test_policy_core.py::test_dry_run_never_latches`

## Quotas

22. **Leases plus takes never exceed a quota across workers.** Held leases are spent first, all
    of them dropped when the window changes; a per-key quota is never leased; a full window is
    cached until it ends (one round trip per window).
    Asserted by: `tests/integration/test_rows_quota_plane.py::test_three_sdk_workers_against_twenty_per_clock_minute_admit_exactly_twenty`, `tests/test_policy_rows.py::test_a_held_lease_is_spent_before_any_take`, `tests/test_policy_rows.py::test_everything_held_is_dropped_when_window_start_changes_or_the_window_has_passed`, `tests/test_policy_rows.py::test_a_per_key_quota_always_takes_with_the_session_key_hash_and_never_leases`, `tests/test_policy_rows.py::test_an_exhausted_window_is_cached_one_round_trip_per_window`

23. **With the plane away the lease in hand is spent, then every class refuses
    `quota_plane_unavailable`** under `fail_mode: closed`; `open` continues at a tenth of the limit
    per heartbeat period.
    Asserted by: `tests/test_policy_rows.py::test_with_the_plane_away_the_lease_is_spent_then_quota_plane_unavailable_for_every_class`, `tests/test_policy_rows.py::test_fail_mode_open_continues_at_a_static_share_per_heartbeat_period`, `tests/integration/test_rows_quota_plane.py::test_a_plane_outage_spends_the_lease_then_refuses_quota_plane_unavailable`

24. **A call that will ask a person takes no unit itself:** the approval create takes it, a retried
    create reuses its `request_id`, and a deferred execution never takes a second. A full quota on
    create refuses `quota_exceeded` with `retry_after` and is not asked again inside the window.
    Asserted by: `tests/test_policy_rows.py::test_a_tool_that_will_ask_a_person_takes_nothing_the_plane_takes_the_unit_at_create`, `tests/test_policy_rows.py::test_a_deferred_execution_does_not_take_a_second_unit`, `tests/test_approvals.py::test_the_create_carries_the_key_hash_and_the_same_request_id_on_a_retry`, `tests/test_approvals.py::test_a_429_on_create_is_quota_exceeded_with_retry_after_and_no_retry`, `tests/test_approvals.py::test_nothing_is_asked_again_inside_the_window`

## Approvals

25. **No connected plane, no approval:** a tool that needs one is refused at once
    (`approval_unavailable`) and its body never runs; a degraded link refuses only *new* requests.
    Asserted by: `tests/test_approvals.py::test_with_no_plane_an_approval_tool_is_refused_at_once_and_the_body_never_runs`, `tests/test_cache.py::test_in_degraded_everything_runs_on_the_cache_but_new_approvals_refuse`

26. **Sync approval:** the request carries the final arguments (after rewrite hooks); a denial
    refuses `approval_denied`, expiry `approval_timeout`; a transport error is retried until the
    deadline and is never read as a denial.
    Asserted by: `tests/integration/test_approvals_plane.py::test_the_request_carries_the_final_arguments_after_a_rewrite_hook`, `tests/integration/test_approvals_plane.py::test_sync_approve_runs_the_tool_and_reports_it`, `tests/integration/test_approvals_plane.py::test_sync_deny_refuses_with_approval_denied`, `tests/integration/test_approvals_plane.py::test_sync_timeout_refuses_with_approval_timeout`, `tests/integration/test_approvals_plane.py::test_a_plane_that_is_down_is_never_read_as_a_denial`

27. **Deferred approval returns `pending_approval` at once and the body does not run.** Once
    approved, one worker executes it through the tool's full chain, so a policy that tightened since
    still refuses it.
    Asserted by: `tests/integration/test_approvals_plane.py::test_deferred_returns_pending_then_a_worker_claims_and_executes`, `tests/integration/test_approvals_plane.py::test_two_replicas_exactly_one_executes`, `tests/test_approvals.py::test_a_policy_that_tightens_after_approval_stops_the_deferred_execution`

28. **The seal is verified before anything runs.** An HMAC-SHA256 over the canonical
    `{v, service, tool, request_id, args, not_after}`; any changed field or wrong secret fails it,
    nothing runs and the plane is told `tampered`; a valid MAC past `not_after` (+300 s) is
    `clock_skew`, and nothing runs either. The secret is never in the seal or sent to the plane.
    Without a secret, only the requesting worker runs the request, with the arguments it parked.
    Asserted by: `tests/test_approvals.py::test_the_mac_is_hmac_sha256_over_the_canonical_body`, `tests/test_approvals.py::test_any_changed_field_or_a_wrong_secret_breaks_the_seal`, `tests/test_approvals.py::test_a_missing_secret_malformed_seal_or_non_dict_args_never_verifies`, `tests/test_approvals.py::test_not_after_has_a_300_second_tolerance`, `tests/test_approvals.py::test_a_valid_mac_with_an_expired_not_after_is_clock_skew_not_tampered`, `tests/test_approvals.py::test_the_seal_does_not_contain_the_secret`, `tests/integration/test_approvals_plane.py::test_a_tampered_seal_is_refused_and_reported_tampered`, `tests/integration/test_approvals_plane.py::test_a_restart_with_no_seal_an_unsealed_request_is_never_run_elsewhere`

29. **`runbound.current_request()` names the request while its deferred execution runs and is
    `None` otherwise**: the idempotency key for a request offered twice. A running execution is
    renewed on every hello; a claim the plane took back stops being renewed.
    Asserted by: `tests/test_approvals.py::test_a_running_execution_is_named_on_every_hello_until_it_finishes`, `tests/test_approvals.py::test_current_request_is_none_outside_an_execution`, `tests/test_approvals.py::test_a_claim_the_plane_took_back_stops_being_renewed_and_a_cap_of_twenty_applies`

30. **An outcome report survives an outage:** queued append-only (`0600`, no argument or result),
    sent on the next connected hello, once.
    Asserted by: `tests/test_approvals.py::test_the_queue_is_append_only_0600_and_holds_no_args_or_results`, `tests/test_approvals.py::test_drain_sends_each_record_once_keeps_the_unsent_and_dedupes`, `tests/test_approvals.py::test_a_report_the_plane_cannot_take_is_queued_and_drained_on_the_next_connected_hello`, `tests/integration/test_reports_queue_real.py::test_a_report_made_while_the_plane_is_unreachable_is_delivered_exactly_once_later`

31. **Registration refuses a deferred tool whose arguments are not JSON-friendly** (`ValueError`).
    Asserted by: `tests/test_approvals.py::test_a_deferred_tool_must_have_json_friendly_arguments`, `tests/integration/test_approvals_plane.py::test_registration_rejects_a_deferred_tool_with_arguments_that_are_not_json`

## Plane loss and the policy cache

32. **Once a worker has had a first handshake or loaded a sealed cache, the plane-loss gate never refuses a `read` or
    `external` tool** (nor `write` with another tag), in any link state, on a cache of any age; a bare `write` (no
    other tag) counts as unclassified and refuses `plane_unavailable` only when the worker holds no policy within
    24 h. (A quota or an approval on that tool still needs the plane: 23, 25.) Before that first handshake or cache
    load no governed call runs: `read`, `write` and `external` refuse `policy_unavailable`, the other classes
    `plane_unavailable`. After the plane is lost, a destructive tool follows the service: with NO service defaults
    it is refused `plane_unavailable` once `consequential_grace_s` has passed (33); with enforced service defaults
    the grace does not apply: the sealed policy holds for the OFFLINE WINDOW, counted from the last policy
    confirmation (an authenticated, whole hello after which the worker holds the policy the plane announced; a
    degraded hello or a failed fetch is not one, and a restart resumes from the cache's `confirmed_at`, never from
    boot). Inside it a destructive tool an operator's row ALLOWS keeps running, while an UNRULED one is decided by
    the defaults' ask and is refused `approval_unavailable` (a new approval needs a connected link); once it has
    ended, EVERY consequential or unclassified tool (`write` + `destructive` included; a bare `write` stays under
    K8), row-allowed or unruled, is refused `plane_unavailable` ("the offline-execution window of N s has ended and the
    plane has not confirmed the policy"). The split is exact: inside the window an unruled tool is `approval_unavailable`;
    after it, every consequential tool is `plane_unavailable`. A row that explicitly DENIES keeps refusing in both; expiry
    only ever ADDS refusals and removes none. The window is the plane's
    `service_defaults.offline.execution_s` (60 to 86400; 15 minutes for a new service), which `init(offline_execution_s=)`
    may only shorten; a served value that cannot be read is 15 minutes; nothing served is the legacy 24 h, unchanged.
    A confirmation dated more than 5 s in the future, or a wall clock that moved back against the monotonic one, counts as expired.
    `read` tools run either way.
    Asserted by: `tests/test_cache.py::test_read_write_external_run_on_the_cache_in_every_state`, `tests/test_cache.py::test_a_bare_write_is_unclassified_for_fail_mode_so_it_refuses_when_the_policy_is_unavailable`, `tests/contract/test_enforcement_floor.py::test_o_flr_4_no_governed_call_runs_before_the_first_handshake_or_sealed_cache_load`, `tests/test_cache.py::test_through_the_decorator_a_consequential_tool_refuses_and_a_read_tool_runs`, `tests/test_session_sync.py::test_a_plane_that_is_down_never_stops_a_tool`, `tests/integration/test_plane_loss_real.py::test_killing_the_plane_refuses_a_destructive_tool_after_the_grace_where_the_service_has_no_defaults_and_a_read_tool_runs`, `tests/integration/test_plane_loss_real.py::test_killing_the_plane_does_not_refuse_a_destructive_tool_a_row_allows_while_the_service_holds_its_defaults`, `tests/test_service_defaults.py::test_an_unruled_destructive_tool_the_defaults_ask_for_is_refused_approval_unavailable_when_the_plane_is_gone_and_its_body_never_runs`, `tests/integration/test_plane_loss_real.py::test_killing_the_plane_refuses_an_unruled_destructive_tool_approval_unavailable_while_the_service_holds_its_defaults`, `tests/test_offline_window.py::test_inside_the_window_a_consequential_tool_runs_and_at_its_end_it_is_refused`, `tests/test_offline_window.py::test_an_unconfirmed_hello_does_not_extend_the_window_and_a_confirmed_one_does`, `tests/test_offline_window.py::test_a_restart_resumes_from_the_cache_and_is_never_renewed_by_booting`, `tests/test_offline_window.py::test_nothing_served_is_exactly_todays_behaviour_and_init_can_still_shorten_it`, `tests/test_offline_window.py::test_an_explicit_deny_row_refuses_inside_the_window_and_after_it_and_a_row_allow_runs_only_inside`

33. **Consequential and unclassified tools refuse `plane_unavailable`** once
    `consequential_grace_s` (15 to 60, default 60) has passed since the last answered hello (or
    start) where the service holds no enforced defaults (with them the grace is inert and the offline window applies: see 32), and, before the first hello, unless the cache was confirmed within 15 minutes.
    Asserted by: `tests/test_cache.py::test_consequential_tools_follow_the_grace_period_and_the_cache`, `tests/test_cache.py::test_a_cache_confirmed_more_than_15_minutes_ago_is_not_trusted_by_consequential_tools_at_boot`, `tests/test_cache.py::test_consequential_grace_s_is_configurable_between_15_and_60`, `tests/integration/test_plane_loss_real.py::test_a_destructive_tool_waits_for_a_hello_when_there_is_neither_a_hello_nor_a_cache`

34. **A process with no plane configured has no plane gate.**
    Asserted by: `tests/test_cache.py::test_no_plane_configured_means_no_plane_gate`

35. **Link state:** `connecting`, `connected`, `degraded` (3 missed hellos, or one reply saying
    the plane cannot read its own state), `unavailable` (60 s since the last answered hello: time,
    not a count).
    Asserted by: `tests/test_cache.py::test_the_four_states_and_their_transitions`, `tests/test_cache.py::test_connecting_counts_misses_toward_degraded`, `tests/test_cache.py::test_one_fleet_state_unavailable_reply_is_degraded`

36. **A restarted worker enforces the cached policy before its first hello.** The cache is written
    atomically (no torn reads), never lets an older `policy_version` overwrite a newer one, sits in
    a `0700` directory as a `0600` file, never under `XDG_RUNTIME_DIR`, and holds no token. Rules
    declared in code never come from it.
    Asserted by: `tests/test_cache.py::test_a_new_worker_enforces_the_cached_policy_before_any_hello`, `tests/test_cache.py::test_four_writers_and_four_readers_give_no_torn_reads`, `tests/test_cache.py::test_an_older_version_never_overwrites_a_newer_one`, `tests/test_cache.py::test_the_layout_and_permissions`, `tests/test_cache.py::test_xdg_runtime_dir_is_never_used`, `tests/test_cache.py::test_round_trip_and_no_token_on_disk`, `tests/test_cache.py::test_code_declared_rules_are_never_in_the_cache`, `tests/integration/test_plane_loss_real.py::test_a_restart_enforces_the_cached_rows_before_the_first_hello`

37. **The cache file is MAC'd with a key derived from the token:** a hand-edited, truncated,
    other-token or other-service file is ignored as if absent. This is tamper-evidence, not a
    boundary against anyone who can read the token.
    Asserted by: `tests/test_cache.py::test_a_hand_edited_cache_is_rejected`, `tests/test_cache.py::test_another_tokens_cache_another_service_garbage_and_truncation_are_rejected`, `tests/test_cache.py::test_a_cache_with_a_different_token_is_not_loaded`

38. **A fleet halt refuses every tool of every class**, whatever `on_halt` says; it holds until the
    plane lifts it (silence never releases it) and survives a restart while the plane is down; a `narrow`
    halt is a full halt; `on_halt="warn"` changes nothing.
    Asserted by: `tests/test_session_sync.py::test_a_fleet_halt_refuses_even_a_read_tool_under_stop`, `tests/test_plane_loss.py::test_a_halt_is_never_released_by_silence`, `tests/test_wall_sdk.py::test_a_narrow_halt_is_a_full_halt`, `tests/test_wall_sdk.py::test_on_halt_warn_logs_and_still_refuses_the_tool`

## What leaves the process

39. **No plane configured, no network:** loading and running a guarded target that inits without a
    token makes no connection.
    Asserted by: `tests/test_check.py::test_no_network_is_touched_without_a_token`, `tests/test_session_sync.py::test_a_process_without_a_plane_url_stays_local`

40. **The plane is off the call path:** a tool call with no approval and no quota row makes no
    network call on the calling thread, and a collector that is down never blocks or raises.
    Asserted by: `tests/test_session_sync.py::test_tool_calls_never_touch_the_network_on_the_calling_thread`, `tests/test_spans.py::test_a_down_collector_never_blocks_or_raises_and_a_400_drops`

41. **Arguments and results stay in the process,** with one documented exception: an approval
    request sends the call's final `args` to the plane so a person can decide. Everywhere else they
    travel only as digests: `args_hash` on events, `args_digest` and `result_bytes` on spans,
    `result_digest` on an approval report. Events carry no error text; refusals and anomalies carry
    no arguments; the tool report carries parameter names and types, never default values.
    Asserted by: `tests/test_spans.py::test_no_argument_no_result_no_exception_text_leaves_the_process`, `tests/test_local_events.py::test_records_never_carry_call_arguments_prompts_or_replies`, `tests/test_policy_hook.py::test_raw_arguments_never_reach_the_exception_or_the_anomaly`, `tests/test_decision_evaluation.py::test_policy_rule_refusals_state_their_rule_and_never_the_arguments`, `tests/test_plane_types.py::test_event_to_wire_has_no_error_text_field_at_all`, `tests/test_change_signals.py::test_records_carry_no_tool_argument`, `tests/test_tool_report.py::test_a_default_makes_a_parameter_optional_and_the_value_never_travels`

42. **The session key leaves only as an unsalted sha256** (so one caller matches across workers),
    unless `send_session_keys=True`.
    Asserted by: `tests/test_privacy_wire.py::test_the_wire_anomaly_carries_the_hash_and_not_the_key`, `tests/test_privacy_wire.py::test_an_exported_anomaly_carries_no_raw_key`, `tests/test_privacy_wire.py::test_the_exporter_sends_raw_keys_only_when_told_to`, `tests/test_otel_export.py::test_a_raw_session_key_never_leaves_only_its_hash`, `tests/test_plane_types.py::test_key_hash_never_contains_the_key`

43. **`args_hash` is salted per process:** an equality token inside one process, not a fingerprint
    across workers or restarts.
    Asserted by: `tests/test_hash_salt.py::test_the_hash_is_not_a_fingerprint_across_processes`, `tests/test_hash_salt.py::test_the_hash_differs_from_a_raw_unsalted_sha256`

44. **One span per call** (`execute_tool {tool}`), to a configured OpenTelemetry provider or the
    stdlib OTLP emitter, never both; with neither, no span. A refusal leaves the status UNSET with
    `runbound.outcome=refused`.
    Asserted by: `tests/test_spans.py::test_one_span_per_call_for_ok_error_refused_and_withheld`, `tests/test_spans.py::test_with_a_provider_configured_spans_go_there_and_the_stdlib_emitter_stays_silent`, `tests/test_spans.py::test_no_endpoint_and_no_provider_means_no_spans_and_no_cost`

45. **The token and the approval secret stay out of `repr()`.**
    Asserted by: `tests/test_config_plane.py::test_repr_hides_the_token`, `tests/test_approvals.py::test_the_secret_comes_from_init_or_the_environment`

---

## Not yet asserted

True in the code today, with no test pinning it:

- **`GuardrailTripped` is `ExecutionRefused`**, the same class object (`runbound.exceptions`).
- **The refusal `reason` set is closed** (`runbound.exceptions.REASONS`), and `retryable` is true only
  for `plane` and `plane_unavailable`. Individual reasons are asserted; the set and the retryable
  table as a whole are not.
- **The halt and plane-loss doors run before the user pre hooks** (`Engine.admit_call` order). The
  later positions are asserted (invariant 6); these two are not.
- **An unclassified decorated tool is refused by the class rules once any class is narrowed**
  (`ClassRules.allows`).
- **The runtime is standard library only** (`dependencies = []` in `pyproject.toml`) and `import
  runbound` opens no connection. Invariant 39 covers a no-token run, not a bare import.
