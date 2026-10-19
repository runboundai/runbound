"""Immutable event records.

Events are frozen: once the ``@tool`` decorator (or a framework adapter) creates
one, nothing downstream can mutate it.
"""

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(frozen=True)
class Event:
    """A single observed action inside an agent session.

    ``ts`` is monotonic time, so events are safe to compare across a clock
    change. ``args_hash`` is a salted sha256 hex digest of canonicalized
    arguments; raw arguments are never stored.

    The kinds: ``tool_call`` (a decorated tool is about to run: ``tool_name``,
    ``args_hash``), ``tool_error`` (that tool raised: ``tool_name``, ``error``),
    ``tool_request`` (a tool call a model asked for, recorded before it is
    dispatched; its hash carries a ``"req:"`` prefix) and ``tool_result`` (the call
    ended: ``duration_s``, ``outcome``, and what the post chain did — ``flags``,
    ``tags``, ``truncated``, sizes; never the result). ``retryable`` carries
    ``@runbound.tool(retryable=True)`` onto a ``tool_error`` event. The token,
    cost and model fields are the wire contract with the control plane
    (:class:`~runbound.plane_types.WireEvent`) and stay at their defaults here.
    """

    kind: str  # "tool_call" | "tool_error" | "tool_request" | "tool_result"
    ts: float  # time.monotonic() at creation
    step: int  # 1-based step number within session
    tokens_in: int = 0
    tokens_cached_in: int = 0  # subset of tokens_in served from the provider's cache
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    tool_name: str | None = None
    args_hash: str | None = None  # sha256 hex of canonicalized args
    error: str | None = None
    duration_s: float = 0.0  # wall time the call took, 0.0 when unmeasured
    tokens_reasoning: int = 0  # thinking tokens, a subset of tokens_out
    loop_exempt: bool = False  # counts for tool_calls()/max_calls, not the loop window
    retryable: bool = False  # @runbound.tool(retryable=True): a grace on the "retry" shape
    priced: str | None = None  # "estimated" when cost_usd came from the unpriced fallback
    partial: bool = False  # an llm_call reported by an abandoned-stream finalizer
    tokens_estimated: bool = False  # a partial call's tokens_out is chars/4, not real usage
    provider: str | None = None  # the endpoint label, "openai@api.openai.com"; model calls only
    # --- tool_result only: what the tool returned, never the result itself ---
    outcome: str | None = None  # "ok" | "error" | "refused" | "pending_approval" | "withheld" | "cancelled"
    flags: tuple = ()  # scan pattern names that matched (never the matched text)
    flag_score: float = 0.0  # the summed weights of those patterns
    flagged: bool = False  # flag_score >= 1.0
    tags: tuple = ()  # e.g. "hook_error:<name>", "scan_failed"
    truncated: bool = False  # the post chain cut the result
    result_chars: int = 0  # serialized size before the post chain
    result_chars_out: int = 0  # ... and after


@dataclass(frozen=True)
class Anomaly:
    """A verdict that something was refused, or would have been.

    ``message`` is human-readable and includes the numbers behind it;
    ``details`` carries the same numbers machine-readably (never call arguments).

    ``anomaly_id`` is this verdict's own identity, stamped once and never again:
    one anomaly reaches the plane through the exporter, and the id is what lets
    the plane recognise two reports of it as one fact. A fresh random hex string
    per anomaly, derived from nothing about the session, the key or the customer.
    """

    detector: str  # "policy" | "capability" | "fanout" | "halt"
    severity: str  # "warn" | "critical"
    message: str  # human-readable, includes numbers
    details: dict
    # Last, and defaulted, so every positional construction keeps working.
    anomaly_id: str = field(default_factory=lambda: uuid4().hex)


#: The verdicts a :class:`Decision` may carry. ``"restrict"`` is a capability
#: class rule stating ``"approve"`` with no approval queue yet: it refuses, but
#: says "restrict", not "deny", because the class is not banned, only unreachable.
DECISION_VERDICTS = ("allow", "deny", "restrict")

