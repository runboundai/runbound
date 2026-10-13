"""Decision lines: the one place the SDK's call path meets the pure contract in ``runbound._rules`` (TR2-O2).

A served row states its rule either as lines (a worker that reported the ``lines`` feature gets them) or only in the legacy fields
(``blocked``, ``approval_required`` ...), from which the lines are derived exactly as the plane derives them on read (WIRE-TR2 §5). Either
way the rule is decided here, by :func:`runbound._rules.evaluator.evaluate` (TPD §4b), and the answer is folded into the
:class:`runbound.policy_rows.Row` the call path already reads. R2 lines carry no conditions: each is unconditional, so a line always
matches and the strictest decision applies (deny > ask > allow); the limits of every line apply (the smallest cap, the smallest quota);
``otherwise`` decides only when a row has no lines.

This module and ``runbound.shared`` (policy states) are the only importers of ``runbound._rules``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._rules import approvals as approvals_rules
from ._rules import evaluator, states
from ._rules.conditions import Cond, Group
from ._rules.evaluator import Ceiling, Rule, evaluate

#: The R2 policy features this SDK enforces; it reports exactly these on every hello. ``defaults_v1`` joins them in TR2-O3.
FEATURES = ("lines", "halt_tools_v1", "defaults_v1", "conditions_v1", "repetition_v1", "grants_v1")

DECISIONS = evaluator.DECISIONS
LINE_KEYS = frozenset({"id", "decision", "approval", "limits", "when", "unless", "repetition"})
MAX_LINES = 12
MAX_GROUP_ITEMS = 20
APPROVAL_KEYS = frozenset({"approval_mode", "approval_timeout_s", "approvers"})
LIMIT_KEYS = frozenset({"max_calls_per_session", "quota"})
OTHERWISE = ("inherit", "allow", "ask", "deny")

#: The ``reason`` codes a worker reports in ``policy_unsupported`` on the hello.
ROW_UNPARSEABLE = "row_unparseable"
UNKNOWN_RESTRICTION_KEY = "unknown_restriction_key"
INVALID_BODY = "invalid_body"
LEGACY_VIEW_MISMATCH = "legacy_view_mismatch"

State = states  # the policy-state model; ``RemoteState.policy_state`` reads it through here


class LineError(ValueError):
    """A served line the SDK cannot read; ``code`` is the reason reported on the hello."""

    def __init__(self, code: str, text: str) -> None:
        self.code = code
        super().__init__(text)


@dataclass(frozen=True)
class Line:
    id: str
    decision: str
    approval: "dict | None" = None
    limits: "dict | None" = None
    when: Any = None  # a _rules Group, or None: the line always matches
    unless: Any = None  # a _rules Cond, or None
    repetition: "dict | None" = None  # {"max": n, "scope": "session"}

    @property
    def conditional(self) -> bool:
        return self.when is not None or self.unless is not None


def _condition(raw: Any) -> Cond:
    """One served Condition: ``{"arg": name | "ctx": name, "op", "value"}`` as a _rules :class:`Cond`. Its operator is not judged here: an unknown one evaluates to unknown."""
    if not isinstance(raw, dict) or set(raw) - {"arg", "ctx", "op", "value"} or ("arg" in raw) == ("ctx" in raw):
        raise LineError(UNKNOWN_RESTRICTION_KEY if isinstance(raw, dict) else ROW_UNPARSEABLE, "a condition is not understood")
    name, op = raw.get("arg", raw.get("ctx")), raw.get("op")
    if not isinstance(name, str) or not name or not isinstance(op, str):
        raise LineError(INVALID_BODY, "a condition needs a name and an operator")
    if "ctx" in raw and name not in ("environment", "version", "service"):
        raise LineError(INVALID_BODY, "a context condition names environment, version or service")
    return Cond(name, op, raw.get("value"), "arg" if "arg" in raw else "context")


def _group(raw: Any, depth: int = 1) -> Group:
    if not isinstance(raw, dict) or len(raw) != 1 or next(iter(raw)) not in ("all", "any"):
        raise LineError(UNKNOWN_RESTRICTION_KEY if isinstance(raw, dict) else ROW_UNPARSEABLE, "a group is {all: [...]} or {any: [...]}")
    kind = next(iter(raw))
    items = raw[kind]
    if not isinstance(items, list) or not items or len(items) > MAX_GROUP_ITEMS:
        raise LineError(INVALID_BODY, "a group has 1 to 20 items")
    out = []
    for item in items:
        if isinstance(item, dict) and (("all" in item) or ("any" in item)):
            if depth >= 2:
                raise LineError(INVALID_BODY, "groups nest one level only")
            out.append(_group(item, depth + 1))
        else:
            out.append(_condition(item))
    return Group(kind, tuple(out))


def _repetition(raw: Any) -> dict:
    if (not isinstance(raw, dict) or set(raw) - {"max", "scope"} or raw.get("scope") != "session"
            or isinstance(raw.get("max"), bool) or not isinstance(raw.get("max"), int) or not 1 <= raw["max"] <= 1_000_000):
        raise LineError(INVALID_BODY, "repetition is {max: 1..1000000, scope: session}")
    return {"max": raw["max"], "scope": "session"}


def parse_lines(raw: Any) -> tuple:
    """The served ``lines`` as :class:`Line` objects. Raises :class:`LineError` for anything this SDK cannot read safely."""
    if not isinstance(raw, list):
        raise LineError(INVALID_BODY, "lines is not a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise LineError(ROW_UNPARSEABLE, "a line is not an object")
        extra = sorted(str(k) for k in item if k not in LINE_KEYS)
        if extra:
            raise LineError(UNKNOWN_RESTRICTION_KEY, f"a line carries unknown key(s): {', '.join(extra)}")
        ident, decision = item.get("id"), item.get("decision")
        if not isinstance(ident, str) or not ident or decision not in DECISIONS:
            raise LineError(INVALID_BODY, "a line needs an id and a decision of allow, ask or deny")
        approval, limits = item.get("approval"), item.get("limits")
        when = _group(item["when"]) if item.get("when") is not None else None
        unless = _condition(item["unless"]) if item.get("unless") is not None else None
        repetition = _repetition(item["repetition"]) if item.get("repetition") is not None else None
        if approval is not None and (not isinstance(approval, dict) or set(approval) - APPROVAL_KEYS):
            raise LineError(UNKNOWN_RESTRICTION_KEY if isinstance(approval, dict) else INVALID_BODY, "a line's approval is not understood")
        if limits is not None and (not isinstance(limits, dict) or set(limits) - LIMIT_KEYS):
            raise LineError(UNKNOWN_RESTRICTION_KEY if isinstance(limits, dict) else INVALID_BODY, "a line's limits are not understood")
        out.append(Line(ident, decision, approval, limits, when, unless, repetition))
    if len(out) > MAX_LINES:
        raise LineError(INVALID_BODY, "a tool has at most 12 lines")
    if len({line.id for line in out}) != len(out):
        raise LineError(INVALID_BODY, "two lines share an id")
    return tuple(out)


def lines_from_legacy(raw: dict) -> tuple:
    """The lines of a row stated only in the legacy fields, derived as the plane derives them on read (WIRE-TR2 §5): never stored."""
    limits = {k: raw[k] for k in ("max_calls_per_session", "quota") if raw.get(k) is not None} or None
    if raw.get("blocked"):
        return (Line("L1", "deny", None, limits),)
    if raw.get("approval_required"):
        approval = {"approval_mode": raw.get("approval_mode"), "approval_timeout_s": raw.get("approval_timeout_s"),
                    "approvers": list(raw.get("approvers") or [])}
        return (Line("L1", "ask", approval, limits),)
    if limits:
        return (Line("L1", "allow", None, limits),)
    return ()


def legacy_decision(raw: dict) -> str:
    """What the legacy view of a row says: deny if blocked, else ask if approval is required, else allow."""
    return "deny" if raw.get("blocked") else "ask" if raw.get("approval_required") else "allow"


@dataclass(frozen=True)
class Plan:
    """What one row decides: the strictest matching line, the limits of the matching lines, and the record behind them."""

    decision: str  # allow | ask | deny | refuse (a line the call could not evaluate: condition_unevaluable)
    reason: str  # the evaluator's primary reason: rule | fallback | condition_unevaluable
    matched: tuple  # line ids that took part, e.g. ("L1",)
    contributors: tuple  # e.g. ("row:L1",), ("default:service",), ("unknown:L2",)
    approval: "dict | None"  # the ask line's own requirement
    max_calls: "int | None"
    quota: "dict | None"
    requirements: tuple = ()
    repetition: "int | None" = None  # the smallest max among matching lines that carry a repetition control


def _quota_limit(lines: tuple) -> "dict | None":
    quotas = [line.limits["quota"] for line in lines if line.limits and isinstance(line.limits.get("quota"), dict)]
    if not quotas:
        return None
    if len({(q.get("window"), q.get("per", "service")) for q in quotas}) != 1:
        raise LineError(INVALID_BODY, "lines carry quotas of different windows or scopes")
    return min(quotas, key=lambda q: q.get("limit", 0))


def plan(lines: tuple, otherwise: "str | None" = None, *, args: Any = None, context: Any = None, static: bool = False) -> Plan:
    """Decide a row with the evaluator (TPD §4b). ``otherwise`` applies only when no line matches; ``inherit`` is ``allow`` until service defaults decide.

    ``static`` reads every line as if it matched (conditions ignored): the strictest outcome the row can produce, which is what the legacy view of the
    row must never exceed. Otherwise each line's ``when`` and ``unless`` are evaluated on ``args`` and ``context``: the strictest MATCHING line decides, an
    unknown line that could make the decision stricter refuses the call (``condition_unevaluable``, K1), and the limits and approval of a line count only
    when it matches.
    """
    fallback = otherwise if otherwise in ("allow", "ask", "deny") else "allow"
    rules = [Rule(line.id, line.decision) if static else Rule(line.id, line.decision, line.when, line.unless) for line in lines]
    outcome = evaluate(Ceiling(), rules, fallback, args=args, context=context)
    live = tuple(line for line in lines if static or line.id in outcome.matched_rule_ids)
    caps = [line.limits["max_calls_per_session"] for line in live if line.limits and line.limits.get("max_calls_per_session") is not None]
    ask = next((line for line in live if line.decision == "ask"), None)
    asks = [approvals_rules.Requirement(f"row:{line.id}", wait_s=(line.approval or {}).get("approval_timeout_s"))
            for line in live if line.decision == "ask"]
    reqs = approvals_rules.requirements(outcome, None, asks, fallback_operative=not outcome.matched_rule_ids)
    repeats = [line.repetition["max"] for line in live if line.repetition]
    contributors = [_contributor(c) for c in outcome.contributors]
    return Plan(
        decision=outcome.decision, reason=outcome.primary_reason, matched=tuple(outcome.matched_rule_ids),
        contributors=tuple(contributors), approval=None if ask is None else (ask.approval or {}),
        max_calls=min(caps) if caps else None, quota=_quota_limit(live), requirements=tuple(reqs), repetition=min(repeats) if repeats else None,
    )


def _contributor(item: Any) -> str:
    return {"rule": f"row:{item.ref}", "unknown_rule": f"unknown:{item.ref}", "code_blocked": "code:blocked", "code_constraint": "code:constraint",
            "code_approval": "code:approval", "fallback": "default:service", "limit": f"limit:{item.ref}"}.get(item.kind, f"{item.kind}:{item.ref}")


# --- service defaults (WIRE-TR2b; TR2-O3) ----------------------------------------------------------------------------------


KEYS = ("read", "write", "external", "financial", "destructive", "privileged", "unclassified")
OPERATIONS = ("read", "write")
TAGS = ("external", "financial", "destructive", "privileged")
FAIL_MODES = ("open", "closed")


@dataclass(frozen=True)
class Defaults:
    """A service's defaults by class: one decision and one fail mode per key, and an approval requirement per key that asks.

    ``state`` is ``enforced`` (applied) or ``dry_run`` (read only to record what it would have done). Used only as a fallback: a line
    that matches a tool decides it, and a tool may be looser than its default within the code ceiling (TPD §5.5).
    """

    revision: int
    state: str
    decision: dict
    fail_mode: dict
    approval: dict
    candidate: "Defaults | None" = None  # a dry-run revision served beside the enforced one: only recorded, never decides


def parse_defaults(raw: Any) -> Defaults:
    """The served ``service_defaults`` envelope as :class:`Defaults`. Raises :class:`LineError` for anything this SDK cannot read safely."""
    if not isinstance(raw, dict):
        raise LineError(ROW_UNPARSEABLE, "service defaults are not an object")
    body = raw.get("defaults")
    state, revision = raw.get("state", "enforced"), raw.get("revision")
    if not isinstance(body, dict) or state not in ("enforced", "dry_run") or isinstance(revision, bool) or not isinstance(revision, int):
        raise LineError(INVALID_BODY, "service defaults need a revision, a state and a defaults object")
    decision, fail_mode, approval = body.get("decision"), body.get("fail_mode"), body.get("approval") or {}
    if (not isinstance(decision, dict) or not isinstance(fail_mode, dict) or not isinstance(approval, dict)
            or set(body) - {"decision", "fail_mode", "approval"}):
        raise LineError(UNKNOWN_RESTRICTION_KEY, "service defaults carry a part this SDK does not know")
    for key in KEYS:
        if decision.get(key) not in DECISIONS or fail_mode.get(key) not in FAIL_MODES:
            raise LineError(INVALID_BODY, f"service defaults miss a valid decision or fail mode for {key}")
    if set(decision) - set(KEYS) or set(fail_mode) - set(KEYS) or set(approval) - set(KEYS):
        raise LineError(UNKNOWN_RESTRICTION_KEY, "service defaults name a key this SDK does not know")
    for key, spec in approval.items():
        if not isinstance(spec, dict) or set(spec) - {"approvers", "wait_s"}:
            raise LineError(INVALID_BODY, f"the approval default for {key} is not understood")
    candidate = None
    if raw.get("candidate") is not None and state == "enforced":
        try:  # a candidate this SDK cannot read is not recorded; it never decides, so it cannot loosen anything
            candidate = parse_defaults(raw["candidate"])
            if candidate.state != "dry_run" or candidate.candidate is not None:
                candidate = None
        except LineError:
            candidate = None
    return Defaults(revision, state, dict(decision), dict(fail_mode), {k: dict(v) for k, v in approval.items()}, candidate)


@dataclass(frozen=True)
class Resolved:
    """What a service's defaults say about one tool, from its declared effects (WIRE-TR2b §3, TPD §5.4)."""

    decision: str  # allow | ask | deny
    fail_mode: str  # open | closed
    keys: tuple  # the keys that decided
    requirements: tuple  # one per key that asks (never a union), as _rules Requirement objects
    wait_s: "float | None"  # the shortest wait among them


