"""Legacy fixtures for per-tool policy rows (R0, TR0-O deliverable E, AC15/AC16): today's decision, frozen.

This is not a pytest test module (no ``test_`` prefix; ``tests/legacy/test_legacy_rows.py`` is the test that
reads it). It is the one place every case is defined, so the capture that produced
``tests/legacy/fixtures/legacy_rows.json`` and the check that guards it can never drift apart. Both call
:func:`run_case` (the ``tests/golden_script.py`` pattern).

WHAT A CASE IS
==============
One served row (or none) folded with one tool's code rules, driven through the **real engine**: a real
:class:`runbound.shared.RemoteState` holds the rows (served by a fake plane client on one ``apply_hello``,
parsed by :mod:`runbound.policy_rows` exactly as on a worker), the real hook chain, :mod:`runbound.quota`
and :mod:`runbound.approvals` decide. Only the plane's HTTP answers and the clocks are fake:

* the link clock is injected into ``RemoteState`` (``connected`` after the hello; ``degraded`` after three
  missed hellos; ``unavailable`` 61 s later, past ``UNAVAILABLE_AFTER_S`` and the 60 s consequential grace);
* the lease clock is a fixed epoch (2030-01-01T00:00:30Z) inside a fixed lease window, so nothing depends on
  the wall clock and nothing sleeps;
* ``control_plane_poll_s=3600`` so ``fail_mode: open``'s static share never rolls over during a case.

Each call records: the decision (``allowed`` | ``refused`` | ``deferred``), the exception class, the reason
code, the detector and rule, the boundary, whether the body ran, the ``would_refuse:*`` tags on its
``tool_result``, the approval request raised to the plane (its mode and timeout) or ``None``, the
``/v1/quota/take`` calls made, and whether a held lease unit was spent.

GROUPS AND IDS
==============
Ids are ``O-LEG-ROW-<n>``, assigned by hand below and never reused: 1-99 every row field alone; 100-199 every
pair of a row field with a code rule (the fold table in ``runbound/policy_rows.py``) plus ``constraint=``,
``require_approval=``/``approval=`` and ``init(capabilities=...)``; 200-299 cases that read the
document-level org policy (``policy`` on ``GET /v1/policy``), tagged ``retires-with-H6`` (register §2, H6);
300+ every concrete row the repo seeds, captured as found with its ``sources`` (``tests/legacy/README.md``).

``same_as`` names a case whose call outcomes must be identical: how a case states that a field the SDK
ignores today (``approvers``, ``headline``, ``identifies``, an unknown key) changes nothing.

REGENERATING
============
Regenerating is a decision to justify in a commit message, never a way to make a red test green::

    python -m tests.legacy.legacy_script --write     # from runbound-sdk/
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import math
import pathlib
import sys
import tempfile
import threading
from typing import Any

import runbound
from runbound import _coverage, api
from runbound.plane_types import HelloReply
from runbound.quota import Leases
from runbound.shared import RemoteState

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES_DIR / "legacy_rows.json"

SERVICE = "legacy-svc"
TOOL = "legacy_tool"
H6 = "retires-with-H6"

#: The lease clock and the one lease window every case lives in (no wall clock anywhere).
FIXED_EPOCH = 1893456030.0  # 2030-01-01T00:00:30Z
LEASE_WINDOW = ("2030-01-01T00:00:00Z", "2030-01-01T00:01:00Z")
CAPABILITY_RANKS = ("read", "write", "external", "financial", "destructive", "privileged")

#: Code predicates, by name, so a case stays plain data.
PREDICATES = {
    "amount_le_100": lambda call: call.kwargs.get("amount", 0) <= 100,
    "never": lambda call: False,
    "always": lambda call: True,
}

#: What the fake plane answers ``POST /v1/quota/take``.
TAKES = {
    "granted": (200, {"granted": True, "retry_after_s": 0}),
    "exhausted": (200, {"granted": False, "retry_after_s": 23}),
    "unknown_quota": (404, {"error": "no_such_quota"}),
    "transport_error": (None, None),
}

#: What the plane fills in for a field a create body leaves out, copied from
#: the plane's policy/tool_rows.py (``DEFAULTS``, ``_quota``: ``per`` defaults to
#: ``service``). Used only to turn a concrete row the repo POSTs at a real plane into the row a worker is served.
PLANE_DEFAULTS: dict = {
    "blocked": False, "max_calls_per_session": None, "quota": None, "approval_required": False,
    "approval_mode": None, "approval_timeout_s": None, "approvers": [], "risk_class": None,
    "fail_mode": "closed", "headline": None, "identifies": [],
}


# --- the fake plane ------------------------------------------------------------------------------------------


class FakePlane:
    """:class:`runbound.plane.PlaneClient`'s methods, in memory: serves one envelope, answers takes and approvals."""

    def __init__(self, envelope: dict, take: str, approve: str) -> None:
        self.envelope, self.take, self.approve = envelope, take, approve
        self.key_state = "unknown"
        self.calls: list = []
        self._lock = threading.Lock()

    def hello(self, payload: dict) -> None:  # pragma: no cover - the harness applies hellos itself
        return None

    def events(self, batch: dict) -> bool:  # pragma: no cover - no exporter
        return True

    def policy(self, service: str) -> dict:
        return copy.deepcopy(self.envelope)

    def approvals(self, method: str, path: str, body: Any = None, timeout: float = 5.0) -> tuple:
        if path.endswith("/report"):  # the background report of an approved run: not a decision, never recorded
            return 200, {}
        with self._lock:
            self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/v1/quota/take":
            return TAKES[self.take]
        if path == "/v1/approvals":
            if self.approve == "denied":
                return 201, {"id": "ap-legacy", "status": "denied", "decision_reason": "no", "quota": None}
            return 201, {"id": "ap-legacy", "status": "approved", "quota": None}
        return 404, {}

    def snapshot(self) -> list:
        with self._lock:
            return list(self.calls)


# --- case builders -------------------------------------------------------------------------------------------


def row(tool: str = TOOL, **fields: Any) -> dict:
    """A served row as the SDK unit tests write it: ``{"tool", "state": "enforced", **fields}``."""
    return {"tool": tool, "state": "enforced", **fields}


def calls(n: int = 1, key: "str | None" = None, amount: int = 0) -> list:
    return [{"key": key, "amount": amount} for _ in range(n)]


def case(n: int, group: str, title: str, *, rows: "list | None" = None, tool: "dict | None" = None,
         init: "dict | None" = None, plane: "dict | None" = None, seq: "list | None" = None,
         tags: "list | None" = None, same_as: "int | None" = None) -> dict:
    spec = {"name": TOOL, "effects": ["read"], "reviewed": True, **(tool or {})}
    if any(k in spec for k in ("blocked", "max_calls", "constraint", "require_approval")) and "reviewed" not in (tool or {}):
        spec["reviewed"] = False  # a tool with a code rule is not "deliberately unrestricted" (blocked + reviewed raises)
    out: dict = {
        "id": f"O-LEG-ROW-{n}", "group": group, "title": title,
        "rows": rows or [], "tool": spec,
        "init": init or {}, "plane": {"link": "connected", "take": "granted", "approve": "approved", **(plane or {})},
        "calls": seq if seq is not None else calls(1),
        "tags": tags or [],
    }
    if same_as is not None:
        out["same_as"] = f"O-LEG-ROW-{same_as}"
    return out


# --- group 1: every row field alone --------------------------------------------------------------------------


