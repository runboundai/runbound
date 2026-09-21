"""The one exception runbound raises on purpose, and the refusal it carries.

``ExecutionRefused`` is the public name for what stops a call: a structured,
retry-aware dependency failure, never a fake success and never a provider
SDK's own exception. ``GuardrailTripped`` is the same class, under the name
every earlier handler already catches — see the module-level alias below.
"""

from collections.abc import Callable

from .events import Anomaly, Decision
from .plane_types import key_hash as _key_hash
from .policy import Violation
from .responses import Refusal, refusal_for

#: The closed set ``exc.reason`` draws from. Stable across releases: a
#: handler that switches on it (``if reason == "budget": ...``) must never
#: see a value this tuple does not list. New refusal sites map onto one of
#: these; none ever invents a new word.
REASONS = (
    "budget", "tokens", "steps", "time", "posture", "policy", "approval",
    "circuit", "concurrency", "blast_radius", "halt", "plane", "loop",
    "error_storm", "spike",
)

#: ``exc.reason`` values a bare retry can safely repeat without human
#: judgment: the dependency itself said "not right now", not "this is over
#: budget/against the rules/the run's own shape is wrong" — those never
#: change by only waiting and asking again.
_RETRYABLE_REASONS = frozenset({"circuit", "concurrency", "plane"})

#: A boundary is a coarser, evaluation-shaped label than a reason; where a
#: refusal carries one, it is the most direct signal of which reason applies.
_BOUNDARY_REASON = {
    "money": "budget",
    "tokens": "tokens",
    "steps": "steps",
    "time": "time",
    "posture": "posture",
    "capability": "policy",
    "blast_radius": "blast_radius",
    "circuit": "circuit",
    "concurrency": "concurrency",
    "policy": "policy",
    "halt": "halt",
    "plane": "plane",
}

#: The fallback when no boundary is on the Decision at all — an anomaly from
#: a post-call wall, or one built by hand.
_DETECTOR_REASON = {
    "circuit": "circuit",
    "halt": "halt",
    "plane": "plane",
    "loop": "loop",
    "error_storm": "error_storm",
    "spike": "spike",
    "steps": "steps",
    "events": "steps",  # nearest bucket: also a throughput/count ceiling
    "timeout": "time",
    "budget": "budget",
    "inflight": "concurrency",
    "fanout": "blast_radius",
    "policy": "policy",
    "safe_mode": "posture",
    "fleet": "plane",  # a latch relayed from another worker via the plane
}

#: ``Violation.rule`` values that name a reason on their own, independent of
#: any Decision boundary — checked first, since a policy violation is the
#: most specific signal available.
_VIOLATION_RULE_REASON = {
    "approval": "approval",
    "deny": "policy",
    "allow": "policy",
    "max_calls": "policy",
    "constraint": "policy",
}


def _infer_reason(
    anomaly: "Anomaly | None", decision: "Decision | None", violation: "Violation | None"
) -> str:
    """Which of :data:`REASONS` this refusal is, from what it actually carries.

    Checked in the order that is most likely to be right: the tool policy's
    own rule name (it knows the difference between "approval" and every other
    kind of policy refusal that a Decision's ``boundary="policy"`` cannot
    tell apart on its own), then the Decision's ``boundary`` (the door's own
    classification), then the anomaly's ``detector`` (a post-call wall trip
    carries no Decision at all). Never raises, and never returns a value
    outside :data:`REASONS`: an unrecognized detector falls back to
    ``"policy"`` rather than inventing a new word.
    """
    if violation is not None:
        rule = getattr(violation, "rule", None)
        if rule in _VIOLATION_RULE_REASON:
            return _VIOLATION_RULE_REASON[rule]
    boundary = getattr(decision, "boundary", None) if decision is not None else None
    if boundary in _BOUNDARY_REASON:
        return _BOUNDARY_REASON[boundary]
    detector = getattr(anomaly, "detector", None) if anomaly is not None else None
    return _DETECTOR_REASON.get(detector, "policy")


