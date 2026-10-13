"""Policy states and the outage rule (TPD §5.7, §12 items 7 and 10; R0 closes §11a items 3 and 6).

The SDK is always in one of three states, recorded on every decision:

* **fresh**: the plane served policy within ``validity.fresh_s``. Decisions are as served.
* **cached**: the plane has not been heard from lately but a **sealed cached policy** is within its validity
  window (``validity.cache_s``). It is enforced **in full, including its asks**; fail mode does not apply.
* **unavailable**: no policy, or the cached one has expired. Fail mode applies per tag; code restrictions
  still hold. Fail mode decides only for a tool the last sealed policy (even expired) would have **allowed**; one it
  denied, or required a human for, is refused ``policy_expired_restricted`` whatever the tag says (K7). With no cache
  at all, fail mode decides as written.

Plane unreachable does not by itself mean unavailable. A cache that expires during a run moves the *next* call
to unavailable (state is a function of ``now``, nothing remembers). **Mandatory approval is never bypassed by
fail-open:** a call whose code declares an approval, or whose cached policy asks, never executes without a
verifiable authorisation. A code predicate's refusal stands in every state.

``state`` and ``outage_outcome`` are pure: ``now`` is passed in, nothing is read from a clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

FRESH, CACHED, UNAVAILABLE = "fresh", "cached", "unavailable"

#: fail-mode values
OPEN, CLOSED = "open", "closed"

#: the tag a tool with no declared effect falls under (§5.5: it has its own default, and that default is closed)
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Validity:
    """How long a served policy stays fresh, and how long a sealed cached policy stays enforceable (seconds)."""

    fresh_s: float
    cache_s: float


@dataclass(frozen=True)
class Cache:
    """A sealed cached policy: when it was sealed, and what it decides for the tool (``allow|ask|deny``)."""

    sealed_at: float
    decision: str = "allow"


@dataclass(frozen=True)
class CodeRules:
    """What the code declares: a predicate that refused, a block, an approval."""

    constraint_refused: bool = False
    blocked: bool = False
    approval: bool = False


@dataclass(frozen=True)
class OutageOutcome:
    """``decision`` is allow | ask | deny | refuse; ``fail_mode_applied`` says whether fail mode decided it."""

    state: str
    decision: str
    reason: str
    fail_mode_applied: bool = False


def state(now: float, served_at: "float | None", cache: "Cache | None", validity: Validity) -> str:
    """fresh | cached | unavailable at ``now``. Boundaries are inclusive: exactly at the limit is still valid."""
    if served_at is not None and now - served_at <= validity.fresh_s:
        return FRESH
    if cache is not None and now - cache.sealed_at <= validity.cache_s:
        return CACHED
    return UNAVAILABLE


def fail_mode(effects: frozenset, fail_mode_by_tag: Mapping) -> str:
    """Closed if any applicable tag is closed (§5.4); a tag with no setting, and an unclassified tool, is closed.

    A tool whose effects are only ``read`` and ``write`` (a bare ``write``, with or without ``read``) carries no tag,
    so it is **unclassified** for fail mode (register K8, §5.5); ``{write, external}`` has a tag and follows it.
    """
    effects = frozenset(effects)
    bare_write = "write" in effects and not (effects - {"read", "write"})
    tags = frozenset({UNCLASSIFIED}) if (not effects or bare_write) else effects
    return CLOSED if any(fail_mode_by_tag.get(tag, CLOSED) != OPEN for tag in tags) else OPEN


def outage_outcome(
    current: str,
    effects: frozenset,
    fail_mode_by_tag: Mapping,
    code_rules: CodeRules,
    cached_policy: "Cache | None" = None,
) -> OutageOutcome:
    """What a call does in state ``current`` (§5.7). Code restrictions are checked first, in every state."""
    if code_rules.constraint_refused:
        return OutageOutcome(current, "deny", "code_constraint")
    if code_rules.blocked:
        return OutageOutcome(current, "deny", "code_blocked")
    if current == FRESH:
        return OutageOutcome(current, "evaluate", "served_policy")
    if current == CACHED:
        decision = cached_policy.decision if cached_policy is not None else "deny"  # a cache with no content decides nothing
        if code_rules.approval and decision == "allow":
            decision = "ask"  # the code's approval is a floor under the cached policy too
        reason = "cached_policy" if cached_policy is not None else "cached_policy_missing"
        return OutageOutcome(current, decision, reason)
    # unavailable: no policy to enforce, but the last sealed one (even expired) still bounds fail-open (register K7)
    if code_rules.approval:
        return OutageOutcome(current, "refuse", "approval_unverifiable")  # whatever fail mode says
    if cached_policy is not None and cached_policy.decision != "allow":
        return OutageOutcome(current, "refuse", "policy_expired_restricted")  # it denied the tool or needed a human
    if fail_mode(effects, fail_mode_by_tag) == CLOSED:
        return OutageOutcome(current, "refuse", "plane_unavailable", fail_mode_applied=True)
    return OutageOutcome(current, "allow", "fail_open", fail_mode_applied=True)