def _field_cases() -> list:
    F = "field"
    q = lambda limit=5, window="minute", per="service": {"limit": limit, "window": window, "per": per}  # noqa: E731
    out = [
        case(1, F, "no row: a read tool with no code rule runs", seq=calls(2)),
        # blocked
        case(2, F, "blocked: true refuses with tool_blocked", rows=[row(blocked=True)], seq=calls(2)),
        case(3, F, "blocked: false changes nothing", rows=[row(blocked=False)], seq=calls(2), same_as=1),
        # max_calls_per_session
        case(4, F, "max_calls_per_session 2: the 3rd call in a session is refused",
             rows=[row(max_calls_per_session=2)], seq=calls(3)),
        case(5, F, "max_calls_per_session 2 counts per keyed session",
             rows=[row(max_calls_per_session=2)], seq=calls(3, key="k1") + calls(1, key="k2")),
        case(6, F, "max_calls_per_session 0 is not a cap (parsed as none)",
             rows=[row(max_calls_per_session=0)], seq=calls(3)),
        case(7, F, "max_calls_per_session true (a bool) is not a cap", rows=[row(max_calls_per_session=True)], seq=calls(3)),
    ]
    # quota per service and per key, at each window
    n = 10
    for per in ("service", "key"):
        for window in ("minute", "hour", "day"):
            key = "k1" if per == "key" else None
            out.append(case(n, F, f"quota per {per} per {window}: each call takes a unit (granted)",
                            rows=[row(quota=q(5, window, per))], seq=calls(2, key=key)))
            out.append(case(n + 1, F, f"quota per {per} per {window}: exhausted refuses quota_exceeded, one take per window",
                            rows=[row(quota=q(5, window, per))], plane={"take": "exhausted"}, seq=calls(2, key=key)))
            n += 2
    out += [
        case(22, F, "quota per service: a held lease is spent before any take",
             rows=[row(quota=q())], plane={"leases": 1}, seq=calls(2)),
        case(23, F, "quota per key: never leased, always takes with the key hash",
             rows=[row(quota=q(per="key"))], plane={"leases": 5}, seq=calls(2, key="k1")),
        case(24, F, "quota per key with no session key: nothing is taken and the call runs",
             rows=[row(quota=q(per="key"))], plane={"take": "exhausted"}, seq=calls(2)),
        case(25, F, "quota the plane does not know (404): cannot verify, so the closed fail mode refuses (C23; was: the call runs)",
             rows=[row(quota=q())], plane={"take": "unknown_quota"}, seq=calls(2)),
        case(26, F, "quota take fails in transport on a connected link: quota_plane_unavailable",
             rows=[row(quota=q())], plane={"take": "transport_error"}, seq=calls(1)),
        case(27, F, "quota without an int limit is ignored", rows=[row(quota={"limit": "5", "window": "minute"})], seq=calls(2)),
        # approval_required / approval_mode / approval_timeout_s
        case(30, F, "approval_required: a sync request (600 s) is raised; approved, it runs",
             rows=[row(approval_required=True)], seq=calls(1)),
        case(31, F, "approval_required with approval_mode sync", rows=[row(approval_required=True, approval_mode="sync")],
             seq=calls(1), same_as=30),
        case(32, F, "approval_required with approval_mode deferred: pending_approval, the body does not run",
             rows=[row(approval_required=True, approval_mode="deferred")], seq=calls(1)),
        case(33, F, "approval_required, the person denies: approval_denied",
             rows=[row(approval_required=True)], plane={"approve": "denied"}, seq=calls(1)),
        case(34, F, "approval_required on a degraded link: refused, nothing asked",
             rows=[row(approval_required=True)], plane={"link": "degraded"}, seq=calls(1)),
        case(35, F, "approval_mode deferred without approval_required asks nobody",
             rows=[row(approval_mode="deferred")], seq=calls(2), same_as=1),
        case(36, F, "approval_timeout_s 30 with approval_required: the request carries 30 s",
             rows=[row(approval_required=True, approval_timeout_s=30)], seq=calls(1)),
        case(37, F, "approval_timeout_s 30 alone asks nobody", rows=[row(approval_timeout_s=30)], seq=calls(2), same_as=1),
        case(38, F, "approval_mode not sync/deferred: the tool is refused policy_unsupported (R2 C2; was: falls back to sync)",
             rows=[row(approval_required=True, approval_mode="later")], seq=calls(1)),
    ]
    # risk_class at each rank
    n = 40
    for rank in CAPABILITY_RANKS:
        out.append(case(n, F, f"risk_class {rank} on a read tool, init(capabilities={{{rank!r}: 'deny'}})",
                        rows=[row(risk_class=rank)], init={"capabilities": {rank: "deny"}}, seq=calls(1)))
        out.append(case(n + 1, F, f"risk_class {rank} on a read tool, plane link unavailable",
                        rows=[row(risk_class=rank)], plane={"link": "unavailable"}, seq=calls(1)))
        n += 2
    out += [
        case(52, F, "risk_class destructive, connected, default capabilities: runs",
             rows=[row(risk_class="destructive")], seq=calls(2), same_as=1),
        case(53, F, "risk_class unknown is ignored: the tool's own class (read, denied here) decides", rows=[row(risk_class="scary")], init={"capabilities": {"read": "deny"}},
             seq=calls(1)),
        # fail_mode, with the plane link up and unavailable (and degraded)
        case(55, F, "fail_mode open, quota, connected: takes as usual",
             rows=[row(fail_mode="open", quota=q(20, "hour"))], seq=calls(2)),
        case(56, F, "fail_mode closed, quota, connected: takes as usual",
             rows=[row(fail_mode="closed", quota=q(20, "hour"))], seq=calls(2), same_as=55),
        case(57, F, "fail_mode open, quota 20, link unavailable: a static share (20 // 10 = 2) then quota_plane_unavailable",
             rows=[row(fail_mode="open", quota=q(20, "hour"))], plane={"link": "unavailable"}, seq=calls(3)),
        case(58, F, "fail_mode closed, quota, link unavailable: quota_plane_unavailable",
             rows=[row(fail_mode="closed", quota=q(20, "hour"))], plane={"link": "unavailable"}, seq=calls(2)),
        case(59, F, "fail_mode open, quota 20, link degraded: the static share",
             rows=[row(fail_mode="open", quota=q(20, "hour"))], plane={"link": "degraded"}, seq=calls(3), same_as=57),
        case(60, F, "fail_mode closed, quota, link degraded: quota_plane_unavailable",
             rows=[row(fail_mode="closed", quota=q(20, "hour"))], plane={"link": "degraded"}, seq=calls(2), same_as=58),
        case(61, F, "fail_mode open, no quota, read tool, link unavailable: runs (fail_mode reads nothing else)",
             rows=[row(fail_mode="open")], plane={"link": "unavailable"}, seq=calls(1)),
        case(62, F, "fail_mode closed, no quota, read tool, link unavailable: runs",
             rows=[row(fail_mode="closed")], plane={"link": "unavailable"}, seq=calls(1), same_as=61),
        case(63, F, "fail_mode open, quota take fails in transport: the static share",
             rows=[row(fail_mode="open", quota=q(20, "hour"))], plane={"take": "transport_error"}, seq=calls(3)),
        case(64, F, "fail_mode not open/closed is closed", rows=[row(fail_mode="ajar", quota=q(20, "hour"))],
             plane={"link": "unavailable"}, seq=calls(2)),
        # state
        case(66, F, "state dry_run, blocked: runs, would_refuse:tool_blocked", rows=[row(state="dry_run", blocked=True)],
             seq=calls(1)),
        case(67, F, "state enforced, blocked", rows=[row(state="enforced", blocked=True)], seq=calls(2), same_as=2),
        case(68, F, "state dry_run, max_calls_per_session 1: runs, would_refuse:session_max_calls on the 2nd",
             rows=[row(state="dry_run", max_calls_per_session=1)], seq=calls(2)),
        case(69, F, "state dry_run, quota exhausted: runs, would_refuse:quota_exceeded, a live take is made (C4)",
             rows=[row(state="dry_run", quota=q())], plane={"take": "exhausted"}, seq=calls(2)),
        case(70, F, "state dry_run, approval_required: asks nobody", rows=[row(state="dry_run", approval_required=True)],
             seq=calls(2), same_as=1),
        case(71, F, "state dry_run, risk_class destructive under capabilities destructive deny: ignored",
             rows=[row(state="dry_run", risk_class="destructive")], init={"capabilities": {"destructive": "deny"}},
             seq=calls(2), same_as=1),
        case(72, F, "state draft is not a served state: the row is skipped and the tool runs unruled",
             rows=[row(state="draft", blocked=True)], seq=calls(2)),
        case(73, F, "no state key: enforced", rows=[{"tool": TOOL, "blocked": True}], seq=calls(2), same_as=2),
        # fields the SDK does not read today
        case(75, F, "approvers alone: no change", rows=[row(approvers=["ana@acme.example"])], seq=calls(2), same_as=1),
        case(76, F, "headline alone: no change", rows=[row(headline="Refund at most {amount}")], seq=calls(2)),
        case(77, F, "identifies alone: no change", rows=[row(identifies=["amount"])], seq=calls(2), same_as=1),
        case(78, F, "approvers with approval_required: the request is the same as without them",
             rows=[row(approval_required=True, approvers=["ana@acme.example"])], seq=calls(1), same_as=30),
        case(79, F, "blocked with approvers, headline and identifies: same as blocked alone",
             rows=[row(blocked=True, approvers=["ana@acme.example"], headline="No refunds", identifies=["amount"])],
             seq=calls(2), same_as=2),
        case(80, F, "a row carrying an unknown restriction key: the key is ignored (C9)",
             rows=[row(allowed_hours=[9, 17])], seq=calls(2)),
        case(81, F, "a row for another tool does not touch this one", rows=[row("other_tool", blocked=True)],
             seq=calls(2), same_as=1),
    ]
    return out


