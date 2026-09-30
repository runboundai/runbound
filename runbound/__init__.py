"""runbound — deterministic, LLM-free runaway detection for AI agents.

Three lines to guard an agent::

    import runbound

    runbound.init(budget_usd=5.0, max_steps=50, on_anomaly="raise")
    client = runbound.wrap(client)          # OpenAI- or Anthropic-shaped

    @runbound.tool                          # every tool call is recorded
    def search(query): ...

Fail-open by design: runbound's own bugs are logged and swallowed, and the
only exception it raises on purpose is :class:`GuardrailTripped`.
"""

from .alerts import verify_webhook_signature
from .api import (
    active_sessions,
    BudgetView,
    assert_guarded,
    budget,
    circuit_state,
    clear,
    coverage,
    current_session,
    decisions,
    envelope,
    enter_safe_mode,
    events,
    exit_safe_mode,
    fleet_status,
    inflight_calls,
    init,
    is_tripped,
    key_hash,
    llm,
    plane_status,
    posture,
    record_call,
    reset,
    safe_mode,
    session,
    session_status,
    tool,
    tool_calls,
    tools,
    unpatch,
    wrap,
)
from .config import GuardrailConfig
from .events import Anomaly, Decision, Event
from .exceptions import (
    CircuitOpen,
    ExecutionRefused,
    GuardrailTripped,
    PolicyViolation,
    SafeModeViolation,
    is_retryable,
)
from .plane_types import PlaneStatus
from .policy import ToolCall, ToolPolicy, Violation
from .responses import Refusal
from .state import PostureState, SessionState

__version__ = "0.7.0"

__all__ = [
    "Anomaly",
    "BudgetView",
    "CircuitOpen",
    "Decision",
    "Event",
    "ExecutionRefused",
    "GuardrailConfig",
    "GuardrailTripped",
    "PlaneStatus",
    "PolicyViolation",
    "Refusal",
    "PostureState",
    "SafeModeViolation",
    "SessionState",
    "ToolCall",
    "ToolPolicy",
    "Violation",
    "__version__",
    "active_sessions",
    "assert_guarded",
    "budget",
    "circuit_state",
    "clear",
    "coverage",
    "current_session",
    "decisions",
    "envelope",
    "enter_safe_mode",
    "events",
    "exit_safe_mode",
    "fleet_status",
    "inflight_calls",
    "init",
    "is_retryable",
    "is_tripped",
    "key_hash",
    "llm",
    "plane_status",
    "posture",
    "record_call",
    "reset",
    "safe_mode",
    "session",
    "session_status",
    "tool",
    "tool_calls",
    "tools",
    "unpatch",
    "verify_webhook_signature",
    "wrap",
]
