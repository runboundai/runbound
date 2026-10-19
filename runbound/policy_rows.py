"""Per-tool policy rows from the dashboard, folded with what the code declares (T1).

A row is the dashboard's statement about **one tool of one service**: numbers and names only. The plane
serves ``rows`` and ``rows_hash`` on ``GET /v1/policy``; the worker folds each row with the tool's code
rules, **most restrictive wins, field by field**, so a row can add a block, a cap or an approval and can
never loosen a code rule (tool-policy.md §4):

=========================  ==============================================================
``blocked``                OR with the code's ``blocked=True``
``max_calls_per_session``  the lower of the two (both are enforced)
``approval_required``      OR with code approval
``approval_mode``          code wins when it declares one; the row decides when code is silent
``approval_timeout_s``     the lower
``risk_class``             the stricter by rank ``read < write < external < financial < destructive < privileged``
``fail_mode``              ``closed`` wins
``quota``                  enforced by :mod:`runbound.quota`
=========================  ==============================================================

A ``dry_run`` row evaluates everything and **refuses nothing**: it records ``would_refuse`` (a Decision, and a
``would_refuse:<rule>`` tag on the ``tool_result``). Order and reasons (§2): ``blocked`` -> ``tool_blocked``;
the session cap -> ``session_max_calls``; quota -> ``quota_exceeded`` / ``quota_plane_unavailable``; then
approval. A refused call never consumes quota or asks a human.

``rows_hash`` is what tells a worker the rows changed; the hello's ``policy_version`` is shared with other
policies and is not proof of it, so a worker compares hashes and keeps what it holds when they are equal.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from . import _row_check as row_check
from . import admission
from . import lines as lines_module
from .policy import CAPABILITIES

_LOG = logging.getLogger("runbound")

RANK = {name: index for index, name in enumerate(CAPABILITIES)}
RANK_DECISION = {"allow": 0, "ask": 1, "deny": 2}
STATES = ("dry_run", "enforced")


@dataclass(frozen=True)
class Row:
    tool: str
    state: str = "enforced"
    blocked: bool = False
    max_calls: int | None = None
    quota: dict | None = None
    approval_required: bool = False
    approval_mode: str | None = None
    approval_timeout_s: float | None = None
    risk_class: str | None = None
    fail_mode: str = "closed"
    headline: str | None = None
    identifies: tuple = ()
    #: Why the SDK cannot interpret this row safely (floor F2), else ``None``: the tool is refused with ``policy_unsupported``.
    unsupported: str | None = None
    unsupported_code: str | None = None  # row_unparseable | unknown_restriction_key | invalid_body | legacy_view_mismatch
    #: The record behind this row's decision (TR2-O2 AC3): ``"<tool>#L1"`` ids of the lines that matched, what contributed, why.
    matched: tuple = ()
    contributors: tuple = ()
    plan_reason: str = "fallback"
    requirements: tuple = ()
    #: True when the row decides nothing itself (no line, ``otherwise`` inherit): the service defaults decide, if it has adopted any.
    inherit: bool = False
    #: The lines of a row with a condition: decided anew for each call from its arguments and context (:func:`resolve_call`); the fields above
    #: then hold the strictest outcome the row can produce, which is what the legacy view must never exceed.
    conditional: tuple = ()
    otherwise: "str | None" = None
    #: A line the call could not evaluate (K1): the call is refused with this ``(code, text)`` and no line decided it.
    refuse: "tuple | None" = None
    #: Refuse the call when the session already made this many calls of the tool with the same arguments (a repetition control on a matching line).
    repetition: "int | None" = None
    #: A dry-run revision served beside this enforced one (WIRE-TR2 §3): evaluated only to record ``would_refuse``, never to decide.
    candidate: "Row | None" = None
    #: The per-tool ordinal the plane served this row under (``rule_revision``, ``revisions_v1``), else ``None``; and, on a row the service defaults decided
    #: (or on a defaults candidate), the defaults revision that did. They name what a decision record was made under; they decide nothing.
    rule_revision: "int | None" = None
    defaults_revision: "int | None" = None

    @property
    def dry_run(self) -> bool:
        return self.state == "dry_run"


def _build(raw: dict, tool: str, state: str) -> Row:
    """One readable served row as a :class:`Row`. A row with ``lines`` is decided from them; one without is read from its legacy fields,
    through the lines the plane would derive from them. Raises :class:`runbound.lines.LineError` for lines it cannot read safely."""
    has_lines = "lines" in raw
    if has_lines:
        lines = lines_module.parse_lines(raw["lines"])
        decided = lines_module.plan(lines, raw.get("otherwise"), static=True)
        # The strictest outcome the row can produce: the strictest line (the static reading treats every line as matching, so the row's ``otherwise`` never applies in it) AND
        # the explicit ``otherwise``, which is what decides a call no line matches. "Allow when ... otherwise deny" can deny; its legacy view says so.
        reachable = max(RANK_DECISION[decided.decision], RANK_DECISION.get(raw.get("otherwise"), 0))
        if RANK_DECISION[lines_module.legacy_decision(raw)] > reachable:
            # The legacy view is the plane's most restrictive reading of the lines and the otherwise; one stricter than both means
            # the lines payload is looser than the plane meant. Never run on it.
            raise lines_module.LineError(lines_module.LEGACY_VIEW_MISMATCH, "the legacy view of this row is stricter than its lines")
    else:
        lines = lines_module.lines_from_legacy(raw)
        decided = lines_module.plan(lines, None, static=True)
    quota = raw.get("quota")
    max_calls = raw.get("max_calls_per_session")
    if has_lines:
        approval = decided.approval or {}
        blocked, required = decided.decision == "deny", decided.decision == "ask"
        mode, timeout = approval.get("approval_mode"), approval.get("approval_timeout_s")
        max_calls, quota = decided.max_calls, decided.quota
    else:
        blocked, required = bool(raw.get("blocked")), bool(raw.get("approval_required"))
        mode, timeout = raw.get("approval_mode"), raw.get("approval_timeout_s")
    row = Row(
        tool=tool, state=state, blocked=blocked, max_calls=max_calls, quota=quota, approval_required=required,
        approval_mode=mode, approval_timeout_s=timeout, risk_class=raw.get("risk_class"),
        fail_mode="open" if raw.get("fail_mode") == "open" else "closed",
        headline=raw.get("headline"), identifies=tuple(raw.get("identifies") or ()),
        matched=tuple(f"{tool}#{i}" for i in decided.matched), contributors=decided.contributors,
        plan_reason=decided.reason, requirements=decided.requirements,
        inherit=not lines and raw.get("otherwise") in (None, "inherit"),
        conditional=lines if any(line.conditional for line in lines) else (), otherwise=raw.get("otherwise") if has_lines else None,
        repetition=decided.repetition,
        rule_revision=raw["rule_revision"] if isinstance(raw.get("rule_revision"), int) and not isinstance(raw.get("rule_revision"), bool) else None,
    )
    candidate = raw.get("candidate")
    if isinstance(candidate, dict):
        try:
            row = dataclasses.replace(row, candidate=_build({**candidate, "tool": tool}, tool, "dry_run"))
        except Exception:
            _LOG.warning("runbound: a candidate rule for %r could not be read; it is not evaluated", tool, exc_info=True)
    return row


def apply_defaults(row: "Row | None", tool: str, effects: "frozenset | None", shared: Any) -> "Row | None":
    """The row this call is decided by once the service's defaults are counted in (WIRE-TR2b; TR2-O3).

    A service defaults is only a **fallback** (TPD §5.5): a tool with no row, or a row that decides nothing itself (no line, ``otherwise``
    inherit), is decided by the default for its declared effects (the strictest of the operation's and the tags'; the unclassified default for a
    tool with none); a row with a line, or an explicit ``otherwise``, keeps deciding. Enforced defaults gate the call; dry-run defaults ride as a
    ``candidate`` and are only recorded. Defaults the SDK cannot read refuse the unruled tool (``policy_unsupported``).
    """
    defaults = getattr(shared, "defaults", lambda: None)()
    problem = getattr(shared, "defaults_problem", lambda: None)()
    if defaults is None and problem is None:
        return row
    if row is not None and (row.unsupported or not row.inherit):
        return row
    base = row if row is not None else Row(tool=tool, inherit=True)
    if defaults is None:  # served enforced defaults this SDK cannot read: never resolve an unruled tool to allow
        return Row(tool=tool, state="enforced", unsupported=problem[1], unsupported_code=problem[0])
    resolved = lines_module.resolve(defaults, effects)
    gate = dataclasses.replace(
        base, blocked=base.blocked or resolved.decision == "deny", approval_required=base.approval_required or resolved.decision == "ask",
        approval_mode=base.approval_mode or ("sync" if resolved.decision == "ask" else None),
        approval_timeout_s=base.approval_timeout_s if resolved.wait_s is None else resolved.wait_s,
        contributors=("default:service",), requirements=resolved.requirements, plan_reason="fallback", defaults_revision=getattr(defaults, "revision", None))
    if defaults.state == "dry_run":
        return dataclasses.replace(base, candidate=dataclasses.replace(gate, state="dry_run", candidate=None))
    if defaults.candidate is not None and base.candidate is None:  # a dry-run revision beside the enforced one: record only
        seen = lines_module.resolve(defaults.candidate, effects)
        shadow = dataclasses.replace(
            base, blocked=base.blocked or seen.decision == "deny", approval_required=base.approval_required or seen.decision == "ask",
            approval_mode=base.approval_mode or ("sync" if seen.decision == "ask" else None), contributors=("default:service",),
            requirements=seen.requirements, plan_reason="fallback", state="dry_run", candidate=None,
            defaults_revision=getattr(defaults.candidate, "revision", None))
        return dataclasses.replace(gate, candidate=shadow)
    return gate
def resolve_call(row: "Row | None", args: "dict | None", context: dict) -> "Row | None":
    """The row for THIS call: a row with a condition is decided anew from the call's final arguments and the trusted context (TPD §4b, §5.2).

    The strictest matching line decides; an unknown line that could make it stricter refuses the call (``condition_unevaluable``, K1); the limits and
    approval of a line count only while it matches; ``otherwise`` and the service defaults decide only when no line matches. A row with no condition is
    returned as it is. The candidate, if any, is resolved the same way.
    """
    if row is None or row.unsupported:
        return row
    candidate = None if row.candidate is None else resolve_call(row.candidate, args, context)
    if not row.conditional:
        return row if candidate is row.candidate else dataclasses.replace(row, candidate=candidate)
    decided = lines_module.plan(row.conditional, row.otherwise, args=args or {}, context=context)
    approval = decided.approval or {}
    refuse = None
    if decided.decision == "refuse":
        refuse = ("condition_unevaluable", "a console rule for this tool could not be evaluated on this call's arguments, and might have refused it")
    return dataclasses.replace(
        row, blocked=decided.decision == "deny", approval_required=decided.decision == "ask",
        approval_mode=approval.get("approval_mode"), approval_timeout_s=approval.get("approval_timeout_s"),
        max_calls=decided.max_calls, quota=decided.quota,
        matched=tuple(f"{row.tool}#{i}" for i in decided.matched), contributors=decided.contributors, plan_reason=decided.reason,
        requirements=decided.requirements, repetition=decided.repetition, candidate=candidate, refuse=refuse,
        # no line matched this call and the row's otherwise is inherit: the service defaults decide it, as for a row with no line (TPD §5.5)
        inherit=not decided.matched and refuse is None and row.otherwise in (None, "inherit"),
    )


def parse(rows: Any) -> "dict[str, Row]":
    """The served rows as ``{tool: Row}``. A row the SDK cannot interpret safely is kept, marked ``unsupported``, never skipped.

    That is the floor (F2, register C2/C9/C19): a row the plane's validator would not have served, one with a key this SDK
    does not know, or one that is not even an object with a tool name must never resolve to *allow*. The first two refuse
    their own tool with ``policy_unsupported``; the last names no tool, so it can only be counted (:func:`unreadable`).
    """
    out: dict = {}
    for raw in rows if isinstance(rows, list) else ():
        try:
            tool = raw["tool"]
            if not isinstance(tool, str) or not tool:
                continue
            state = raw.get("state", "enforced")
            why = row_check.problem(raw)
            code = row_check.code_of(raw)
            if why is None:
                try:
                    out[tool] = _build(raw, tool, state)
                    continue
                except lines_module.LineError as error:
                    why, code = str(error), error.code
            out[tool] = Row(tool=tool, state=state if state in STATES else "enforced", unsupported=why, unsupported_code=code)
        except Exception:
            _LOG.debug("runbound: a served policy row could not be read", exc_info=True)
    return out


def unreadable(rows: Any) -> int:
    """How many served entries name no tool (not an object, or no string ``tool``): nothing to refuse, so only counted."""
    return sum(1 for raw in (rows if isinstance(rows, list) else ()) if not (isinstance(raw, dict) and isinstance(raw.get("tool"), str) and raw["tool"]))


def effective_effects(row: "Row | None", effects: "frozenset | None") -> "frozenset | None":
    """The tool's capability classes with the row's ``risk_class`` folded in: the stricter wins, and a row never loosens.

    A tool that declares no effects is *unclassified* and fails closed. A row naming ``read``, ``write`` or ``external``
    would turn it into a fail-open tool, which is a loosening of what the code declared, so such a row leaves it
    unclassified; ``financial``, ``destructive`` and ``privileged`` raise it as before (register C15). A declared class
    that is as strict as the row's, or stricter, is left alone.
    """
    if row is None or row.risk_class is None or row.dry_run:
        return effects
    own = frozenset(effects or ())
    if not own:
        return effects if RANK[row.risk_class] < RANK["financial"] else frozenset({row.risk_class})
    if max(RANK.get(c, 0) for c in own) >= RANK[row.risk_class]:
        return effects
    return frozenset({row.risk_class}) | own


def requirement_of(row: "Row") -> "str | None":
    """The requirement a request is for, as the plane stores it: the matched console line's id (``L2``), a default (``default:financial``) or ``code``.

    Several together are joined in a fixed order, so a grant made for one set satisfies only that same set (never one of them alone). ``None`` with none.
    """
    names = sorted({source[4:] if source.startswith("row:") else source for source in (r.source for r in row.requirements)})
    joined = ",".join(names)
    if len(joined) > 64:
        joined = "set:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:20]
    return joined or None


def approval_spec(row: "Row | None", code_spec: Any, names: tuple, json_ok: bool, spec_type: type) -> Any:
    """The approval this call needs: the code's, the row's, or both folded (OR; code's mode wins; lower timeout).

    ``None`` when none is required (a dry_run row never asks anyone). A deferred row on a tool whose
    arguments are not JSON-friendly falls back to sync.
    """
    if row is None or row.dry_run:
        return code_spec
    if not row.approval_required:
        # A row that only sets a wait still shortens the code's own approval ("the lower"); it asks nobody by itself (TR2 C17).
        if code_spec is not None and row.approval_timeout_s:
            return spec_type(code_spec.mode, float(min(code_spec.timeout_s, row.approval_timeout_s)), code_spec.names or names)
        return code_spec
    requirement = requirement_of(row)
    if code_spec is not None:
        mode = code_spec.mode
        # Two requirements, the code's and the row's, never merged: the wait is the shortest named (TPD §4b step 6, via _rules).
        timeout = lines_module.wait_s(row.requirements, code_spec.timeout_s) if row.requirements else None
        if timeout is None:
            timeout = min(code_spec.timeout_s, row.approval_timeout_s) if row.approval_timeout_s else code_spec.timeout_s
        return spec_type(mode, float(timeout), code_spec.names or names, requirement)
    # Deferred execution needs the tool's own registration (the code states ``mode="deferred"``): a row alone never makes a tool deferred.
    return spec_type("sync", float(row.approval_timeout_s or 600.0), names, requirement)


def _refuse(engine: Any, session: Any, call: Any, row: Row, code: str, reason: str, tags: list | None,
            retry_after_s: float | None = None) -> None:
    """Refuse for a row's rule, or, for a dry_run row, record ``would_refuse`` and let the call go."""
    from .engine import _anomaly, _stamp_decision

    decision = admission.row(code, reason, retry_after_s)
    anomaly = _anomaly(session, "policy", "warn" if row.dry_run else "critical",
                       f"{'Dry run: would refuse' if row.dry_run else 'Refused'} tool {call.name!r}{{whose}}: {reason}",
                       {"tool": call.name, "rule": code, "origin": "org", "state": row.state})
    if row.dry_run:
        # Every would-refuse call is evidence (the console counts them), so it goes down the refusal path: recorded one by one up
        # to the record cap per session, rule and tool, then counted and summarised. What it must not do is page anyone: a
        # dry_run reaction never matches an alert route unless the route asks for it.
        engine._alert(_stamp_decision(anomaly, decision, session=session), session, "dry_run", refusal=True)
        if tags is not None:
            tags.append(f"would_refuse:{code}")
        return
    engine.refuse(session, anomaly, decision, reacted="blocked")


def _would_ask(engine: Any, session: Any, call: Any, row: Row) -> None:
    """File that a live row would have asked a person before this call (a dry run, or a candidate beside the enforced row). Nothing is asked and nothing is
    refused; the event never pages anyone (its reaction stays ``dry_run``)."""
    from .engine import _anomaly, _stamp_decision

    reason = f"tool {call.name!r} would ask a person before it runs"
    anomaly = _anomaly(session, "policy", "info", f"Dry run: would ask a person: {reason}{{whose}}", {"tool": call.name, "rule": "would_ask", "origin": "org", "state": row.state})
    engine._alert(_stamp_decision(anomaly, admission.row("approval", reason), session=session), session, "dry_run", refusal=True)


def _blocked_text(tool: str, row: Row) -> str:
    """The refusal text for a ``blocked`` row. A tool blocked outright is "blocked by policy"; a row with a condition blocks only the calls its deny
    line matches, so it names the line (and says the tool itself is not off), never the tool as blocked (DRACO F8). The reason code is the same."""
    if row.conditional and row.matched:
        lines = ", ".join(m.rsplit("#", 1)[-1] for m in row.matched)
        return f"tool {tool!r} was refused by rule line {lines}: a condition on this call matched a deny line (the tool itself is not blocked)"
    return f"tool {tool!r} is blocked by policy"


def enforce(engine: Any, session: Any, call: Any, row: "Row | None", tags: list | None) -> None:
    """``blocked`` then the session cap, in that order (tool-policy.md §2). Consumes nothing."""
    if row is None:
        return
    if row.unsupported is not None:
        _refuse(engine, session, call, row, "policy_unsupported",
                f"tool {call.name!r} has a console rule this SDK cannot interpret safely ({row.unsupported}); it is refused until the rule is changed or the SDK upgraded",
                tags)
        return
    if row.refuse is not None:
        _refuse(engine, session, call, row, row.refuse[0], f"tool {call.name!r}: {row.refuse[1]}", tags)
        return
    if row.blocked:
        _refuse(engine, session, call, row, "tool_blocked", _blocked_text(call.name, row), tags)
    if row.max_calls is not None:
        used = session.counted_key(call.name, ",".join(row.matched)) if row.conditional else session.counted_calls(call.name)
        if used > row.max_calls:
            _refuse(engine, session, call, row, "session_max_calls",
                    f"tool {call.name!r} has been called {used} times in this session; the limit is {row.max_calls}", tags)
    if row.repetition is not None:
        digest = engine.call_digest(call)
        said = f"tool {call.name!r} was already called {row.repetition} times with these arguments in this session"
        if row.dry_run:  # a dry run keeps a count of its own, apart from the live one, so it never changes what a live rule would see
            if not session.reserve_repeat(call.name, "dry:" + digest, row.repetition):
                _refuse(engine, session, call, row, "repetition_exceeded", said, tags)
        elif engine.hold_repeat(session, call, digest, row.repetition):
            pass
        else:
            _refuse(engine, session, call, row, "repetition_exceeded", said, tags)
    if row.dry_run and row.approval_required:
        # A dry run asks nobody; it records that a live rule would have, as evidence the console can count: the tag on this call's result, and an event filed the way a
        # would-refuse is (reaction ``dry_run``, rule ``would_ask``) so the plane's evidence reads it (VPC-02: a dry run of an ask rule is not "0 evaluations").
        _would_ask(engine, session, call, row)
        if tags is not None:
            tags.append("would_ask:approval")
    if row.candidate is not None and not row.dry_run:
        # A candidate (dry-run) revision beside the enforced one: what it would have refused is recorded, the call goes on. It never
        # decides, never moves a live counter and never asks anyone (its quota and approval are not evaluated: R3 simulates those).
        enforce(engine, session, call, row.candidate, tags)


def supersede(engine: Any, session: Any, call: Any, row: "Row | None", tags: list | None) -> None:
    """R5 (VPC-07): a call a person approved, or a grant admitted, re-checks the CURRENT accepted policy before it runs.

    ``row`` is resolved afresh by the caller. Anything that now refuses the call (a block, a deny line, a rule this SDK cannot read, the
    session cap) refuses it as ``approval_superseded``: an approval alone never runs a call the policy no longer allows. Consumes nothing.
    """
    if row is None or row.dry_run:
        return
    why = code = contributor = None
    if row.unsupported is not None:
        why, code = f"a console rule this SDK cannot interpret safely ({row.unsupported})", "policy_unsupported"
    elif row.refuse is not None:
        from .engine import _LIMIT_REFS

        why, code = row.refuse[1], row.refuse[0]
        contributor = _LIMIT_REFS.get(code) or next((c for c in row.contributors if c != "code:approval"), None)
    elif row.blocked:
        why, code = "it is now blocked by policy", "tool_blocked"
        contributor = next((c for c in row.contributors if c != "code:approval"), None)
    elif row.max_calls is not None:
        used = session.counted_key(call.name, ",".join(row.matched)) if row.conditional else session.counted_calls(call.name)
        if used > row.max_calls:
            why, code, contributor = f"it has been called {used} times in this session; the limit is {row.max_calls}", "session_max_calls", "limit:session_cap"
    if why is not None:
        # WIRE-VPC 12e: the re-check phase names the refusal reason itself and the one contributor that refused (``reason_code`` stays approval_superseded).
        engine.note_recheck(recheck_reason=code, recheck_contributor=contributor)
        _refuse(engine, session, call, row, "approval_superseded",
                f"tool {call.name!r} was approved, but the policy changed before it ran and now refuses it: {why}", tags)


def rows_hash(rows: list) -> str:
    """The plane's hash of a row set, for a runtime that wants to verify what it holds (see the plane README)."""
    fields = ("state", "blocked", "max_calls_per_session", "quota", "approval_required", "approval_mode",
              "approval_timeout_s", "approvers", "risk_class", "fail_mode", "headline", "identifies")
    canonical = [{"tool": r.get("tool"), **{f: r.get(f) for f in fields}} for r in sorted(rows, key=lambda r: r.get("tool", ""))]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
