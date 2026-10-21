# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0a7] - 2026-10-21

### Added

- **A worker that stops cleanly tells the plane.** A process that exits normally, by `sys.exit` or by Ctrl-C, and `runbound.shutdown()` send one `goodbye` (at most 1.5 seconds, never raises), so the console shows the worker as stopped and not as disconnected, and a stopped worker does not hold a Promote or Restore waiting for an acknowledgement. `init(goodbye_on_exit=False)` turns the automatic goodbye off (`shutdown()` always says it). A crash, a kill or a default SIGTERM says nothing and the worker stays disconnected, as before. Needs a plane that has the route; an older plane answers 404 and the SDK ignores it.

- A call that `RunboundMiddleware` (or `govern_tools`) itself governs, a plain LangChain tool, is now recorded at its own boundary: `runbound.boundary` is `langchain-middleware` on the call span and on the start marker (whose `runbound.declared_target` takes the same value, so starts are still counted), and `runbound.capability_source` is `langchain_middleware` when the effects were stated to the middleware. Before, these calls were labelled `sdk-decorator`, so the console could not show which boundary decided. A `@runbound.tool` function under the middleware (the decorator decides) and the callback handler keep `sdk-decorator`. `runbound.contract_version` stays `1`: a new value of an existing attribute.

### Changed

- **What to check before upgrading (a7): the connected plane is now the default destination of the built-in tool-call span exporter.** Starting with a7, when you connect a control plane and leave `otel_endpoint` unset, Runbound configures its built-in tool-call span exporter to send to that plane. Set `otel_endpoint=False` before upgrading to disable this built-in export, or specify an explicit collector URL. An application-configured OpenTelemetry provider continues to take precedence and must be configured separately. This setting does not disable other control-plane communication. `python -m runbound check` (and its JSON, additive `spans` object) shows the resolved destination, mode and this process's sent, queued and dropped counts. Legacy per-call events follow the resolved exporter: they leave `/v1/events` only while the exporter is on, so nothing is dropped or counted twice. An application `TracerProvider` present at `init()` keeps the call spans and the plane default is not started. Install your `TracerProvider` before `runbound.init()`, or set `otel_endpoint=False`: a provider installed after `init()` still gets the spans while the plane default is on.

### Fixed

- **A worker that starts while the plane is down logs one warning line, not a 55-line traceback.** The line names the cause (for example `URLError: <urlopen error [Errno 61] Connection refused>`) and says to check that the control plane is reachable at the configured URL. The chained traceback is logged only when the logger is at DEBUG. Behaviour is unchanged: the call still fails closed on the sealed cache or `policy_unavailable`.
- **The first call of a brand-new worker no longer fails just because `init()` was followed at once by a call.** A worker with no cache and no answered hello used to refuse its very first call `policy_unavailable` ("the control plane has not answered yet and there is no sealed policy cache"), so the README quick start's first run was a refusal. That call now waits for the first answer within `confirm_wait_s` (default 3 s, 0 turns it off), like the restart case, and is judged by the policy the answer brought; with no answer in time it is refused as before. It waits at the start of admission, before anything reads the policy, and an async call waits without blocking the event loop.
- `python -m runbound check` (and `runbound.check()`) now recognises an app governed by the LangChain middleware. With plain tools and `RunboundMiddleware` or `govern_tools`, it used to print "Nothing is guarded in this process" and exit 1, which failed a CI gate on a correctly governed app. It reports `GUARDED` with the line "A LangChain middleware was created in this process; it governs the tools of any agent it is attached to (each tool is listed after its first call)", exits 0, and the JSON report gains an additive `middleware` object (`installed`, `tools`); `runbound.coverage()` gains `middleware_installed` and `middleware_tool_names`. The heartbeat's coverage block is unchanged.

### Documentation

- Approval mode for a chat agent: the LangChain guide and the README say what a wait (the application holds the chat turn open: give it a timeout and a way to cancel), a deferred result (the model gets `pending_approval` now; the later outcome reaches `on_deferred_result`, not that conversation, so the application delivers the update and never describes pending as done) and no opt-in (`approval_unavailable`) look like to the model. Suspended/resume stays experimental.

**What to check before upgrading**

- A filter, alert or query on `integration_boundary = 'sdk-decorator'` no longer matches calls the LangChain middleware governs; those now carry `langchain-middleware`. Ingest, the plane and the console store and show the value as given, so nothing else changes.

## [1.0.0a6] - 2026-10-20

What this release is for: LangChain / LangGraph as an opt-in, supported reference integration at four tested combinations, a startup wait that stops a restart from refusing a worker's first consequential calls while its first hello is in flight, and a floor for the optional `langchain` extra. Nothing is enabled by default and no decision is loosened: the policy a worker holds still decides, and neither waiting nor restarting renews the offline window.

**LangChain and LangGraph.** Runbound supports an opt-in LangChain / LangGraph reference integration for locally executed tools through `RunboundMiddleware` (`wrap_tool_call` / `awrap_tool_call`) and `govern_tools`. It provides tool-boundary policy enforcement, explicitly configured approval handling and correlated action records. Support is limited to the tested combinations, execution layouts and approval configurations documented in the [LangChain guide](docs/guides/langchain.md); installing the extra does not enable governance automatically. This is an alpha release, not a compatibility claim for every version in the dependency range.

| Python | langchain | langgraph |
|---|---|---|
| 3.11.17 | 1.2.11 | 1.2.0 |
| 3.13.16 | 1.3.0 | 1.2.0 |
| 3.13.16 | 1.4.6 | 1.2.16 |
| 3.12.15 | 1.4.6 | 1.2.16 |

All four rows: macOS arm64, langchain-core 1.6.12, langgraph-prebuilt 1.1.1, langgraph-checkpoint 4.3.0, langgraph-checkpoint-sqlite 3.1.2, pydantic 2.14.1; 111 tests per row. These are not four full reruns of every scenario (the restart and race checks ran at one pinned point). Linux, Windows, versions between the rows and other checkpointers are untested. Middleware, approval waits, execution binding and resume stay explicit opt-ins, and callback installations are not migrated automatically.

### Added

- `runbound.flush(timeout=3.0)` and `runbound.shutdown(timeout=3.0)`: deliver what is queued (call spans and events) before a process ends. Python skips its exit hooks when SIGTERM takes its default action, which is what Docker and Kubernetes send, so a worker stopped that way lost the spans still in its queue; call `runbound.shutdown()` in your SIGTERM handler or a `preStop` hook. Both are idempotent, do nothing before `init()` and hold the caller at most three seconds. `flush` drains to a reachable ingest; spans queued during an outage are lost if the worker exits before it reconnects (there is no disk spool, and lossless telemetry is not claimed). (An opt-in SIGTERM handler is not part of this release.)
- `init(confirm_wait_s=)` (seconds, 0 to 10, default 3; 0 disables the wait): see Fixed. The span of a call that waited carries `runbound.waited_ms`.

### Fixed

- **Plain LangChain tools through `RunboundMiddleware` / `govern_tools`: a console rule with an argument condition (`amount > 200 -> deny`) is now evaluated on the call's arguments.** The middleware did not register the tool's signature, so a condition could not read the arguments and the unknown-condition rule refused every call (a 19 refund included) as `condition_unevaluable`; an approval request also lacked the bound arguments. A `@runbound.tool` function was not affected. **The defect is present since the early integration**, so it is in 1.0.0a5 too, where the LangChain path was internal and off by default. The log line for a decorated tool under the middleware is now informational (that layout is expected); an attached callback handler still warns. The messages say the integration is off until enabled (no longer "internal and disabled by default"), and the missing-package error names the extra and the guide instead of an old floor.
- After a restart, a consequential call whose cached policy confirmation has expired can wait for the first policy confirmation, within the first three seconds after boot. The call is then evaluated against the policy the worker holds, including restrictions received during that wait. If no valid confirmation arrives, the call remains refused. Waiting and restarting do not renew the offline window. Set `confirm_wait_s=0` to disable the wait; the maximum setting is 10 seconds. Async calls do not block the event loop while waiting. A worker that restarts after a quiet period longer than its offline window no longer has its first calls refused merely because its first hello was still in flight, when that hello confirms the policy in time.

### Changed

- With `RunboundMiddleware` (or `govern_tools`), a refusal by a `@runbound.tool` function inside the tool now reaches the model as the same error `ToolMessage` the middleware uses for its own refusals, instead of an `ExecutionRefused` raised out of `agent.invoke`. Presentation only: the decorator still decides, and a refusal by an attached `RunboundCallbackHandler`, or by a decorator with no middleware, is raised as before.
- The `langchain` extra now installs `langchain>=1.2.11,<2` and `langgraph>=1.2,<2` next to `langchain-core`: the install constraints of the lowest tested combination. They are constraints, not a statement that every version in between is supported.

### Documentation

- The LangChain guide gains the tested combinations, a tested-pin install example, the supported layouts and the limits (no exactly-once claim, key-based redaction, provider-hosted tools and custom or unwrapped paths outside support), and states the migration rule below.

**What to check before upgrading**

- `runbound[langchain]` now installs `langchain>=1.2.11` and `langgraph>=1.2` (with `langchain-core`). An app pinned to an older langchain keeps using the callback handler with `langchain-core` alone: install `runbound` and `langchain-core` without the extra.
- **Leaving the old callback handler installed can leave its weaker governance in charge.** Nothing migrates for you. The new middleware raises no exception on a refusal (an error `ToolMessage` instead), and when both are installed the boundary closest to the tool governs: the old handler then keeps its weaker checks (no argument validation, no class rules for an undeclared tool). Duplicate-install detection prevents a double admission and a double quota unit; it does not make the new middleware's guarantees apply. Remove the handler deliberately when you switch.
- **Suspended and resume execution is experimental in this release.** How long an approved request stays resumable is a control-plane deployment setting (`RUNBOUND_SUSPENDED_RESUME_S`), not one the SDK or the console selects, and the seven-day default it falls back to is not ratified. Fresh-call governance and approval waits are not affected. Enabling any of middleware, approval wait, suspension or execution binding is your explicit choice; none is on by default.
- **A restarted worker can hold its first consequential call for up to three seconds** in the case described under Fixed. If that is not wanted, `init(confirm_wait_s=0)` keeps the previous behaviour (the call is refused at once). Only an authenticated policy confirmation accepted by the worker renews the offline window.
- No exactly-once or independently verified external-outcome claim is made: an execution bind is not a start marker. Redaction is key-based, not a scan of free text. Records from SDKs up to 1.0.0a4 can lack an action identity or start evidence, and those SDKs emit no span for a cancelled call.

## [1.0.0a5] - 2026-10-19

What this release is for: deferred approvals that work for real tools, a bounded offline window, and a record of each call that survives loss (identities, a start marker, honest delivery counters). Everything new is additive or off by default; a plane that does not know a new key ignores it, and an older plane leaves the SDK's behaviour as it was.

**What a team gets**

- **Deferred approvals for real tools.** A parameter the app supplies (a DB session, a client, the tenant) is marked `Annotated[Session, runbound.Injected]`: it is never stored, sealed, shown to the approver or read by a rule, and `init(provide={...})` supplies it again when the approved request runs. pydantic v2 models, `datetime`, `Decimal`, `UUID` and `Path` are accepted as deferred arguments.
- **A bounded offline window.** A service with enforced defaults runs consequential tools on its last policy confirmation for a window: **new services default to 15 minutes; existing services keep 24 hours until an operator changes it**; `init(offline_execution_s=)` can only shorten it. A restart does not renew it.
- **A call you can follow.** Every governed call carries an `action_id`, an `attempt_id` and an `evaluation_id`; the span and the refusal name each other; an `execution_started` marker is emitted just before the tool's own code runs; a cancelled call is settled; the re-check of an approved call is kept as an evaluation of its own; a span says where its capability declaration came from.
- **Delivery that tells you what it lost.** Telemetry lanes tell a plane that said no from a plane that is down and count what they drop; span batches carry an `X-Runbound-Batch` token so an ingest can drop a retried insert, and a 413 halves a batch instead of dropping it; the hello says where this worker's evidence can go (`evidence_coverage`).
- **An approval that cannot run twice (opt-in).** `init(approval_bind=True)` asks for requests the plane binds to one execution attempt. Off by default.

**What to check before upgrading**

- `exc.reason` has a new value, `prior_outcome_unknown` (not retryable; only with `approval_bind=True`), and `on_deferred_result` has a new outcome, `superseded` (an approved request that did not run because the policy moved). A handler that switches on the closed sets needs a branch or a default.
- **The marker and your ingest.** The `execution_started` marker is a span of its own: a receiver that counts every span it is sent counts it as a call. Runbound's ingest excludes it by name, and **that ingest must be deployed before an SDK that emits the marker**; with any other receiver, tell it apart by name.
- **The offline window needs no action** for an existing service (it keeps 24 hours). A new service refuses consequential tools 15 minutes after the plane stops confirming its policy; read tools, external tools and a bare `write` are unaffected.
- A `datetime` argument on a deferred tool used to be refused at decoration and is now accepted. A deferred call whose model does not decode back to what was passed (a `SecretStr`, `exclude=True`, an alias-only field) is refused `invalid_arguments` before any approval is opened.
- **Pending ratification:** the seven-day (604800 s) default for a suspended approval is a plane-side setting and reaches nobody through this release (the mode is only reachable through the LangChain integration, which is not part of it).

The detail follows.

### Added

