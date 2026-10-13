"""§4b steps 1-6 as data in, decision out (R0; closes TPD §11a item 4).

``evaluate(ceiling, rules, fallback, limits)`` returns an :class:`Outcome`. Pure: no I/O, no clock, no
globals; the same inputs give the same outcome, always.

Order (the result does not depend on where a restriction came from):

1. **Code ceiling**: code ``blocked`` -> deny; a code ``constraint=`` that refused -> deny, whatever 2-3 said;
   a code-declared approval is a floor of ``ask`` that nothing below may lower. The tool's declared *effects*
   select which defaults apply (the caller passes the resulting ``fallback``); they are never a ceiling.
2. **Service fallback**: the default decision, used only when no rule matches.
3. **Rules**: each rule's block (``when`` ALL OF ``NOT unless``) is true, false or unknown. The baseline is
   the most restrictive of the ceiling floor and the matching rules (the fallback when none match). Register
   K1: refuse with ``condition_unevaluable`` iff the baseline is not ``deny`` and some *unknown* rule decides
   ``ask`` or ``deny``, or carries an exhausted limit. An unknown ``allow`` with no exhausted limit never
   refuses. (K1 measures "more restrictive" on the outcome, decision AND approval-requirement set.)
4. **Limits**: the matching rules' limits, the code ``max_calls`` and any service limit (``limits``): one
   exhausted turns ``allow`` or ``ask`` into ``deny`` with reason ``limit``. This is part of the baseline, so
   an exhausted limit denies before an unknown rule can make the call ``condition_unevaluable``.
5. Approval requirements are built from the outcome by :mod:`.approvals`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .conditions import block

DECISIONS = ("allow", "ask", "deny")
RANK = {name: index for index, name in enumerate(DECISIONS)}

#: the reasons an :class:`Outcome` carries as ``primary_reason``
CODE_BLOCKED = "code_blocked"
CODE_CONSTRAINT = "code_constraint"
CODE_APPROVAL = "code_approval"
RULE = "rule"
FALLBACK = "fallback"
LIMIT = "limit"
CONDITION_UNEVALUABLE = "condition_unevaluable"


@dataclass(frozen=True)
class Ceiling:
    """What the code declares for this call. ``effects`` is classification only: it never changes the result."""

    blocked: bool = False
    approval: bool = False
    constraint_refused: bool = False
    effects: frozenset = frozenset()


@dataclass(frozen=True)
class LimitState:
    """A counted limit's state for this call: ``exhausted`` or not, and where it came from."""

    source: str
    exhausted: bool = False


@dataclass(frozen=True)
class Rule:
    id: str
    decision: str
    when: Any = None
    unless: Any = None
    limit: "LimitState | None" = None


@dataclass(frozen=True)
class Contributor:
    """One restriction that took part: ``kind`` is code_blocked, code_constraint, code_approval, rule,
    unknown_rule, limit or fallback; ``ref`` names it; ``effect`` is the decision it pushed toward."""

    kind: str
    ref: str
    effect: str


@dataclass(frozen=True)
class Outcome:
    decision: str  # allow | ask | deny | refuse
    primary_reason: str
    matched_rule_ids: tuple = ()
    unknown_rule_ids: tuple = ()
    contributors: tuple = field(default=())


def _check(rules: Sequence[Rule], fallback: str) -> None:
    if fallback not in RANK:
        raise ValueError(f"the fallback is allow, ask or deny, not {fallback!r}")
    seen = set()
    for rule in rules:
        if rule.decision not in RANK:
            raise ValueError(f"rule {rule.id!r}: the decision is allow, ask or deny, not {rule.decision!r}")
        if rule.id in seen:
            raise ValueError(f"rule id {rule.id!r} appears twice")
        seen.add(rule.id)


def evaluate(
    ceiling: Ceiling,
    rules: Sequence[Rule],
    fallback: str,
    limits: Sequence[LimitState] = (),
    *,
    args: Any = None,
    context: Any = None,
) -> Outcome:
    """The decision for one call. Raises ``ValueError`` on a malformed rule set (never resolves to allow)."""
    _check(rules, fallback)
    states = [(rule, block(rule.when, rule.unless, args, context)) for rule in rules]
    matched = [rule for rule, value in states if value is True]
    unknown = [rule for rule, value in states if value is None]
    matched_ids = tuple(rule.id for rule in matched)
    unknown_ids = tuple(rule.id for rule in unknown)

    contributors: list = []
    if ceiling.blocked:
        contributors.append(Contributor(CODE_BLOCKED, "code", "deny"))
    if ceiling.constraint_refused:
        contributors.append(Contributor(CODE_CONSTRAINT, "code", "deny"))
    if ceiling.approval:
        contributors.append(Contributor(CODE_APPROVAL, "code", "ask"))
    contributors.extend(Contributor("rule", rule.id, rule.decision) for rule in matched)
    contributors.extend(Contributor("unknown_rule", rule.id, rule.decision) for rule in unknown)
    if not matched:
        contributors.append(Contributor(FALLBACK, "service", fallback))

    exhausted = [limit for limit in limits if limit.exhausted]
    exhausted += [rule.limit for rule in matched if rule.limit is not None and rule.limit.exhausted]
    contributors.extend(Contributor(LIMIT, limit.source, "deny") for limit in exhausted)

    def done(decision: str, reason: str) -> Outcome:
        return Outcome(decision, reason, matched_ids, unknown_ids, tuple(contributors))

    if ceiling.constraint_refused:  # step 4 of the TPD list: a code predicate's refusal is final
        return done("deny", CODE_CONSTRAINT)
    if ceiling.blocked:
        return done("deny", CODE_BLOCKED)

    # baseline: the most restrictive of the ceiling floor and the matching rules (the fallback when none match)
    candidates = []
    if matched:
        candidates.extend((RANK[rule.decision], 3, RULE) for rule in matched)
    else:
        candidates.append((RANK[fallback], 1, FALLBACK))
    if ceiling.approval:
        candidates.append((RANK["ask"], 2, CODE_APPROVAL))
    top_rank, _tie, reason = max(candidates)
    baseline = DECISIONS[top_rank]

    if baseline != "deny":
        if exhausted:  # the baseline is already a refusal: nothing unknown can make it stricter
            return done("deny", LIMIT)
        could_tighten = any(rule.decision != "allow" for rule in unknown)
        exhausted_unknown = any(rule.limit is not None and rule.limit.exhausted for rule in unknown)
        if could_tighten or exhausted_unknown:
            return done("refuse", CONDITION_UNEVALUABLE)
    return done(baseline, reason)