# --- group 2: every row field paired with a code rule -----------------------------------------------------------


def _pair_cases() -> list:
    P = "pair"
    q = {"limit": 5, "window": "minute", "per": "service"}
    fin = {"effects": ["financial"], "reviewed": False}
    return [
        # blocked: OR
        case(100, P, "row blocked, code not blocked (row tightens): tool_blocked", rows=[row(blocked=True)], seq=calls(1)),
        case(101, P, "row blocked false, code blocked (row would loosen): the code refuses",
             rows=[row(blocked=False)], tool={"blocked": True}, seq=calls(1)),
        case(102, P, "row blocked, code blocked: the code's refusal comes first", rows=[row(blocked=True)],
             tool={"blocked": True}, seq=calls(1), same_as=101),
        # max_calls: the lower
        case(103, P, "row cap 2, code max_calls 5: the row's cap refuses the 3rd", rows=[row(max_calls_per_session=2)],
             tool={"max_calls": 5}, seq=calls(3)),
        case(104, P, "row cap 9, code max_calls 5 (row would loosen): the code refuses the 6th",
             rows=[row(max_calls_per_session=9)], tool={"max_calls": 5}, seq=calls(6)),
        case(105, P, "row cap 3, code max_calls 3: the code's refusal comes first on the 4th",
             rows=[row(max_calls_per_session=3)], tool={"max_calls": 3}, seq=calls(4)),
        case(106, P, "row cap 99, code max_calls 1: the code refuses the 2nd", rows=[row(max_calls_per_session=99)],
             tool={"max_calls": 1}, seq=calls(2)),
        # approval_required: OR
        case(107, P, "row approval, code none (row tightens): asked", rows=[row(approval_required=True)], seq=calls(1),
             same_as=30),
        case(108, P, "row approval false, code approval required (row would loosen): asked",
             rows=[row(approval_required=False)], tool={"approval": "required"}, seq=calls(1)),
        case(109, P, "row approval and code approval: one request", rows=[row(approval_required=True)],
             tool={"approval": "required"}, seq=calls(1), same_as=108),
        # approval_mode: code wins when it declares one
        case(110, P, "row deferred, code sync: sync", rows=[row(approval_required=True, approval_mode="deferred")],
             tool={"approval": "required", "mode": "sync"}, seq=calls(1)),
        case(111, P, "row sync, code deferred: deferred", rows=[row(approval_required=True, approval_mode="sync")],
             tool={"approval": "required", "mode": "deferred"}, seq=calls(1)),
        case(112, P, "row deferred, code approval with no mode: the code's default (sync) wins",
             rows=[row(approval_required=True, approval_mode="deferred")], tool={"approval": "required"}, seq=calls(1)),
        case(113, P, "row deferred, code silent: the row decides (deferred)",
             rows=[row(approval_required=True, approval_mode="deferred")], seq=calls(1), same_as=32),
        # approval_timeout_s: the lower
        case(114, P, "row timeout 10, code 300: 10", rows=[row(approval_required=True, approval_timeout_s=10)],
             tool={"approval": "required", "timeout_s": 300}, seq=calls(1)),
        case(115, P, "row timeout 300, code 10: 10", rows=[row(approval_required=True, approval_timeout_s=300)],
             tool={"approval": "required", "timeout_s": 10}, seq=calls(1), same_as=114),
        case(116, P, "row no timeout, code 30: 30", rows=[row(approval_required=True)],
             tool={"approval": "required", "timeout_s": 30}, seq=calls(1)),
        case(117, P, "row timeout 30, code default (600): 30", rows=[row(approval_required=True, approval_timeout_s=30)],
             tool={"approval": "required"}, seq=calls(1)),
        case(118, P, "row timeout 30 without approval_required, code approval 300: the row's timeout lowers the wait (R2 C17: was 300)",
             rows=[row(approval_timeout_s=30)], tool={"approval": "required", "timeout_s": 300}, seq=calls(1)),
        # risk_class: the stricter
        case(120, P, "row destructive, code read (row raises), capabilities destructive deny: refused",
             rows=[row(risk_class="destructive")], init={"capabilities": {"destructive": "deny"}}, seq=calls(1)),
        case(121, P, "row read, code financial (row would lower), capabilities financial deny: refused",
             rows=[row(risk_class="read")], tool=fin, init={"capabilities": {"financial": "deny"}}, seq=calls(1)),
        case(122, P, "row write, code financial (row would lower), link unavailable: the plane door refuses",
             rows=[row(risk_class="write")], tool=fin, plane={"link": "unavailable"}, seq=calls(1)),
        case(123, P, "row financial, code read (row raises), link unavailable: the plane door refuses",
             rows=[row(risk_class="financial")], plane={"link": "unavailable"}, seq=calls(1)),
        case(124, P, "row privileged, code financial, capabilities privileged deny: refused",
             rows=[row(risk_class="privileged")], tool=fin, init={"capabilities": {"privileged": "deny"}}, seq=calls(1)),
        case(125, P, "row read, code unclassified (no effects), link unavailable: the plane door refuses",
             rows=[row(risk_class="read")], tool={"effects": None, "reviewed": True}, plane={"link": "unavailable"},
             seq=calls(1)),
        # fail_mode: closed wins (the code's classes fail closed at the plane door)
        case(127, P, "row fail_mode open + quota, code financial, link unavailable: the plane door refuses first",
             rows=[row(fail_mode="open", quota=q)], tool=fin, plane={"link": "unavailable"}, seq=calls(1)),
        case(128, P, "row fail_mode open + quota, code read, link unavailable: the static share",
             rows=[row(fail_mode="open", quota={"limit": 20, "window": "hour", "per": "service"})],
             plane={"link": "unavailable"}, seq=calls(3), same_as=57),
        case(129, P, "row fail_mode open, code approval required, link degraded: refused (approval needs a connected link)",
             rows=[row(fail_mode="open")], tool={"approval": "required"}, plane={"link": "degraded"}, seq=calls(1)),
        # quota with code rules
        case(131, P, "row quota, code blocked: refused by the code, no unit taken", rows=[row(quota=q)],
             tool={"blocked": True}, seq=calls(1)),
        case(132, P, "row quota, code max_calls 1: one take, then the code refuses with no take",
             rows=[row(quota=q)], tool={"max_calls": 1}, seq=calls(2)),
        case(133, P, "row quota, code approval required: asked, no take (the plane takes at create)",
             rows=[row(quota=q)], tool={"approval": "required"}, seq=calls(1)),
        case(134, P, "row quota, code constraint failing: refused, no take", rows=[row(quota=q)],
             tool={"constraint": "amount_le_100"}, seq=calls(1, amount=500)),
        case(135, P, "row quota exhausted, code approval required: asked, no take",
             rows=[row(quota=q)], tool={"approval": "required"}, plane={"take": "exhausted"}, seq=calls(1), same_as=133),
        case(136, P, "row quota + row approval, code silent: asked, no take", rows=[row(quota=q, approval_required=True)],
             seq=calls(1), same_as=30),
        # code constraint=
        case(140, P, "row blocked, code constraint passing: tool_blocked", rows=[row(blocked=True)],
             tool={"constraint": "amount_le_100"}, seq=calls(1, amount=50)),
        case(141, P, "row cap 99, code constraint failing: the constraint refuses", rows=[row(max_calls_per_session=99)],
             tool={"constraint": "amount_le_100"}, seq=calls(1, amount=500)),
        case(142, P, "row approval, code constraint failing: refused, nothing asked", rows=[row(approval_required=True)],
             tool={"constraint": "amount_le_100"}, seq=calls(1, amount=500)),
        case(143, P, "row approval, code constraint passing: asked", rows=[row(approval_required=True)],
             tool={"constraint": "amount_le_100"}, seq=calls(1, amount=50)),
        case(144, P, "row cap 1, code constraint: a refused attempt does not use the row's cap",
             rows=[row(max_calls_per_session=1)], tool={"constraint": "amount_le_100"},
             seq=calls(1, amount=500) + calls(1, amount=50) + calls(1, amount=50)),
        # code require_approval= (a predicate)
        case(146, P, "row approval, code require_approval never: refused by the predicate, nothing asked",
             rows=[row(approval_required=True)], tool={"require_approval": "never"}, seq=calls(1)),
        case(147, P, "row approval, code require_approval always: the row's request is raised",
             rows=[row(approval_required=True)], tool={"require_approval": "always"}, seq=calls(1)),
        case(148, P, "row approval false, code require_approval never: refused by the predicate",
             rows=[row(approval_required=False)], tool={"require_approval": "never"}, seq=calls(1)),
        case(149, P, "row blocked, code require_approval always: tool_blocked", rows=[row(blocked=True)],
             tool={"require_approval": "always"}, seq=calls(1)),
        # init(capabilities=...)
        case(151, P, "row blocked, capabilities deny the tool's class: tool_blocked (rows are judged first)",
             rows=[row(blocked=True)], init={"capabilities": {"read": "deny"}}, seq=calls(1)),
        case(152, P, "row cap 1, capabilities deny the tool's class: the capability refuses every call",
             rows=[row(max_calls_per_session=1)], init={"capabilities": {"read": "deny"}}, seq=calls(2)),
        case(153, P, "row approval, capabilities approve the tool's class: refused (no approval queue), nothing asked",
             rows=[row(approval_required=True)], init={"capabilities": {"read": "approve"}}, seq=calls(1)),
        case(154, P, "row risk_class read, code financial, capabilities financial approve: refused",
             rows=[row(risk_class="read")], tool=fin, init={"capabilities": {"financial": "approve"}}, seq=calls(1)),
        case(155, P, "row quota, capabilities deny the tool's class: refused, no unit taken", rows=[row(quota=q)],
             init={"capabilities": {"read": "deny"}}, seq=calls(1)),
        case(156, P, "row approval, capabilities allow: asked", rows=[row(approval_required=True)],
             init={"capabilities": {"read": "allow"}}, seq=calls(1), same_as=30),
        # dry_run rows next to code rules: the code still governs
        case(158, P, "dry_run row blocked, code blocked: the code refuses", rows=[row(state="dry_run", blocked=True)],
             tool={"blocked": True}, seq=calls(1), same_as=101),
        case(159, P, "dry_run row cap 1, code max_calls 5: runs, would_refuse on the 2nd",
             rows=[row(state="dry_run", max_calls_per_session=1)], tool={"max_calls": 5}, seq=calls(2)),
        case(160, P, "dry_run row approval deferred, code approval sync: the code's request is raised",
             rows=[row(state="dry_run", approval_required=True, approval_mode="deferred", approval_timeout_s=5)],
             tool={"approval": "required", "mode": "sync", "timeout_s": 300}, seq=calls(1)),
    ]


