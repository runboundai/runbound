"""Admission — pure stage functions, numbers in and a :class:`Decision` out.

Every function here is plain arithmetic and comparison: no lock, no I/O, no
session object, no clock read. That is what makes them table-driven to test
without mocks (:mod:`tests.test_admission_stages`) and safe to call from
inside a lock section that already holds ``session.lock`` (:mod:`runbound.engine`
snapshots the numbers this module needs *once*, under the lock, then calls
these functions with the plain values it read).

Each stage answers one question about the :ref:`execution envelope
<CONTROLS-2.2>`'s boundaries and returns :data:`~runbound.events.ALLOW` — the
same shared instance, so the allow path (overwhelmingly the common case)
allocates nothing — or a fresh, denying :class:`~runbound.events.Decision`.
Nothing here raises, alerts, latches or reports to the fleet: that is
:mod:`runbound.engine`'s job, once it has a Decision in hand.

The boundary condition on every stage is the same shape: "exactly at the
limit still allows; one over denies" — the same strict ``>`` (or ``>=`` where
the quantity being compared is a *count already spent*, e.g. ``turns`` and
``executed``, so that the *next* one would be the first over) the wall
detectors in :mod:`runbound.detectors` use, so the envelope agrees with the
wall about exactly where the line is; see each function's own docstring for
which of the two applies.
"""

from .events import ALLOW, Decision


def circuit(allowed: bool, provider: str, state: str, cooldown_s: float) -> Decision:
    """Is ``provider``'s circuit letting this call out?

    ``allowed`` is :meth:`~runbound.engine.Engine.circuit_allows`'s own
    answer; this stage only shapes it into a :class:`Decision`.
    """
    if allowed:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="circuit",
        detector="circuit",
        reason=f"provider {provider!r} circuit is {state}",
        evaluation={"cooldown_s": cooldown_s},
    )


def unpriced(model_is_priced: bool, model: str | None) -> Decision:
    """Does ``on_unpriced_model="refuse"`` apply to a model with no known price?

    ``model_is_priced`` is whether :func:`runbound.pricing.price_for` found
    one; the caller has already checked ``on_unpriced_model == "refuse"``
    before calling this.
    """
    if model_is_priced:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="money",
        detector="budget",
        reason=f"model {model!r} has no known price",
        evaluation={"model": model},
    )


def steps(turns: int, max_steps: int | None) -> Decision:
    """Would the call about to be made be the ``(max_steps + 1)``\\ th step?

    ``turns`` is the session's model-turn count *before* this call. The wall
    (:class:`~runbound.detectors.StepDetector`) fires once ``turns`` has
    already reached ``max_steps + 1`` — i.e. once the offending call has
    already happened. The door must catch that same call *before* it goes
    out, so its condition is one step earlier in the count: ``turns >=
    max_steps`` means the call about to be made would be turn
    ``turns + 1``, which is already over. ``evaluation["used"]`` reports that
    number — the one the wall would have reported — not the current
    ``turns``, so a caller reading either the wall's anomaly or the door's
    seed sees the same figure.
    """
    if max_steps is None or turns < max_steps:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="steps",
        detector="steps",
        reason=(
            f"step limit reached: the next call would be step {turns + 1}, "
            f"limit {max_steps}"
        ),
        evaluation={"limit": max_steps, "used": turns + 1},
    )


def run_time(elapsed_s: float, max_seconds: float | None) -> Decision:
    """Has the run already run past its wall-clock limit?

    ``elapsed_s`` is measured the moment this call is about to be made, so a
    session already past the limit is refused before spending any more of it
    — the same strict ``>`` :class:`~runbound.detectors.TimeoutDetector` uses,
    checked one call earlier than the wall (which only sees the elapsed time
    *after* a call returns).
    """
    if max_seconds is None or elapsed_s <= max_seconds:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="time",
        detector="timeout",
        reason=f"run time limit reached: {elapsed_s:.0f}s elapsed, limit {max_seconds:.0f}s",
        evaluation={"limit": max_seconds, "used": elapsed_s},
    )


def tokens(total_tokens: int, stated_cap: int | None, max_total_tokens: int | None) -> Decision:
    """Would this call's stated output cap push the token wall over?

    Mirrors the money reservation's own shape: only a request
    that *states* its own output cap can be checked exactly, so an uncapped
    call is not checked here at all (``ALLOW`` — the post-call wall is the
    only check for it, unchanged). ``total_tokens`` already includes any
    fleet offset the caller folded in.
    """
    if max_total_tokens is None or stated_cap is None:
        return ALLOW
    projected = total_tokens + stated_cap
    if projected <= max_total_tokens:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="tokens",
        detector="budget",
        reason=(
            f"token limit would be crossed: a {stated_cap}-token cap would bring the "
            f"total to {projected}, limit {max_total_tokens}"
        ),
        evaluation={"limit": max_total_tokens, "used": total_tokens, "estimate": stated_cap},
    )