- **Deferred approvals for real tools.** `runbound.Injected` marks a parameter the app supplies (or LangChain's `InjectedToolArg` when already imported); it is never stored, sealed, digested, shown to the approver, read by a rule or part of the span's `args_digest`, and the new `init(provide={Session: make_session, "tenant": fn})` supplies it again, afresh, when the approved request runs (else its default; else the request ends `failed`, `injected_unavailable:<param>`). Deferred arguments may be pydantic v2 models, `datetime`/`date`/`time`, `Decimal`, `UUID` and `Path` (also inside `Optional`/`list`/`dict`): encoded when filed, decoded when run; the digest and the seal are over the encoded JSON, so the plane and the seal format are unchanged. A registration error names the parameter and the fix. A parameter marked `Injected` may have a hint that cannot be resolved at run time (a class imported only under `TYPE_CHECKING`); it is then supplied by name or default, not by type.
- **The offline-execution window.** The plane serves `service_defaults.offline.execution_s` (60 to 86400); a service with enforced defaults counts it from the last *policy confirmation* (an authenticated hello not flagged degraded, after which the worker holds the policy announced), not from any answered hello. After it every `financial`/`destructive`/`privileged`/unclassified tool (also `write`+`destructive`) refuses `plane_unavailable`, naming the window. A restart resumes from the cache's new `confirmed_at`; a confirmation dated in the future or a wall clock that moved back counts as expired. A served value the SDK cannot read is 15 minutes; nothing served (an older plane) is the old behaviour exactly. The hello carries `offline_window_s`; `plane_status()` gains `offline_window_s` and `offline_deadline` (an estimate).
- **Identities.** `action_id`, `attempt_id` (when a call is accepted) and `evaluation_id` (once, when the engine starts the chain) are random, carry nothing about the call and are never an argument. They ride on the span (`runbound.action_id`, `.attempt_id`, `.evaluation_id`, `runbound.boundary` = `sdk-decorator`, `runbound.contract_version` = 1), on the refusal's anomaly and on the approval request body. A governed call made inside another governed tool's body is a new action that names `parent_action_id` (kept across asyncio tasks and `asyncio.to_thread`; a bare thread records none).
- **A refused call's span and its refusal name each other.** The span carries `runbound.anomaly_id` and the anomaly carries the span's `span_id` and `trace_id`.
- **The re-check of an approved call is an evaluation of its own.** The span carries `runbound.recheck.evaluation_id`, `.outcome` (`passed` or `refused`, with `.reason`) and what the re-check read (`.rule_revision`, `.cfg_hash`, `.policy_version`, `.service_default_revision`). The `recheck_*` attributes keep their contract.
- **An execution start marker.** Just before a tool's own code runs (after admission, any approval, the re-check and every gate) the SDK ends a zero-length child span `execution_started`, once per attempt, never for a call that is refused, parked for an approval or refused again by the re-check. It carries the attempt's identity and `runbound.marker.tool`, never an argument, digest, decision or reason. It travels on the span's exporter, so a death inside the exporter's one-second window loses it; under your own `TracerProvider` it goes to your tracer.
- **Capability provenance.** `runbound.capability_source` (`decorator`, `mcp_annotation`, `undeclared`), `runbound.capability_effective` (only when a console row raised the classes) and `runbound.service_version` (`RUNBOUND_SERVICE_VERSION`); new `@tool(effects_source=)` for integrations that translate an annotation.
- **Batch identity on spans.** Each OTLP post carries `X-Runbound-Batch: <boot_id>.<exporter_id>.<seq>(.p<N>)*`. A batch that fails retryably is kept as an immutable unit, resent first and byte for byte, with new spans queued behind it; a 413 halves it down to four levels (a part refused at the limit, or a single span, is dropped, counted once). An ingest that does not read the header ignores it.
- **Evidence coverage in the hello.** `coverage_digest` (every hello) and `evidence_coverage` (first hello, on a change, at most hourly): whether `otel_endpoint` is set, whether a `TracerProvider` is detected, the events lane (`open`/`stopped`/`disabled`) and the span and events counters. Local state only; under 300 bytes.
- **The execution bind, opt-in.** `init(approval_bind=True)` sends `capabilities: ["bind_v1"]` on the approval create and the hello; for a request the plane files as bind-required, the approved call (or its deferred execution, under its `claim_token`) binds to its attempt before the body runs: `first: true` goes on; `first: false` or a 409 refuses `prior_outcome_unknown`; no answer refuses `approval_unavailable` whatever the fail mode. A legacy request, and a plane that does not know the key, behave as before. Library calls for integrations: `engine.resume_admit` (atomic reservation of the session's units for a resumed call), `approvals.resolve_replay` (the by-request lookup table) and `approvals.withdraw` (pending only). An approval the plane says holds no quota unit (`quota_held: false`) takes one the normal way; a held unit is never taken twice.

### Fixed

- **A tool whose served effects name a class this version does not know is judged as its known classes plus `unclassified`.** It used to be judged on its known classes alone, so a set like `read` plus an unknown class ran as plain `read`, and a set of only unknown classes raised on the call path. The strictest reading now applies, never looser than either.
- **`RunboundCallbackHandler` no longer records a LangGraph interrupt or parent command raised in a tool as a tool error;** the call ends `cancelled`. Every other behaviour of the handler is unchanged.
- **The `execution_started` marker carries the service version** (`runbound.service_version`) when `RUNBOUND_SERVICE_VERSION` is set, as the call span already did, so an action whose call span was lost can still be placed in a version.
- **A resumed approval no longer counts twice against `max_actions_per_run`.** A dispatched resume was counted once when it was admitted and again when its start marker committed, so a cap of two refused the second legitimate resume.
- **A deferred request refused by the execution-entry re-check reports `superseded`, not `failed`.** It used to be reported `failed` with no decision record, and the plane kept the quota unit the request held. It now reports `superseded` with the reason and both phases of the record, as the sync path always did, and `on_deferred_result` receives `outcome="superseded"`.
- **A cancelled call is settled.** A task cancelled while the tool body runs, or while the call waits for a synchronous approval, used to leave its span un-ended and tell the plane nothing. The span now ends with outcome `cancelled`, the `tool_result` carries `cancelled` (not `tool_error`), and a synchronous approval is reported (`failed` after the body began, the quota unit kept; `superseded` with reason `cancelled_by_caller` for a call cancelled during the wait whose approval is granted later, which never runs). `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are untouched.
- **Telemetry lanes tell a plane that said no from a plane that is down, and count what they lose.** On `/v1/events`: 400, 404, 409 and 422 drop the batch and count it by status (they used to be requeued forever), 413 sends the batch in halves, `ledger_unavailable` drops with that reason, and a lane stopped by a rejected key is visible in `runbound.delivery.snapshot()` and tried again once an hour. On spans, an OTLP `partialSuccess.rejectedSpans` count is counted as rejected, not sent. An approval report a plane refuses for an outcome it does not know is counted `plane_too_old`.

## [1.0.0a4] - 2026-10-18

What this release is for: the SDK half of Verified Policy Changes. A worker now says honestly which configuration it holds and which process it is, every decision names the revision it was made under, and an approval can no longer run a call that the policy of the moment forbids.

**What a team gets**

- **Honest acceptance.** Every hello carries `boot_id`, `seq` and `accepted_cfg` (the hash the plane served, echoed and never computed here, and `null` unless every row and the defaults were read and applied). The plane can tell a worker-reported acceptance from a version-only report.
- **A decision record that names its revision.** `rule_revision`, `cfg_hash`, `boot_id` on refusals, spans and approval requests; `otherwise:<decision>` and `limit:*` contributors; `candidate_outcome` on every call a revision being tried was evaluated on.
- **An approval never outlives the policy.** A call a person approved, or a grant admitted, re-checks the policy held at that moment and refuses `approval_superseded` (halt, unavailable policy, block, deny, cap). Its record carries both phases (`recheck_*`), and the plane gets the quota unit back through the `superseded` report.
- **Dry runs touch nothing live.** A dry-run quota row takes no unit (it asks the plane's peek); a dry run of an ask rule is counted (`would_ask`) and asks nobody.
- **The tool report says `deferred`** only for a tool whose code registered it.
- **No window after a start.** A destructive tool of a brand-new service no longer runs unasked in the first seconds after the worker connects (see Fixed).

**What to check before upgrading**

- `exc.reason` has a new value, `approval_superseded` (not retryable). A handler that switches on the closed set needs a branch or a default.
- Code that parses the `contributors` of a record: an explicit tool-level `otherwise` is now `otherwise:<decision>`, no longer `default:service`; a limit that refused the call adds `limit:quota`, `limit:session_cap` or `limit:repetition`.
- Ten legacy fixtures changed deliberately (the dry-run quota rows listed under "A dry-run quota row takes no live unit" below); every other legacy case is byte-identical.
- A served row carrying a key this SDK does not know still refuses only that tool (`policy_unsupported`); `rule_revision` is a known key from this release, and a plane serves it only to a worker that reports `revisions_v1`.
- Set `worker_id` to a stable name (a pod or host name) so a restart is recognised as the same worker; `boot_id` tells the processes apart.

**Compatibility.** Against the plane at the VPC exit, 1.0.0a2 and 1.0.0a3 keep enforcing what they understood and report `version_only`; the tool-call overhead is a few microseconds at most over 1.0.0a3, not measurably different. The older-plane legs were run once during the VPC build and have not been re-run since.

The detail follows.

### Fixed

- **A destructive tool of a brand-new service no longer runs unasked in the first seconds after the worker connects.** A new service starts with enforced standard defaults (destructive: ask), but the plane's first hello announces `policy_version: 0` because writing those defaults does not bump the live version. The worker read 0 as "this service has no policy, so there is nothing to hold" and lifted its refusal of consequential classes on that hello with no defaults applied, so the call ran, until a later hello (about two seconds on) announced the policy. The worker now reads its policy once on its first answered hello, whatever version it announced, **before** that hello counts as answered, so the startup floor lifts only with the policy and its defaults in hand; and a worker that holds no policy (the read failed) keeps refusing financial, destructive, privileged and unclassified tools until a read succeeds. A service that really has nothing still answers the read with an empty policy and behaves as before.
- **An `async` tool that the service defaults ask for no longer freezes the event loop while it waits for the approval.** Only a tool with a dashboard row that asks (or takes a quota unit) had its pre-chain run off the loop; a tool with no row that the service defaults ask for (a destructive-only or unclassified tool) waited on the loop, so every other coroutine in the process stopped until a person answered or the wait ran out. The wrapper now also hands the pre-chain to a thread when the service defaults ask for the tool (or for a conditional row whose lines may all miss the call). Defaults that allow or deny, a service without defaults and a tool with no row stay on the loop, and no decision changes.
- **A rule that allows only when a condition holds and says what happens otherwise now works** ("allow when `amount > 1000`, otherwise deny"). The plane serves such a row with a legacy view of `blocked: true` (the strictest reading, the `otherwise` included), but the SDK's static check compared it with the lines alone, saw only `allow`, called the legacy view "stricter than its lines" and refused the whole tool as `policy_unsupported`, even for a call the allow line matches. The check now compares the legacy view with the strictest of the lines **and** the row's explicit `otherwise`; a legacy view stricter than both is still refused.
- **A call that passed the execution-entry re-check and ran no longer carries `recheck_*` on its record or span.** The re-check's facts were added to the record before its outcome was known, so an approved call that was re-checked and then ran (for example after a promote that still let it through) showed a re-check phase, which reads as a refusal. The keys are now removed when the re-check passes; a refused call is unchanged.

### Added

- `approval_superseded` (not retryable): a call a person approved, or a grant admitted, re-checks the policy the worker holds when the approval arrives. If that policy now blocks, denies or caps the call, it is refused with this reason and never runs on the approval alone. Also applies to the execution of an approved deferred request.
- The tool report carries `deferred: bool` per tool: `true` only when the decorator registered `mode="deferred"`, `false` for any other decorated tool, never inferred. The plane stores it and refuses a row that asks for deferred approval on a tool that reported `false`.

- **Acceptance evidence on the hello (`acceptance_v1`).** Every heartbeat now carries `boot_id` (a uuid drawn once when the process starts, so a restart is a new boot), `seq` (this boot's hello number, from 1, strictly increasing) and `accepted_cfg`: the configuration hash the plane served (`cfg_hash` in the policy envelope, falling back to `rows_hash`), echoed exactly as served and **never computed in the SDK**, and only once every row and the service defaults were read and applied. It is `null` while any served row or the defaults is unreadable or unsupported (the worker is not enforcing what was served, so it says nothing is accepted), left out until a policy has been applied, and `""` for a policy that carried no hash. With them the plane keeps the newest report of a worker and can tell "worker-reported acceptance" from a version-only report. `policy_features` gains `acceptance_v1` and `revisions_v1`.
- **`rule_revision` on served rows (`revisions_v1`).** A served row, and its `candidate`, may carry `rule_revision` (the tool row's per-tool ordinal, a whole number from 1): it names the revision a decision record was made under and decides nothing. Any other key this SDK does not know still refuses the tool (`policy_unsupported`), and a `rule_revision` that is not a whole number does too. The plane serves the key only to a worker that reported `revisions_v1`.

- **An approved call that is refused at execution entry carries its record in two phases.** A promote, a restore or a defaults change that lands while a person decides now leaves both: what the call was approved under (`rule_revision`, `cfg_hash`, `policy_version`, `service_default_revision`, as before) and what the execution-entry re-check read (`recheck_rule_revision`, `recheck_cfg_hash`, `recheck_policy_version`, `recheck_service_default_revision`) with the one contributor that refused it (`recheck_contributor`, e.g. `row:L2`, `default:service`, `limit:session_cap`) and the reason it returned (`recheck_reason`, e.g. `tool_blocked`). They ride the refusal's record, its span (`runbound.decision.recheck_*`) and the `record` of the `superseded` approval report, so the request keeps them and an explain by approval id shows both. `reason_code` stays `approval_superseded`; each key is present only when known.
- **The decision record names the revision and configuration, and says what decided.**
  - `rule_revision` (the per-tool ordinal the row was served under, `revisions_v1`), `cfg_hash` (the configuration hash the worker held) and `boot_id` (which process decided the call) ride on every refusal, span and approval request, only when known (a worker served neither records exactly what it did). `policy_version` stays the distribution generation and `service_default_revision` the defaults revision. Span attributes: `runbound.decision.rule_revision`, `runbound.decision.cfg_hash`.
  - **A row's explicit `otherwise` is now the contributor `otherwise:<decision>`**, no longer `default:service`: `default:service` is said only when the service defaults decide (or nothing did), so Explain can tell the tool's own fallback from an inherited one.
  - **A limit that refused the call is a contributor**: `limit:quota`, `limit:session_cap`, `limit:repetition`.
  - **`candidate_outcome` on every call a revision being tried was evaluated on** (`allow`, `ask`, `deny`, or `unknown` when a condition could not be judged), with `candidate_revision` and `candidate_of` (`rule` or `defaults`); span attributes `runbound.decision.candidate_outcome`, `.candidate_revision`, `.candidate_of`. The candidate never decides. Present for a row served in dry run, a candidate beside an enforced row, and a defaults candidate; absent when nothing is being tried.

- **A dry-run quota row takes no live unit and spends no lease**: the SDK reads the window with the plane's quota peek (`POST /v1/quota/take {"peek": true}`) and adds the dry-run calls this worker has seen in it, so `would_refuse:quota_exceeded` is still recorded when a live row would have been over, and the call runs. Evidence from several workers is therefore a lower bound. A plane that cannot be asked is read as a live row reads it (`quota_plane_unavailable`, recorded); a plane with no peek (one that serves no `cfg_hash`) is never asked, so no unit moves and no evidence comes from its counter. Registered change to ten legacy fixtures (O-LEG-ROW-69, -328, -329, -411, -430, -438, -443, -457, -489, -500): the dry-run row's `quota_takes` no longer lists a take, and O-LEG-ROW-457 now records `would_refuse:quota_exceeded` on its 2nd and 3rd calls.

- **A dry run of an ask rule is counted.** A dry-run row, or a candidate beside the enforced row, that would have asked a person now files an event for every such call (reaction `dry_run`, rule `would_ask`, with the decision record, capped per session like every dry-run record), besides the `would_ask:approval` tag: nobody is asked, nothing is refused, nobody is paged. The plane's evidence counts these as `would_ask`; before, a dry run of an ask rule read "0 evaluations".

## [1.0.0a3] - 2026-10-14

### Added

- **Approval grants (`grants_v1`).** The worker reports `grants_v1` and sends the call's `args_digest`, the `requirement` it asks for (the console line id, a default or `code`, several joined) and the decision record on `POST /v1/approvals`. When an operator approved an earlier request for this requester "for the session" or "for N minutes", the plane answers `granted: true` and the call runs without waiting; the record names `grant:<id>` in `contributors`, and the call takes its own quota unit. A grant covers the same tool, requester (`requested_for`, set by the app and not authenticated by Runbound) and arguments only. A worker without `grants_v1` is never admitted by a grant.

- **Decision lines.** A worker now tells the plane what it enforces (`policy_features`: `lines`, `halt_tools_v1`, and `policy_unsupported` for any tool it cannot read) on **every** heartbeat, so the plane can serve it a console rule as lines: each `allow`, `ask` or `deny`, with an optional approval requirement and optional limits. The strictest line wins, the limits of every line apply, and a rule stated in the older fields decides exactly as before (the SDK reads it through the lines the plane derives from it). A row whose older fields are stricter than its own lines refuses its tool (`policy_unsupported`, reported as `legacy_view_mismatch`). Lines carry no conditions in this release. A console `candidate` revision served beside an enforced one is evaluated only to record `would_refuse`.
- **Halted tools.** When the plane cannot give this worker a rule it can enforce for one tool, it halts that tool alone (`halted_tools` in the heartbeat reply): the tool is refused with `reason="policy_halted"`, the others run, and it holds until the plane lifts it, including across a restart while the plane is down.
- **A decision record on every refusal and every span.** The primary reason code (one of the existing `exc.reason` values), `matched_rule_ids` (empty when no line matched), the policy version, the policy state (`fresh`, `cached` or `unavailable`), what contributed (`row:L1`, `code:blocked`, `default:service`) and the SDK version. A refusal carries them in `exc.decision.evaluation` and in its event; a span as `runbound.decision.reason_code`, `.matched_rule_ids`, `.policy_state`, `.contributors`, `.service_default_revision`, `.policy_version` and `runbound.sdk.version`. The span of a refused call now carries the reason code as well as the message.
- Three refusal reasons: `policy_unavailable`, `policy_expired_restricted` and `policy_halted` (with `policy_unsupported`, listed below), so `exc.reason` has new values a `match` on it should expect.

- **Service defaults.** A service that has adopted defaults is served them with its policy (`service_defaults`, reported as the `defaults_v1` feature). They are the **fallback** for a tool no line decides: a tool with no console rule, or one whose rule decides nothing, is decided by the default for what it declares (the strictest of its operation and its tags; the unclassified default for a tool that declares nothing), one approval requirement per class that asks, never merged. A line that decides the tool keeps deciding it, even looser than the default. Fail mode follows the same classes, with a bare `write` counting as unclassified for fail mode only. Dry-run defaults only record what they would have done. The decision record names the defaults revision. A service that has not adopted defaults is unaffected.
- **A service with enforced defaults gets one 24-hour validity window.** A policy sealed within 24 hours is enforced in full while the plane is away, and fail mode applies only once it is older. A service without enforced defaults keeps its per-class cache trust.
- **Conditions and repetition on console rules.** A line may carry `when` / `unless` conditions on the call's arguments (and on the context: environment, version, service) using the closed list of operators (number `>`, `>=`, `<`, `<=`, `between`; string `equals`, `starts_with`, `ends_with`, `is_one_of`; boolean `is_true`, `is_false`; list `contains`, `length_lt`, `length_gt`), and a `repetition` control (`reason="repetition_exceeded"`). A test is true, false or unknown; an unknown test never reads as false, and one that could have made the decision stricter refuses the call (`reason="condition_unevaluable"`). The SDK reports `conditions_v1` and `repetition_v1`. A dry-run candidate records `would_ask:approval` as well as `would_refuse:<reason>`. Two new refusal reasons: `condition_unevaluable` and `repetition_exceeded`.

### Behaviour

- **A row never makes a tool deferred.** Deferred execution needs the tool's own `mode="deferred"` registration in code; a console row that says `deferred` on a tool that registered nothing now asks synchronously. The same person is asked and the call is held until they answer.

- **`approval_unavailable` is retryable.** No approver is reachable while the plane link is down, which is as transient as `plane_unavailable`; `exc.retryable` and `runbound.is_retryable` are now true for it, so a retry loop waits out the outage.

These change what a worker does. Each is the enforcement floor (F1 to F3) or the policy-state rule of the tool-rules contract, and each flips a test that was waiting for it.

- **A fleet halt refuses every tool, whatever `on_halt` says.** `on_halt="warn"` used to log a halt and let the call through. It now changes nothing: a halted fleet refuses every tool under either value, and the setting is kept so existing code still starts. A halt holds until the plane lifts it; it no longer lapses 60 seconds after the plane stops answering, and it is kept in the policy cache, so a worker restarted while the plane is down starts halted.
- **A console rule the SDK cannot read refuses its own tool.** A row with a field this SDK does not know, a value the console itself would not accept (a quota limit of 0, an approval mode that is neither `sync` nor `deferred`, a headline outside the grammar, a state that is not `dry_run` or `enforced`), or a headline naming an argument the tool does not have used to be skipped, so its tool ran with no rule at all. It is now refused with `reason="policy_unsupported"` until the rule is changed or the SDK is upgraded. Other tools are not affected. The worker keeps the list (`RemoteState.policy_unsupported()`).
- **No tool runs before a worker has been told what the plane says.** Until a worker's first answered hello, or the load of a sealed policy cache, every tool is refused: a `read`, `write` or `external` tool with `reason="policy_unavailable"`, the consequential and unclassified classes with `plane_unavailable` as before. Both are retryable. A process with no plane configured is not affected.
- **Two plane-loss refusals are stricter; the rest of plane loss is unchanged.** A `write` tool with no other tag (a bare write) counts as unclassified for fail mode: it now refuses with `plane_unavailable` when the worker holds no policy within 24 hours (none, or a cache older than that), where it used to run. A tool that the expired policy blocked or asked a person about now refuses with `policy_expired_restricted` instead of the row's own reason. Everything else is as it was: `consequential_grace_s` and the 15-minute cache check still decide when `financial`, `destructive`, `privileged` and unclassified tools stop. A single 24-hour window for every class arrives only for a service that has adopted enforced service defaults.
- **A row's wait lowers the code's.** A console row that sets only `approval_timeout_s` (without `approval_required`) now shortens the wait of a tool whose code already asks for approval; before, the code's own wait won.

### Deprecated

- **The organisation-wide policy document (deny and allow lists served by the control plane) is deprecated.** If your plane still serves one, the SDK now logs a single warning per process saying so. It is still read and enforced exactly as before in this release, and will stop being read in a later one: move its rules to per-tool rules in the console.

### Fixed

- **A control plane error no longer erases the rules a worker holds.** When the plane could not read its policy rows it said so with an empty answer, and a worker took that for "no rules": it dropped every console rule it held and saved the empty set to its cache, so one short database error on the plane removed your rules from every worker that asked during it, restarts included. A worker now keeps the rules it has, saves nothing, and asks again on the next heartbeat. A plane that deliberately serves an empty set still clears them.
- **A worker the plane has told about a policy, but that has not received it, no longer runs consequential tools unguarded.** If the heartbeat worked but the policy itself could not be fetched, and the worker had no saved policy, it counted as connected and ran `financial`, `destructive`, `privileged` and unclassified tools on your code rules alone. Those tools are now refused with `plane_unavailable` until the first policy arrives (or a recent saved one is loaded); `read`, `write` and `external` tools carry on as they do whenever the plane is away. A plane that has no policy for the service gates nothing, and a worker that already holds a policy keeps using it.
- **A quota the plane cannot find no longer lets calls through.** When a worker held a quota for a tool and the plane answered the take request with 400 or 404 (for example when a proxy in front of it did not route `/v1/quota/*`), the call ran with no limit at all. The quota now counts as unverifiable and the row's fail mode decides, the same as when the plane is unreachable: `closed` (the default) refuses with `quota_plane_unavailable`, `open` admits a small share per heartbeat. One warning a minute names the status.
- **A console row can no longer loosen a tool.** A tool with no declared effects fails closed, and `init(capabilities=...)` refuses it whenever anything is narrowed. A row that named `risk_class` `read`, `write` or `external` made it a fail-open tool instead, so with the plane away it ran where it had been refused, and a row naming any class could give it a more lenient capability verdict than your code's own declaration. A row of those three classes now leaves the tool unclassified, and the capability rules judge an unclassified tool as the stricter of "unclassified" and the row's class. One tightening to expect: an undeclared tool whose row names `financial`, `destructive` or `privileged` used to be judged only as that class, so with `capabilities={"financial": "approve"}` it needed approval; it is now denied, because it is also unclassified. A tool your code declares `financial` is unchanged.
- **A dry-run rule's evidence now counts every call it would have refused.** The console shows how many calls a `dry_run` rule would have stopped, but the SDK reported only the first one for each session, rule and tool, so a rule that would have refused 40 calls showed 1. Each call it would have refused is now reported (up to 100 per session, rule and tool, then counted and summarised); a dry run still refuses nothing and never pages anyone.
- **A resumed deferred approval keeps its run and names its request.** The execution of an approved deferred request ran with no session bound, so its span carried the process's default run id (and so a different trace) and no `runbound.approval.request_id`. It now runs inside the run that filed the request, and its span carries that run id and the request id. A request parked on this worker, sealed or not, is covered; a sealed request another replica claims takes its run from the plane's claim.

## [1.0.0a2] - 2026-10-12

1.0.0a1 was built and tagged but never published; 1.0.0a2 is the first release of the 1.0 line on PyPI, so everything
listed under it and under 1.0.0a1 below arrives together.

### Added

- **`runbound.session(requested_for=...)`.** An email or user id (at most 128 characters) that rides the approval request of any tool that asks a person inside the block; the plane refuses the same person approving it (409 `self_approval`). A value that is not such an id is ignored with one log line, never raised.
- **`plane_status().plane_degraded`.** A hello may carry `plane_degraded` (`"ledger_unreachable"`: the plane answers but cannot reach its own database); the SDK surfaces it as `PlaneStatus.plane_degraded` (`None` when absent or empty) and records ONE `plane_degraded` event per change (`from`, `to`, `cleared`), never one per hello. Informational: no decision changes on it. Unknown keys in a hello are still ignored.
- **A chaos harness** (`tests/chaos/`): a scripted `FlappingPlane` (503 bursts, slow hellos past the timeout, halts that flip, the same approval offered twice) drives the real link code on a fake clock, and asserts the plane-loss invariants: consequential tools refuse at the grace and read tools never do, the cache is enforced before the first hello, the modes only walk connecting, connected, degraded, unavailable and back.

### Changed

- **`GuardrailConfig` is now `RunboundConfig` (V8).** `runbound.RunboundConfig` is the class `init()` builds; `runbound.GuardrailConfig` (and `runbound.config.GuardrailConfig`) stays as a deprecated alias for the same class and emits one `DeprecationWarning` naming the new name on first use. The docs, README and API.md use the new name.
- **`GuardrailCallbackHandler` is now `RunboundCallbackHandler` (V6).** `runbound.integrations.langchain.RunboundCallbackHandler` is the handler; `GuardrailCallbackHandler` stays as a deprecated alias for the same class and emits one `DeprecationWarning` naming the new name on first use. The LangChain guide, the example and API.md use the new name.
- The seal's `not_after` check is documented as what it is, one-sided: a clock more than 300 s past `not_after` fails the seal (`clock_skew`), a clock behind it by any amount still verifies, and the plane's claim TTL bounds replay. The docstrings said "±300 s"; no behaviour changed.
- **`max_calls` counts admitted calls only (V5a).** A call refused by a constraint, a validator, a capability
  class, an approval or a gate no longer uses up the tool's allowance: `max_calls=2` allows two calls that
  clear the chain, however many were refused first. The same holds for a row's `max_calls_per_session`.
  `tool_calls()` still reports every attempt. Concurrent calls hold their unit while in the chain, so two
  threads cannot both take the last one.
- **One refusal reason for a blocked tool (V5c).** `blocked=True`, a `deny` entry and an org policy's `deny` now
  raise with `reason="tool_blocked"`, as a console row's block already did; they were `"policy"`. The other code
  rules (`allow`, `max_calls`, `constraint`) keep `"policy"`. `exc.reason` is host-side only: it is not on the wire.
- **The Python floor is stated (V5b).** `import runbound` on Python 3.9 raises an `ImportError` that says 3.10 or
  newer is needed, and the README says so; `requires-python` already was `>=3.10`.

### Removed

- **Wire types nothing reads (V5e).** `EntryDecision`, `ExitDelta` and `TripReport` are gone from
  `runbound.plane_types`, and `WireEvent` loses the model-call fields (`tokens_in`, `tokens_out`, `tokens_reasoning`,
  `cost_usd`, `model`, `priced`, `partial`, `tokens_estimated`, `provider`): the SDK guards tool calls only, and the
  plane reads none of them. `WireAnomaly` stays: the exporter still sends it. `API.md` lists none of these.
- `runbound.shared.RETRY_EVERY_S` (unused since hellos continue every poll period while degraded).

### Fixed

- **Waits imposed by a dashboard row no longer stop the event loop (V1).** An `async def` tool whose policy row adds a
  sync approval or a quota (the `POST /v1/quota/take` round trip) now runs its pre-chain off the loop, as a
  code-declared approval was meant to; the declared path also raised `NameError` (`asyncio` was never imported).

## [1.0.0a1] - 2026-10-08

A rewrite. 0.11.x guarded LLM calls (provider wrappers, budgets, circuits, loop and spike detection, postures);
1.0.0a1 guards **tool calls only**: `@runbound.tool`, a hook chain around each call, per-tool policy from code and
from the plane, quotas, human approvals, fail-closed behaviour when the plane is lost, and one OpenTelemetry span
per call. The runtime has no dependencies outside the standard library. `API.md` is the full list of public names.

### Removed

- **Public names (S1).** `wrap`, `unpatch`, `llm`, `record_call`, `budget`, `BudgetView`, `circuit_state`,
  `inflight_calls`, `active_sessions`, `posture`, `envelope`, `safe_mode`, `enter_safe_mode`, `exit_safe_mode`,
  `fleet_status`, `is_tripped`, `clear`, `session_status`, `key_hash`, `verify_webhook_signature`, `Refusal`. Each
  raises `AttributeError` on `runbound`, and none exists in `runbound.api`. Also gone from `runbound.__all__`:
  `CircuitOpen`, `PostureState`, and `SafeModeViolation` (renamed `CapabilityRefused`, no alias).
- **Modules (S2).** `runbound.wrappers` (OpenAI and Anthropic wrappers), `autowrap`, `circuit`, the provider
  rate-limit header parser that was `runbound.quota` (the name now holds the new quota code), `pricing`,
  `detectors`, `ladder`, `posture`, `responses`, `alerts`, `controls_merge`, `demo` (`python -m runbound.demo`).
  `import runbound` needs no provider SDK.
- **Features.** Provider wrappers and auto-wrapping; money and token budgets, soft budgets and admission
  estimates; the price table and custom prices; provider circuits (local and fleet) and retry-storm handling; loop,
  spike, error-storm, step, timeout and per-call size detectors; the posture ladder and safe mode; the execution
  envelope; refusal response profiles; `/v1/enter` fleet entry decisions and their 5 s per-key cache; fleet latches
  (`PlaneClient.enter`, `trip`, `clear`) and fleet spend offsets; entitlements and plan limits; the `/v1/controls`
  fetch and merge (`PlaneClient.controls`); webhook signature verification; the LangChain handler's `on_llm_end`
  and `on_llm_error`. A plane halt with `halt_mode="narrow"` is now a full halt.
- **`init()` parameters.** Budgets and limits: `budget_usd`, `max_total_tokens`, `budget_window`,
  `run_budget_usd`, `run_max_total_tokens`, `max_steps`, `max_events`, `tokens_per_minute_limit`,
  `max_call_seconds`, `max_tokens_out_per_call`, `max_cost_per_call_usd`, `max_session_seconds`,
  `max_session_lifetime_seconds`, `max_active_sessions`, `max_session_depth`, `max_child_sessions`,
  `max_inflight_calls`, `estimate_tokens`, `budget_admission`, `admission_output_tokens`, `budget_soft`,
  `on_budget_soft`. Pricing: `custom_prices`, `on_unpriced_model`, `unpriced_price_per_1m_usd`. Loops and spikes:
  `loop_threshold`, `loop_window`, `on_loop`, `loop_hard_threshold`, `loop_alert_threshold`,
  `loop_contain_threshold`, `loop_max_period`, `loop_stall_turns`, `loop_shapes`, `loop_ignore_tools`,
  `throttle_base_seconds`, `throttle_max_seconds`, `spike_detection`, `on_spike`, `spike_limit_calls`,
  `spike_cooldown_seconds`, `spike_max_strikes`, `spike_warmup_calls`, `spike_window`, `spike_factor`,
  `spike_confirm`, `spike_min_duration_s`, `spike_min_output_tokens`, `error_storm_limit`. Providers and circuits:
  `on_provider_failure`, `circuit_failure_threshold`, `circuit_window_seconds`, `circuit_cooldown_seconds`,
  `circuit_reads_quota`, `circuit_mode`, `circuit_failure_rate`, `circuit_min_calls`, `circuit_slow_call_seconds`,
  `circuit_slow_rate`, `circuit_half_open_calls`, `circuit_fleet`, `circuit_posture`, `auto_wrap`,
  `coverage_check_seconds`. Reactions: `on_trip`, `latch_ttl_seconds`, `on_anomaly`, `callback`, `postures`,
  `refusals`, `envelope`. Plane: `api_key` (the deprecated alias of `token`), `control_plane_timeout_s`,
  `control_plane_cache_s`, `stale_halt`, `on_plane_loss`. Passing any of them raises `TypeError`.
- **Refusal reasons.** `budget`, `tokens`, `steps`, `time`, `posture`, `circuit`, `concurrency`, `loop`,
  `error_storm`, `spike`.
- **Wire and status.** `HelloReply.circuits` (an older plane that still sends it is ignored); the `circuits` lane
  of the `/v1/events` batch; posture-change records. `plane_status().mode` no longer reads `limited`;
  `PlaneStatus.entitlements`, `entries_window` and `entries_local_share` stay on the type at their defaults.
- **OpenTelemetry counters.** `runbound.posture_changes`, `runbound.guarded_calls`, `runbound.estimated_usd`
  (`otel.enable()` keeps `runbound.refusals` and `runbound.anomalies`).
- **Packaging.** The `dev` extra.

### Added

- **The hook chain (S5).** Every decorated call runs, in a fixed order: user rewrite hooks, validation of the final
  arguments against the signature and type hints (`reason="invalid_arguments"`, nothing coerced, sync and async),
  the tool policy rules, the capability class rules and action cap, approval, then user gate hooks, then the body.
  First refuse wins. `init(pre_hooks=, gate_hooks=, hook_timeout_s=5.0)` and `@tool(pre_hooks=, gate_hooks=)`; a
  hook returns `hooks.allow()`, `hooks.refuse(reason)` (`reason="hook_refused"`) or, in a pre hook only,
  `hooks.rewrite(args=, kwargs=)`. A user hook that raises or outlives `hook_timeout_s` refuses
  (`reason="hook_error"`) for a `financial`, `destructive`, `privileged` or unclassified tool, and is logged and
  skipped for `read`, `write` and `external`. A built-in that raises refuses every class.
- **The post chain and `tool_result` (S6).** A result is redacted (37 secret key names, plus
  `init(redact_keys=)`), truncated with JSON kept valid (`init(result_limits=)`, `@tool(max_result_chars=)`),
  scanned for 15 prompt-injection patterns (tags, never blocks), then passed to `init(post_hooks=)` /
  `@tool(post_hooks=)` (`hooks.replace`, `hooks.withhold`). Every call ends in a `tool_result` record (`duration_s`,
  `outcome` of `ok` / `error` / `refused` / `withheld` / `pending_approval`, scan flags, sizes, tags; never the result)
  delivered to `on_event` and `runbound.tool_results()`, a ring of its own apart from `events()`. The LangChain
  handler gains `on_tool_end`.
- **Approvals (A5).** `@runbound.tool(approval="required", mode="sync" | "deferred", timeout_s=600)`, slot 5 of
  the chain. Sync waits on the plane's answer; deferred returns `{"status": "pending_approval", "request_id": ...}`
  and a worker of the service executes the approved call from `hello.approved[]` through the full chain, then calls
  `init(on_deferred_result=)`. Deferred arguments are sealed with HMAC-SHA256 under a per-service secret
  (`init(approval_secret=)` or `RUNBOUND_APPROVAL_SECRET`, never sent to the plane); a bad or expired seal runs
  nothing and reports `tampered`. Claims on executing requests are renewed on each hello (`renew_claims`,
  `claims_lost`). A report the plane cannot take is queued to `<service>.reports.jsonl` beside the policy cache and
  sent once on the next connected hello. A policy that tightens after approval stops the deferred execution.
  `runbound.current_request()`. Reasons `approval_denied`, `approval_timeout`, `approval_tampered`,
  `approval_unavailable`; a full quota on create is `quota_exceeded` with `retry_after`.
- **Per-tool policy rows and quotas (T1).** `GET /v1/policy` serves `rows` (state `dry_run` or `enforced`) and
  `rows_hash`. Rows fold with code rules field by field, most restrictive wins, and never loosen a code rule
  (blocked, session cap, approval, approval timeout, risk class, fail mode). A `dry_run` row refuses nothing and
  records `would_refuse` (a Decision and a `would_refuse:<rule>` tag). A row's `quota` is
  `{"limit", "window": "minute" | "hour" | "day", "per": "service" | "key"}` in a fixed, calendar-aligned UTC
  window, leased on the heartbeat (`quota_held`, `quota_leases`) and taken with `POST /v1/quota/take`. Reasons
  `tool_blocked`, `session_max_calls`, `quota_exceeded`, `quota_plane_unavailable`. A refused call takes no quota.
- **A code-declared approval is reported.** The tool report's `rules` carry `approval` and `approval_mode` for a
  `@tool(approval="required")` tool, so the plane never leases it quota (its approval create takes the unit).
- **Plane loss and the policy cache (S7).** The last policy served is written atomically to disk
  (`init(cache_dir=)` / `RUNBOUND_CACHE_DIR`, else the platform cache dir; `0700` / `0600`, with a MAC keyed from
  the token) and enforced on restart before the first heartbeat. `plane_status().mode` is `local`, `connecting`,
  `connected`, `degraded` (3 missed hellos, or the plane could not read its own state) or `unavailable` (60 s since
  the last answered hello). Hellos continue every `poll_s` (±10%) in every state. `read`, `write` and `external`
  tools keep running on the cache; `financial`, `destructive`, `privileged` and unclassified tools refuse with
  `reason="plane_unavailable"` (retryable) once `init(consequential_grace_s=60)` (15 to 60) has passed since the
  last answered hello. `PlaneStatus.cache_age_s` and `stale`.
- **Spans (S8).** One INTERNAL span `execute_tool {tool}` per decorated call and per LangChain tool, scope
  `runbound`. It goes to the global OpenTelemetry `TracerProvider` when one is configured (imported lazily, never
  installed), otherwise to `init(otel_endpoint=)` as OTLP/JSON over the standard library with `token` as the Bearer
  key. With `otel_endpoint` set, per-call events leave `/v1/events`, which keeps refusals and changes. Attributes
  are pinned in `runbound/otel_names.py` and never carry an argument, a result or an exception message.
- **Public names.** `tool_results`, `current_request`, `ToolRules`, `CapabilityRefused`; the `runbound.hooks`
  module. New `init()` parameters: `pre_hooks`, `gate_hooks`, `hook_timeout_s`, `post_hooks`, `redact_keys`,
  `result_limits`, `otel_endpoint`, `cache_dir`, `consequential_grace_s`, `approval_secret`,
  `on_deferred_result`. New `@tool()` parameters: `pre_hooks`, `gate_hooks`, `post_hooks`, `max_result_chars`,
  `approval`, `mode`, `timeout_s`.
- **Refusal reasons.** `capability`, `invalid_arguments`, `hook_refused`, `hook_error`, `approval_denied`,
  `approval_timeout`, `approval_tampered`, `approval_unavailable`, `quota_exceeded`, `quota_plane_unavailable`,
  `plane_unavailable`, `tool_blocked`, `session_max_calls`. `exc.retry_after` is set for `quota_exceeded`.

### Changed

- **Breaking: the SDK guards tool calls only.** It no longer sees, prices or counts model calls.
- **Breaking: a capability refusal's `exc.reason` is `capability`** (it was `policy`), and its class is
  `CapabilityRefused` (was `SafeModeViolation`).
- **Breaking: retryable reasons** are `plane`, `plane_unavailable`, `quota_exceeded` and `quota_plane_unavailable` (were `circuit`, `concurrency`, `plane`). The two quota reasons carry `retry_after`: the rest of the window, or the plane poll interval.
- **Breaking: `plane_status().mode`.** The 0.11 `degraded` (three failed calls, after which the SDK stopped
  calling the plane) is now `unavailable`; `degraded` is the earlier state in which everything still runs on the
  cache. Missed hellos count, not failed policy fetches.
- **`assert_guarded()`** raises when no tool is decorated (it used to look at provider SDKs and wrapped clients).
  `check()` reports decorated tools; it has no wrapped-client section.
- **`@tool(polling=, idempotent=, retryable=)`** are reported facts only; with no loop detector they change nothing.
- **`init(capabilities=)`** is a static class rule; nothing in the process or the plane narrows it at run time.
- **The events batch** sends no posture changes and no `circuits` lane; `exits` and `changes` stay.

### Migrating from 0.11.x

| 0.11.x | 1.0.0a1 |
| --- | --- |
| `runbound.wrap(client)`, `auto_wrap`, `runbound.llm`, `record_call`, `unpatch` | Gone. Decorate the functions the agent calls with `@runbound.tool`; leave the model client unwrapped. |
| `budget_usd`, `max_total_tokens`, `run_budget_usd`, `budget_window`, `budget()`, `BudgetView`, pricing | Gone. Bound call counts instead: a row `quota` per clock minute / hour / day (per service or per key), `@tool(max_calls=n)` per session, `init(max_actions_per_run=n)`. |
| Provider circuits, `CircuitOpen`, `circuit_state()`, `on_provider_failure` | Gone. Handle provider errors in your own client code. |
| Loop / spike / error-storm detectors, `on_loop`, `on_spike` | Gone from the SDK. `max_calls`, `max_actions_per_run` and quotas cap repetition. |
| `posture()`, `safe_mode()`, `enter_safe_mode()`, `exit_safe_mode()`, `postures=` | `init(capabilities={"destructive": "deny", ...})` for a standing rule; a plane halt (`on_halt`) to stop the fleet; consequential classes fail closed on plane loss. |
| `SafeModeViolation` | `CapabilityRefused` (still a `PolicyViolation`). |
| `envelope()`, `envelope=`, `max_steps`, `max_session_seconds` | `init(max_actions_per_run=)`, `@tool(max_calls=)`. |
| `is_tripped()`, `clear()`, `on_trip`, `latch_ttl_seconds` | A session still latches on `ToolPolicy(on_violation="block_and_latch")`: read `runbound.current_session().tripped_by`; `runbound.reset()` or a new `session(key)` starts clean. |
| `session_status(key)`, `fleet_status()`, `inflight_calls()`, `active_sessions()` | `runbound.tool_calls(key)`, `runbound.plane_status()`; the rest is gone. |
| `on_anomaly=`, `callback=` | `init(on_event=fn)`; `runbound.events()`, `runbound.decisions()`. |
| `refusals=`, `Refusal` | Gone. Switch on the fixed `exc.reason` codes. |
| `api_key=` | `token=` (or `RUNBOUND_TOKEN`). |
| `on_plane_loss=`, `stale_halt=`, `control_plane_timeout_s=`, `control_plane_cache_s=` | Gone. Plane loss follows the capability classes; tune with `consequential_grace_s=` and `cache_dir=`. |
| `key_hash()`, `verify_webhook_signature()` | Gone from the SDK. A refusal still carries `exc.scope["key_hash"]`. |
| `exc.reason in ("budget", "circuit", "loop", ...)` | Those codes are never produced. Handle `quota_exceeded` (`exc.retry_after`), `plane_unavailable` and the `approval_*` codes. |
| `require_approval=predicate` | Still works (a local check, `reason="approval"`). For a person in the loop use `@tool(approval="required", mode=...)`. |

A minimal 0.11.x setup and its 1.0.0a1 form:

```python
# 0.11.x
import runbound
runbound.init(budget_usd=5.0, auto_wrap=True)
client = runbound.wrap(OpenAI())

# 1.0.0a1
import runbound

@runbound.tool(effects={"financial"}, max_calls=1, approval="required")
def issue_refund(order_id: str, amount: float) -> dict: ...

runbound.init(token="...", control_plane_url="https://plane.internal.example.com", service="refunds")
```

### Notes

- `1.0.0a1` is an alpha: names and wire fields may still change before 1.0.0.
- Python `>=3.10`. No runtime dependencies. Extras: `otel` (the OpenTelemetry provider path and `otel.enable()`),
  `langchain` (`GuardrailCallbackHandler`), `test`.
- **Decorate before `init()` is fine.** Tools are usually decorated at import, before `init()` runs. They are
  registered then, run unguarded until `init()`, and are enforced from their first call after it.
  `init(require_rules=True)` names every already-decorated tool without a rule in a `ValueError`.

## [0.11.2] - 2026-10-05

### Fixed

- **A second outage of the same provider is reported again.** A worker paged a provider's circuit once for the life of the process
  (`Engine._alerted`, keyed on the provider label and never cleared), so in a long-lived worker the first outage was reported and every
  later one, hours or days afterwards, was silent. The memory is now forgotten when the circuit closes (a successful probe, or the
  fleet closing it), so one outage pages once however many retries run into it, and the next outage pages again.

## [0.11.1] - 2026-10-04

### Fixed

- **Claude pricing.** The Claude 5 family (Fable 5.1 and 5, Mythos 5.1 and 5, Opus 5.5 and 5, Sonnet 5.5 and 5) now has price rows: until now each was unpriced, so a money budget counted it as $0.00 or, under `on_unpriced_model="refuse"`, refused it. Opus 4.6, 4.7 and 4.8 were matched to the retired Opus 4 row ($15 / $75) and over-charged three times; they are $5 / $25. A one-hour cache write was priced as a five-minute one (1.25x instead of 2x), an under-charge: the wrapper now reads `cache_creation.ephemeral_1h_input_tokens` and prices it at the new fifth column of a price row. Every row carries its read date (`PRICES_AS_OF`, now 2026-10-03) and source URL, and a test pins the table to the published list.
- **Admission for newer Claude tokenizers.** Claude 4.7 and later count about 30% more tokens than the characters-over-four input estimate; budget admission scales its input estimate by 1.3 for them (`runbound.pricing.token_estimate_factor`).

### Added

- **Price multipliers a request carries.** `inference_geo="us"` (1.1x, Claude 4.6 and later) and `speed="fast"` (2x on Opus 5.5, Opus 5 and Opus 4.8) are applied to the cost, and to the admission estimate, only when the request itself includes the field (`runbound.pricing.request_multiplier`). What the SDK cannot see (an account-level setting, the Batch API, server tools) stays a documented limit in "What the SDK actually sees".
- `custom_prices` accepts a fifth number, the one-hour cache-write rate.
- **A stated limit of this release.** The `inference_geo` and `speed` request fields are priced as Anthropic documents
  them on 2026-10-03; their spellings and any beta header they may need are not exercised against the live API in this
  release.

## [0.11.0] - 2026-10-03

### Added

- **`plane_status().applied_policy_version`**: the org policy version this worker has actually fetched and installed
  (`None` until one has been, and without a plane). Not the version the last heartbeat announced, so a policy write
  can be checked as having reached this worker. Read-only; nothing new on the wire.
- **`runbound[otel]`: OpenTelemetry export.** `from runbound import otel; otel.enable()` sends anomalies, refusals,
  posture changes and runtime changes as log records (a Decision's fields as `runbound.decision.*` attributes) and
  five counters (`runbound.refusals`, `runbound.anomalies`, `runbound.posture_changes`, `runbound.guarded_calls`,
  `runbound.estimated_usd`; the last two tick once per guarded model call). A session key leaves only
  as its hash. It does nothing, and returns `False`, when the packages are absent. The local event sinks now also
  receive the raw session key of an anomaly or refusal (never `events()` or `on_event`), so a sink that leaves the
  process can redact it.

## [0.10.0] - 2026-10-03

### Added

- **`runbound.check()` and `python -m runbound check`: what is guarded in this process, in one report.**
  Wrapped clients by provider, decorated tools with their capability classes, whether a plane is connected (its
  address, never its key), the posture in force, the budgets and limits, and the last ten events. It makes no
  network call. `check()` is called in your own process; `python -m runbound check [--json] [TARGET]` loads a script
  or module first (as `runbound_check`, so an `if __name__ == "__main__":` block does not run) and exits `0` when
  something is guarded, `1` when nothing is, `2` when the target cannot be loaded. `--json` has a stable shape
  (`schema` 1). A new guide, "Attach with an agent", is a prompt a coding agent follows to attach the SDK or the
  gateway, and a test checks that every command in it exists.

### Changed

- The configuration reference is grouped by purpose (budgets and limits; loops, spikes and failing providers;
  postures and tools; the plane; privacy; other), with each parameter's type, its real default and whether a
  connected plane can tighten it. Three `init()` parameters the old page never listed are there now:
  `budget_window`, `run_budget_usd` and `run_max_total_tokens`. A test reads the page against `GuardrailConfig`,
  so a parameter cannot be missing, duplicated, or shown with a wrong default. The repository manual is the
  canonical one; the site renders it at build time.

## [0.9.0] - 2026-10-02

### Changed

- A refusal's `decision.level` (and `exc.scope["level"]`) now names the scope that really decided,
  where it used to read `"session"` for all of these: an open provider circuit and a capability
  rule given to `init()` say `"process"`; a refusal because the run is stopped, or because a
  tool's class is denied by a posture, says `"session"`, `"process"` or `"fleet"` according to
  whose posture did it (a session's own safe mode, `runbound.enter_safe_mode`, or a posture or
  Narrow halt the control plane states). When several narrowings apply, the one that is actually
  the effective posture is named, along with its `source` and `reason`, rather than the first
  one set. Code that compared `decision.level == "session"` for these refusals should read the
  boundary and `level` together.

## [0.8.0] - 2026-10-01

### Fixed

- A dollar reservation's refusal no longer shares an alert key with the budget wall's own
  alert for the same session. Its anomaly now carries `details["reservation_level"]` where it
  carried `level`, so under `on_anomaly="warn"` the wall's alert is no longer dropped after a
  reservation refusal.
- A malformed limit value from a control plane is now ignored for that limit alone;
  the rest of the plane's limits still apply. Before, one value that was not a
  number, on a limit your code also caps, set the whole plane body aside. It is
  logged once, at warning level, naming the limit.

### Changed

- A token budget stated by a control plane is now merged like a dollar budget: the
  stricter one wins, across the org, service and run levels and against what your
  code sets. It is the control plane's name for `max_total_tokens`, and is
  enforced like it (next item).
- **A token budget is now enforced at the door as well as after the call.** A token limit
  (`max_total_tokens`, or a stricter `budget_tokens` a control plane states) is reserved
  like a dollar budget: the call's worst case (its input at four characters a token plus its
  output cap) is held until the call closes out. A call that would cross the limit is refused
  with `GuardrailTripped` before it goes out **only under `on_anomaly="raise"`**; under
  `"warn"` (the default) or `"callback"` the anomaly and its decision are recorded, your
  observers are told, and the call goes out, and the post-call wall stops the next call. A call
  that exactly reaches the limit is admitted. This follows `budget_admission` (default
  `"capped"`, so it applies to requests that state an output cap), needs no price (an
  unpriced model is reserved too), and is per worker: the fleet total is applied at entry. The
  dollar door is unchanged and still refuses whatever `on_anomaly` says. Set
  `budget_admission=False` to keep the post-call wall alone.

## [0.7.0] - 2026-09-30

### Changed

- **A loop is now answered in three rungs instead of stopping the session at
  the first repeat.** `on_loop=None` (the default) now means the new
  `on_loop="graded"` policy. Before, a call repeated `loop_threshold` times
  (3) was a critical anomaly that, with `on_anomaly="raise"`, latched the
  session with no way back until you cleared it. Now, 3 is worth a line, 6 is
  worth a person, 9 is a runaway: the third repeat is a warning in your log and
  in `runbound.events()` (reacted `notify`, so it pages no route); the sixth
  (`loop_alert_threshold`, twice `loop_threshold`) is a critical that pages your
  alert routes and stops nothing; the ninth (`loop_contain_threshold`, three
  times) hands the loop to the spike ladder: the session is limited to the
  `restricted` posture, so financial, external and destructive tools are refused
  before they execute while reads and writes still run, each further repeat
  spends the ladder's allowance, and the last closes the session for the
  cooldown and lets the key back in across the fleet when it is served, with a
  strike counted as for a spike. The anomalies keep detector `loop`. It covers
  the repeated-call, tool-cycle and retry shapes. Containment needs a keyed
  session, `on_spike="limit"` and `on_anomaly="raise"`; otherwise the ninth
  repeat is one more critical notice that says why it was not contained. A
  normal model call between two repeats no longer heals a session a loop limited.
  **To keep the old behaviour, name it:** `on_loop="break"` stops the session at
  the threshold (what `None` did under `on_anomaly="raise"`); `"throttle"` and
  `"escalate"` are unchanged. `loop_alert_threshold` and
  `loop_contain_threshold` move the rungs.
- **Every branch refused at the fan-out door is its own record.** A branch
  turned away by `max_active_sessions` (rule `active`), `max_session_depth` or
  `max_child_sessions` used to reach the record only once per session and rule,
  however many branches were refused. Each is now recorded and exported as its
  own anomaly and `Decision`, under the same cap and summary as every other
  refusal.

## [0.6.0] - 2026-09-30

### Added

- **The admission estimate's formula is public.** `runbound.pricing` gains
  `admission_worst_case(price, output_tokens, input_chars)`, the dollar worst
  case of one call (its input at the plain input rate plus the output it may
  produce), and `request_chars(request)`, the characters of input a request
  will be billed for. The engine's budget admission uses exactly these, so
  anything that estimates a call the same way composes the same numbers.

### Changed

- **The worst-case admission estimate now counts what providers bill as
  input.** It counts the Responses API's `input` and `instructions`, Anthropic's
  `system` (a string or blocks), the tool definitions (`tools`, and the legacy
  `functions`) and the tool calls and tool results carried in a conversation.
  Before, it read only chat `messages`, so a Responses call estimated no input
  at all and a request's system prompt and tools were skipped: with
  `budget_admission` on, budgets undercounted those calls and admitted some that
  should have been refused. The estimate used when a server reports no usage
  counts the same fields. Nothing is estimated for content whose size cannot be
  known (an image).

## [0.5.0] - 2026-09-30

### Added

- **A session the spike ladder stopped comes back, and says so.** When its
  cooldown is served, the session records its return (a posture change back
  to full), and the key is let back in across the fleet, on every worker,
  rather than only on the worker that closed it.
- **What changed, and how posture moved, now travel as records.**
  `runbound.events()` gains a `"runtime_change"` record whenever a value this
  process runs on moves from one value to another: the `model` a call used,
  the `provider` it went to, and the `policy_version` or `controls_version`
  a control plane delivered. Only on a real change: never on the first value
  seen and never while it stays the same. Posture records gain `"from"` (the
  posture they replaced), `"scope"` (`"session"` or `"process"`), `"level"`
  (the spike ladder's rung, when the ladder moved it) and, for a session, its
  `"key"`; a session narrowed by the ladder, by hand, by the soft budget line
  or lifted by `runbound.clear()` is now recorded, not only the process's own
  posture. With a control plane connected, both kinds are sent to it on a
  priority lane, with the session key as a hash and never raw; a posture
  change is sent even with `export_events=False`, like a trip, while a
  runtime change is telemetry and follows that setting. No record carries a
  prompt, a reply or a tool argument.
- **Every model call's event names its provider.** `Event` and its wire form
  gain `provider`, the endpoint label the call went to
  (`"openai@api.openai.com"`), from every wrapped client, `record_call()` and
  `@runbound.llm`.
- **A partial outage now reads as one.** `PlaneStatus` gains `entries_window`
  (`{"plane", "cache", "local"}`, a trailing one-minute count of how session
  entries were actually decided), `entries_local_share` (the local share of
  that window) and `reason`. A plane that answers every heartbeat but keeps
  missing `control_plane_timeout_s` on the hot `/v1/enter` path — the
  previously invisible case, since one entry timeout is a single failure and
  the next successful heartbeat reset the old count before three ever landed
  in a row — now degrades `mode` on its own once the window holds enough
  entries to mean something, with `reason="entry timeouts"`. The older,
  coarser case (the link has stopped answering anything at all) still
  degrades the same way and now says so: `reason="heartbeat failures"`.
  `coverage()["fleet"]` carries the same distinction in one sentence —
  `"connected"`, `"connected; N% of entries in the last minute were decided
  locally (plane timeouts)"`, or the existing `"local protection active;
  fleet coordination: not connected"` — and `coverage()["fleet_pending"]`
  reports the exporter's own queued-record count alongside it. The
  heartbeat now carries this worker's own local share too, so a connected
  console can show how many of a service's workers are deciding locally
  right now. See the "What connected means" section of the fleet mode guide.
- **A plane that has lost its own state says so, instead of answering as if
  nothing were wrong.** A hello reply can now carry `fleet_state:
  "unavailable"` — the plane is answering (the link is fine) but cannot
  currently read the store every halt, latch and fleet budget lives in.
  `RemoteState` treats this as plane loss for state, not for the link: a
  halt, its posture and the policy/Controls versions are all held exactly
  as they were rather than read from the reply's own "nothing to report"
  defaults, so an absent halt is never mistaken for a lifted one.
  `plane_status().mode` reads `"degraded"` with a third `reason`,
  `"fleet state unavailable"`, within one heartbeat; `coverage()["fleet"]`
  reads `"connected; fleet state unavailable: deciding locally, last halt
  and posture held"`. `stale_halt` applies the same as it always has, except
  its window now starts at the moment fleet state was first found
  unavailable rather than at the last successful heartbeat — a dead plane's
  own store outage does not stop it from answering its heartbeats, so the
  old clock would never let a held halt go stale under `stale_halt=
  "release"`. When the state comes back — a heartbeat says so, or the plane
  answers a session entry again — `reason` clears within one heartbeat: the
  entries the outage forced a worker to decide locally stay in
  `entries_window` as history but stop counting toward `"degraded"`.
  Timeouts are not cleared this way and keep the window's one-minute
  smoothing. See the new paragraph in the fleet mode guide and the updated
  `stale_halt` row in the reactions reference.
- **The entry window now records *why*, not just that, an entry was decided
  locally.** `entries_window` gains `local_causes` —
  `{"timeout", "plane_loss", "plane_unavailable", "error"}` — a breakdown
  of the existing `"local"` count by cause: a genuine client-side timeout,
  a 503 the plane answered because its own state store is specifically
  unreachable, the same shape of 503 for any other reason the plane could
  not help (a saturated connection pool, a handler bug — never a state
  outage), or anything else. The three existing counts (`"plane"`,
  `"cache"`, `"local"`) are unchanged. When the entry window is what
  degrades `mode` (as opposed to the older consecutive-failure count, or a
  hello's `fleet_state: "unavailable"`), `reason` now names the *majority*
  cause among that window's local entries instead of always saying
  `"entry timeouts"` — a genuine state outage answers `/v1/enter` with a
  503 naming that specifically, and for up to a minute after the outage
  recovers, the window still holds those entries; they now read `"fleet
  state unavailable"`, the same string the hello-based signal already
  uses, rather than the misleading `"entry timeouts"`. Two new reason
  strings: `"plane unavailable"` for a 503 shaped like plane loss but
  naming no specific cause — a plane that is merely overloaded, with its
  state store perfectly healthy, must never be reported as having lost
  its fleet state — and `"plane errors"` for anything else (a connection
  refused, a malformed reply). See the reason table in the fleet mode
  guide.

### Changed

- **Every refusal is recorded and exported, not only the first in a
  session.** A tool or call the SDK turns away (a posture or capability
  denial, a tool-policy block, a budget or envelope door) is now its own
  anomaly and `Decision` in `runbound.events()`, `runbound.decisions()` and
  the export, every time. Before, the second refusal in a session was dropped
  as a repeat, even one of a different tool, so a call refused a refund and
  then a transfer showed only the refund. To keep a runaway loop from
  flooding the record, identical refusals are recorded one by one up to 100
  per session, rule and tool (`runbound.engine.REFUSAL_RECORD_CAP`); past
  that they are counted, and one summary anomaly per rule and tool, carrying
  `details["suppressed_count"]` (how many were not recorded since the last
  summary), is reported when the session block exits, ahead of the session's
  exit record. Two rules or two tools refused at once never hide each other.
  Turning an already-stopped key away at the door is still counted per knock,
  as before, and the loop detector's once-per-session notification under
  `on_loop="throttle"` and `"escalate"` is unchanged.
- **A refusal is exported even when the plan's telemetry cap is reached.** A
  control plane whose monthly event allowance is used up closes the telemetry
  lanes; it no longer silences anomalies that record a refusal
  (`reacted` of `"raise"`, `"blocked"` or `"door"`), which are evidence that
  something was stopped, not telemetry. Everything else on those lanes stays
  closed, and `export_events=False` still turns all of it off.

## [0.4.0] - 2026-09-23

Every runtime control held to the dominant version of itself. Additive
unless a line says otherwise, and every change of behaviour is named where it
happens.

### Added

- **Every refusal site carries a `Decision`, including the fleet ones.** An
  org budget the control plane itself decided, an unreachable plane under
  `on_plane_loss="refuse"`, a fleet-wide halt, and a latch relayed from
  another worker used to carry no `Decision` at all — `exc.decision` was
  `None`, `exc.provider_called` fell back to `False` by default rather than
  by statement, and the completeness this SDK claims for every other
  refusal did not literally hold for these four. Each now carries one:
  `level="fleet"`, `provider_called=False` (none of the four is ever
  decided after a provider call), and a `boundary` — `"money"`, `"plane"`,
  or `"halt"` — read off the relayed detector name. None states
  `limit`/`used`/`reserved`/`estimate`/`remaining`: what reached this
  worker is a verdict and a name, not the numbers behind it, and a made-up
  number would be less honest than an `evaluation` that says only what is
  actually known.

- **Run and key budgets, named as such.** `budget_usd` stays the **key**'s
  budget — the identity behind a `session(key)`, which can live for months —
  and gains `budget_window`: `"hour"` / `"day"` / `"month"` (a calendar UTC
  boundary) or a float number of seconds (a rolling window), governing every
  cumulative number on the key (spend, tokens, the estimated-cost slice),
  which reset together the moment the window rolls over. In memory only: a
  restart starts a fresh window. New, independent controls `run_budget_usd`
  and `run_max_total_tokens` are the **run**'s own budget — one
  `session(key)` block — reset to a fresh number on every entry, exactly as
  `max_session_seconds`'s own clock already is. A call is checked against
  whichever of the run's and the key's own remaining is tighter, and
  `Decision.level` (`"run"` or `"key"`) says which one actually bound a
  refusal. A keyed session with a real `budget_usd` and no `budget_window`
  warns once after living past 24 hours — spend on it will otherwise
  accumulate for as long as the key is reused, which is almost never what a
  customer meant for a key that represents a person or a tenant. See
  [docs/guides/runs.md](docs/guides/runs.md#key-and-run-are-two-different-things).

- **`ExecutionRefused` — the refusal contract.** The exception runbound
  raises is now publicly named for what it is: a structured, retry-aware
  dependency failure, never a fake success and never a provider SDK's own
  exception. `GuardrailTripped` is the identical class under its old name,
  permanently (`runbound.GuardrailTripped is runbound.ExecutionRefused`) —
  every handler already written as `except GuardrailTripped:` keeps
  working. Every refusal now exposes `reason` (a stable code from a
  15-value closed set: `budget`, `tokens`, `steps`, `time`, `posture`,
  `policy`, `approval`, `circuit`, `concurrency`, `blast_radius`, `halt`,
  `plane`, `loop`, `error_storm`, `spike`), `boundary`, `decision`
  (unchanged), `retryable` (`False` for every reason a bare wait cannot fix,
  `True` — with a `retry_after` for `circuit` and `concurrency` — for the
  three reasons where the dependency itself said "not right now" rather
  than "this is against the rules"), `provider_called` (`True` only for a
  post-call budget crossing, where the call already happened and its result
  is withheld — `False` for every other, pre-call refusal), `scope`
  (`{"level": ..., "key_hash": ...}`) and `refusal` (unchanged). New:
  `runbound.is_retryable(exc)`, the predicate a retry loop should check in
  place of guessing from an exception's type or message. No refusal ever
  subclasses a provider SDK's own exception, so a provider's own retry
  layer — or a bare `except openai.APIError: retry` — never mistakes a
  refusal for its own kind of failure worth retrying, whatever `max_retries`
  is set to. New guide, `docs/guides/handling-refusals.md`; new table in
  `docs/reference/reactions.md`.

- **Complete Decisions.** The money reservation's own `Decision` used to
  state only `estimate`/`remaining`; `limit` and `reserved` were in the
  anomaly's own details but not on the object an exception actually hands
  the caller. Both are now on it too. Every refusal's `Decision.evaluation`
  now also states `provider_called: bool` — including two api-level entry
  doors (the fan-out limits, the in-flight cap) that predate the `Decision`
  object and previously omitted it.

- **A `stopped` posture now actually stops model calls, not only tools.**
  Every posture but `stopped` already kept serving model calls, as the
  posture table always said; `stopped` itself only refused a decorated
  tool's own capability classes, so a process (or a session) narrowed to
  `stopped` by hand could still call the provider. `Engine.admit` now runs a
  posture check first, before even the circuit, for a model call reached any
  way — a wrapped client's sync, async or streamed `create`, or a
  `@runbound.llm`-decorated function — so no provider call ever begins while
  the effective posture is `stopped`. The refusal carries `detector=
  "safe_mode"`, `boundary="posture"`, and latches only when the abuse
  ladder's own closed rung is what set it (that path already latches the
  session the moment it closes); a manual or plane-directed `stopped` never
  latches here — lifting it is exactly what lets calls through again.

- **Four action counters instead of one.** `SessionState` used to bump a
  single `executed_actions` the moment a tool call's event was recorded,
  before admission (posture, a capability rule, `max_actions_per_run`) or
  the tool policy had a say — so a refused attempt still counted as
  executed, and `max_actions_per_run` refused one attempt earlier than its
  own message said it did. `requested_actions` (a model asking for a tool,
  unchanged), `admitted_actions`, `executed_actions` and `refused_actions`
  are now four separate counters, and `executed_actions` only rises once an
  attempt has actually cleared admission and the tool policy —
  `max_actions_per_run` is measured against it. `tool_calls()` and the loop
  window are unaffected: they still count every attempt, refused or not, the
  same reasoning that lets a refused repeat still trip a loop. `envelope()`'s
  `execution` object and `session_status()`'s new `actions` object both
  report all four.

- **Safe mode, capabilities and postures.** A tool declares the
  capability classes it carries — `@runbound.tool(effects={"financial"})`, from
  `read`, `write`, `external`, `financial`, `destructive`, `privileged` — and a
  **posture** says which classes may run right now: `full`, `restricted`,
  `read_only`, `no_side_effects`, `stopped`. A run that gets hot keeps thinking
  and loses the right to act. While a session
  (`session.enter_safe_mode(posture=…)`), the process
  (`runbound.enter_safe_mode(posture=…)`, `exit_safe_mode()`,
  `runbound.posture()`, `safe_mode()`) or the fleet (a per-service `posture`
  field on the heartbeat reply, tighten-only) is narrowed, a tool is refused
  before its body runs whenever the posture denies **any one** of its classes —
  and an unclassified tool is refused by every posture but `full`. The refusal
  is `CapabilityRefused`, a `PolicyViolation` with detector `safe_mode` and a
  built-in 403 "That action isn't available right now."; it names the posture
  and the class it denied, and it latches nothing. `init(capabilities={class:
  verdict})` states a class rule that holds whatever the posture is, and
  `init(postures={name: {class: verdict}})` edits a row of the table. One
  function decides, `runbound.posture.Posture.allows`. Undecorated tools are
  never refused by a posture. `coverage()` gains `posture` and
  `tools_by_class`, `session_status()` gains `posture`, and the tool report
  gains a seventh key, `effects`, so every worker's tools hash changes once.

- **A soft line under the budget, and a readable budget.**
  `budget_soft=0.8` warns once at the first call that takes a session past 80%
  of `budget_usd` (detector `budget`, `details["limit_hit"] == "budget_soft"`);
  it never latches. `on_budget_soft="safe_mode"` also puts the session in safe
  mode until spend is back under the line or the key is cleared.
  `runbound.budget(key=None)` returns a `BudgetView` — `limit`, `spent`,
  `remaining`, `soft_at`, `window`, `resets_at`, `scope` — and
  `session_status()` carries the same numbers under `"budget"`.

- **The execution envelope and the Decision.** The runtime was
  `execute -> observe -> detect -> react`; it is now
  `admit -> execute -> reconcile -> record`. With `envelope=True` (the new
  default), a model call is checked at the door — circuit, unpriced model,
  `max_steps`, `max_session_seconds`, the `max_total_tokens` half of `budget`
  for a request stating its own output cap, then the money hold — before it
  goes out, instead of only being discovered over after the call already
  happened; a tool action is checked the same way — posture, a capability
  class rule, then a new limit, `max_actions_per_run`. Every refusal **at the
  door** — before a call or an action happens — now carries a `Decision`
  (`runbound.Decision`; `exc.decision`, also `anomaly.details["decision"]`):
  its verdict, which boundary of the envelope it hit, the detector, and the
  numbers behind it (`limit`, `used`, `reserved`, `estimate`). A **post-call**
  detector trip (`loop`, the post-call `budget`/`steps`/`timeout` walls,
  `spike`, `error_storm`) does not carry one — `exc.decision` is `None` for
  those — because `detectors.py` is deliberately out of scope here.
  `runbound.envelope(key=None)` reads the whole picture back as one object —
  money, steps, time, actions and capabilities — composed from `budget()`,
  the session's counters, `posture()` and any class rules; `None` before
  `init()`.

  The three door stages that stand in front of a **pre-existing** wall —
  `max_steps`, `max_session_seconds`, and the `max_total_tokens` half of
  `budget` for a request stating its own cap — honor `on_anomaly` exactly as
  that wall always has: **`"raise"` refuses here, at the door**, one call
  earlier than the wall would have. **`"warn"` and `"callback"` both log/alert
  at the door and let the call through** (a customer who asked only to be
  told is not silently upgraded to enforcing) — neither latches and neither
  calls the customer's handler *at the door*: the call's own event still gets
  recorded, and the wall behind the door detects the same crossing on it, for
  real, and reacts exactly once, including invoking the callback and
  latching under `"callback"`, one call later than the door, precisely as it
  always has. One consequence to expect in your telemetry: under `"warn"` and
  `"callback"` a single crossing now produces **two** anomalies rather than
  one — the door's projection (`details["rule"] == "envelope"`) and then the
  wall's own confirmed trip — which is also why the door's carries that rule,
  so neither dedupes the other away. What *runs* is unchanged.
  `max_actions_per_run`, a **brand-new** control with no prior
  wall to stay faithful to, and the money/reservation door (unchanged by
  this release) both refuse unconditionally, ignoring `on_anomaly`, as
  they always have.

  A steps, run-time or `max_actions_per_run` door refusal latches exactly as
  the wall it stands in front of would have; a money or tokens door refusal
  never does, for the same reason the money reservation never has — an
  estimate says nothing
  about the next call. `runbound/admission.py` is new: nine pure stage
  functions, numbers in and a `Decision` out, with no lock, no I/O and no
  session object, so the shape of each boundary is table-driven to test.

- **Three more loop shapes, beside the original "repeat".** The loops
  agents actually fall into are the edit-test-edit cycle, the retry storm and
  the stall — not `search("x")` three times, which "repeat" already caught.
  All four are deterministic and content-blind; general graph inference is
  refused, because it would cross into behaviour understanding.
  `config.loop_shapes` (default `("repeat", "sequence", "retry")` —
  `"stall"` is opt-in) says which of `runbound.config.LOOP_SHAPES` run, in
  what order; an unknown name is a `ValueError` at `init()`, not a silently
  skipped shape.
  - **`"sequence"`**: a period-k *rotation of tool names* (not exact
    arguments — the edit and the test each cycle usually differ),
    `2 <= k <= loop_max_period` (new knob, default `6`), repeated
    `loop_threshold` times back to back.
  - **`"retry"`**: one tool's `tool_error`s reaching `loop_threshold` within
    the trailing `loop_window` failures of *that* tool — distinct from
    `error_storm`, which counts every failure, any tool, any kind, in a
    trailing 60-second window rather than a trailing action count.
  - **`"stall"`**: `loop_stall_turns` (new knob, default `5`) consecutive
    turns that introduce no hash the session has not already seen.
  A `loop` anomaly's `details` gains `"shape"`, `"period_tools"` (the
  rotation's tool names, `[]` for `"stall"`), `"repeats"`, `"started_turn"`
  and `"usd_inside_loop"` — the model spend since `started_turn`, exact when
  the loop began within the trailing `spike_window` turns and an honestly
  documented lower bound otherwise (`SessionState.recent_calls` is the only
  per-call cost history a session keeps, and it is bounded by `spike_window`).
  Never `args_hash`, or anything else that could leak call arguments, in
  `message`. `@runbound.tool` gains `idempotent=` and `retryable=`, both
  purely declarative today — reported in the tool report and the control
  surface, enforced by nothing yet.

- **The plane delivers Controls.** A `Controls` body —
  per-level `limits` (`budget_usd`, `max_steps`, `max_events`,
  `loop_threshold`, `max_cost_per_call_usd`, `max_call_seconds`,
  `max_tokens_out_per_call`), `capabilities`, `posture`, per-detector
  `detectors` (`{name: {"action": "stop"|"notify", "mode":
  "shadow"|"enforce"}}`) and `envelope` — now reaches a connected worker on
  the heartbeat, the same way the org policy does: `HelloReply.controls_
  version` (new field, both sides of the wire) changes, the worker fetches
  `/v1/controls`, and the body is tightened against what your own `init()`
  already configured — **never the reverse**. A field the plane states that
  would loosen your own value is refused, your own value is kept, and the
  fact is reported back on the next heartbeat (`controls_refused`) for the
  dashboard's Controls tab to show. `runbound.coverage()`'s new `can_stop`
  key answers whether this process's own `on_anomaly` can actually stop a
  run (`in ("raise", "callback")`); a process where it cannot logs one
  WARNING at `init()` — "runbound is in warn mode: nothing will be
  stopped" — and the same fact rides every heartbeat for the plane's own
  warn-mode badge.

  **Per-detector `action`/`mode`, tightened against this worker's own code
  in *both* directions — this is `can_stop`'s whole reason to exist.** Each
  detector's own code baseline is table-driven (`Engine.
  _local_detector_action`), not one blanket default: `circuit` reads
  `on_provider_failure`, `loop` reads `on_loop`, `spike` reads `on_spike`,
  `velocity` (always `severity="warn"`) can never stop at all, and every
  other name (`budget`, `steps`, `events`, `error_storm`, `timeout`) reads
  `on_anomaly`. A plane `"notify"`/`mode: "shadow"` that would *loosen*
  that baseline is refused — the worker keeps stopping exactly as its own
  code said, and the refused field is reported (`detectors.<name>.action`
  and/or `.mode`) — so a raise-mode worker's walls, money included, cannot
  be switched off from a dashboard, silently or otherwise. A plane
  `"stop"`/`"enforce"` that *tightens* past a `"notify"` baseline (for
  example: code `on_spike="notify"`, a plane `spike: stop`) is applied
  for real only when this worker `can_stop` (`on_anomaly in ("raise",
  "callback")`); under `on_anomaly="warn"` it is not applied at all —
  forcing a `GuardrailTripped` nothing catches is exactly the bug fixed
  for the local case, not a remote feature — and is
  reported with `"reason": "cannot_stop"` instead, so the dashboard's
  warn-mode badge and this report tell the same story. `mode: "shadow"`
  never stops anything either way, whatever `can_stop` says. A
  warn-severity anomaly (a spike still watching, an escalating loop not
  yet confirmed, `velocity`, always) is never forced to stop by any
  control — the existing rule, unaffected.

  **Enforced at the session/process level only**: `agent`, `key`, `action`
  levels are carried and shown, never enforced, until a scope registry
  exists. `capabilities` and `envelope` merge through the existing
  paths; `posture` is unaffected — it still rides `HelloReply.posture`
  alone. The shared `controls_cases.json` fixture that
  proves the SDK's own merge (`runbound.controls_merge`) agrees with the
  control plane's own merge module case for case moved to `runbound-sdk/
  tests/fixtures/` from the plane's own test tree — the SDK cannot import
  the plane, so the one canonical copy lives on the side that can be read
  from.

- **Kill switch: Stop and Narrow, with a convergence number.** A
  fleet-wide halt now carries a `mode`: `"stop"` (unchanged — refuses every
  guarded session at the door) or `"narrow"` (states posture `restricted`
  fleet-wide — model calls keep serving, every non-read decorated tool
  refuses `CapabilityRefused`). `HelloReply.halt_mode` (new field, both
  sides of the wire, default `"stop"` so an older plane's halt keeps its
  old meaning exactly) carries it; `Engine.halt_posture()` reads it,
  tightened into `effective_posture()` from its own source, `"halt"` (new
  entry in `runbound.state.POSTURE_SOURCES`) — deliberately independent of
  the Controls-stated posture (`"plane"`), so lifting one can never
  clear the other. `on_halt` (`"raise"`/`"warn"`) now gates Narrow the same
  way it always gated Stop: `"warn"` never installs the posture at all
  (logged once a minute instead — "Fleet narrowed by the control plane...")
  rather than force a `CapabilityRefused` into code that asked never to be
  stopped by the kill switch. Every worker acks a halt it has seen and
  applied on its next heartbeat (`halt_ack`, staleness-aware — a worker
  that gave up enforcing a stale halt under `stale_halt="release"` stops
  acking it too); the control plane stamps `converged_at` on the halt event
  once every worker with a heartbeat in the last `2 x poll_s` has acked,
  and the dashboard's kill switch shows the live count and, once
  converged, the time it took, per service and org-wide, with history.

- **Circuit: rate mode, slow calls, `prevented`, fleet-wide.** The
  provider circuit gains `circuit_mode`: `"count"` (default, unchanged) or
  `"rate"` — [resilience4j](https://resilience4j.readme.io/docs/circuitbreaker)'s
  sliding-window model. Below `circuit_min_calls` (5) calls in
  `circuit_window_seconds`, nothing is judged; at or above it, either the
  fraction that failed crossing `circuit_failure_rate` (0.5) or the fraction
  slower than `circuit_slow_call_seconds` (`None`, off) crossing
  `circuit_slow_rate` (0.5) opens it — a slow-call rate can open the circuit
  with zero errors, which failure-counting alone never could. Both lines are
  strict `>`; an ordinary (non-slow) success in rate mode only adds a data
  point, unlike count mode's reset-on-success. A **streamed** call's slow-call
  measure is time-to-*first*-chunk, not the whole stream: a long, healthy
  answer (the exact shape of a chat UI or long-form generation) must never
  read as "slow" just for taking a while to finish saying it — only whether
  the provider started answering promptly. Everything else about a stream —
  its full duration for usage and spike detection, its token counts — is
  unaffected; a call that fails, at any point, is a failure regardless of
  timing, and a stream abandoned before ever being read reaches neither side
  of the circuit. `circuit_half_open_calls` (default **1**, the prior
  single-probe rule) admits that many probes at once, in either mode, before
  the next is refused. `CircuitOpen.retry_after` is a snapshot taken the
  instant it is raised — the breaker's own clock and the cooldown remaining
  then — and decays from that on every read, never from a number frozen
  once, and never from a live engine looked up later (which would answer
  wrong, or not at all, once a later `runbound.init()`/`reset()` swaps the
  breaker it actually opened under out from under it).
  `CircuitBreaker.snapshot()` gains `prevented`, per label: calls
  `on_provider_failure="open"` actually turned away, reset when the circuit
  fully closes. `circuit_posture` (opt-in, default `False`) narrows the
  *process* to `restricted` (source `"circuit"`) while a circuit sits
  half-open, lifting only that source when it closes — there is no
  per-provider tool scoping in this SDK, so this is process-wide, the same
  mechanism a manual call or the ladder use.

  On the plane: a per-service circuit, already folded from every worker's
  own transitions, is now explicitly a two-way opt-out —
  `circuit_fleet: bool`, **default `True` whenever a plane is connected**
  (a named default change; irrelevant, and never read, with no plane) — and
  a worker that connects while the fleet's circuit for a provider is already
  open starts open on its very first hello. That fleet instruction is never
  forced into a *refusal*: `on_provider_failure` is read after the breaker's
  state, not before it, so a `"notify"` worker sees the same `"open"` state a
  fleet-joining `"open"` worker does and keeps calling anyway, exactly as it
  would for an outage it found on its own.

  `docs/reference/circuit-breaker.md` is rewritten around the two modes.

- **Baselines that survive a restart, and peer baselines.** Connected
  to a plane, every session exit reports this key's *held, trusted* spike
  baseline (never a live median taken mid-spike — that would let a
  sustained abuse run teach itself a new "normal") and the abuse ladder's
  own rung — level, and, while limited, the allowance left. The plane keeps
  one row per key (a new `baselines` table) and hands both back on the next
  entry, from any worker: a worker restarted mid-spike re-enters a limited
  key at the same rung — never reset to level 0 (forgiveness by redeploy)
  and never escalated — and a brand-new key is judged against the
  service-wide median from its first call rather than served a free
  warmup nobody else got. The service median is a median of every key's
  own baseline, one vote per key, never weighted by call volume, so one
  high-volume caller cannot define what "normal" means for a key that has
  never called before.

  Spike detection and the ladder are local and free (`spike_detection`
  defaults `True` with no plane and no enable flag of any kind — see
  "The paid line" below),
  so what this task delivers is the connected half on top of a detector
  that already runs without one: the restored baseline and rung, the peer
  baseline, and the wire shape to carry both. The per-key restored
  baseline and rung ride `EntryDecision` — the same per-key payload
  `strikes`/`generation`/`latch` already do, since `Controls` (the
  heartbeat's per-*service* payload) has no per-key dimension — while the
  service-wide median rides `Controls` itself
  (`runbound.shared.RemoteState.service_baseline()`, read off the same
  Controls body `controls_directive()` is, but never gated on `dry_run`:
  a peer baseline is advisory data, not an enforcement directive). One
  honest consequence, still true under the local-by-default detector: a
  service needs *some* Controls row published (any rollout state) before
  its peer baseline reaches a worker at all — a service with none
  configured never fetches `/v1/controls` in the first place, and the
  session simply keeps judging itself against its own baseline alone.
  `anomaly.details` gains `baseline_source`
  (`"local"` / `"restored"` / `"peer"`) and, whenever a service baseline is
  known, `vs_service` — the call's ratio to it, reported alongside the
  existing ratio to the session's own baseline in the anomaly's message
  ("N× its own normal, M× this service's"). `session_status(key)["why"]`
  gains the same `source` on `baseline` and a new `service_baseline` key.
  None of this changes what `on_spike` does with a confirmed spike — a
  restored or peer baseline only changes what counts as abnormal, never
  the reaction — and without a plane every one of these fields is exactly
  what it always was (`baseline_source: "local"`, `service_baseline:
  None`). `state.spike_baseline_samples` -- the count an exit delta reports
  as this baseline's support -- tracks the consecutive calls that were
  individually clean, not the raw length of the trailing call window: a
  review of a real spike-train probe found the count still growing through
  calls that came after an abnormal one aged out of the ladder's own
  5-call confirm window while remaining in the up-to-`spike_window` (50)
  history the median is drawn from (harmless to the median itself, which
  stays a robust minority-outlier case, but not to a count claiming to
  describe this baseline's clean support). `docs/guides/spike-detection.md`
  is retitled "Progressive degradation" around this: the ladder as the
  automatic driver of postures, never a headline abuse score.

- **Local telemetry is free.** `runbound.events(n=100)` and
  `runbound.decisions(n=100)` read an in-memory ring of this process's own
  anomalies, refusals, posture transitions and Decisions — no account, no
  plane, nothing written to disk, nothing sent anywhere, and no record ever
  carries a call argument, a prompt or a reply. An optional `on_event=`
  callback on `init()` is told about every record as it happens; a raising
  callback is logged and swallowed (fail-open).

### Changed

- **`@runbound.tool(repeatable=True)` is now `polling=True`.** Same
  meaning — a tool that is *supposed* to run with the same arguments over and
  over never feeds the loop window — new name, chosen to read next to the
  new `idempotent=`/`retryable=` keywords. `repeatable=` is gone rather than
  aliased (the package is public but unannounced, so there is no
  compatibility burden): passing it now raises `TypeError`.

- **`@runbound.tool(effect=...)` is now `effects={...}`.** One value on
  one axis became a set of capability classes; `effect=` is gone rather than
  deprecated (the package is public but unannounced, so there is no
  compatibility burden). A tool is refused when the posture denies **any** of
  its classes, and an unclassified tool is refused by every posture but
  `full`. `require_rules=True` now fails an unruled tool carrying `financial`
  or `destructive` — the consequential classes — where it previously named
  `irreversible`. The tool report's `effect` key became `effects` (a sorted
  list), `coverage()["tools_by_effect"]` became `["tools_by_class"]` counting a
  tool under every class it carries, `coverage()` gained `posture`, and
  `session_status()["safe_mode"]` became `["posture"]` carrying the posture's
  name. The spike ladder's limited rung sets `restricted` and its closed rung
  `stopped`; a budget soft line sets `restricted`. On the wire, the heartbeat's
  `safe_mode: bool` became `posture: str | None`.

- **Default change: `budget_admission="capped"`.** `budget_admission`
  was `False`. It now defaults to `"capped"`: a request that states its own
  output cap (`max_tokens`, `max_completion_tokens`, `max_output_tokens`) is
  refused before it goes out when that cap at the model's output rate, plus its
  input estimate, would cross `budget_usd` (`details["reason"] ==
  "reservation"`; it never latches). A request with no stated cap takes 0.3.0's
  path unchanged, and `True` keeps the assumed-cap estimate. Anthropic's
  `messages.create` always states `max_tokens`, so every Anthropic call under a
  `budget_usd` is now reserved. The trade-off, as the docs state it:

  > A call whose cap-priced worst case exceeds what is left is refused even if it
  > would have used less: with $0.10 left, a request capped at 15,000 output
  > tokens on a $10-per-million model is refused although its answer might have
  > cost a cent. That is the price of "cannot be crossed". Two limits are stated
  > rather than hidden: the output side of the worst case is exact, and the input
  > side is the same chars/4 estimate used everywhere else in runbound, so a prompt
  > much denser than four characters a token can still take the session past the
  > line by the difference; and a call with no stated cap is not reserved at all.
  > `budget_admission=False` restores 0.3.0.

- **The reservation now holds for the call's lifetime, not just at the
  door.** The admission check used to compare a call's worst case
  against *settled* spend only, so N calls racing the budget edge at once were
  all admitted — the overshoot window was N × worst case. The estimate that
  passes is now held on the session's own ledger (`session.reserved`) from
  admission until the call closes out, however it ends (return, error, or —
  for a stream — exhaustion, an early close, a mid-flight failure, or being
  abandoned and collected), so near the budget edge a second, otherwise
  generous, stated cap is now refused while another capped call is still in
  flight — exactly `floor(remaining / worst_case)` of N concurrent calls are
  admitted, not N. A refusal now also names what is already held:
  `details["reserved_usd"]`, with `remaining_usd` already reduced by it.
  Under `budget_admission=True` the *assumed* `admission_output_tokens` cap is
  held the same way, so an uncapped call — an open stream especially — holds a
  guess's worth of money for as long as it runs. `runbound.budget().reserved`
  and `BudgetView.reserved` (its last field) read
  what this worker currently holds — worker-local, never folded into fleet
  spend or sent to the control plane — and `session_status()["budget"]
  ["reserved"]` carries the same number. `record_call()` never takes a hold,
  because the call it describes is already over.

- **Default change: `envelope=True`.** `envelope` is a new field,
  defaulting on: `runbound.init()` with no other change now refuses the
  `max_steps`/`max_session_seconds`/(capped) `max_total_tokens` overrun one
  call earlier than 0.3.0 did — at the door, before the offending call goes
  out, rather than after it returns — which is one fewer paid provider call
  per over-limit session. No other observable behaviour moves: money
  admission (`budget_admission`) and posture/capability enforcement are
  unaffected either way, since they are their own, older opt-ins, not new
  envelope controls. `envelope=False` restores 0.3.0's admission behaviour
  exactly, proven by `tests/test_behaviour_golden.py`'s golden fixture,
  captured against 0.3.0 code and still passing byte-for-byte under
  `envelope=False` today.
- **`on_spike="limit"`: the limited rung puts the session in safe mode.**
  Its tools stop acting while `spike_limit_calls` abnormal model calls are
  served; healing or closing lifts it. In 0.3.x a limited session's tools kept
  running.
- **`require_rules=True` fails a tool carrying a consequential capability class
  — `financial` or `destructive` — with no rule that can refuse it, even with
  `reviewed=True`.** A tool that declares no such
  class is judged exactly as before.

### Fixed

- **The action-cap refusal's own sentence miscounted what it was reporting.**
  "Too many actions this run: N taken, limit L" read `N` as
  `executed + 1` — the count this attempt would have brought the tally to,
  the same "count already spent" convention `max_steps`'s own door uses —
  as if it were a count of completed actions, so a cap of 3 with 3 actions
  actually executed read "4 taken". `Decision.evaluation` and
  `session_status()["actions"]` were already correct; only the message
  lied. Now: "the next action would be action N, limit L", the same shape
  the steps door's own message already uses correctly.

- **A warn-severity anomaly never stops the run, in any `on_anomaly` mode.**
  `on_anomaly` is the reaction to a *critical* anomaly, as the reactions table
  has always said, and `velocity` "never stops anything" — but under
  `on_anomaly="raise"` a velocity warning raised `GuardrailTripped` on the
  crossing call (without latching), and under `"callback"` it was handed to
  the kill switch. Both now log and alert only; observers and the plane see
  the reaction as `warn`.

- **The source distribution ships `examples/` and `docs/`.** 0.3.0's sdist
  carried `tests/` but not `examples/`, so its bundled docs-example test
  failed when run from the sdist (the wheel was unaffected). `MANIFEST.in`
  now grafts both directories, and `tests/test_docs_examples.py` skips with
  a reason instead of failing when `examples/docs/` is absent.

### The paid line

Restated here rather than as a Removed-then-Added pair, since nothing
between was ever released: **open-source runtime, cloud control plane.**
Every local, deterministic control is free and local, configurable in code,
with no account. `postures`, `capabilities`, `budget_soft`,
`on_budget_soft`, `max_actions_per_run`, the eight circuit-rate knobs
(`circuit_mode`, `circuit_failure_rate`, `circuit_min_calls`,
`circuit_slow_call_seconds`, `circuit_slow_rate`, `circuit_half_open_calls`,
`circuit_fleet`, `circuit_posture`), the three loop-shape knobs
(`loop_shapes`, `loop_max_period`, `loop_stall_turns`), and the eleven
spike/ladder knobs (`spike_detection` — defaulting `True` again —, `on_spike`
and its nine tuning fields) are all real `init()` keywords. A connected
control plane can only *tighten* what your code configures for these six
controls (circuit rate mode and posture-narrowing, loop shapes, the budget
soft line, `max_actions_per_run`, class-rule capabilities, and spike
detection/the ladder), or state one from scratch where you leave a field
unconfigured — one merge function (`runbound.controls_merge`), the same
algebra whichever field it touches; a looser plane value is refused and the
refusal is visible (`Engine.controls_refusals()`). `enter_safe_mode()` /
`exit_safe_mode()` and `runbound.envelope()` are public again, on the
process and the session — `envelope()`'s object also still rides the
heartbeat. `coverage()` drops `advanced_controls` for a single `fleet` key:
`"connected"` while a plane answers, else the honest "local protection
active; fleet coordination: not connected" — the one nudge, never a
per-call log line. `entitlements` on hello now carries only coordination
and scale facts, never a control name; the control engine reads no
entitlement at all. See
[Free SDK, connected plane](docs/concepts/free-and-connected.md).

## [0.3.0] - 2026-09-12

First public release, as `runbound` (import `runbound`), by Runbound AI.
Earlier versions were internal.

### Added

- **Fleet mode — a control plane, so N workers share one truth.** Connect
  with `token` alone (hosted) or `control_plane_url` plus a `token`
  (self-hosted — `token=""` if that plane needs no auth; see "Two kinds
  of connected customer" below for how the two are told apart), and the
  replicas serving one end-user
  share a budget, a latch, a strike count, an org action policy and a set of
  provider circuits. Detection does not move: every detector still runs
  in-process, on your thread, with no model calls.
  - **Shared budget.** A session entry carries the fleet's spend and tokens for
    that key; the SDK folds them in as offsets, so `budget_usd` and
    `max_total_tokens` trip on the turn a single worker with those numbers
    would (`details["fleet_spend_offset_usd"]`, `["fleet_tokens_offset"]`).
  - **Shared latch and strikes.** A critical trip is reported synchronously and
    the next worker to open that key is refused at the door, with what is left
    of the fleet's ttl; `clear(key)` clears it fleet-wide.
  - **Org-wide action policy**, merged with the local `tool_policy`
    most-restrictive-wins (`policy.merge()`) — bans unioned, allow-lists
    intersected, `max_calls` the lower of the two, callables never taken off
    the wire. Org rules report `details["origin"] == "org"`. A `dry_run`
    version is logged and alerted and never blocks, which is how a rule is
    rolled out across a fleet; local rules keep blocking.
  - **Fleet circuits.** The heartbeat can open or close a provider circuit on
    every worker at once (`circuit.force_open()` / `force_close()`). What an
    open circuit does is still `on_provider_failure`.
  - **Fleet kill switch**, with `on_halt="raise"` (refuse every guarded
    `session()` block, detector `halt`) or `"warn"` (log once a minute, keep
    serving).
  - **The plane is contacted at four moments and nowhere else**: an `init()`
    heartbeat every `control_plane_poll_s` (5 s), a session entry (bounded by
    `control_plane_timeout_s`, 150 ms, and cached per key for 5 s), a session
    exit (queued, posted in background batches) and a trip or circuit change.
    Never per model call, never per tool call.
  - **Fail-open throughout.** A plane that is down costs one timeout, warns once
    a minute, degrades after 3 consecutive failures and retries once every 30 s;
    a rejected key goes local-only; a fleet halt stops being enforced 60 s after
    the last contact. `plane_status()` reports `"local"` / `"connected"` /
    `"degraded"`.
  - **Hashes and counts, never content.** One module, `runbound.plane_types`,
    defines every outbound record: session keys travel as `sha256(key)` (raw
    only with `send_session_keys=True`), tool arguments only as `args_hash`,
    error messages only as `error_class`, detector `details` scrubbed to JSON
    scalars, tags capped. Prompts and replies have no field to travel in.
  - New config: `control_plane_url`, `token` (`api_key` is the deprecated
    alias), `service`, `worker_id`, `control_plane_timeout_s`,
    `control_plane_poll_s`, `export_events`, `send_session_keys`, `on_halt`.
    New API: `plane_status()`, `fleet_status(key)`, `key_hash(key)`.
- **Fleet mode talks to any server that speaks six endpoints, not a bespoke
  client.** `POST /v1/hello`, `/v1/enter`, `/v1/trip`, `/v1/events`,
  `GET /v1/policy`, `POST /v1/clear` are the whole wire protocol a connected
  plane must answer; the SDK's half of fleet mode is free, MIT-licensed, and
  works against anything that speaks them, ours or not.
- **Webhook signatures verify the same way after delivery left the SDK.**
  `runbound.verify_webhook_signature()` is the receiver-side check kept when
  the SDK's own `WebhookAlerter` retired (see "Delivery is not in the SDK"
  below): a constant-time
  compare of `X-Runbound-Timestamp` / `X-Runbound-Signature` =
  `"sha256=" + HMAC_SHA256(secret, f"{timestamp}.{body}")`, with a
  ±5-minute replay window, `False` rather than an exception for anything
  malformed. Whatever now sends a webhook uses the same envelope, headers
  and signing string the retired sender did, so a receiver that only checks
  the signature needs no changes — but two body fields did change:
  `session.id` is now the sha256 key hash rather than a per-process id, and
  `session.key` (what `send_session_keys=True` used to add) does not exist
  in the body at all; a raw key reaches you only through your own
  `link_template`.
- **Instrumentation coverage.** `auto_wrap` (default `True`) patches the OpenAI
  and Anthropic SDK classes at `init()`, so a client built anywhere — inside a
  framework, in code written before runbound was installed — is guarded
  without a `wrap()` call; per-call endpoint labels still come from that
  client's `base_url`, and `unpatch()` reverses it. `coverage()` reports what is
  actually instrumented right now (auto-wrapped SDKs, wrapped clients, decorated
  tools, guarded calls, providers imported but never seen guarded);
  `assert_guarded()` raises for a startup check or a CI smoke test; and
  `coverage_check_seconds` (default 60 s) warns once when a provider SDK is
  imported and no guarded call has ever been recorded.
- **Customer-set refusal responses.** runbound raises `GuardrailTripped`; it
  never used to say what the end user should be told. `exc.refusal` now
  carries the HTTP status and sentence *you* set — `runbound.init(refusals={...})`
  locally, or set once on a connected control plane, on every plan, never
  gated. Precedence is checked field by field: a connected plane's
  per-service profile, then its org-wide profile, then your local
  per-detector entry, then your local `default`, then a documented
  `BUILTIN` fallback. Keys are `"default"` or a detector name (`budget`,
  `loop`, `spike`, `velocity`, `steps`, `error_storm`, `timeout`, `fanout`,
  `inflight`, `policy`, `circuit`, `halt`, `fleet`) — per-call hard caps
  report through `spike`, so there is no `cap` key. A message may carry
  `{retry_after_s}` / `{detector}`, formatted safely, and
  `exc.refusal.headers` carries a rounded-up `Retry-After` when a latch's
  remaining time is known. A profile set on a connected plane reaches every
  worker within `control_plane_poll_s`, no restart, and survives a plane
  outage like a policy rollout does. `coverage()["refusals"]` reports which
  tier is answering. New module `runbound.responses` (`Refusal`,
  `refusal_for`, `BUILTIN`); new config `refusals`; `examples/stress` now
  answers refusals entirely from a profile instead of a hardcoded status and
  sentence.

- **Honest promises.** A review of the positioning and a few silent gaps
  in the code, fixed together.
  - **An unpriced-model policy, `on_unpriced_model`.** `"zero"` (default,
    unchanged behavior) now warns once per model **by default**, so the
    blind spot is not a silent one — anyone self-hosting a model with no
    price set gets one warning, not zero. `"estimate"` prices from a new
    `unpriced_price_per_1m_usd` fallback pair instead, marking the number
    `priced="estimated"`. `"refuse"` stops the call at the door, before it
    goes out, whatever `on_anomaly` says — a choice you stated on purpose.
  - **Tools that are supposed to repeat.** `@runbound.tool(repeatable=True)`
    and the new `loop_ignore_tools` config mark a polling tool exempt from
    the loop window only; it still counts toward `tool_calls()` and any
    `max_calls` in an action policy.
  - **The async throttle actually throttles.** `on_loop="throttle"` used to
    warn-and-skip under a running event loop, silently doing nothing. It now
    hands the delay through a contextvar and the async wrapper `await
    asyncio.sleep()`s it, so throttling works the same way under asyncio as
    it does under threads.
  - **Argument hashes are salted per process.** `args_hash` now mixes in a
    random salt generated once at import (kept across `reset()`). It remains
    an equality token for spotting a repeat inside one process; it is no
    longer even accidentally usable as a fingerprint to correlate calls
    across two processes or two workers from the hash alone.
  - **Abandoned streams are counted, not lost.** A stream that is never
    exhausted or closed used to vanish — no tokens, no step, ever. It is now
    recorded as one partial call the moment Python garbage collects it
    (never at interpreter exit), in the session that opened it, with the
    time actually streamed and tokens from usage if any chunk carried it,
    else `ceil(chars/4)` of the streamed text marked `estimated`. Counted as
    neither a circuit success nor a failure; its in-flight slot is freed.
  - **`stale_halt` and `on_plane_loss`, two more explicit customer choices.**
    `stale_halt="release"` (default, today's behavior) lifts an enforced
    halt 60 s after the last plane contact; `"hold"` keeps it enforced on a
    dead link until a heartbeat says otherwise.
    `on_plane_loss="guard_locally"` (default) falls back to local detection
    when an entry question could not be answered at all; `"refuse"` refuses
    the entry itself instead, latching nothing. An invalid API key is
    always a configuration error, guarded locally under both settings.
  - **The plane's own refusals are now honored at the door.** Before this
    release the SDK read only the *facts* a plane decision carried (spend
    offsets, strikes, a latch) and never its `allow` field, so a plane that
    refused a session outright — an org daily budget already spent, or an
    entry refused under `on_plane_loss="refuse"` — was silently overruled
    and the call went out anyway. It is now refused at the door, with
    `details["origin"] == "plane"` either way. An entry refused under
    `on_plane_loss="refuse"` is detector `plane` and resolves against a new
    `"plane"` key in refusal profiles (built-in: HTTP 503); an org daily
    budget already spent is detector `budget` (`details["rule"] ==
    "org_budget"`) — a budget like any other, just decided by the plane —
    and resolves against the `"budget"` key.
  - New `INVARIANTS.md`: the budget, latch, circuit, policy-monotonic, halt,
    plane-loss, abandoned-stream and unpriced-model guarantees, each with
    its bound and the tests that assert it — or "not yet asserted" where
    honest.
  - Positioning: the README leads with "runtime controls for autonomous AI
    agents," names the buyer (the platform team that owns AI agents in
    production), and states the fleet budget as a bound (worst case:
    `workers × one in-flight turn` plus the entry-cache window) rather than
    calling it a hard limit. A compatibility matrix (provider ×
    sync/async/stream/tools/usage/live) is filled in only where a named test
    proves the cell.

### Added

- **`init()`'s heartbeat now reports coverage.** The hello payload the SDK
  sends the plane carries `coverage: {guarded_calls, decorated_tools,
  providers_imported, providers_unguarded}` — the same numbers
  `runbound.coverage()` already exposed locally — so a connected plane's
  Services page can show, per worker, what is actually guarded; any
  failure computing it degrades to `{}` rather than breaking the
  heartbeat. Shown as counts, deliberately not a percentage.

### Changed

- **`token` replaces `api_key` as the one way to connect.** A dashboard
  credential is now `token`. `api_key` is the old name for the same secret:
  it still works, folding into `token` at `validate()` with a one-time
  `WARNING` — fired whether or not `token` was *also* set, so the two never
  sit on the object together — and cleared to `None` afterwards so nothing
  downstream reads it. `token` also reads from the `RUNBOUND_TOKEN`
  environment variable when unset (blank or whitespace counts as unset).
- **Two kinds of connected customer, not one setting with a truthiness
  test.** `token` alone — with no `control_plane_url` set anywhere, and none
  in `RUNBOUND_PLANE_URL` either — is **hosted**: you are on our server, we
  resolve the endpoint at `HOSTED_PLANE_URL` (`None` until that plane
  exists; a bare token then logs one WARNING and the process stays local,
  rather than the ~45-line urllib traceback a placeholder domain pointing
  at a parked host used to produce). A `control_plane_url`, from the call or from
  `RUNBOUND_PLANE_URL`, is **self-hosted** instead: your own cluster, and
  `token=""` is how you say that plane has no auth. Both facts can also come
  from the environment
  (`RUNBOUND_TOKEN`, `RUNBOUND_PLANE_URL`), and a blank value from either
  source is unset either way — there is no such thing as a plane at the
  empty url. `GuardrailConfig.plane_mode` (`"off"` / `"hosted"` /
  `"self_hosted"`) is the one question every internal reader now asks
  instead of re-deriving its own falsiness test; `shared.build` asks it
  first. A `control_plane_url` with no token still raises, reworded around
  the two modes (`'a self-hosted plane needs a token ... token="" if that
  plane has no auth'`).
- **Delivery is not in the SDK.** A paid feature cannot be enforced by a
  conditional running inside the customer's own process, so the code that
  sent alerts directly left: `SlackAlerter`, `PagerDutyAlerter` and
  `WebhookAlerter`, the PagerDuty constants, the posting helpers and the
  alert rate limiter are gone from `runbound/alerts.py`, and `Engine._alert`
  now only notifies observers. `init()` answers the five retired keywords —
  `slack_webhook`, `pagerduty_routing_key`, `webhook_url`, `webhook_secret`,
  `link_template` — with a `ValueError` naming where the setting actually
  lives now (an alert route, or a per-service field, on your runbound
  dashboard) instead of a bare `TypeError: unexpected keyword`.
  `verify_webhook_signature` and the outbound-thread draining bookkeeping
  stay in `runbound/alerts.py` — the receiver's helper, and what
  `export.py` needs at interpreter exit — and `on_anomaly="callback"` stays
  free and local, a hook into the customer's own process, not a delivery
  channel. This changes nothing about detection: every detector, the
  latch, refusals, tool policy, the provider circuit and the in-flight cap
  are identical with and without a token, `on_anomaly`'s three reactions
  (`raise`, `callback`, `warn`) are alternatives — a raise or a callback
  does not also log — and behave identically with or without one, all
  asserted directly in `tests/test_token_and_delivery.py` and
  `tests/test_plane_modes.py`. See the rewritten invariant in
  `INVARIANTS.md`, "The SDK detects, stops, refuses and reports. The plane
  routes and delivers."

Three fixes to what public knobs mean, landed before the first PyPI
release because each changes the meaning of a public knob — renaming or
re-meaning one after real customers hold a version would break them.

- **`max_session_seconds` measures the run, not the end-user's whole
  history.** Before this, `SessionState.started_at` was set once, at
  creation, and `max_session_seconds` measured elapsed time from it — so for
  a keyed chatbot session that is reused across requests, the wall clock
  counted from the user's very first message ever, and a returning user
  could trip the timeout on their next word. `SessionState.run_started_at` is
  now reset on every entry of `runbound.session(key)`, and the `timeout`
  detector measures `max_session_seconds` against it instead
  (`details["scope"] == "run"`). The default (unkeyed) session, which has no
  entry to reset on, is unchanged: it *is* the run, from `init()` onward.
  `started_at` keeps its old meaning — the session's whole existence, never
  reset — for the new, opt-in `max_session_lifetime_seconds`
  (`details["scope"] == "lifetime"`), for a customer who wants the old
  behavior back on purpose. The two fire independently.
- **Steps are model turns; a new `max_events` counts everything.**
  `max_steps` counted every recorded event before this — a turn with one
  model call and three tool calls read as four steps. `SessionState.turns`
  now counts `llm_call` events only, and `max_steps` (detector `steps`) is
  measured against it. `SessionState.step_count` is renamed
  `event_count` (measuring what `max_steps` used to: every recorded event),
  and a new `max_events` (detector `events`) is measured against it instead.
  `step_count` is kept for one release as a read-only alias for
  `event_count`. `ExitDelta.steps_delta` now carries turns, not the raw event
  count — see "What the plane needs from this" below.
- **Anomaly ties are resolved by a stated order, not list order.**
  When more than one detector fires critical on the same event, which one
  drove the reaction used to depend on `detectors.DEFAULT_DETECTORS`' list
  order — invisible, and never a decision anyone actually made. A new
  `runbound.events.PRIORITY` table states the order once (highest first):
  `policy`, `budget`, `loop`, `error_storm`, `steps`, `events`, `timeout`,
  `spike`, `velocity`. `Engine._winner` (renamed from `_most_severe`)
  resolves ties against `(severity, priority, detector name)` instead of
  iteration order — reversing `DEFAULT_DETECTORS` now produces the same
  winner. A detector name the table has never heard of (a customer's own)
  sorts after every named one and warns once; it never crashes the
  selection. Door anomalies (`halt`, `circuit`, `inflight`, `plane`) are
  raised before detection runs and are never part of a tie.

  **What the plane needs from this:** `ExitDelta.steps_delta` on the
  wire now means model turns, not the raw event count it meant before this
  release — a dashboard's "Steps" column reading it should say "model turns."
  This release only prepares the SDK side (`SessionState.turns`,
  `SessionState.event_count`, the `events` detector) and repoints the one
  existing wire field. It does not add a separate wire field for the raw
  event count, `errors_delta`, `tokens_cached_delta`, `last_detector`, or
  the two trigger fields — those are a later, plane-side addition, together
  with the plane-side router, ledger and migration this SDK release does
  not touch.

### Fixed

- **A healed latch re-arms detection.** `latch_ttl_seconds` expired a
  latch, but every detector still fires only once per session id and no
  counter was ever reset — so a session that was still over budget when the
  ttl elapsed ran with *no budget wall at all* afterward, the opposite of
  what the setting promises. On heal, the engine now calls a new
  `rearm(session_id)` on every detector (`_FireOnceDetector.rearm`,
  overridden by `TimeoutDetector` for its second, lifetime-scoped memo, and
  by `SpikeDetector` for its two-phase warn/confirm memos), so a session
  whose condition still holds re-trips on its very next event, with the same
  detector. This is re-admission, not a clean slate: no counter is reset —
  only `runbound.clear()` does that — so `latch_ttl_seconds` is not a
  windowed budget (that stays a separate, unbuilt feature). The stale claim
  that a healed session "stays quiet about the condition it already
  reported" is gone from the docs and the code's own docstrings.

### Added

- **Admission: a named phase, and an opt-in pre-call budget
  estimate.** The circuit check and the `on_unpriced_model="refuse"` door
  refusal — previously inline in the api's `_Hooks.before` — are now
  `Engine.admit(session, provider, model, request)`, one named phase every
  wrapped call passes through before it goes out. Opt-in and **off by
  default** — `budget_admission: bool = False` — because estimation is not
  deterministic and this product's identity is: `budget_usd`'s post-call
  wall stays the default reaction, exact and unchanged. Set
  `budget_admission=True` to also refuse a call whose *estimated* cost would
  cross `budget_usd` before it goes out: `estimated_tokens(chars of the
  request's messages)` at the model's input rate, plus the request's own
  output-token cap (`max_tokens` / `max_completion_tokens` /
  `max_output_tokens`, read by a new `request_output_cap(kwargs)` in each
  wrapper) or, absent one, the new `admission_output_tokens: int = 1024`, at
  the output rate — the same price table the post-call check prices with. An
  unknown model skips the estimate for that call and warns once per model,
  rather than inventing a limit the customer never set (the post-call wall
  still watches it). A refused admission **never latches** — a cheaper call
  minutes later may still fit — and is alerted once per session
  (`details["rule"] == "admission"` keeps it from being deduped against an
  unrelated post-call `budget` trip in the same session). `call_before` (and
  `_Hooks.before`) now also pass the request's raw kwargs through, tolerant
  of hooks written before this release the same way an older, model-less
  `before` was already tolerated. With `budget_admission` left off, every
  existing call path is unchanged.

- **`@runbound.tool` states the rule, in the same line as the function.** No
  policy file, no CLI, no second configuration surface to drift from the code:
  `blocked=True`, `max_calls=n`, `constraint=predicate`,
  `require_approval=predicate` and `reviewed=True` join the existing `name` and
  `repeatable`. They fold into an ordinary `ToolPolicy` — `blocked` becomes a
  `deny` entry, so the rule name in a refusal, an anomaly and the ledger is the
  one it always was — and `policy.evaluate` and `policy.merge` are byte-for-byte
  unchanged. The builder is `policy.from_decorators()`; the statement itself is
  `policy.ToolRules`.
- **The fold is live, not snapshotted at `init()`.** In a normal module `init()`
  runs at the top and the tools are defined below it, so a policy folded once at
  start-up would see an empty registry and enforce *nothing*. The decorators
  write into a process-lifetime registry and `Engine._local_policy()` composes
  on demand, cached on the registry's version and the configured policy's
  identity — the same object comes back while neither has moved, so the org
  merge above it does not re-key on every tool call.
- **Each tool gets its own approval callback.** `ToolPolicy` asks one callback;
  the decorator gives a different callable per tool, so the fold builds one
  dispatcher that routes on the tool's name, falls through to the
  `approval_callback` configured on `init()` for a tool no decorator claimed,
  and **refuses** when there is neither — an approval nobody can answer is a
  refusal, not a permission.
- **`init(require_rules=True)` is the CI gate.** A `@runbound.tool` that states
  no rule at all raises `ValueError` — named at `init()` for every such tool
  already imported, and raised at decoration for every one declared afterwards,
  which is where they normally are. Any CI step that imports the app fails with
  it, so a tool cannot reach production without a stated rule. A tool that
  genuinely needs none says so with `reviewed=True`.
- **`reviewed=True` is not `ToolPolicy.allow`.** The decorator's `reviewed=True` means
  "reviewed, deliberately unrestricted" and is folded nowhere;
  `ToolPolicy.allow` is a fleet-wide *inverting* allow-list, and putting one
  reviewed tool on it would deny every other tool in the process.
- **`tool_policy=` on `init()` stays** for the two things a decorator cannot
  say — the fleet-wide `allow` list and rules for framework tools that carry no
  decorator of yours — plus `on_violation`, which is a property of the policy
  and not of any one tool. Where both name the same tool the decorator wins,
  with one warning naming it.
- **The tool report carries the rules.** Each entry gains
  `"rules": {...}` as the decorator stated them (`{}` when it stated none), with
  a `constraint` or `require_approval` rendered as `"module:qualname"` — never
  the callable, which is never shipped and never called off-process. This moves
  `tools_hash`, which resends each worker's report once, by design.
- An unenforceable decorator keyword (`max_calls=0`, a `constraint` that is not
  callable, `blocked=True` with `reviewed=True`) raises `ValueError` at decoration,
  where it was written, exactly as a bad `init()` argument does.
- **The circuit can read the provider's own rate-limit headers**, opt-in via
  `circuit_reads_quota=True` (default off). A 429's `Retry-After` sets that
  opening's cooldown instead of `circuit_cooldown_seconds`, and a remaining
  bucket at zero opens the circuit until its reset without waiting for
  `circuit_failure_threshold` failures — reported as the usual `circuit`
  anomaly with `details["reason"] == "quota"` (a failure-driven one now says
  `"failures"`). No header may hold a circuit shut for more than an hour.
  Fail-open throughout: an unreadable header says nothing and is never read
  as zero. Note the limit, which is in both provider SDKs rather than in
  runbound — a plain successful call returns a parsed model with no headers
  at all, so a pre-emptive opening happens only from an error response or
  from a call your own code made through `with_raw_response` / `.parse()`;
  runbound never changes how your call is made to get at headers. Streaming
  is out of scope. With the option off, the circuit behaves exactly as before.

### Fixed

- **Anthropic's extended-thinking count is read.** `anthropic`
  1.5.0 states it as `usage.output_tokens_details.thinking_tokens`, one
  level below where the wrapper was looking, so `tokens_reasoning` had
  been reporting 0 for every thinking call. **No bill changes**: thinking
  tokens are a *subset* of `output_tokens` and were always priced at the
  output rate. What you get back is the split — how much of an answer was
  thinking. Found by replaying a real recorded response; no test could
  have caught it, because every test built its own usage object.

### Changed

- README: a "Fleet mode" section (when the plane is contacted, what it adds,
  what goes on the wire, and reading the link), a "What the SDK actually sees —
  and what it never sees" sensor matrix, five new rows in the reactions table
  (`on_halt`, fleet budget, remote latch, org policy dry-run, fleet circuit),
  and an "Alerting" section pointing delivery at the control plane's
  own docs, with the receiver-side `verify_webhook_signature` walkthrough kept
  here.
- README and the SDK's own docstrings lead with an autonomous agent: the quick
  start is a tool-using agent run, "Per-user sessions" is now "Runs keyed by
  any id" (a run id, a job, a tenant, a user), the spike ladder is "Many
  callers behind one service", and "end user"/"abuser" read as
  "caller"/"repeat-offender key". Both worked stories stay — a runaway agent
  and a chatbot free-rider. Wording only; no behaviour changed.

### Docs

Documentation only, no code changes:

- **A formal definition of "turn"** replaces the informal `workers × one
  in-flight turn` phrasing everywhere it appeared (`INVARIANTS.md`'s Budget
  section, README's fleet-budget paragraph): a turn is one
  `runbound.session()` block on one worker, and the fleet-budget bound is
  stated as a sum over workers of in-flight spend plus spend admitted while
  stale.
- README's stream-abandonment text now says plainly that exhausting, closing,
  or exiting the `with` block reports a stream immediately — garbage
  collection is the safety net for a forgotten stream, not the mechanism to
  rely on.
- README now opens with the problem runbound solves (runaway loops,
  uncontrolled spend, tool abuse, cascading provider failures, unstoppable
  fleet-wide execution) before the tagline.
- A "What it controls" table (README) groups every knob by area — cost,
  execution, authorization, reliability, governance — instead of by
  detector name.
- The provider-compatibility claim is narrowed: OpenAI and Anthropic are
  tested against the real SDKs; OpenAI-compatible servers are proven live
  only on Ollama, with vLLM/Groq/OpenRouter/Azure OpenAI/LM Studio sharing
  the code path untested; Gemini/Bedrock/Mistral/Cohere have no adapter.
- Quick start gets a fourth, numbered step — "Check what is actually
  guarded" — putting `runbound.coverage()` and `runbound.assert_guarded()`
  in the startup path, since an unguarded path looks exactly like a quiet
  one.

## [0.2.0] - 2026-09-01

### Changed

- Renamed the package to `runbound`; the earlier working name was retired.

### Added

- **LangChain / LangGraph support** — `GuardrailCallbackHandler` in
  `runbound.integrations.langchain` records framework-driven tool and model
  calls, so agents with no client to `wrap()` and no function to decorate are
  guarded too. Requires `langchain-core` (extra: `runbound[langchain]`).
- **Async clients** — `AsyncOpenAI` and `AsyncAnthropic` are guarded through
  the same `wrap()` call.
- **Streamed responses** — a guarded stream yields provider chunks unchanged
  and records exactly one model call when the stream ends. OpenAI reports
  stream usage only when the request sets `stream_options={"include_usage":
  True}`; a stream that is abandoned rather than exhausted or closed records
  nothing.
- Packaging metadata (authors, MIT license, classifiers, keywords, project
  URLs), a `LICENSE` file, this changelog, and a GitHub Actions CI matrix
  running the test suite on Python 3.10–3.13.

## [0.1.0] - 2026-09-01

### Added

- Core SDK: an in-process session that records every model call and tool call
  as an immutable event.
- Four deterministic detectors — `loop`, `budget`, `velocity`, `steps` — all
  plain counting over in-memory state, no model calls.
- `wrap()` for OpenAI- and Anthropic-shaped clients (recognized by shape, so
  Azure OpenAI, Ollama, vLLM, Groq, OpenRouter and other compatible endpoints
  work too), and the `@tool` decorator for tool functions.
- Reactions on anomaly: `warn`, `raise` (`GuardrailTripped` on the agent's own
  thread), or your own `callback`.
- Slack and PagerDuty alerting, fire-and-forget on a daemon thread.
- Cost estimation from a static list-price table, overridable via
  `custom_prices`; token limits for unpriced and local models.
- Fail-open throughout: every internal failure is logged and swallowed.