# --- group 3: the document-level org policy (retires with H6) --------------------------------------------------


def _document_cases() -> list:
    D = "document"
    t = [H6]
    return [
        case(200, D, "org policy deny, no row: refused by the policy", plane={"policy": {"deny": [TOOL]}}, tags=t),
        case(201, D, "org policy deny and a blocked row: the policy refuses first",
             rows=[row(blocked=True)], plane={"policy": {"deny": [TOOL]}}, tags=t, same_as=200),
        case(202, D, "org policy max_calls 1 and a row cap 5: the policy refuses the 2nd",
             rows=[row(max_calls_per_session=5)], plane={"policy": {"max_calls": {TOOL: 1}}}, seq=calls(2), tags=t),
        case(203, D, "org policy max_calls 5 and a row cap 1: the row refuses the 2nd",
             rows=[row(max_calls_per_session=1)], plane={"policy": {"max_calls": {TOOL: 5}}}, seq=calls(2), tags=t),
        case(204, D, "dry_run org policy deny and an enforced blocked row: the row refuses",
             rows=[row(blocked=True)], plane={"policy": {"deny": [TOOL]}, "policy_dry_run": True}, tags=t),
        case(205, D, "dry_run org policy deny, no row: runs", plane={"policy": {"deny": [TOOL]}, "policy_dry_run": True},
             tags=t),
        case(206, D, "org policy require_approval (no callback can be served) and no row",
             plane={"policy": {"require_approval": [TOOL]}}, tags=t),
        case(207, D, "org policy allow-list without the tool, a row cap 5: refused by the policy",
             rows=[row(max_calls_per_session=5)], plane={"policy": {"allow": ["other_tool"]}}, tags=t),
        case(208, D, "org policy deny another tool and a row approval: asked",
             rows=[row(approval_required=True)], plane={"policy": {"deny": ["other_tool"]}}, tags=t),
    ]


# --- group 4: every concrete row the repo seeds -------------------------------------------------------------------


def plane_served(tool: str, fields: dict, state: str) -> "dict | None":
    """The row a real plane serves for a create body ``fields`` rolled out to ``state`` (``None``: a draft, not served)."""
    if state not in ("dry_run", "enforced"):
        return None
    served = {"tool": tool, "version": 1, **copy.deepcopy(PLANE_DEFAULTS), **copy.deepcopy(fields), "state": state,
              "orphaned": False}
    quota = served.get("quota")
    if isinstance(quota, dict):
        served["quota"] = {"limit": quota.get("limit"), "window": quota.get("window"), "per": quota.get("per", "service")}
    return served


#: Rows fed straight to the SDK by its own unit tests (``FakeShared``/``policy_rows.parse``), literally.
SDK_FED: list = [
    (row("look", blocked=True), ["tests/test_policy_rows.py:118", "tests/test_policy_rows.py:284",
                                 "tests/test_policy_rows.py:400"]),
    (row("look", state="dry_run", blocked=True), ["tests/test_policy_rows.py:127", "tests/test_spans.py:409"]),
    (row("look", max_calls_per_session=2), ["tests/test_policy_rows.py:137"]),
    (row("search", max_calls_per_session=9), ["tests/test_policy_rows.py:137"]),
    (row("look", blocked=True, max_calls_per_session=1, quota={"limit": 5, "window": "minute", "per": "service"}),
     ["tests/test_policy_rows.py:150"]),
    (row("pay", approval_required=False, blocked=False, max_calls_per_session=99), ["tests/test_policy_rows.py:161"]),
    (row("look", approval_required=True, approval_timeout_s=30), ["tests/test_policy_rows.py:179"]),
    (row("look", approval_required=True, approval_mode="deferred"), ["tests/test_policy_rows.py:187"]),
    (row("when", approval_required=True, approval_mode="deferred"), ["tests/test_policy_rows.py:196"]),
    (row("pay", approval_required=True, approval_mode="deferred", approval_timeout_s=10), ["tests/test_policy_rows.py:206"]),
    (row("look", state="dry_run", approval_required=True), ["tests/test_policy_rows.py:218"]),
    (row("look", risk_class="destructive"), ["tests/test_policy_rows.py:227"]),
    (row("search", risk_class="read"), ["tests/test_policy_rows.py:227"]),
    (row("t", risk_class="read"), ["tests/test_policy_rows.py:234"]),
    (row("t", risk_class="privileged"), ["tests/test_policy_rows.py:235"]),
    (row("a", fail_mode="open"), ["tests/test_policy_rows.py:239"]),
    (row("b"), ["tests/test_policy_rows.py:239"]),
    ({"tool": 5}, ["tests/test_policy_rows.py:239"]),
    ("junk", ["tests/test_policy_rows.py:239"]),
    (row("c", state="draft"), ["tests/test_policy_rows.py:239"]),
    (row("d", max_calls_per_session=0), ["tests/test_policy_rows.py:240"]),
    (row("b", blocked=True), ["tests/test_policy_rows.py:249"]),
    (row("a"), ["tests/test_policy_rows.py:249"]),
    (row("look", quota={"limit": 5, "window": "minute", "per": "service"}),
     ["tests/test_policy_rows.py:%d" % n for n in (369, 379, 391, 419, 456, 462, 500)]),
    (row("search", state="enforced"), ["tests/test_policy_rows.py:400"]),
    (row("look", quota={"limit": 5, "window": "hour", "per": "key"}), ["tests/test_policy_rows.py:408"]),
    (row("look", quota={"limit": 40, "window": "hour", "per": "service"}, fail_mode="open"),
     ["tests/test_policy_rows.py:430"]),
    (row("look", quota={"limit": 5, "window": "minute", "per": "service"}, approval_required=True),
     ["tests/test_policy_rows.py:441", "tests/test_policy_rows.py:489"]),
    (row("look", state="dry_run", quota={"limit": 5, "window": "minute", "per": "service"}),
     ["tests/test_policy_rows.py:448"]),
    (row("look", state="dry_run", quota={"limit": 5, "window": "minute", "per": "service"}, approval_required=True),
     ["tests/test_policy_rows.py:515"]),
    (row("hold", state="enforced"), ["tests/test_spans.py:409"]),
]