class ExecutionRefused(Exception):
    """The public name for the one exception runbound raises on purpose.

    A structured, retry-aware dependency failure — never a fake success,
    never a business decision made for the customer, and never something a
    generic retry loop turns into the storm runbound exists to stop. Every
    refusal exposes:

    - ``reason`` — a stable code from the closed set :data:`REASONS`.
    - ``boundary`` — the execution-envelope dimension involved, from
      ``exc.decision.boundary`` (``None`` for a post-call wall trip that
      carries no Decision).
    - ``decision`` — the full :class:`~runbound.events.Decision`, or
      ``None``.
    - ``retryable`` — whether blindly repeating the same call could ever
      succeed without a human or the application changing anything. ``False``
      for ``budget``, ``tokens``, ``steps``, ``time``, ``posture``,
      ``policy``, ``halt``, ``blast_radius``, ``approval``, ``loop``,
      ``error_storm`` and ``spike`` — every one of those is refused for a
      reason that only waiting and asking again cannot fix. ``True`` for
      ``circuit`` and ``concurrency`` (with a ``retry_after``) and ``plane``
      (a degraded link that may already have recovered by the next request).
    - ``retry_after`` — seconds until retrying could plausibly succeed, when
      known; ``None`` otherwise. :class:`CircuitOpen` overrides this with the
      breaker's own decaying snapshot.
    - ``provider_called`` — whether the provider was actually reached before
      this refusal. ``False`` for every admission (pre-call) refusal; ``True``
      only for a post-call budget crossing, where the call already happened
      and its result is withheld.
    - ``scope`` — ``{"level": ..., "key_hash": ...}``: which scope decided
      (``"session"``, ``"process"`` or ``"fleet"``) and a salted hash of the
      session's key, never the key itself.
    - ``refusal`` — the customer's own status and sentence (unchanged from
      earlier releases; see :mod:`runbound.responses`).

    This is the only exception runbound lets escape into host code; every
    other internal failure is logged and swallowed. ``str(exc)`` is the
    anomaly's message and the full anomaly stays reachable as ``exc.anomaly``.
    No refusal here ever subclasses a provider SDK's own exception: catching
    ``openai.APIError`` or its Anthropic equivalent never catches one of
    these, so a provider's own retry layer never sees a refusal as its own
    kind of failure to retry.
    """

    def __init__(self, anomaly: Anomaly) -> None:
        super().__init__(anomaly.message)
        self.anomaly = anomaly

    @property
    def decision(self) -> "Decision | None":
        """Why this call was refused, as a :class:`~runbound.events.Decision`.

        Resolved lazily from ``anomaly.details["decision"]``, via
        :meth:`Decision.from_dict` — no constructor change, so every existing
        caller of ``ExecutionRefused(anomaly)`` keeps working. ``None`` when
        the anomaly carries no decision at all: a refusal built by hand in a
        test, a post-call wall trip, or one raised by a release that predates
        this field.
        """
        details = self.anomaly.details if isinstance(self.anomaly.details, dict) else {}
        raw = details.get("decision")
        return None if raw is None else Decision.from_dict(raw)

    @property
    def boundary(self) -> "str | None":
        """The execution-envelope dimension this refusal hit, or ``None``."""
        decision = self.decision
        return None if decision is None else decision.boundary

    @property
    def reason(self) -> str:
        """A stable code from the closed set :data:`REASONS`. Never raises."""
        try:
            return _infer_reason(self.anomaly, self.decision, getattr(self, "violation", None))
        except Exception:
            return "policy"

    @property
    def retryable(self) -> bool:
        """Would blindly repeating this exact call ever plausibly succeed?

        The predicate :func:`is_retryable`/a retry loop's own ``should_retry``
        is meant to read — never guessed at by inspecting the message.
        """
        return self.reason in _RETRYABLE_REASONS

    @property
    def retry_after(self) -> "float | None":
        """Seconds until retrying could plausibly succeed, or ``None``.

        :class:`CircuitOpen` overrides this with the breaker's own decaying
        snapshot; this base implementation only has a number for
        ``concurrency`` (an in-flight slot has no real cooldown to report, so
        this is a heuristic poll interval, not a measured one) — every other
        reason, retryable or not, has nothing concrete to offer and reports
        ``None``.
        """
        if self.reason == "concurrency":
            return 1.0
        return None

    @property
    def provider_called(self) -> bool:
        """Was the provider actually reached before this refusal?

        ``False`` for every admission (pre-call) refusal. ``True`` only for a
        post-call budget crossing — the call already happened, its usage is
        known, and its result is withheld; the money was spent regardless of
        what the caller does next. Read from ``decision.evaluation``, which is
        where every refusal site states it, folded in by
        :func:`runbound.engine._stamp_decision`.
        """
        decision = self.decision
        if decision is None:
            return False
        evaluation = decision.evaluation if isinstance(decision.evaluation, dict) else {}
        return bool(evaluation.get("provider_called", False))

    @property
    def scope(self) -> dict:
        """Which scope decided, and a salted hash of the session's key.

        ``{"level": "session" | "process" | "fleet", "key_hash": str | None}``.
        ``key_hash`` is ``None`` for the default (unkeyed) session and
        whenever no session was in scope at all — never the raw key, the same
        salted digest :func:`runbound.key_hash` computes.
        """
        decision = self.decision
        level = decision.level if decision is not None else "session"
        details = self.anomaly.details if isinstance(self.anomaly.details, dict) else {}
        return {"level": level, "key_hash": details.get("key_hash")}

    @property
    def refusal(self) -> Refusal:
        """What the app should tell the caller: the customer's own words.

        Resolved lazily, on each access, from whatever plane profile and
        local ``GuardrailConfig.refusals`` are in effect *right now* — a
        profile the customer changes on the plane reaches an exception that
        was raised (and is still being handled) moments earlier, exactly as
        it reaches the next request. See :mod:`runbound.responses`.
        """
        return refusal_for(self.anomaly)


