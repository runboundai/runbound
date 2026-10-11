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

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from . import admission
from .policy import CAPABILITIES

_LOG = logging.getLogger("runbound")

RANK = {name: index for index, name in enumerate(CAPABILITIES)}
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

    @property
    def dry_run(self) -> bool:
        return self.state == "dry_run"


def parse(rows: Any) -> "dict[str, Row]":
    """The served rows as ``{tool: Row}``. Tolerant: a malformed row is skipped, never an error."""
    out: dict = {}
    for raw in rows if isinstance(rows, list) else ():
        try:
            tool, state = raw["tool"], raw.get("state", "enforced")
            if not isinstance(tool, str) or state not in STATES:
                continue
            quota = raw.get("quota")
            risk = raw.get("risk_class")
            max_calls = raw.get("max_calls_per_session")
            out[tool] = Row(
                tool=tool, state=state, blocked=bool(raw.get("blocked")),
                max_calls=max_calls if isinstance(max_calls, int) and not isinstance(max_calls, bool) and max_calls >= 1 else None,
                quota=quota if isinstance(quota, dict) and isinstance(quota.get("limit"), int) else None,
                approval_required=bool(raw.get("approval_required")),
                approval_mode=raw.get("approval_mode") if raw.get("approval_mode") in ("sync", "deferred") else None,
                approval_timeout_s=raw.get("approval_timeout_s") if isinstance(raw.get("approval_timeout_s"), (int, float)) else None,
                risk_class=risk if risk in RANK else None,
                fail_mode="open" if raw.get("fail_mode") == "open" else "closed",
            )
        except Exception:
            _LOG.debug("runbound: skipping a malformed policy row", exc_info=True)
    return out


def effective_effects(row: "Row | None", effects: "frozenset | None") -> "frozenset | None":
    """The tool's capability classes with the row's ``risk_class`` folded in: the stricter wins."""
    if row is None or row.risk_class is None or row.dry_run:
        return effects
    own = frozenset(effects or ())
    if own and max(RANK.get(c, 0) for c in own) >= RANK[row.risk_class]:
        return effects
    return frozenset({row.risk_class}) | own


def approval_spec(row: "Row | None", code_spec: Any, names: tuple, json_ok: bool, spec_type: type) -> Any:
    """The approval this call needs: the code's, the row's, or both folded (OR; code's mode wins; lower timeout).

    ``None`` when none is required (a dry_run row never asks anyone). A deferred row on a tool whose
    arguments are not JSON-friendly falls back to sync.
    """
    if row is None or not row.approval_required or row.dry_run:
        return code_spec
    if code_spec is not None:
        mode = code_spec.mode
        timeout = min(code_spec.timeout_s, row.approval_timeout_s) if row.approval_timeout_s else code_spec.timeout_s
        return spec_type(mode, float(timeout), code_spec.names or names)
    mode = row.approval_mode or "sync"
    if mode == "deferred" and not json_ok:
        mode = "sync"
    return spec_type(mode, float(row.approval_timeout_s or 600.0), names)


def _refuse(engine: Any, session: Any, call: Any, row: Row, code: str, reason: str, tags: list | None,
            retry_after_s: float | None = None) -> None:
    """Refuse for a row's rule, or, for a dry_run row, record ``would_refuse`` and let the call go."""
    from .engine import _anomaly, _stamp_decision

    decision = admission.row(code, reason, retry_after_s)
    anomaly = _anomaly(session, "policy", "warn" if row.dry_run else "critical",
                       f"{'Dry run: would refuse' if row.dry_run else 'Refused'} tool {call.name!r}{{whose}}: {reason}",
                       {"tool": call.name, "rule": code, "origin": "org", "state": row.state})
    if row.dry_run:
        engine._alert(_stamp_decision(anomaly, decision, session=session), session, "dry_run")
        if tags is not None:
            tags.append(f"would_refuse:{code}")
        return
    engine.refuse(session, anomaly, decision, reacted="blocked")


def enforce(engine: Any, session: Any, call: Any, row: "Row | None", tags: list | None) -> None:
    """``blocked`` then the session cap, in that order (tool-policy.md §2). Consumes nothing."""
    if row is None:
        return
    if row.blocked:
        _refuse(engine, session, call, row, "tool_blocked", f"tool {call.name!r} is blocked by policy", tags)
    if row.max_calls is not None:
        used = session.counted_calls(call.name)
        if used > row.max_calls:
            _refuse(engine, session, call, row, "session_max_calls",
                    f"tool {call.name!r} has been called {used} times in this session; the limit is {row.max_calls}", tags)


def rows_hash(rows: list) -> str:
    """The plane's hash of a row set, for a runtime that wants to verify what it holds (see the plane README)."""
    fields = ("state", "blocked", "max_calls_per_session", "quota", "approval_required", "approval_mode",
              "approval_timeout_s", "approvers", "risk_class", "fail_mode", "headline", "identifies")
    canonical = [{"tool": r.get("tool"), **{f: r.get(f) for f in fields}} for r in sorted(rows, key=lambda r: r.get("tool", ""))]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