#: Rows the repo writes at a real plane (``POST /v1/admin/tool-policies`` then a rollout): (tool, create body
#: fields, served state or "draft", sources, note). The worker is served :func:`plane_served` of it.
PLANE_SEEDED: list = [
    # the SDK's real-plane suite
    ("delete_customer", {"blocked": True}, "enforced", ["tests/integration/test_rows_quota_plane.py:56"], ""),
    ("export_report", {"blocked": True}, "dry_run", ["tests/integration/test_rows_quota_plane.py:57"], ""),
    ("look", {"max_calls_per_session": 5}, "enforced", ["tests/integration/test_rows_quota_plane.py:74"], ""),
    ("other", {"blocked": True}, "draft", ["tests/integration/test_rows_quota_plane.py:83"], "created, never rolled out"),
    ("burst", {"quota": {"limit": 20, "window": "minute", "per": "service"}}, "enforced",
     ["tests/integration/test_rows_quota_plane.py:97"], ""),
    ("look", {"quota": {"limit": 20, "window": "hour", "per": "service"}}, "enforced",
     ["tests/integration/test_rows_quota_plane.py:140"], ""),
    ("wire", {"quota": {"limit": 4, "window": "hour", "per": "service"}, "approval_required": True, "approval_timeout_s": 60},
     "enforced", ["tests/integration/test_rows_quota_plane.py:166"], ""),
    ("wire", {"quota": {"limit": 1, "window": "hour", "per": "service"}}, "enforced",
     ["tests/integration/test_rows_quota_plane.py:196"], ""),
    ("wire", {"approval_required": True, "approval_mode": "sync", "approval_timeout_s": 60}, "enforced",
     ["tests/integration/test_row_waits_off_loop.py:64"], ""),
    ("lookup", {"quota": {"limit": 100, "window": "hour", "per": "key"}}, "enforced",
     ["tests/integration/test_row_waits_off_loop.py:86"], ""),
    ("issue_credit", {"max_calls_per_session": 1}, "enforced",
     ["tests/integration/test_examples_plane.py:62", "examples/README.md:31"], "examples/tool_rules.py's console row"),
    # the demo fleet (the A7 show and its scorecard beat)
    ("refund", {"quota": {"limit": 3, "window": "minute", "per": "service"}}, "dry_run",
     ["demo fleet: a7_beats.py:199", "demo fleet: a7_beats.py:202"], "A7 beat_rules, first rolled out dry_run"),
    ("refund", {"quota": {"limit": 3, "window": "minute", "per": "service"}}, "enforced",
     ["demo fleet: a7_beats.py:199", "demo fleet: a7_beats.py:222"], "A7 beat_rules, then promoted"),
    # the chaos harness
    ("refund", {"quota": {"limit": 100, "window": "minute", "per": "service"}}, "enforced",
     ["chaos harness: test_scenarios.py:25", "chaos harness: test_scenarios.py:104"], "_quota_row default limit"),
    ("refund", {"quota": {"limit": 1000, "window": "minute", "per": "service"}}, "enforced",
     ["chaos harness: test_scenarios.py:25", "chaos harness: test_scenarios.py:57"], "_quota_row(limit=1000)"),
    # the cross-tree fold check
    ("refund", {"max_calls_per_session": 3}, "draft", ["cross-tree suite: test_fold_matches_plane.py:811"], "created, never rolled out"),
    ("export", {"blocked": True}, "draft", ["cross-tree suite: test_fold_matches_plane.py:816"], "created through the dash backend, never rolled out"),
]

_TP = "plane suite: test_tool_policies.py:"
_QT = "plane suite: test_quota.py:"
_A2 = "plane suite: test_a2b_claims.py:"
_PR = "plane suite: test_propagation.py:"
_GR = "plane suite: test_gateway_read.py:"
_RC = "plane suite: test_approval_alert_risk_class.py:"
_DR = "console backend suite: test_tool_policies_read.py:"
_DT = "console backend suite: test_traces.py:"
_DC = "console backend suite: test_console_reads.py:"
_E2 = "console e2e: "


def _src(prefix: str, *lines: int) -> list:
    return [f"{prefix}{n}" for n in lines]


def _qh(limit: int, window: str = "hour", per: "str | None" = None) -> dict:
    out = {"quota": {"limit": limit, "window": window}}
    if per is not None:
        out["quota"]["per"] = per
    return out