#: ``GuardrailTripped`` is not a separate class that happens to behave the
#: same — it *is* ``ExecutionRefused``, the identical object, under the name
#: every handler written before this release already catches
#: (``except GuardrailTripped:``). ``runbound.GuardrailTripped is
#: runbound.ExecutionRefused`` holds forever; there is no alias to keep in
#: sync because there is only one class.
GuardrailTripped = ExecutionRefused


def is_retryable(exc: BaseException) -> bool:
    """Is ``exc`` a refusal a retry loop may safely repeat?

    The predicate a retry loop should use in place of ``except Exception:
    retry`` — ``exc.retryable`` when ``exc`` is one of runbound's own
    refusals, ``False`` for anything else (an exception runbound never
    raised carries no opinion here; a caller's own retry policy still
    applies to those). Never raises.
    """
    try:
        return bool(getattr(exc, "retryable", False))
    except Exception:
        return False


class CircuitOpen(ExecutionRefused):
    """Raised instead of calling a provider whose circuit runbound opened.

    A subclass of :class:`ExecutionRefused` (``GuardrailTripped``), so a host
    that already catches that keeps working unchanged — the call simply fails
    fast instead of joining the retry storm. A host that wants to fall back to
    another provider catches this one and reads ``exc.provider`` (``"openai"``,
    ``"anthropic"``): runbound says *this one is down*, and the routing
    decision stays where it belongs, with the application.

    Raised before the provider is called, and only under
    ``on_provider_failure="open"``. ``exc.reason == "circuit"`` and
    ``exc.retryable`` is always ``True``.
    """

    def __init__(
        self,
        anomaly: Anomaly,
        provider: str | None = None,
        retry_snapshot: "tuple[float, Callable[[], float]] | None" = None,
    ) -> None:
        super().__init__(anomaly)
        if provider is None:
            details = anomaly.details if isinstance(anomaly.details, dict) else {}
            provider = details.get("provider")
        self.provider = provider
        # A snapshot of the breaker's own clock and how many seconds were
        # left on the cooldown at the moment this was raised — see
        # :meth:`~runbound.circuit.CircuitBreaker.retry_snapshot`, which
        # builds it, and :meth:`retry_after` below, which decays it.
        # Deliberately *not* a live lookup: this exception must keep
        # answering correctly on its own after runbound.init() or reset()
        # swaps the engine (and its circuit breaker) out from under it,
        # which reading api._ENGINE later would not.
        self._retry_remaining: float | None = None
        self._retry_clock: "Callable[[], float] | None" = None
        self._retry_reference: float | None = None
        if retry_snapshot is not None:
            try:
                remaining, clock = retry_snapshot
                self._retry_remaining = float(remaining)
                self._retry_clock = clock
                self._retry_reference = clock()
            except Exception:
                self._retry_remaining = None
                self._retry_clock = None
                self._retry_reference = None

    @property
    def retry_after(self) -> "float | None":
        """Seconds until this provider's circuit may next be probed.

        Computed from the snapshot taken when this was raised — how many
        seconds were left then, and a reference reading of the *same* clock
        the breaker used (``time.monotonic`` in production, a test's
        hand-moved fake) — never from a number frozen once and reused, and
        never from a live engine looked up later. That is what makes it both
        safe to read twice (a handler that logs it, retries, and logs it
        again a second later sees the cooldown actually count down, not the
        same stale number twice) and correct after a later ``runbound.
        init()``/``reset()`` swaps the engine and its breaker out from under
        it: this keeps counting down against the breaker it actually opened
        under, not whatever breaker happens to be live when asked.

        ``None`` when this exception was built with no snapshot at all (a
        bare ``CircuitOpen(anomaly)`` built by hand, as in a test) or on any
        failure reading the clock — never raises.
        """
        if self._retry_clock is None or self._retry_remaining is None:
            return None
        try:
            elapsed = self._retry_clock() - self._retry_reference
            return max(0.0, self._retry_remaining - elapsed)
        except Exception:
            return None


class PolicyViolation(ExecutionRefused):
    """Raised when a tool call breaks the customer's own action policy.

    A subclass of :class:`ExecutionRefused` (``GuardrailTripped``), so a host
    that already catches that keeps working unchanged, while a host that wants
    to tell "the agent tried something it may not do" apart from "the agent
    ran away" can catch this instead. The rule that was broken is on
    ``exc.violation``; ``str(exc)`` is still the anomaly's message.
    """

    def __init__(self, anomaly: Anomaly, violation: Violation) -> None:
        super().__init__(anomaly)
        self.violation = violation


class SafeModeViolation(PolicyViolation):
    """A tool refused because its session, the process or the fleet is in safe mode.

    A :class:`PolicyViolation` on purpose. The tool body did not run, the
    session is not stopped and nothing latches, which is exactly what a
    handler written for a refused action already assumes; a handler that must
    tell the two apart catches this first. ``exc.violation.rule`` is
    ``"safe_mode"`` and ``exc.anomaly.detector`` is ``"safe_mode"``.
    """
