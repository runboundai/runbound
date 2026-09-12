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
    assert_guarded,
    circuit_state,
    clear,
    coverage,
    current_session,
    fleet_status,
    inflight_calls,
    init,
    is_tripped,
    key_hash,
    llm,
    plane_status,
    record_call,
    reset,
    session,
    session_status,
    tool,
    tool_calls,
    unpatch,
    wrap,
)
from .config import GuardrailConfig
from .events import Anomaly, Event
from .exceptions import CircuitOpen, GuardrailTripped, PolicyViolation
from .plane_types import PlaneStatus
from .policy import ToolCall, ToolPolicy, Violation
from .responses import Refusal
from .state import SessionState

__version__ = "0.3.0"

__all__ = [
    "Anomaly",
    "CircuitOpen",
    "Event",
    "GuardrailConfig",
    "GuardrailTripped",
    "PlaneStatus",
    "PolicyViolation",
    "Refusal",
    "SessionState",
    "ToolCall",
    "ToolPolicy",
    "Violation",
    "__version__",
    "active_sessions",
    "assert_guarded",
    "circuit_state",
    "clear",
    "coverage",
    "current_session",
    "fleet_status",
    "inflight_calls",
    "init",
    "is_tripped",
    "key_hash",
    "llm",
    "plane_status",
    "record_call",
    "reset",
    "session",
    "session_status",
    "tool",
    "tool_calls",
    "unpatch",
    "verify_webhook_signature",
    "wrap",
]