def _decision_keys(effects: "frozenset | None") -> tuple:
    classes = frozenset(effects or ())
    if not classes:
        return ("unclassified",)
    ops = tuple(k for k in OPERATIONS if k in classes)
    tags = tuple(k for k in TAGS if k in classes)
    return ops + tags  # an unknown operation contributes nothing: the tags decide (K4)


def _fail_keys(effects: "frozenset | None") -> tuple:
    classes = frozenset(effects or ())
    if not classes or ("write" in classes and not classes - {"read", "write"}):
        return ("unclassified",)  # K8: a write with no tag counts as unclassified for fail mode only
    return _decision_keys(effects)


def resolve(defaults: Defaults, effects: "frozenset | None") -> Resolved:
    """The strictest of the operation's and the tags' defaults; fail mode closed if any contributing key is closed; one requirement per asking key."""
    keys = _decision_keys(effects)
    rank = {name: index for index, name in enumerate(DECISIONS)}
    decision = max((defaults.decision[k] for k in keys), key=rank.__getitem__)
    fail = "closed" if any(defaults.fail_mode[k] == "closed" for k in _fail_keys(effects)) else "open"
    asking = [k for k in keys if defaults.decision[k] == "ask"] if decision == "ask" else []
    reqs = tuple(approvals_rules.Requirement(f"default:{k}", frozenset(defaults.approval.get(k, {}).get("approvers") or ()),
                                             defaults.approval.get(k, {}).get("wait_s")) for k in asking)
    return Resolved(decision, fail, keys, reqs, approvals_rules.wait_s(reqs))


def wait_s(plans_requirements: tuple, code_wait: "float | None" = None) -> "float | None":
    """The shortest wait among a call's approval requirements (§4b step 6), including the code's own."""
    reqs = list(plans_requirements)
    if code_wait is not None:
        reqs.append(approvals_rules.Requirement("code", wait_s=code_wait))
    return approvals_rules.wait_s(reqs)