#: Rows the plane, dash-backend and dashboard-e2e suites write at a plane or seed into its ``tool_policies`` table,
#: as their final served state ("draft"/"deleted": not served). A row edited by PATCH is captured as it ends up;
#: a table row seeded with the helper's defaults is written with the defaults elided (``plane_served`` restores them).
#: Merged by (tool, fields, state): one case per distinct row, every source listed. Append only: ids follow this order.
PLANE_SUITES: list = [
    ("refund", {"blocked": False, "max_calls_per_session": 5}, "enforced", _src(_TP, 70, 72, 74), "make, dry_run then enforced"),
    ("refund", {}, "enforced", _src(_TP, 84, 86, 171, 172, 443, 452), "rollout of an empty row"),
    ("refund", {"max_calls_per_session": 6}, "deleted", _src(_TP, 97, 98, 100), "draft, PATCHed, deleted"),
    ("refund", {"blocked": False}, "draft", _src(_TP, 107, 108, 111), "PATCHed blocked true then false, still a draft"),
    ("refund", {}, "draft", _src(_TP, 119, 179, 284, 306) + ["plane suite: test_tool_policy_version_atomic.py:129",
                                                             "plane suite: test_tool_policies_migration.py:78"], ""),
    ("a_draft", {}, "draft", _src(_TP, 128), ""),
    ("a_dry", {"blocked": True}, "dry_run", _src(_TP, 129, 131), ""),
    ("an_enforced", {"max_calls_per_session": 3}, "enforced", _src(_TP, 130, 132), ""),
    ("refund", {"max_calls_per_session": 3}, "enforced", _src(_TP, 145, 146, 152, 157), "PATCHed 3 -> 4 -> 3"),
    ("unserved_draft", {"blocked": False}, "draft", _src(_TP, 149, 150), ""),
    ("refund", {"blocked": True}, "dry_run", _src(_TP, 162, 163, 330), "then rolled out enforced"),
    ("refund", {"blocked": True}, "enforced",
     _src(_TP, 165, 199, 200, 345) + _src(_PR, 45) + ["plane suite: test_rehydrate.py:105",
                                                      "plane suite: test_rehydrate.py:107"]
     + _src(_GR, 115, 129) + [_E2 + "console.ts:24", _E2 + "console.ts:26", _E2 + "stack/traces.spec.ts:58",
                              _E2 + "stack/traces.spec.ts:59"] + _src(_DR, 122, 181, 233) + _src(_DT, 184)
     + ["cross-tree suite: test_fold_matches_plane.py:842"],
     "created enforced at test_fold_matches_plane.py:842; test_rehydrate populate() is also called by test_halt_window"),
    ("gone_tool", {"blocked": True}, "enforced", _src(_TP, 184, 185), "flagged orphaned by the plane"),
    ("refund", {"quota": {"limit": 60, "window": "minute"}}, "dry_run", _src(_TP, 215), "created dry_run"),
    ("refund", {"quota": {"limit": 60, "window": "minute"}}, "enforced", _src(_TP, 215, 217), ""),
    ("refund", {"quota": {"limit": 40, "window": "minute"}}, "enforced", _src(_TP, 218), "PATCHed quota 60 -> 40"),
    ("export_report", {"max_calls_per_session": 5}, "enforced", _src(_TP, 216) + _src(_DR, 96) + _src(_DT, 151), "deleted at :219"),
    ("delete_customer", {"blocked": True}, "draft", _src(_TP, 220), ""),
    ("refund", {"max_calls_per_session": 2}, "draft", _src(_TP, 236, 237), "PATCHed 1 -> 2"),
    ("refund", {"headline": "Refund {args.order_id} ({args.amount})", "identifies": ["order_id"]}, "draft", _src(_TP, 299), ""),
    ("zeta", {"blocked": True}, "enforced", _src(_TP, 392), ""),
    ("alpha", {"quota": {"limit": 3, "window": "day"}, "headline": "Do {args.x}", "identifies": ["x"]}, "dry_run", _src(_TP, 393), ""),
    ("refund", {"blocked": True, "max_calls_per_session": 3}, "enforced", _src(_TP, 409, 411, 412), ""),
    ("refund", {"max_calls_per_session": 4}, "enforced", _src(_TP, 424, 425, 427, 429, 433), ""),
    ("export", {}, "dry_run", _src(_TP, 451), ""),
    # test_quota.py (_row_with_quota: a draft, then rolled out; quota default {"limit": 3, "window": "hour"})
    ("refund", _qh(3), "enforced", _src(_QT, 184, 236), ""),
    ("refund", _qh(3), "dry_run", _src(_QT, 198), ""),
    ("refund", _qh(1, per="key"), "enforced", _src(_QT, 206, 394), ""),
    ("no_quota", {"blocked": True}, "enforced", _src(_QT, 215, 216), ""),
    ("drafted", _qh(5), "draft", _src(_QT, 217), ""),
    ("refund", _qh(5), "enforced", _src(_QT, 226, 229) + _src(_A2, 213), "test_quota: PATCHed 1 -> 5"),
    ("refund", _qh(20), "enforced", _src(_QT, 273), ""),
    ("refund", _qh(12, "day"), "enforced", _src(_QT, 288), ""),
    ("keyed", _qh(5, per="key"), "enforced", _src(_QT, 299), ""),
    ("plain", {"blocked": True}, "enforced", _src(_QT, 300, 301), ""),
    ("refund", _qh(5, "minute"), "enforced", _src(_QT, 309), ""),
    ("refund", {**_qh(2), "approval_required": True}, "enforced", _src(_QT, 335) + _src(_A2, 231), ""),
    ("refund", _qh(2), "enforced", _src(_QT, 349), ""),
    ("refund", {**_qh(1), "approval_required": True}, "enforced", _src(_QT, 357, 442), ""),
    ("refund", _qh(1), "enforced", _src(_QT, 368, 377, 405, 428, 471) + _src(_A2, 181), ""),
    ("dry", _qh(1), "dry_run", _src(_QT, 387), ""),
    ("refund", _qh(4), "enforced", _src(_QT, 455, 489, 499, 512), ""),
    ("refund", _qh(8), "enforced", _src(_QT, 520) + _src(_A2, 243), ""),
    ("refund", {**_qh(8), "approval_required": True}, "enforced", _src(_QT, 538, 542), "PATCHed approval_required true"),
    ("export", _qh(8, "day"), "enforced", _src(_A2, 242), ""),
    # test_propagation.py, test_halt_window.py
    ("unserved_draft", {"blocked": True}, "draft", _src(_PR, 111), ""),
    ("export", {"blocked": True}, "enforced", _src(_PR, 218), ""),
    ("only_a_draft", {"blocked": True}, "draft", _src(_PR, 245), ""),
    ("fresh", {"blocked": True}, "enforced", _src(_PR, 248), ""),
    ("export", _qh(5, per="service"), "enforced",
     ["plane suite: test_halt_window.py:25", "plane suite: test_halt_window.py:96"], ""),
    # test_gateway_read.py (tool_rows.create / rollout directly)
    ("drafted", {"blocked": True}, "draft", _src(_GR, 82), ""),
    ("rehearsal", {"blocked": True}, "dry_run", _src(_GR, 83), ""),
    ("refund", {"max_calls_per_session": 3, "quota": {"limit": 20, "window": "hour"}, "approval_required": True,
                "approval_mode": "sync", "risk_class": "financial", "headline": "Refund {args.order_id}"}, "enforced", _src(_GR, 86), ""),
    ("a", {"blocked": True, "max_calls_per_session": 2}, "enforced", _src(_GR, 100, 104), ""),
    ("b", {"blocked": True}, "enforced", _src(_GR, 101), ""),
    # test_approval_alert_risk_class.py (drafts, and one dry_run)
    ("delete_customer", {"risk_class": "destructive"}, "draft", _src(_RC, 72), ""),
    ("delete_customer", {}, "draft", _src(_RC, 101), ""),
    ("delete_customer", {"risk_class": "financial"}, "deleted", _src(_RC, 110, 111), ""),
    ("delete_customer", {"risk_class": "privileged"}, "draft", _src(_RC, 120), ""),
    ("delete_customer", {"risk_class": "external"}, "draft", _src(_RC, 128, 130), "parametrised state=draft"),
    ("delete_customer", {"risk_class": "external"}, "dry_run", _src(_RC, 128, 130), "parametrised state=dry_run"),
    # drafts with all defaults written only to exercise the version counter or the audit trail
    ("tool0", {}, "draft", ["plane suite: test_audit_principal.py:73"], "tool{n}, n = 0, 1, ..."),
    ("tool-0-0", {}, "draft", ["plane suite: test_tool_policy_version_atomic.py:80",
                               "plane suite: test_tool_policy_version_atomic.py:104"], "tool-{round}-{i}, 48 rows"),
    ("a", {}, "draft", ["plane suite: test_tool_policy_version_atomic.py:116",
                        "plane suite: test_tool_policy_version_atomic.py:138"], "also b, c, d, e"),
    ("export", {}, "draft", ["plane suite: test_tool_policies_migration.py:80"], "table insert"),
    # the console backend suite: rows seeded into the plane's tool_policies table (helper defaults: state enforced)
    ("refund", {"max_calls_per_session": 5}, "enforced", _src(_DR, 38) + _src(_DT, 151), ""),
    ("refund", {"max_calls_per_session": 3}, "enforced", _src(_DR, 39) + _src(_DT, 151), ""),
    ("export", {"blocked": True}, "enforced", _src(_DR, 40), ""),
    ("export", {}, "deleted", _src(_DR, 41), ""),
    ("elsewhere", {}, "enforced", _src(_DR, 42), "org2"),
    ("billing_tool", {}, "enforced", _src(_DR, 43), ""),
    ("live", {}, "enforced", _src(_DR, 57), "also gone, waiting"),
    ("refund", {}, "dry_run", _src(_DR, 74, 245) + _src(_DC, 60), ""),
    ("export", {}, "enforced", _src(_DR, 74), ""),
    ("refund", {"quota": {"limit": 60, "window": "minute", "per": "service"}}, "dry_run", _src(_DR, 95), ""),
    ("refund", {"quota": {"limit": 60, "window": "minute", "per": "service"}}, "enforced", _src(_DR, 97), ""),
    ("refund", {"quota": {"limit": 40, "window": "minute", "per": "service"}}, "enforced", _src(_DR, 98), ""),
    ("export_report", {"max_calls_per_session": 5}, "deleted", _src(_DR, 99), ""),
    ("delete_customer", {"blocked": True}, "enforced", _src(_DR, 100), ""),
    ("refund", {}, "enforced", _src(_DR, 121, 246) + _src(_DC, 60), ""),
    ("a_draft", {"blocked": True}, "draft", _src(_DR, 182), ""),
    ("refund", {"max_calls_per_session": 1, "approval_required": True}, "enforced", _src(_DT, 151), ""),
    ("other", {}, "enforced", _src(_DT, 152), ""),
    ("lookup", {}, "enforced", _src(_DC, 60), ""),
    ("export", {}, "dry_run", _src(_DC, 60), ""),
    ("wipe", {}, "enforced", _src(_DC, 61), ""),
    ("refund", {"approval_required": True, "approval_mode": "sync", "approval_timeout_s": 600, "approvers": ["@oncall"],
                "risk_class": "financial", "identifies": ["order_id", "amount"]}, "enforced",
     ["console backend suite: test_approvals_read.py:55"], ""),
    # the console e2e suite (a real stack)
    ("refund", {"blocked": False, "max_calls_per_session": 50}, "enforced", [_E2 + "stack/traces.spec.ts:63"], "PATCHed after the blocked row"),
    ("refund", {"quota": {"limit": 3, "window": "minute", "per": "service"}, "headline": "Refund {args.amount} to {args.customer_id}",
                "blocked": False, "max_calls_per_session": None, "approval_required": False, "approval_mode": None,
                "approval_timeout_s": None, "approvers": [], "risk_class": None, "fail_mode": "closed", "identifies": []},
     "dry_run", [_E2 + "stack/tools-rules.spec.ts:60", _E2 + "stack/tools-rules.spec.ts:71"], "the console editor's body"),
    ("refund", {"quota": {"limit": 3, "window": "minute", "per": "service"}, "headline": "Refund {args.amount} to {args.customer_id}",
                "blocked": False, "max_calls_per_session": None, "approval_required": False, "approval_mode": None,
                "approval_timeout_s": None, "approvers": [], "risk_class": None, "fail_mode": "closed", "identifies": []},
     "enforced", [_E2 + "stack/tools-rules.spec.ts:60", _E2 + "stack/tools-rules.spec.ts:94"], "then promoted"),
]

