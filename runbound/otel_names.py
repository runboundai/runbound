"""Every span, scope and attribute string the SDK emits, in one place.

The GenAI semantic conventions are still in Development and moved repositories in
2026-06, so the strings are pinned here rather than imported from
``opentelemetry.semconv`` (an ``_incubating`` module). Change a name here and nowhere else.
"""

SCOPE_NAME = "runbound"
SPAN_NAME = "execute_tool {tool}"
START_MARKER_NAME = "execution_started"  # AAI-1 FR-3: a zero-length child span ended just before the tool body runs

# --- the GenAI standard ---------------------------------------------------------------
OPERATION_NAME = "gen_ai.operation.name"
OPERATION_EXECUTE_TOOL = "execute_tool"
TOOL_NAME = "gen_ai.tool.name"
TOOL_TYPE = "gen_ai.tool.type"
TOOL_TYPE_FUNCTION = "function"
TOOL_CALL_ID = "gen_ai.tool.call.id"
ERROR_TYPE = "error.type"
ERROR_TYPE_REFUSED = "runbound.refused"

# --- ours ------------------------------------------------------------------------------
SERVICE = "runbound.service"
WORKER_ID = "runbound.worker_id"
RUN_ID = "runbound.run_id"
CAPABILITY = "runbound.capability"
# PS-AAI-03j: where the capability declaration came from ("decorator", "mcp_annotation", or "undeclared" when the tool states none), the classes a console row RAISED it to
# (only when a row did), and the deployment's own version (RUNBOUND_SERVICE_VERSION), the comparison context for cohorts of the same tool across releases.
CAPABILITY_SOURCE = "runbound.capability_source"
CAPABILITY_EFFECTIVE = "runbound.capability_effective"
SERVICE_VERSION = "runbound.service_version"
OUTCOME = "runbound.outcome"
DECISION_VERDICT = "runbound.decision.verdict"
DECISION_KIND = "runbound.decision.kind"
DECISION_BOUNDARY = "runbound.decision.boundary"
DECISION_LEVEL = "runbound.decision.level"
DECISION_DETECTOR = "runbound.decision.detector"
DECISION_POLICY_VERSION = "runbound.decision.policy_version"
DECISION_REASON = "runbound.decision.reason"
# The decision record (TPD §12 item 6; WIRE-TR2 §10g): why this call was decided as it was. ``reason`` stays the human sentence.
DECISION_REASON_CODE = "runbound.decision.reason_code"
DECISION_MATCHED_RULE_IDS = "runbound.decision.matched_rule_ids"
DECISION_SERVICE_DEFAULT_REVISION = "runbound.decision.service_default_revision"
DECISION_POLICY_STATE = "runbound.decision.policy_state"
DECISION_CONTRIBUTORS = "runbound.decision.contributors"
# VPC: the revision and configuration a call was decided under, and (R-C) what the revision being tried made of it.
DECISION_RULE_REVISION = "runbound.decision.rule_revision"
DECISION_CFG_HASH = "runbound.decision.cfg_hash"
WAITED_MS = "runbound.waited_ms"  # how long the call waited for the first policy confirmation after a restart (bounded by init(confirm_wait_s)); absent when it did not wait
DECISION_BOOT_ID = "runbound.decision.boot_id"  # which process decided the call (a restart is a new boot)
DECISION_CANDIDATE_OUTCOME = "runbound.decision.candidate_outcome"
DECISION_CANDIDATE_REVISION = "runbound.decision.candidate_revision"
DECISION_CANDIDATE_OF = "runbound.decision.candidate_of"
# VPC (RVPC-2): what the execution-entry re-check of an approved call read and who refused it, beside the record it was approved under (WIRE-VPC 12e).
DECISION_RECHECK_RULE_REVISION = "runbound.decision.recheck_rule_revision"
DECISION_RECHECK_CFG_HASH = "runbound.decision.recheck_cfg_hash"
DECISION_RECHECK_POLICY_VERSION = "runbound.decision.recheck_policy_version"
DECISION_RECHECK_SERVICE_DEFAULT_REVISION = "runbound.decision.recheck_service_default_revision"
DECISION_RECHECK_CONTRIBUTOR = "runbound.decision.recheck_contributor"
DECISION_RECHECK_REASON = "runbound.decision.recheck_reason"
SDK_VERSION = "runbound.sdk.version"
# The start marker carries the attempt's identity, so it survives the loss of the call span that otherwise carries it (Apollo, contract rule 9). The tool is NOT gen_ai.tool.name:
# ingest rollups key on that, and the marker is not a call.
MARKER_TOOL = "runbound.marker.tool"
DECLARED_TARGET = "runbound.declared_target"  # the boundary this marker says executes the call (the declared target); equal to runbound.boundary for the decorator
# AAI-1 D1: the execution-entry re-check of an approved call as an evaluation of its own (additive to the ``runbound.decision.recheck_*`` attributes of a refusal).
RECHECK_PREFIX = "runbound.recheck."
RECHECK_FIELDS = ("evaluation_id", "outcome", "reason", "rule_revision", "cfg_hash", "policy_version", "service_default_revision")
# AAI-1 FR-1: the identities of the call (separate action, attempt and evaluation), the boundary that saw it, and the version of this contract.
ACTION_ID = "runbound.action_id"
ATTEMPT_ID = "runbound.attempt_id"
EVALUATION_ID = "runbound.evaluation_id"
PARENT_ACTION_ID = "runbound.parent_action_id"
ANOMALY_ID = "runbound.anomaly_id"  # on a refused span: the id of the refusal anomaly it records (the anomaly names the span back by span_id / trace_id)
BOUNDARY = "runbound.boundary"
CONTRACT_VERSION = "runbound.contract_version"
APPROVAL_REQUEST_ID = "runbound.approval.request_id"
ARGS_DIGEST = "runbound.args_digest"
RESULT_BYTES = "runbound.result_bytes"
RESULT_FLAGS = "runbound.result.flags"
RESULT_FLAG_SCORE = "runbound.result.flag_score"

# --- resource ---------------------------------------------------------------------------
RESOURCE_SERVICE_NAME = "service.name"
RESOURCE_INSTANCE_ID = "service.instance.id"
RESOURCE_SDK_NAME = "telemetry.sdk.name"