def money(
    estimate: float,
    remaining: float,
    *,
    limit: float | None = None,
    reserved: float | None = None,
    level: str = "session",
) -> Decision:
    """Would this call's estimated worst case cross the dollar budget?

    ``remaining`` already has any hold in flight and any fleet spend
    subtracted (the caller's job — this stage is pure arithmetic on the
    numbers it is handed). ``limit`` (the budget itself) and ``reserved``
    (what other in-flight calls on this worker already hold) are optional
    and, when given, folded into ``evaluation`` alongside ``remaining`` and
    ``estimate`` — a refusal is understandable six hours later without
    re-deriving them from mutable state, so every number that went into the
    comparison belongs on the Decision, not only in the anomaly's own
    ``details``. Omitted (not ``0.0``) when the caller has nothing to state,
    so an old caller passing only the first two positional arguments keeps
    exactly the two-key ``evaluation`` it always had.

    ``level`` names which scope actually decided — ``"run"`` when a
    per-session()-entry ``run_budget_usd`` was the tighter of the two,
    ``"key"`` when the key's own (possibly windowed) ``budget_usd`` was, or
    the default ``"session"`` for a caller with no run/key distinction to
    make (every pre-existing caller).
    """
    if estimate <= remaining:
        return ALLOW
    evaluation = {"remaining": remaining, "estimate": estimate}
    if limit is not None:
        evaluation["limit"] = limit
    if reserved is not None:
        evaluation["reserved"] = reserved
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="money",
        level=level,
        detector="budget",
        reason=f"estimated ${estimate:.4f} would exceed ${remaining:.4f} remaining",
        evaluation=evaluation,
    )


def actions(executed: int, max_actions_per_run: int | None) -> Decision:
    """Would the action about to run be the ``(max_actions_per_run + 1)``\\ th *executed* one?

    ``executed`` is this session's ``executed_actions`` count *before* the
    attempt being judged — an admission refusal must not itself count as
    having executed, so the attempt in front of this stage is not yet
    reflected in the number handed in. The comparison mirrors ``steps``
    above: ``executed >= max_actions_per_run`` means this attempt, if
    admitted, would be the one that pushes the count over the limit.
    """
    if max_actions_per_run is None or executed < max_actions_per_run:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="action",
        boundary="blast_radius",
        detector="fanout",
        reason=(
            f"action limit reached: {executed} actions already executed this run, "
            f"limit {max_actions_per_run}"
        ),
        evaluation={"limit": max_actions_per_run, "used": executed + 1},
    )


def stopped(is_stopped: bool, posture_name: str, source: str, reason: str) -> Decision:
    """The very first stage a model call passes: is the effective posture ``stopped``?

    Unlike every other posture, ``stopped`` says "the run is latched; nothing
    runs" (the posture table above) rather than denying a set of
    capability classes — so it is the one posture a *model call* itself must
    also be refused for, not only a decorated tool's classes. ``is_stopped``
    is the caller's own answer (``effective_posture(session).name ==
    "stopped"``); this stage only shapes it into a :class:`Decision`.
    """
    if not is_stopped:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="model_call",
        boundary="posture",
        detector="safe_mode",
        reason=reason,
        evaluation={"posture": posture_name, "source": source},
    )


def posture(verdict: str, denied_class: str, posture_name: str, source: str, reason: str) -> Decision:
    """What the effective posture says about a tool carrying some capability class.

    ``verdict`` is :meth:`~runbound.posture.Posture.allows`'s own answer
    (``"allow"``, ``"deny"`` or ``"approve"``); an ``"approve"`` with no
    approval queue yet still refuses, reported as ``"restrict"`` — the
    class is not banned, only unreachable right now.
    """
    if verdict == "allow":
        return ALLOW
    return Decision(
        verdict="restrict" if verdict == "approve" else "deny",
        kind="action",
        boundary="posture",
        detector="safe_mode",
        reason=reason,
        evaluation={"posture": posture_name, "denied_class": denied_class, "source": source},
    )


def capability(verdict: str, denied_class: str) -> Decision:
    """What an ``init(capabilities=...)`` class rule says, independent of posture."""
    if verdict == "allow":
        return ALLOW
    return Decision(
        verdict="restrict" if verdict == "approve" else "deny",
        kind="action",
        boundary="capability",
        detector="safe_mode",
        reason=f"a capability class rule on init() denies {denied_class}",
        evaluation={"denied_class": denied_class},
    )