#: The first 18 plane-rejected bodies (O-LEG-ROW-600..617) moved to the contract suite in R2; the last three name an argument the tool does
#: not declare, which the SDK does not check (it is display, not authorisation), so they keep their legacy outcome (618..620).
MOVED_TO_CONTRACT = 18

#: Bodies the plane REFUSES (400) in its own suite, so no worker is ever served them. Fed to the SDK as if a plane had
#: served them enforced, to freeze how tolerant the SDK's parse is (register C2/C9; O-FLR-2/3).
PLANE_REJECTED: list = [
    ({"quota": {"limit": 0, "window": "hour"}}, _TP + "253"),
    ({"quota": {"limit": 5, "window": "week"}}, _TP + "254"),
    ({"quota": {"limit": 5, "window": "hour", "per": "planet"}}, _TP + "255"),
    ({"quota": {"limit": 5, "window": "hour", "burst": 9}}, _TP + "256"),
    ({"quota": 5}, _TP + "257"),
    ({"max_calls_per_session": 0}, _TP + "258"),
    ({"max_calls_per_session": "five"}, _TP + "259"),
    ({"blocked": "yes"}, _TP + "260"),
    ({"approval_mode": "later"}, _TP + "261"),
    ({"approval_timeout_s": 0}, _TP + "262"),
    ({"risk_class": "scary"}, _TP + "263"),
    ({"fail_mode": "ajar"}, _TP + "264"),
    ({"approvers": "ana"}, _TP + "265"),
    ({"approvers": [""]}, _TP + "266"),
    ({"identifies": ["_private"]}, _TP + "267"),
    ({"state": "superseded"}, _TP + "268"),
    ({"nonsense": 1}, _TP + "269"),
    ({"callback": "def f(): pass"}, _TP + "270"),
    ({"headline": "Refund {args.order}"}, _TP + "297"),
    ({"identifies": ["customer"]}, _TP + "298"),
    ({"quota": {"limit": 3, "window": "minute", "per": "service"}, "headline": "Refund {args.bogus}"},
     _E2 + "stack/tools-rules.spec.ts:55"),
]

#: Rows in served shape a plane test hands straight to the plane's quota service (not through the admin API).
PLANE_LITERAL: list = [
    ({"tool": "refund", "state": "enforced", "quota": {"limit": 2, "window": "hour", "per": "service"}}, [_QT + "148"]),
    ({"tool": "refund", "state": "enforced", "quota": {"limit": 3, "window": "hour", "per": "service"}}, [_QT + "162"]),
]


def _merged(entries: list) -> list:
    """Group identical (tool, fields, state) entries: one case each, every source and note kept, in first-seen order."""
    out: dict = {}
    for tool, fields, state, sources, note in entries:
        key = json.dumps([tool, fields, state], sort_keys=True)
        if key not in out:
            out[key] = [tool, fields, state, [], []]
        out[key][3].extend(s for s in sources if s not in out[key][3])
        if note:
            out[key][4].append(note)
    return [(t, f, s, src, "; ".join(n)) for t, f, s, src, n in out.values()]


#: Document policies the demo fleet writes at the plane (``POST /v1/admin/policies``): (body, state, tool probed, sources).
DEMO_DOCUMENTS: list = [
    ({"deny": ["transfer_call"]}, "enforced", "transfer_call",
     ["demo fleet: fleet_verify.py:931", "demo fleet: fleet_verify.py:2772"]),
    ({"deny": []}, "enforced", "transfer_call",
     ["demo fleet: fleet_verify.py:939", "demo fleet: fleet_verify.py:2796"]),
    ({"deny": ["send_email"]}, "dry_run", "send_email", ["demo fleet: fleet_verify.py:985"]),
    ({"deny": ["send_email"]}, "enforced", "send_email",
     ["demo fleet: fleet_verify.py:985", "demo fleet: fleet_verify.py:1012"]),
    ({"deny": ["email_report"]}, "enforced", "email_report", ["demo fleet: fleet_verify.py:2989"]),
    ({"deny": []}, "enforced", "email_report", ["demo fleet: fleet_verify.py:3021"]),
    ({"deny": ["page_oncall"]}, "enforced", "page_oncall", ["demo fleet: fleet_verify.py:3247"]),
    ({"deny": []}, "enforced", "page_oncall", ["demo fleet: fleet_verify.py:3280"]),
]

#: Every concrete row is probed the same small way: a read tool of the row's name (``reviewed=True``), three calls
#: in one keyed session on a connected link (takes granted, approvals approved), then one call with the link unavailable.
PROBES: dict = {
    "connected": {"plane": {"link": "connected"}, "calls": calls(3, key="k1")},
    "unavailable": {"plane": {"link": "unavailable"}, "calls": calls(1, key="k1")},
}


def _probe_tool(literal: Any) -> str:
    name = literal.get("tool") if isinstance(literal, dict) else None
    return name if isinstance(name, str) else TOOL


def _concrete_cases() -> list:
    out: list = []
    n = 300
    for literal, sources in SDK_FED:
        out.append({"id": f"O-LEG-ROW-{n}", "group": "concrete", "title": "a row the SDK unit tests feed straight in",
                    "fed": "sdk", "sources": sources, "literal": literal, "served": literal,
                    "tool": {"name": _probe_tool(literal), "effects": ["read"], "reviewed": True}, "tags": []})
        n += 1
    n = 400
    for tool, fields, state, sources, note in _merged(PLANE_SEEDED + PLANE_SUITES):
        out.append({"id": f"O-LEG-ROW-{n}", "group": "concrete", "title": f"a row seeded at a plane ({state})",
                    "fed": "plane", "sources": sources, "note": note,
                    "literal": {"tool": tool, **fields}, "rollout": state, "served": plane_served(tool, fields, state),
                    "tool": {"name": tool, "effects": ["read"], "reviewed": True}, "tags": []})
        n += 1
    assert n <= 600, "the plane-seeded range ran into the next one"
    n = 600
    for index, (fields, source) in enumerate(PLANE_REJECTED):
        if index < MOVED_TO_CONTRACT:  # O-LEG-ROW-600..617: the SDK now refuses these tools (R2 C19), see tests/contract/test_plane_rejected_rows.py
            n += 1
            continue
        literal = {"tool": "refund", **fields}
        out.append({"id": f"O-LEG-ROW-{n}", "group": "concrete",
                    "title": "a body the plane refuses (400), fed to the SDK as if served enforced",
                    "fed": "hypothetical", "sources": [source], "literal": literal,
                    "served": {"tool": "refund", "state": "enforced", **fields},
                    "tool": {"name": "refund", "effects": ["read"], "reviewed": True}, "tags": ["plane-rejects"]})
        n += 1
    n = 650
    for literal, sources in PLANE_LITERAL:
        out.append({"id": f"O-LEG-ROW-{n}", "group": "concrete", "title": "a served-shape row a plane test uses directly",
                    "fed": "sdk", "sources": sources, "literal": literal, "served": literal,
                    "tool": {"name": literal["tool"], "effects": ["read"], "reviewed": True}, "tags": []})
        n += 1
    n = 260
    for body, state, tool, sources in DEMO_DOCUMENTS:
        out.append({"id": f"O-LEG-ROW-{n}", "group": "document", "title": f"a demo fleet document policy ({state})",
                    "fed": "plane_document", "sources": sources, "literal": body, "rollout": state, "served": None,
                    "policy": body, "policy_dry_run": state == "dry_run",
                    "tool": {"name": tool, "effects": ["read"], "reviewed": True}, "tags": [H6]})
        n += 1
    return out


