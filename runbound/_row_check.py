"""Would the plane's validator accept this served row? The SDK interprets only what it would (enforcement floor F2).

A served row the SDK cannot read safely is never skipped: its tool is refused with ``policy_unsupported`` until the
plane serves something the SDK understands. "Cannot read" means the row carries a key the SDK does not know or a value
the plane's own validator (``policy/tool_rows.py``) would have refused. A ``headline`` or ``identifies`` is checked for
grammar only: one that names an argument the tool does not have is display, not authorisation, so it never refuses the tool.
``tests-workspace/test_row_check_fixture_is_the_planes.py`` holds the fixture this is checked against to the plane's validator.
"""

from __future__ import annotations

import re
from typing import Any

STATES = ("dry_run", "enforced")
RISK_CLASSES = ("read", "write", "external", "financial", "destructive", "privileged")
QUOTA_WINDOWS = ("minute", "hour", "day")
QUOTA_SCOPES = ("service", "key")
APPROVAL_MODES = ("sync", "deferred")
FAIL_MODES = ("closed", "open")
MAX_APPROVERS = 20
MAX_APPROVAL_TIMEOUT_S = 7 * 24 * 3600
HEADLINE_MAX = 300

#: The restriction fields of a row, and the labels the plane adds when it serves one (never restrictions).
FIELDS = ("state", "blocked", "max_calls_per_session", "quota", "approval_required", "approval_mode", "approval_timeout_s",
          "approvers", "risk_class", "fail_mode", "headline", "identifies")
#: ``rule_revision`` (WIRE-VPC 1, 3): the tool row's per-tool ordinal, served only to a worker that reported ``revisions_v1``. It names the revision a record was made under;
#: it decides nothing, so it is accepted on a row and on the row's ``candidate`` and read nowhere else.
SERVED_EXTRAS = ("tool", "version", "orphaned", "by", "at", "rule_revision")
#: Served only to a worker that reported ``lines`` (WIRE-TR2 §3); their bodies are read by :mod:`runbound.lines`, not here.
LINES_KEYS = ("lines", "otherwise", "candidate")

_IDENT = r"[a-z][a-z0-9_]*"
_PLACEHOLDER = re.compile(r"\{(?:(tool)|(service)|args((?:\." + _IDENT + r")+))\}")
_IDENT_ONLY = re.compile(r"^" + _IDENT + r"$")


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def headline_args(template: Any) -> "list[str] | None":
    """The first path segment of every ``{args....}`` placeholder, or ``None`` when the template breaks the grammar."""
    if not isinstance(template, str) or len(template) > HEADLINE_MAX:
        return None
    names: list = []
    pos = 0
    for found in _PLACEHOLDER.finditer(template):
        if "{" in template[pos:found.start()] or "}" in template[pos:found.start()]:
            return None
        if found.group(3):
            names.append(found.group(3).split(".")[1])
        pos = found.end()
    tail = template[pos:]
    return None if "{" in tail or "}" in tail else names


def problem(raw: Any) -> "str | None":
    """Why the plane would not have served ``raw``, else ``None``. Checks every field the body carries."""
    if not isinstance(raw, dict):
        return "the row is not an object"
    unknown = sorted(str(k) for k in raw if k not in FIELDS and k not in SERVED_EXTRAS and k not in LINES_KEYS)
    if unknown:
        return f"unknown field(s): {', '.join(unknown)}"
    if "rule_revision" in raw and not _whole(raw["rule_revision"], 1, 10_000_000):
        return "rule_revision must be a whole number from 1"
    if raw.get("otherwise") is not None and raw["otherwise"] not in ("inherit", "allow", "ask", "deny"):
        return "otherwise is not inherit, allow, ask or deny"
    if "state" in raw and raw["state"] not in STATES:
        return "state is not dry_run or enforced"
    for name in ("blocked", "approval_required"):
        if name in raw and not isinstance(raw[name], bool):
            return f"{name} must be true or false"
    if raw.get("max_calls_per_session") is not None and not _whole(raw["max_calls_per_session"], 1, 1_000_000):
        return "max_calls_per_session must be a whole number from 1 to 1000000, or null"
    quota = raw.get("quota")
    if quota is not None:
        if not isinstance(quota, dict) or set(quota) - {"limit", "window", "per"}:
            return "quota must be {limit, window, per}"
        if isinstance(quota.get("limit"), bool) or not isinstance(quota.get("limit"), int) or quota["limit"] < 1:
            return "quota.limit must be a whole number, at least 1"
        if quota.get("window") not in QUOTA_WINDOWS:
            return "quota.window is not minute, hour or day"
        if quota.get("per", "service") not in QUOTA_SCOPES:
            return "quota.per is not service or key"
    if raw.get("approval_mode") is not None and raw["approval_mode"] not in APPROVAL_MODES:
        return "approval_mode is not sync or deferred"
    if raw.get("approval_timeout_s") is not None and not _whole(raw["approval_timeout_s"], 1, MAX_APPROVAL_TIMEOUT_S):
        return "approval_timeout_s must be a whole number of seconds from 1 to 604800, or null"
    approvers = raw.get("approvers")
    if approvers is not None:
        if (not isinstance(approvers, list) or not all(isinstance(v, str) and v.strip() and len(v) <= 128 for v in approvers)
                or len(approvers) > MAX_APPROVERS):
            return "approvers must be a list of at most 20 names"
    if raw.get("risk_class") is not None and raw["risk_class"] not in RISK_CLASSES:
        return "risk_class is not a capability class"
    if "fail_mode" in raw and raw["fail_mode"] not in FAIL_MODES:
        return "fail_mode is not closed or open"
    if raw.get("headline") is not None and headline_args(raw["headline"]) is None:
        return "headline is outside the grammar"
    names = raw.get("identifies")
    if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) and _IDENT_ONLY.match(n) for n in names)):
        return "identifies must be a list of argument names"
    return None



def code_of(raw: Any) -> str:
    """The reason code a worker reports for an unreadable ``raw`` on the hello (WIRE-TR2 §1)."""
    if not isinstance(raw, dict):
        return "row_unparseable"
    if any(k not in FIELDS and k not in SERVED_EXTRAS and k not in LINES_KEYS for k in raw):
        return "unknown_restriction_key"
    return "invalid_body"