#: What kind of thing a :class:`Decision` is about. Only ``"action"`` is made
#: today; the others stay valid so a Decision relayed by another release decodes.
DECISION_KINDS = ("model_call", "action", "entry")

#: Which scope decided: a ``"session"`` (the common case), the ``"process"`` (an
#: ``init(capabilities=...)`` rule) or the ``"fleet"`` (a halt or narrowing from
#: the control plane). ``"run"`` and ``"key"`` stay valid for decoding only.
DECISION_LEVELS = ("session", "process", "fleet", "run", "key")


@dataclass(frozen=True)
class Decision:
    """What runbound decided about one request, and why.

    ``verdict`` is the answer; ``kind`` says what was judged; ``boundary`` names
    the dimension involved (``"policy"``, ``"capability"``, ``"blast_radius"``,
    ``"halt"``, or ``None`` for an ordinary allow); ``level`` is which scope
    decided; ``reason`` is the human sentence; ``detector`` is the same string an
    :class:`Anomaly` carries, so a refusal's Decision and its anomaly always agree
    on who is speaking; ``policy_version`` is the org policy version in force, when
    there is one; ``evaluation`` is the numbers behind the verdict (``limit``,
    ``used``, ``count`` — whichever apply), never call arguments or content.

    ``as_dict``/``from_dict`` round-trip this through ``Anomaly.details["decision"]``,
    a plain JSON-able dict, and back — tolerantly: :meth:`from_dict` never
    raises, so a Decision relayed by a different release (the control plane,
    an older or newer SDK) degrades to sensible defaults instead of breaking
    ``exc.decision``.
    """

    verdict: str = "allow"
    kind: str = "model_call"
    boundary: str | None = None
    level: str = "session"
    reason: str | None = None
    detector: str | None = None
    policy_version: int | None = None
    evaluation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """A plain, JSON-able copy — what travels in ``Anomaly.details["decision"]``."""
        return {
            "verdict": self.verdict,
            "kind": self.kind,
            "boundary": self.boundary,
            "level": self.level,
            "reason": self.reason,
            "detector": self.detector,
            "policy_version": self.policy_version,
            "evaluation": dict(self.evaluation) if isinstance(self.evaluation, dict) else {},
        }

    @staticmethod
    def from_dict(data: object) -> "Decision":
        """The :class:`Decision` ``data`` describes, tolerating anything.

        Never raises: a value of the wrong shape or type for any field falls
        back to that field's own default, exactly as if it had been omitted.
        Not a dict at all yields the all-defaults ``Decision`` (equal to
        :data:`ALLOW`).
        """
        if not isinstance(data, dict):
            return Decision()
        policy_version = data.get("policy_version")
        if isinstance(policy_version, bool) or not isinstance(policy_version, int):
            policy_version = None
        evaluation = data.get("evaluation")
        evaluation = dict(evaluation) if isinstance(evaluation, dict) else {}
        return Decision(
            verdict=_choice(data.get("verdict"), DECISION_VERDICTS, "allow"),
            kind=_choice(data.get("kind"), DECISION_KINDS, "model_call"),
            boundary=_optional_str(data.get("boundary")),
            level=_choice(data.get("level"), DECISION_LEVELS, "session"),
            reason=_optional_str(data.get("reason")),
            detector=_optional_str(data.get("detector")),
            policy_version=policy_version,
            evaluation=evaluation,
        )


def _choice(value: object, allowed: tuple, default: str) -> str:
    """``value`` when it is one of ``allowed``, else ``default``."""
    return value if isinstance(value, str) and value in allowed else default


def _optional_str(value: object) -> str | None:
    """``value`` when it is a string, else ``None``."""
    return value if isinstance(value, str) else None


#: The allow verdict every admission stage returns when nothing is denied.
#: A module-level singleton so the (overwhelmingly common) allow path
#: allocates nothing — every stage function returns this same object rather
#: than building a fresh ``Decision(verdict="allow", ...)`` each time.
ALLOW = Decision()
