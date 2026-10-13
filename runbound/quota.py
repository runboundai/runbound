"""Quotas across replicas (T1b): leases on the heartbeat, a synchronous take when the lease is gone.

A policy row's ``quota`` is ``{"limit": n, "window": "minute"|"hour"|"day", "per": "service"|"key"}`` in a
**fixed, calendar-aligned UTC window** ("20 per clock hour"). The plane keeps one exact counter; every grant of
every kind passes through it, so leases plus takes never exceed the limit for any number of workers.

* **Leases.** Each hello may carry ``quota_held`` (what this worker still holds in the current window); the
  reply's ``quota_leases`` says how much was newly ``granted``. Spend a lease before taking anything, and
  **drop everything held the moment ``window_start`` changes**.
* **A take.** With the lease gone, one call is taken with ``POST /v1/quota/take``; a refusal is cached as
  "exhausted" until the window ends, so a loop costs one round trip per window. A per-key quota is never
  leased: it takes synchronously, with the session key's hash.
* **The plane away.** The lease in hand keeps being spent. Past it, ``quota_plane_unavailable`` for every
  class while the row's ``fail_mode`` is ``closed`` (the default). ``open`` continues at a static share
  (:data:`OPEN_SHARE_DIVISOR`-th of the limit) per heartbeat period.
* **Approvals.** A tool that will ask a person does not take here: ``POST /v1/approvals`` takes the unit
  before any approval row exists (a full quota is its 429), and the approval object's ``quota`` says so.
* **dry_run** rows are counted like enforced ones; the call is never refused, ``would_refuse`` is recorded.

Order (tool-policy.md §2): blocked, session cap, **quota**, approval. Nothing here runs for a call that an
earlier rule refused, so a refused call never consumes a unit.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .plane_types import key_hash

_LOG = logging.getLogger("runbound")

#: ``fail_mode: open`` admits ``limit // OPEN_SHARE_DIVISOR`` (at least 1) calls per heartbeat period while
#: the plane is away: a static share, since the worker does not know how many peers it has.
OPEN_SHARE_DIVISOR = 10


def _epoch(stamp: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


class Leases:
    """What this worker holds of each service-scoped quota, and what it has learned is exhausted."""

    def __init__(self, now: Any = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._held: dict = {}  # tool -> {"start": str, "end": float, "units": int}
        self._exhausted: dict = {}  # (tool, key) -> epoch until which the window is known full
        self._open_budget: dict = {}  # tool -> [period_started_monotonic, spent]

    def apply(self, leases: Any) -> None:
        """The hello reply's ``quota_leases``: add ``granted``; drop everything when ``window_start`` moved.

        An entry with ``"drop": true`` pops everything held for that tool and adds nothing.
        """
        with self._lock:
            for lease in leases if isinstance(leases, list) else ():
                try:
                    tool = lease["tool"]
                    if lease.get("drop") is True:  # the tool now asks a person: the plane takes its units at approval create
                        self._held.pop(tool, None)
                        continue
                    start = lease["window_start"]
                    held = self._held.get(tool)
                    if held is None or held["start"] != start:
                        held = {"start": start, "end": _epoch(lease.get("window_end")), "units": 0}
                        self._exhausted = {k: v for k, v in self._exhausted.items() if k[0] != tool}
                    held["units"] += max(0, int(lease.get("granted", 0)))
                    held["end"] = _epoch(lease.get("window_end")) or held["end"]
                    self._held[tool] = held
                except Exception:
                    _LOG.debug("runbound: skipping a malformed quota lease", exc_info=True)

    def report(self) -> dict:
        """``quota_held`` for the hello: units held in the CURRENT window only."""
        now = self._now()
        with self._lock:
            return {t: h["units"] for t, h in self._held.items()
                    if h["units"] > 0 and (h["end"] is None or now < h["end"])}

    def spend(self, tool: str) -> bool:
        """Spend one held unit. A lease belongs to its window: one past its end is dropped, not spent."""
        now = self._now()
        with self._lock:
            held = self._held.get(tool)
            if held is None:
                return False
            if held["end"] is not None and now >= held["end"]:
                del self._held[tool]
                return False
            if held["units"] > 0:
                held["units"] -= 1
                return True
            return False

    def exhausted_for(self, tool: str, key: str | None) -> float | None:
        """Seconds until a window known full ends, or ``None``."""
        with self._lock:
            until = self._exhausted.get((tool, key))
        if until is None or self._now() >= until:
            return None
        return until - self._now()

    def mark_exhausted(self, tool: str, key: str | None, retry_after_s: float) -> None:
        with self._lock:
            self._exhausted[(tool, key)] = self._now() + max(0.0, retry_after_s)

    def open_allows(self, tool: str, limit: int, period_s: float) -> bool:
        """``fail_mode: open``: a static share per heartbeat period."""
        share = max(1, limit // OPEN_SHARE_DIVISOR)
        now = time.monotonic()
        with self._lock:
            started, spent = self._open_budget.get(tool, (now, 0))
            if now - started >= period_s:
                started, spent = now, 0
            if spent >= share:
                self._open_budget[tool] = [started, spent]
                return False
            self._open_budget[tool] = [started, spent + 1]
            return True


_UNVERIFIABLE_WARNED_AT = [0.0]


def _warn_unverifiable(status: Any, tool: str) -> None:
    """One warning a minute naming the status: the plane answered the take with 400/404, so the quota cannot be verified."""
    now = time.monotonic()
    if _UNVERIFIABLE_WARNED_AT[0] == 0.0 or now - _UNVERIFIABLE_WARNED_AT[0] >= 60.0:
        _UNVERIFIABLE_WARNED_AT[0] = now
        _LOG.warning("runbound: the control plane answered the quota take for %r with %s; the quota cannot be verified, "
                     "so the row's fail mode decides", tool, status)


def admit(engine: Any, session: Any, call: Any, row: Any, will_ask_person: bool, tags: list | None) -> None:
    """The quota stage for one call. Raises (or, for a dry_run row, records ``would_refuse``) when over."""
    quota = getattr(row, "quota", None)
    if not quota or will_ask_person:
        return
    from .policy_rows import _refuse

    shared = engine.shared
    leases = getattr(shared, "quota", None)
    if leases is None:
        return
    per_key = quota.get("per") == "key"
    key = key_hash(session.key) if per_key and getattr(session, "key", None) else None
    if per_key and key is None:
        return  # no session key to count under: nothing to hold this call to
    limit = int(quota["limit"])
    if not per_key and leases.spend(call.name):
        return
    wait = leases.exhausted_for(call.name, key)
    if wait is not None:
        _refuse(engine, session, call, row, "quota_exceeded",
                f"tool {call.name!r} is over its quota of {limit} per {quota.get('window')}; try again in {int(wait)} s", tags, wait)
        return
    link = getattr(shared, "link_state", lambda: "connected")()
    if link != "connected":  # a request-path call to the plane is exactly what a degraded link must not attempt
        if row.fail_mode == "open" and leases.open_allows(call.name, limit, float(engine.config.control_plane_poll_s)):
            return
        _refuse(engine, session, call, row, "quota_plane_unavailable",
                f"tool {call.name!r} cannot be run: its quota cannot be verified while the control plane is unreachable", tags,
                float(engine.config.control_plane_poll_s))
        return
    body = {"tool": call.name, "service": engine.config.service}
    if key is not None:
        body["key"] = key
    status, data = shared.approvals("POST", "/v1/quota/take", body, timeout=3.0)
    if status == 200 and isinstance(data, dict):
        if data.get("granted"):
            return
        wait = float(data.get("retry_after_s") or 1.0)
        leases.mark_exhausted(call.name, key, wait)
        _refuse(engine, session, call, row, "quota_exceeded",
                f"tool {call.name!r} is over its quota of {limit} per {quota.get('window')}; try again in {int(wait)} s", tags, wait)
        return
    if status in (400, 404):  # the plane says it has no such quota: cannot verify, so the fail-mode path decides (C23)
        _warn_unverifiable(status, call.name)
    if row.fail_mode == "open" and leases.open_allows(call.name, limit, float(engine.config.control_plane_poll_s)):
        return
    _refuse(engine, session, call, row, "quota_plane_unavailable",
            f"tool {call.name!r} cannot be run: its quota cannot be verified while the control plane is unreachable", tags,
                float(engine.config.control_plane_poll_s))