def cases() -> list:
    """Every case, in id order. Ids are fixed above; a duplicate is a bug in this file."""
    every = _field_cases() + _pair_cases() + _document_cases() + _concrete_cases()
    ids = [c["id"] for c in every]
    assert len(ids) == len(set(ids)), "duplicate O-LEG-ROW id"
    return sorted(every, key=lambda c: int(c["id"].rsplit("-", 1)[1]))


# --- running one case -------------------------------------------------------------------------------------------


def _decorate(spec: dict, ran: list) -> Any:
    kwargs: dict = {}
    for name in ("blocked", "max_calls", "reviewed", "approval", "mode", "timeout_s"):
        if name in spec:
            kwargs[name] = spec[name]
    if spec.get("effects") is not None:
        kwargs["effects"] = set(spec["effects"])
    if "constraint" in spec:
        kwargs["constraint"] = PREDICATES[spec["constraint"]]
    if "require_approval" in spec:
        kwargs["require_approval"] = PREDICATES[spec["require_approval"]]

    def body(amount: int = 0) -> str:
        ran.append(amount)
        return "ok"

    return runbound.tool(name=spec["name"], **kwargs)(body)


def _lease(tool: str, granted: int) -> dict:
    return {"tool": tool, "state": "enforced", "limit": 100, "window": "minute", "window_start": LEASE_WINDOW[0],
            "window_end": LEASE_WINDOW[1], "granted": granted, "remaining": 100 - granted}


def _refusal(exc: BaseException) -> dict:
    anomaly = getattr(exc, "anomaly", None)
    details = getattr(anomaly, "details", None) or {}
    decision = getattr(exc, "decision", None)
    retry_after = getattr(exc, "retry_after", None)
    return {
        "decision": "refused",
        "exception": type(exc).__name__,
        "reason": getattr(exc, "reason", None),
        "detector": getattr(anomaly, "detector", None),
        "rule": details.get("rule"),
        "boundary": getattr(decision, "boundary", None),
        "retryable": getattr(exc, "retryable", None),
        "retry_after_ceil_s": None if retry_after is None else math.ceil(retry_after),
    }


def _allowed(result: Any) -> dict:
    deferred = isinstance(result, dict) and result.get("status") == "pending_approval"
    return {"decision": "deferred" if deferred else "allowed", "exception": None, "reason": None, "detector": None,
            "rule": None, "boundary": None, "retryable": None, "retry_after_ceil_s": None}


def _run(spec: dict, rows: list, plane_spec: dict, seq: list, init: dict) -> dict:
    """One fresh engine, one served row set, one sequence of calls; returns what each call did."""
    api._teardown_for_tests()
    _coverage.reset_for_tests()
    try:
        with tempfile.TemporaryDirectory(prefix="rb-legacy-") as cache_dir:
            runbound.init(service=SERVICE, control_plane_poll_s=3600, cache_dir=cache_dir, **init)
            envelope = {"version": 1, "policy": plane_spec.get("policy"), "dry_run": bool(plane_spec.get("policy_dry_run")),
                        "rows": copy.deepcopy(rows), "rows_hash": "legacy"}
            plane = FakePlane(envelope, plane_spec.get("take", "granted"), plane_spec.get("approve", "approved"))
            clock = [1000.0]
            shared = RemoteState(plane, None, api._ENGINE.config, now=lambda: clock[0])
            shared.quota = Leases(now=lambda: FIXED_EPOCH)
            api._ENGINE.shared = shared
            api._SHARED = shared
            leases = [_lease(spec["name"], plane_spec["leases"])] if plane_spec.get("leases") else []
            shared.apply_hello(HelloReply(policy_version=1, quota_leases=leases))
            link = plane_spec.get("link", "connected")
            if link == "degraded":
                for _ in range(3):
                    shared.record_miss()
            elif link == "unavailable":
                clock[0] += 61.0
            held = shared.row_for(spec["name"])
            ran: list = []
            fn = _decorate(spec, ran)
            outcomes = []
            for step in seq:
                before_calls, before_results = len(plane.snapshot()), len(runbound.tool_results(1000))
                before_lease, before_ran = shared.quota.report().get(spec["name"], 0), len(ran)
                try:
                    if step["key"] is None:
                        outcome = _allowed(fn(amount=step["amount"]))
                    else:
                        with runbound.session(step["key"]):
                            outcome = _allowed(fn(amount=step["amount"]))
                except Exception as exc:  # every refusal is an exception: that is what is recorded
                    outcome = _refusal(exc)
                made = plane.snapshot()[before_calls:]
                results = runbound.tool_results(1000)[before_results:]
                asks = [b for _m, p, b in made if p == "/v1/approvals"]
                takes = [b for _m, p, b in made if p == "/v1/quota/take"]
                outcome.update(
                    ran=len(ran) > before_ran,
                    would_refuse=sorted(t for r in results for t in (r.get("tags") or []) if str(t).startswith("would_refuse")),
                    approval_raised=[{"mode": b["mode"], "timeout_s": b["timeout_s"], "keyed": "key" in b} for b in asks],
                    quota_takes=[{"keyed": "key" in b} for b in takes],
                    lease_spent=shared.quota.report().get(spec["name"], 0) < before_lease,
                )
                outcomes.append(outcome)
            return {
                "link": shared.link_state(),
                "row_held": None if held is None else _held_row(held),
                "calls": outcomes,
            }
    finally:
        api._teardown_for_tests()
        _coverage.reset_for_tests()


def _description(c: dict) -> dict:
    """The case's inputs, as stored beside its outcome in the fixture (so a changed input is a changed fixture)."""
    return {k: v for k, v in c.items() if k != "id"}


#: The fields of a held row the fixtures record. Row fields added later (R2: ``headline``, ``identifies``, ``unsupported``) are
#: not in them: the fixtures freeze what the SDK did, and a field the fixtures never held cannot move.
HELD_ROW_FIELDS = ("tool", "state", "blocked", "max_calls", "quota", "approval_required", "approval_mode", "approval_timeout_s",
                   "risk_class", "fail_mode")


def _held_row(row) -> dict:
    return {name: getattr(row, name) for name in HELD_ROW_FIELDS}


def run_case(case_id: str) -> dict:
    """Run one case from scratch and return ``{"input": ..., "outcome": ...}`` (concrete rows: one outcome per probe)."""
    (c,) = [c for c in cases() if c["id"] == case_id]
    if "served" in c:  # a concrete row: the small set of probes
        rows = [] if c["served"] is None else [c["served"]]
        probes = {}
        for name, probe in PROBES.items():
            plane = dict(probe["plane"])
            if c.get("policy") is not None:
                plane.update(policy=c["policy"], policy_dry_run=c["policy_dry_run"])
            probes[name] = _run(c["tool"], rows, plane, probe["calls"], {})
        return {"input": _description(c), "outcome": probes}
    return {"input": _description(c), "outcome": _run(c["tool"], c["rows"], c["plane"], c["calls"], c["init"])}


def run_scenario() -> dict:
    """Every case, keyed by id."""
    return {c["id"]: run_case(c["id"]) for c in cases()}


# --- capture entry point ------------------------------------------------------------------------------------------


def main(argv: "list | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.legacy.legacy_script",
        description="Legacy row fixtures. Regenerating is a decision to justify in a commit message, never a way to make a red test green.",
    )
    parser.add_argument("--write", action="store_true", help="Rewrite the fixture from today's engine.")
    parser.add_argument("--print", dest="show", action="store_true", help="Print the scenario's JSON to stdout.")
    args = parser.parse_args(argv)
    if args.write:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        body = {
            "about": "Today's SDK decision for per-tool policy rows (R0 legacy, TR0-O E). Captured by "
                     "tests/legacy/legacy_script.py --write; compared by tests/legacy/test_legacy_rows.py.",
            "cases": run_scenario(),
        }
        with open(FIXTURE, "w") as fh:
            json.dump(body, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"wrote {FIXTURE} ({len(body['cases'])} cases)")
        return 0
    if args.show:
        print(json.dumps(run_scenario(), sort_keys=True))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
