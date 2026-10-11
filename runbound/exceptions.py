"""The one exception runbound raises on purpose.

``ExecutionRefused`` is the public name for what stops a call: a structured,
retry-aware refusal, never a fake success. ``GuardrailTripped`` is the same
class, under the name every earlier handler already catches.
"""

from .events import Anomaly, Decision
from .policy import Violation

#: The closed set ``exc.reason`` draws from. Stable across releases: a
#: handler that switches on it must never see a value this tuple does not list.
REASONS = (
    "policy", "approval", "capability", "blast_radius", "halt", "plane",
    "invalid_arguments", "hook_refused", "hook_error",
    "approval_denied", "approval_timeout", "approval_tampered", "approval_unavailable", "quota_exceeded", "plane_unavailable",
    "tool_blocked", "session_max_calls", "quota_plane_unavailable",
)

#: ``exc.reason`` values a bare retry can safely repeat: the dependency said
#: "not right now", not "this is against the rules".
_RETRYABLE_REASONS = frozenset({"plane", "plane_unavailable", "quota_exceeded", "quota_plane_unavailable"})

#: A Decision's ``boundary`` is the most direct signal of which reason applies.
_BOUNDARY_REASON = {
    "capability": "capability",
    "blast_radius": "blast_radius",
    "policy": "policy",
    "halt": "halt",
    "plane": "plane",
    "arguments": "invalid_arguments",
    "hook": "hook_refused",
    "hook_error": "hook_error",
}

#: The fallback when no boundary is on the Decision at all.
_DETECTOR_REASON = {
    "halt": "halt",
    "plane": "plane",
    "fanout": "blast_radius",
    "policy": "policy",
    "capability": "capability",
    "validation": "invalid_arguments",
    "hook": "hook_refused",
}

#: ``Violation.rule`` values that name a reason on their own — checked first,
#: since a policy violation is the most specific signal available.
_VIOLATION_RULE_REASON = {
    "approval": "approval",  # the policy's require_approval rule (local callback)
    "deny": "tool_blocked",  # blocked=True, a deny list entry, or an org policy's deny: one reason for "this tool is off"
    "allow": "policy",
    "max_calls": "policy",
    "constraint": "policy",
}


def _infer_reason(
    anomaly: "Anomaly | None", decision: "Decision | None", violation: "Violation | None"
) -> str:
    """Which of :data:`REASONS` this refusal is, from what it actually carries.

    The tool policy's own rule name first (it can tell "approval" from every
    other policy refusal), then the Decision's ``boundary``, then the anomaly's
    ``detector``. Never raises, never returns a value outside :data:`REASONS`.
    """
    if violation is not None:
        rule = getattr(violation, "rule", None)
        if rule in _VIOLATION_RULE_REASON:
            return _VIOLATION_RULE_REASON[rule]
    boundary = getattr(decision, "boundary", None) if decision is not None else None
    if boundary == "plane":
        return "plane_unavailable" if (getattr(decision, "evaluation", None) or {}).get("code") == "plane_unavailable" else "plane"
    if boundary in ("approval", "row"):  # the Decision names which way it ended
        code = (getattr(decision, "evaluation", None) or {}).get("code")
        return code if code in REASONS else "approval_unavailable"
    if boundary in _BOUNDARY_REASON:
        return _BOUNDARY_REASON[boundary]
    detector = getattr(anomaly, "detector", None) if anomaly is not None else None
    return _DETECTOR_REASON.get(detector, "policy")


class ExecutionRefused(Exception):
    """The public name for the one exception runbound raises on purpose.

    Every refusal exposes:

    - ``reason`` — a stable code from the closed set :data:`REASONS`.
    - ``boundary`` — the dimension involved, from ``exc.decision.boundary``.
    - ``decision`` — the full :class:`~runbound.events.Decision`, or ``None``.
    - ``retryable`` — whether blindly repeating the same call could ever
      succeed without a human or the application changing anything.
    - ``retry_after`` — seconds until retrying could plausibly succeed, when
      known; ``None`` otherwise.
    - ``scope`` — ``{"level": ..., "key_hash": ...}``: which scope decided and
      a sha256 hash of the session's key, never the key itself.

    ``str(exc)`` is the anomaly's message and the full anomaly stays reachable
    as ``exc.anomaly``. No refusal ever subclasses a provider SDK's exception.
    """

    def __init__(self, anomaly: Anomaly) -> None:
        super().__init__(anomaly.message)
        self.anomaly = anomaly

    @property
    def decision(self) -> "Decision | None":
        """Why this call was refused, resolved from ``anomaly.details["decision"]``."""
        details = self.anomaly.details if isinstance(self.anomaly.details, dict) else {}
        raw = details.get("decision")
        return None if raw is None else Decision.from_dict(raw)

    @property
    def boundary(self) -> "str | None":
        """The dimension this refusal hit, or ``None``."""
        decision = self.decision
        return None if decision is None else decision.boundary

    @property
    def reason(self) -> str:
        """A stable code from :data:`REASONS`. Never raises."""
        try:
            return _infer_reason(self.anomaly, self.decision, getattr(self, "violation", None))
        except Exception:
            return "policy"

    @property
    def retryable(self) -> bool:
        """Would blindly repeating this exact call ever plausibly succeed?"""
        return self.reason in _RETRYABLE_REASONS

    @property
    def retry_after(self) -> "float | None":
        """Seconds until retrying could plausibly succeed (a quota's window), or ``None``."""
        decision = self.decision
        value = (getattr(decision, "evaluation", None) or {}).get("retry_after_s")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def scope(self) -> dict:
        """Which scope decided, and a sha256 hash of the session's key."""
        decision = self.decision
        level = decision.level if decision is not None else "session"
        details = self.anomaly.details if isinstance(self.anomaly.details, dict) else {}
        return {"level": level, "key_hash": details.get("key_hash")}


#: ``GuardrailTripped`` *is* ``ExecutionRefused``, the identical object, under
#: the name every handler written before already catches.
GuardrailTripped = ExecutionRefused


def is_retryable(exc: BaseException) -> bool:
    """Is ``exc`` a refusal a retry loop may safely repeat?

    ``exc.retryable`` for one of runbound's own refusals, ``False`` for
    anything else. Never raises.
    """
    try:
        return bool(getattr(exc, "retryable", False))
    except Exception:
        return False


class PolicyViolation(ExecutionRefused):
    """Raised when a tool call breaks the customer's own action policy.

    A host that already catches :class:`ExecutionRefused` keeps working; a host
    that wants to tell "the agent tried something it may not do" apart catches
    this. The rule that was broken is on ``exc.violation``.
    """

    def __init__(self, anomaly: Anomaly, violation: Violation) -> None:
        super().__init__(anomaly)
        self.violation = violation


class CapabilityRefused(PolicyViolation):
    """A tool refused because a capability class rule denies one of its classes.

    A :class:`PolicyViolation` on purpose: the tool body did not run, the
    session is not stopped and nothing latches. ``exc.violation.rule`` is
    ``"capability"`` and ``exc.anomaly.detector`` is ``"capability"``.
    """
