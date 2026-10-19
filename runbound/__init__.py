"""runbound — a policy boundary for the tools an AI agent can call.

Three lines to guard an agent::

    import runbound

    runbound.init(tool_policy=runbound.ToolPolicy(...))

    @runbound.tool                          # every tool call is checked and recorded
    def search(query): ...

The public API is listed in ``API.md``. Fail-open by default: runbound's own
bugs are logged and swallowed, and the exceptions it raises on purpose are the
ones in :mod:`runbound.exceptions`.
"""

import sys as _sys

if _sys.version_info < (3, 10):  # before any import that needs 3.10 syntax, so the failure names the cause
    raise ImportError(
        f"runbound needs Python 3.10 or newer; this is Python {_sys.version_info[0]}.{_sys.version_info[1]}. "
        "Run the agent on Python 3.10+ (pip refuses the install on older ones for the same reason)."
    )

from .api import (  # noqa: E402
    assert_guarded,
    coverage,
    current_session,
    decisions,
    events,
    init,
    plane_status,
    reset,
    session,
    tool,
    tool_calls,
    tool_results,
    tools,
)
from .approvals import Injected, current_request
from .config import RunboundConfig, deprecated_alias as _deprecated_alias
from .events import Anomaly, Decision, Event
from .exceptions import (
    ExecutionRefused,
    GuardrailTripped,
    PolicyViolation,
    CapabilityRefused,
    is_retryable,
)
from .plane_types import PlaneStatus
from .policy import ToolCall, ToolPolicy, ToolRules, Violation
from .state import SessionState

__version__ = "1.0.0a5"

from ._check import check  # noqa: E402  (after __version__: the report prints it)


def __getattr__(name: str):
    """``GuardrailConfig`` is the deprecated former name of ``RunboundConfig`` (PEP 562; warns once)."""
    return _deprecated_alias(name, __name__, stacklevel=3)


__all__ = [
    "Anomaly",
    "Decision",
    "Event",
    "ExecutionRefused",
    "GuardrailTripped",
    "PlaneStatus",
    "PolicyViolation",
    "RunboundConfig",
    "CapabilityRefused",
    "SessionState",
    "ToolCall",
    "ToolPolicy",
    "ToolRules",
    "Violation",
    "__version__",
    "assert_guarded",
    "check",
    "current_request",
    "Injected",
    "coverage",
    "current_session",
    "decisions",
    "events",
    "init",
    "is_retryable",
    "plane_status",
    "reset",
    "session",
    "tool",
    "tool_calls",
    "tool_results",
    "tools",
]
