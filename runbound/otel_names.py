"""Every span, scope and attribute string the SDK emits, in one place.

The GenAI semantic conventions are still in Development and moved repositories in
2026-06, so the strings are pinned here rather than imported from
``opentelemetry.semconv`` (an ``_incubating`` module). Change a name here and nowhere else.
"""

SCOPE_NAME = "runbound"
SPAN_NAME = "execute_tool {tool}"

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
SDK_VERSION = "runbound.sdk.version"
APPROVAL_REQUEST_ID = "runbound.approval.request_id"
ARGS_DIGEST = "runbound.args_digest"
RESULT_BYTES = "runbound.result_bytes"
RESULT_FLAGS = "runbound.result.flags"
RESULT_FLAG_SCORE = "runbound.result.flag_score"

# --- resource ---------------------------------------------------------------------------
RESOURCE_SERVICE_NAME = "service.name"
RESOURCE_INSTANCE_ID = "service.instance.id"
RESOURCE_SDK_NAME = "telemetry.sdk.name"
