"""The plane protocol: what the SDK says to a control plane, and what it believes.

:class:`LocalState` is the no-plane behavior, free and unchanged.
:class:`RemoteState` talks to a plane over three calls and nowhere else:
``hello`` (a heartbeat carrying this worker's identity, tool report and halt
ack, answered with the halt, the policy version and the poll interval),
``policy`` (the org policy, fetched when its version changes) and ``events``
(telemetry, via the exporter).

Three rules hold everywhere in this module:

* **The plane is an optimization, never a dependency.** Every method catches
  everything and falls back to the local answer. The link moves through
  *connecting* (before the first hello), *connected*, *degraded* (3 hellos missed in a
  row, or the plane could not read its own state) and *unavailable* (a grace period
  since the last answered hello); see :meth:`RemoteState.link_state`. Hellos go on at
  every ``poll_s`` (±10%) whatever the state: there is no separate probe.
* **Hashes and counts, never content.** A key travels as
  :func:`~runbound.plane_types.key_hash` unless the customer opted into
  ``send_session_keys``.
* **No network under a lock.** The lock guards a handful of fields for a few
  microseconds; every HTTP call is made outside it.

What the plane says is cached: a fleet-wide halt it served holds until the plane
lifts it (the enforcement floor, F1), whether or not the plane keeps answering.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import cache as cache_module
from . import local_events
from .export import Exporter
from .plane import PlaneClient, Poller
from .plane_types import PlaneStatus

_LOG = logging.getLogger("runbound")

#: Consecutive failed calls before the link is called degraded and stops
#: asking. Three, so a single dropped packet is not an outage.
DEGRADE_AFTER = 3

#: How long without an answered hello before the link mode is ``unavailable`` (a label; the policy state is separate).
UNAVAILABLE_AFTER_S = 60.0

#: ``init(consequential_grace_s=)``: how long after the last answered hello (or after start) financial /
#: destructive / privileged and unclassified tools keep running on the cache. Never above 60 s, so the
#: grace can not outlast a halt (a halt now holds until lifted, but the grace stays the bound for a service
#: without enforced defaults).
GRACE_DEFAULT_S = 60.0

#: The offline-execution window (a service with enforced defaults): how long consequential tools keep running on the last POLICY CONFIRMATION while the plane is away.
#: The plane serves it (``service_defaults.offline.execution_s``, 60 to 86400); a worker that cannot read what it was served uses the SHORTEST of ``OFFLINE_SAFE_S`` and the
#: legacy behaviour; with nothing served the behaviour is today's, exactly (the 24 h validity window). ``init(offline_execution_s=)`` may only shorten it.
OFFLINE_MIN_S, OFFLINE_MAX_S, OFFLINE_SAFE_S = 60, 86400, 900
#: A wall-clock disagreement larger than this (a confirmation in the future, a wall clock that moved back against the monotonic one) counts as an expired window.
OFFLINE_SKEW_S = 5.0


class LocalState:
    """No control plane: the single-process behavior, and it asks nothing."""

    #: There is no plane to tell.
    fleet = False
    policy_version = 0
    policy_dry_run = False

    def halted(self) -> bool:
        return False

    def halt_mode(self) -> "str | None":
        return None

    def policy(self) -> dict | None:
        return None

    def status(self) -> PlaneStatus:
        return PlaneStatus()

    def pending_events(self) -> "int | None":
        return None

    def approvals(self, method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> tuple:
        return None, None

    def observers(self) -> list:
        return []

    def start(self, engine: Any) -> None:
        return None

    def stop(self) -> None:
        return None


class RemoteState:
    """Plane state backed by a control plane, and by local answers when not.

    ``client`` is a :class:`~runbound.plane.PlaneClient` (or anything with its
    methods), ``exporter`` an :class:`~runbound.export.Exporter`, and ``now``
    the monotonic clock, injected so tests can move it.
    """

    #: There is a plane, and it wants to hear about all of it.
    fleet = True

    def __init__(
        self,
        client: Any,
        exporter: Any,
        config: Any,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._exporter = exporter
        self._config = config
        self._now = now
        self._lock = threading.Lock()
        self._failures = 0
        #: Consecutive hellos that did not come back (the Poller reports each), and when this link began.
        self._misses = 0
        self._started_at = now()
        self._grace = float(getattr(config, "consequential_grace_s", GRACE_DEFAULT_S))
        #: Whether the sealed policy this worker holds carries ENFORCED service defaults (``defaults_v1``, TR2-O3). Until a plane serves
        #: them, every service keeps today's per-class cache trust (``plane_refusal``); this stays False.
        self._defaults_enforced = False
        self._defaults_raw: "dict | None" = None  # the served service_defaults envelope, kept for the cache
        self._defaults: Any = None  # lines.Defaults, when readable
        self._defaults_problem: "tuple | None" = None  # (reason code, text) when served defaults cannot be read safely
        self._last_success: float | None = None
        #: The evidence-coverage body the plane last ANSWERED a hello for: ``(digest, monotonic time)``; ``_evidence_inflight`` is the one on the hello being built.
        self._evidence_sent: tuple = (None, None)
        self._evidence_inflight: "tuple | None" = None
        #: The last POLICY CONFIRMATION (not any answered hello): an authenticated, whole hello after which this worker held the policy the plane announced. Monotonic for this
        #: process; wall clock for the cache (a restart resumes from the file's ``confirmed_at``, never from boot).
        self._confirmed_mono: float | None = None
        self._confirmed_wall: float | None = None
        self._wall: Callable[[], float] = time.time
        self._last_attempt: float | None = None
        self._halt = False
        self._halt_mode = "stop"
        self._notice: str | None = None
        self._policy: dict | None = None
        self._policy_version = 0
        self._policy_dry_run = False
        #: Has this worker ever held a policy (a fetch that succeeded, or a cache it loaded)? And the newest
        #: ``policy_version`` a hello announced: a plane that says a policy exists while the worker holds none
        #: has not governed this worker yet (see :meth:`plane_refusal`).
        self._policy_held = False
        self._sealed_cache_loaded = False
        self._announced_version = 0
        self._poller: Any = None
        #: The tool-report hash the plane has acknowledged, and the one riding a
        #: heartbeat whose reply has not come back yet; see :meth:`_tools_payload`.
        self._tools_sent_hash: str | None = None
        self._tools_pending_hash: str | None = None
        #: The plane could not read its own live store on the last hello: a halt
        #: and the policy version are held as they were rather than read from
        #: a reply that only carries wire defaults.
        self._fleet_state_unavailable = False
        self._state_unavailable_since: float | None = None
        #: What the plane last said is wrong with itself (``HelloReply.plane_degraded``); informational.
        self._plane_degraded: str | None = None
        #: The on-disk policy cache (S7): loaded now, so the last policy is enforced before the
        #: first heartbeat; rewritten as the plane serves new ones.
        self._cache_path = cache_module.resolve(
            getattr(config, "cache_dir", None), getattr(config, "control_plane_url", None), config.service)
        self._cache_written_at: float | None = None  # wall clock, from the file
        self._cache_saved_mono: float | None = None
        from . import policy_rows
        from .quota import Leases

        #: The dashboard's per-tool rows (T1a) and their hash; the leases this worker holds (T1b).
        self._rows: dict = {}
        self._rows_hash: str | None = None
        #: The plane's own hash of what it served (``cfg_hash`` in the policy envelope; the same value as ``rows_hash``). Held as served, never computed here:
        #: the plane is the only one that hashes (WIRE-VPC 3). ``None`` until an envelope carries it (a cache load, an older plane).
        self._cfg_hash: str | None = None
        #: Who this PROCESS is, for the acceptance evidence (WIRE-VPC 2): drawn once when it starts, so a restart is a new boot and a late or duplicated
        #: hello of an old boot can be told from a new one. ``seq`` counts this boot's hellos from 1.
        self._boot_id = str(uuid.uuid4())
        self._hello_seq = 0
        self._hello_seq_lock = threading.Lock()
        self._rows_raw: list | None = None
        self._unreadable_rows = 0
        self._halted_tools: dict = {}  # tool -> reasons the plane halted it for (halt_tools_v1); refused with policy_halted
        self.quota = Leases()
        self._load_cache()
        from .approvals import ReportQueue

        #: Deferred-approval reports the plane could not take; drained on the next connected hello.
        self.reports = ReportQueue(
            None if self._cache_path is None else self._cache_path.with_name(self._cache_path.stem + ".reports.jsonl"))

    # --- the on-disk cache (S7) -------------------------------------------------------

    def _load_cache(self) -> None:
        body, _status = cache_module.read(self._cache_path, getattr(self._config, "token", None), self._config.service)
        if body is None:
            return
        self._policy_held = True
        self._sealed_cache_loaded = True
        policy = body.get("policy")
        self._policy = policy if isinstance(policy, dict) else None
        self._policy_version = _as_int(body.get("policy_version"))
        self._policy_dry_run = bool(body.get("policy_dry_run", False))
        self._set_rows(body.get("rows"), body.get("rows_hash"))
        self._set_defaults(body.get("service_defaults"))
        if body.get("halt") in ("stop", "narrow"):  # a served halt survives a restart while the plane is down
            self._halt, self._halt_mode = True, body["halt"]
        self._halted_tools = _halted_tools([{"tool": t, "reasons": r} for t, r in (body.get("halted_tools") or {}).items()]) if isinstance(body.get("halted_tools"), dict) else {}
        written = body.get("written_at")
        self._cache_written_at = float(written) if isinstance(written, (int, float)) else None
        if "confirmed_at" in body:  # a cache written since the offline window: None means it never held a confirmation
            confirmed = body.get("confirmed_at")
            self._confirmed_wall = float(confirmed) if isinstance(confirmed, (int, float)) and not isinstance(confirmed, bool) else None
        else:  # written by an older SDK: the file's age is all there is
            self._confirmed_wall = self._cache_written_at
        age = self.cache_age_s()
        if age is not None and age > cache_module.STALE_AFTER_S:
            cache_module._warn_once(("stale",), "runbound: the policy cache is %.0f h old and the plane has not answered", age / 3600)

    def _set_rows(self, rows: Any, digest: Any) -> None:
        from . import policy_rows

        self._rows_raw = rows if isinstance(rows, list) else None
        self._rows = policy_rows.parse(rows)
        self._unreadable_rows = policy_rows.unreadable(rows)
        self._rows_hash = digest if isinstance(digest, str) else None

    def halted_reasons(self, tool: str) -> "list | None":
        """The reasons the plane halted ``tool`` for (``halted_tools``, ``halt_tools_v1``), or ``None`` when it did not."""
        return self._halted_tools.get(tool)

    def _set_defaults(self, raw: Any) -> None:
        """Hold the served service defaults. Enforced ones switch the service to the single validity window and to fail mode per key."""
        from . import lines

        self._defaults_raw = raw if isinstance(raw, dict) else None
        self._defaults, self._defaults_problem = None, None
        if self._defaults_raw is not None:
            try:
                self._defaults = lines.parse_defaults(self._defaults_raw)
            except lines.LineError as error:
                self._defaults_problem = (error.code, str(error))
        self._defaults_enforced = (self._defaults is not None and self._defaults.state == "enforced") or (
            self._defaults_problem is not None and self._defaults_raw.get("state", "enforced") == "enforced")

    def boot_id(self) -> str:
        """Which process this is (drawn once at construction): a decision record names it, so a call can be traced to the process that decided it."""
        return self._boot_id

    def quota_peek_supported(self) -> bool:
        """Does the plane answer ``POST /v1/quota/take`` with ``"peek": true`` as a peek (read the counter, take nothing)? A plane that serves a ``cfg_hash`` shipped the
        peek with it; an older one would ignore the key and TAKE a unit, so it is never sent one."""
        return self._cfg_hash is not None

    def served_cfg(self) -> "str | None":
        """The configuration hash this worker holds (the plane's ``cfg_hash``, else its ``rows_hash``), or ``None`` with no rows: what a decision record names as the
        configuration the call was decided under. Held as served, never computed here."""
        return self._cfg_hash or self._rows_hash or None

    def defaults(self) -> Any:
        """The service defaults this worker holds (``lines.Defaults``), or ``None`` when the service has not adopted any."""
        return self._defaults

    def defaults_problem(self) -> "tuple | None":
        return self._defaults_problem

    def policy_unsupported(self) -> list:
        """The tools whose served rule this SDK cannot interpret safely (floor F2), as ``[{"tool", "why"}]``, plus an entry with no
        tool for served entries that name none. Each such tool is refused with ``policy_unsupported``; the hello will report this list."""
        found = [{"tool": tool, "why": row.unsupported, "reason": row.unsupported_code or "invalid_body"}
                 for tool, row in sorted(self._rows.items()) if row.unsupported]
        if self._defaults_problem is not None:
            found.append({"tool": "*", "reason": self._defaults_problem[0], "why": self._defaults_problem[1]})
        if getattr(self, "_unreadable_rows", 0):
            found.append({"tool": None, "reason": "row_unparseable",
                          "why": f"{self._unreadable_rows} served entr{'y' if self._unreadable_rows == 1 else 'ies'} name no tool"})
        return found

    def row_for(self, tool: str) -> Any:
        """The dashboard row for ``tool`` (``None`` when there is none). A plain dict read: no lock, no copy."""
        return self._rows.get(tool)

    def cache_age_s(self) -> float | None:
        """Seconds since the cache this worker runs on was written, or ``None`` with none."""
        written = self._cache_written_at
        return None if written is None else max(0.0, time.time() - written)

    def _save_cache(self, force: bool = False) -> None:
        """Write what the plane last served. Throttled; called on the poller's thread. Never raises."""
        now = self._now()
        with self._lock:
            if not force and self._cache_saved_mono is not None and now - self._cache_saved_mono < cache_module.REFRESH_EVERY_S:
                return
            self._cache_saved_mono = now
            body = cache_module.body_for(self._config.service, self._policy_version, self._policy, self._policy_dry_run,
                                         self._rows_hash, self._rows_raw, halt=self._halt_mode if self._halt else None,
                                         halted_tools=self._halted_tools, service_defaults=self._defaults_raw, confirmed_at=self._confirmed_wall)
        if cache_module.write(self._cache_path, getattr(self._config, "token", None), body):
            self._cache_written_at = body["written_at"]

    def link_state(self) -> str:
        """``connecting`` | ``connected`` | ``degraded`` | ``unavailable`` (see the module docstring).

        *unavailable* is time, not a count: :data:`UNAVAILABLE_AFTER_S` since the last answered hello, or
        since the link began with none. *degraded* is 3 missed hellos in a row, or one reply saying the
        plane could not read its own state. *connecting* is the time before the first answer.
        """
        with self._lock:
            return self._link_state()

    def _link_state(self) -> str:
        reference = self._last_success if self._last_success is not None else self._started_at
        if self._now() - reference >= UNAVAILABLE_AFTER_S:
            return "unavailable"
        if self._misses >= DEGRADE_AFTER or self._fleet_state_unavailable:
            return "degraded"
        return "connected" if self._last_success is not None else "connecting"

    def record_miss(self) -> None:
        """The Poller's hello did not come back. Counts toward *degraded*."""
        with self._lock:
            self._misses += 1

    def awaiting_first_policy(self) -> bool:
        """True until this worker has had a first answered hello or loaded a sealed cache: no governed call runs before then (F3)."""
        with self._lock:
            return self._last_success is None and not self._sealed_cache_loaded

    def policy_state(self) -> str:
        """``fresh`` | ``cached`` | ``unavailable`` (TPD §5.7), a function of now and what this worker holds.

        *fresh*: a hello was answered recently. *cached*: it was not, but the policy this worker holds was sealed within the
        validity window (:data:`runbound.cache.STALE_AFTER_S`, 24 h): it is enforced in full and fail mode does not apply.
        *unavailable*: there is no policy, or the one held has expired. Plane unreachable does not by itself mean unavailable.
        """
        from .lines import State as states

        with self._lock:
            now = self._now()
            serving = self._last_success is not None and self._policy_held
            served_at = self._last_success if serving else None
            sealed_ages = [age for age in (self.cache_age_s(), None if served_at is None else now - served_at) if age is not None]
        sealed = states.Cache(sealed_at=now - min(sealed_ages)) if sealed_ages else None
        fresh_s = max(30.0, 2.0 * float(getattr(self._config, "control_plane_poll_s", 5.0)))
        return states.state(now, served_at, sealed, states.Validity(fresh_s=fresh_s, cache_s=cache_module.STALE_AFTER_S))

    # --- the offline-execution window ------------------------------------------------

    def _offline_bound(self) -> "int | None":
        """The window (seconds) that bounds consequential execution on the last policy confirmation, or ``None`` when today's behaviour applies unchanged.

        Only a service with ENFORCED defaults has one. The served ``offline.execution_s`` applies when it is an integer 60..86400; a served value this SDK cannot read uses the
        shortest of 900 s and the legacy behaviour (never longer); nothing served is today's behaviour. ``init(offline_execution_s=)`` may only shorten it.
        """
        if not self._defaults_enforced:
            return None
        raw = self._defaults_raw if isinstance(self._defaults_raw, dict) else {}
        window: "int | None" = None
        if "offline" in raw:
            served = raw["offline"].get("execution_s") if isinstance(raw["offline"], dict) else None
            ok = isinstance(served, int) and not isinstance(served, bool) and OFFLINE_MIN_S <= served <= OFFLINE_MAX_S
            window = served if ok else OFFLINE_SAFE_S
        own = getattr(self._config, "offline_execution_s", None)
        if own is not None:
            ceiling = cache_module.STALE_AFTER_S if window is None else window  # nothing served: the legacy 24 h is what it may shorten
            if own < ceiling:
                window = own
            elif own > ceiling:
                from .hooks import _warn_once

                _warn_once(("offline-longer",), "runbound: init(offline_execution_s=%d) is longer than the %d s window in force; ignored (it may only shorten it)", own, ceiling)
        return window

    def offline_window_s(self) -> "int | None":
        """The window this worker reports and applies, in seconds: ``None`` without enforced defaults; the legacy 24 h when nothing shorter is served or configured."""
        if not self._defaults_enforced:
            return None
        bound = self._offline_bound()
        return bound if bound is not None else cache_module.STALE_AFTER_S

    def offline_deadline(self) -> "float | None":
        """An ESTIMATE of when consequential execution on the last confirmation ends (wall-clock epoch seconds), or ``None`` without a window or a confirmation."""
        window = self.offline_window_s()
        return None if window is None or self._confirmed_wall is None else self._confirmed_wall + window

    def _confirmation_expired(self, window: int) -> bool:
        """Has the last policy confirmation outlived ``window``? Expiry only ever refuses MORE, so every doubt counts as expired: no confirmation at all, one dated in the future
        (more than 5 s), a wall clock that moved back against the monotonic clock, and the larger of the two ages."""
        wall = self._confirmed_wall
        if wall is None:
            return True
        wall_age = self._wall() - wall
        if wall_age < -OFFLINE_SKEW_S:
            return True
        age = max(wall_age, 0.0)
        if self._confirmed_mono is not None:
            mono_age = self._now() - self._confirmed_mono
            if wall_age < mono_age - OFFLINE_SKEW_S:
                return True
            age = max(age, mono_age)
        return age > window

    def _confirm(self) -> None:
        """A policy confirmation: stamp now (monotonic and wall clock). Caller holds no lock."""
        with self._lock:
            self._confirmed_mono, self._confirmed_wall = self._now(), self._wall()

    def plane_refusal(self, effects: "frozenset | None") -> str | None:
        """Why a tool carrying ``effects`` must be refused because the plane is gone, else ``None``.

        Which rule applies follows the service (Apollo's ruling on TR2-O1, TPD §5.5 "existing services keep today's behaviour until an
        operator adopts defaults"):

        * A service **without enforced service defaults** (every service today) keeps the per-class cache trust it has always had.
          ``read``, ``write`` + a tag and ``external`` tools run on the cache (or on code rules) whatever the state. ``financial`` /
          ``destructive`` / ``privileged`` and unclassified tools refuse once ``consequential_grace_s`` has passed since the last
          answered hello (or since start, with none), and, while still *connecting*, unless the cache was last confirmed within 15
          minutes. One change on top (register K8): a **bare write** (``write`` alone, or with ``read``) counts as unclassified for fail
          mode, so it refuses when the policy is *unavailable* (no policy, or one older than the 24 h window), and not otherwise.
        * A service whose sealed policy carries **enforced defaults** (``defaults_v1``, TR2-O3) buys the single window: fresh and cached
          policies are enforced in full and fail mode applies only when unavailable.
        """
        from .hooks import fails_closed, fails_closed_unavailable

        if self._defaults_enforced:
            bound = self._offline_bound()
            if bound is not None and fails_closed(effects) and self._confirmation_expired(bound):
                return "the offline-execution window of %d s has ended and the plane has not confirmed the policy" % bound
            if self.policy_state() != "unavailable":
                return None  # fresh and cached: the sealed policy, defaults included, is enforced in full
            if self._defaults is not None:
                from . import lines

                if lines.resolve(self._defaults, effects).fail_mode != "closed":
                    return None
            elif not fails_closed_unavailable(effects):
                return None
            return "the control plane is unreachable and this worker holds no policy within its validity window (%d h)" % (cache_module.STALE_AFTER_S // 3600)
        if not fails_closed(effects):
            if fails_closed_unavailable(effects) and self.policy_state() == "unavailable":
                return "the control plane is unreachable and this worker holds no policy within its validity window (%d h)" % (cache_module.STALE_AFTER_S // 3600)
            return None
        with self._lock:
            reference = self._last_success if self._last_success is not None else self._started_at
            if self._now() - reference >= self._grace:
                return "the control plane has not answered for %d s" % self._grace
            if self._last_success is not None and self._policy_held:  # a hello answered AND the policy it announced (or its absence) read: not the hello alone
                return None
            age = self.cache_age_s()  # no hello yet, or told a policy exists while holding none: the cache decides
        if age is not None and age <= cache_module.FRESH_FOR_CONSEQUENTIAL_S:
            return None
        return "the control plane has not answered yet, or has not served its policy, and there is no fresh policy cache"

    def approval_refusal(self) -> str | None:
        """Why a NEW approval cannot be requested now, else ``None``: only a connected link can take one."""
        state = self.link_state()
        return None if state == "connected" else f"the control plane link is {state}"

    # --- what the plane said ----------------------------------------------------

    @property
    def policy_version(self) -> int:
        with self._lock:
            return self._policy_version

    @property
    def policy_dry_run(self) -> bool:
        with self._lock:
            return self._policy_dry_run

    def policy(self) -> dict | None:
        """The org policy the plane last served, or ``None``."""
        with self._lock:
            return self._policy

    def halted(self) -> bool:
        """Is the fleet halted? A halt the plane served holds until the plane lifts it (the enforcement floor, F1): it does not lapse."""
        with self._lock:
            return bool(self._halt)

    def halt_mode(self) -> "str | None":
        """``"stop"`` or ``"narrow"`` while a halt is enforced, else ``None``."""
        return self._halt_mode if self.halted() else None

    def _staleness_clock(self) -> "float | None":
        """What a halt's staleness is measured from (caller holds ``self._lock``): the
        last contact, or the moment the plane's state became unavailable."""
        if self._fleet_state_unavailable and self._state_unavailable_since is not None:
            return self._state_unavailable_since
        return self._last_success

    def status(self) -> PlaneStatus:
        """Where the link stands, from cached state only; never opens a socket."""
        with self._lock:
            mode = self._link_state()
            last = self._last_success
            age = None if last is None else max(0.0, self._now() - last)
            halt_stale = None
            if self._halt and last is not None:  # informational: how long the halt has gone unconfirmed; it is enforced regardless
                halt_stale = max(0.0, self._now() - (self._staleness_clock() or last))
            reason = None
            if self._misses >= 1:
                reason = "heartbeat failures"
            elif self._fleet_state_unavailable:
                reason = "fleet state unavailable"
            cache_age = self.cache_age_s()
            return PlaneStatus(
                mode=mode,
                cache_age_s=cache_age,
                stale=bool(cache_age is not None and cache_age > cache_module.STALE_AFTER_S and mode != "connected"),
                last_contact_age_s=age,
                consecutive_failures=self._misses,
                notice=self._notice,
                halt_stale_s=halt_stale,
                reason=reason,
                applied_policy_version=self._policy_version if self._policy is not None else None,
                plane_degraded=self._plane_degraded,
                offline_window_s=self.offline_window_s(),
                offline_deadline=self.offline_deadline(),
            )

    # --- the heartbeat ------------------------------------------------------------

    def apply_hello(self, reply: Any) -> None:
        """Apply one heartbeat reply (the :class:`~runbound.plane.Poller` calls this).

        Can change the halt and its mode, the policy version (fetched here, on the
        poller's thread) and the poll interval (``HelloReply.poll_s``).
        ``fleet_state == "unavailable"`` means the plane could not read its own live
        store: the halt and policy version are held, not read from wire defaults.
        A reply that arrived also settles the tool report's hash. Never raises.
        """
        try:
            unavailable = getattr(reply, "fleet_state", "ok") == "unavailable"
            version = _as_int(getattr(reply, "policy_version", 0))
            notice = getattr(reply, "notice", None)
            mode = getattr(reply, "halt_mode", "stop")
            mode = mode if mode in ("stop", "narrow") else "stop"
            halt_moved = halted_moved = False
            first_read = fetched_ok = False
            if not unavailable and not self._policy_held:
                # The FIRST answered hello, whatever version it announced: a new service starts with enforced standard defaults the plane has not announced yet (it says
                # version 0), and a worker that read 0 as "no policy" ran consequential tools unasked until a later hello announced one. The policy is read BEFORE the
                # hello counts as answered, so the startup floor lifts only with the policy (and its defaults) in hand.
                fetched_ok = self._fetch_policy(version)
                first_read = self._policy_held
            with self._lock:
                if unavailable:
                    if not self._fleet_state_unavailable:
                        self._state_unavailable_since = self._now()
                    self._fleet_state_unavailable = True
                    changed = False
                else:
                    self._fleet_state_unavailable = False
                    self._state_unavailable_since = None
                    halt_before = self._halt
                    tools_before = self._halted_tools
                    self._halted_tools = _halted_tools(getattr(reply, "halted_tools", None))
                    halted_moved = tools_before != self._halted_tools
                    self._halt = bool(getattr(reply, "halt", False))
                    self._halt_mode = mode if self._halt else "stop"
                    halt_moved = halt_before != self._halt
                    changed = version != self._policy_version and not first_read  # a policy just read is not read again for the same hello
                    self._announced_version = version
                self._notice = notice if isinstance(notice, str) else None
                said = getattr(reply, "plane_degraded", None)
                said = said if isinstance(said, str) and said else None
                moved = said != self._plane_degraded
                before, self._plane_degraded = self._plane_degraded, said
                self._last_success = self._now()
                self._failures = 0
                self._misses = 0
                if self._evidence_inflight is not None:  # the plane answered the hello that carried it: it has the body
                    self._evidence_sent, self._evidence_inflight = self._evidence_inflight, None
                self._settle_tools_hash(reply)
            if moved:  # once per change, never per hello
                from . import local_events

                local_events.record_plane_degraded(before, said)
            poll_s = getattr(reply, "poll_s", 0.0)
            if self._poller is not None and isinstance(poll_s, (int, float)) and poll_s > 0:
                self._poller.set_interval(poll_s)
            if changed:
                fetched_ok = self._fetch_policy(version)
            if not unavailable and said is None and self._policy_held and (fetched_ok or self._policy_version == version):
                self._confirm()  # an authenticated, whole hello after which this worker holds the policy the plane announced: the only thing that renews the offline window
                if fetched_ok:
                    self._save_cache(force=True)  # the fetch wrote the file before this hello was a confirmation
            if not changed and not unavailable and self._policy_version:
                self._save_cache()  # keep the cache fresh (its confirmed_at is carried, never advanced by an unconfirmed hello)
            if not unavailable and (halt_moved or halted_moved):
                self._save_cache(force=True)  # a halt put on or lifted is on disk at once
            if not unavailable:
                from . import approvals

                approvals.drain_reports(self)
            if not unavailable:
                self.quota.apply(getattr(reply, "quota_leases", None))
            lost = getattr(reply, "claims_lost", None)
            if lost:
                from . import approvals

                approvals.claims_lost(lost)
            approved = getattr(reply, "approved", None)
            if approved:
                from . import approvals

                approvals.handle_approved(approved)
        except Exception:
            _LOG.warning("runbound: could not apply the control plane's reply", exc_info=True)

    def _settle_tools_hash(self, reply: Any) -> None:
        """Acknowledge the tool report that rode the last heartbeat (caller holds
        ``self._lock``); ``tools_known: false`` makes the next one resend it in full."""
        if getattr(reply, "tools_known", True) is False:
            self._tools_sent_hash = None
        elif self._tools_pending_hash is not None:
            self._tools_sent_hash = self._tools_pending_hash
        self._tools_pending_hash = None

    def _fetch_policy(self, version: int) -> bool:
        """Pull the org policy for this service; keep the old one on failure. ``True`` when it was applied."""
        data = self._attempt(lambda: self._client.policy(self._config.service), "policy")
        if data is None:
            return False
        rows, digest = (data.get("rows"), data.get("rows_hash")) if isinstance(data, dict) else (None, None)
        if digest == "":
            # The plane's read-error marker: it could not read its rows, so this is not an answer. Apply nothing (the
            # held rows, document and version stay, which also makes the next hello fetch again) and write no cache
            # (register C12: a transient plane error must never erase every rule on every worker that polls during it).
            self._record(False)
            _warn_policy_unread()
            return False
        body, resolved, dry_run = _policy_envelope(data, version)
        if body is not None:
            _warn_legacy_document()
        with self._lock:
            self._policy = body
            self._policy_version = resolved
            self._policy_dry_run = dry_run
            self._policy_held = True
            # A bumped policy_version is not proof the rows changed (it is shared with document policies and
            # refusal profiles): replace them only when their hash differs from the one held.
            if isinstance(data, dict):
                self._set_defaults(data.get("service_defaults"))  # the envelope carries them every time; absent = not adopted
                served_cfg = data.get("cfg_hash")
                self._cfg_hash = served_cfg if isinstance(served_cfg, str) and served_cfg else None
            if isinstance(digest, str) and digest != self._rows_hash:
                self._set_rows(rows, digest)
            elif not isinstance(digest, str) and rows is None:
                pass  # an older plane that serves no rows: keep what is held
        self._save_cache(force=True)
        try:
            local_events.note_runtime_value("policy_version", resolved)
        except Exception:
            _LOG.warning("runbound: could not note the policy_version", exc_info=True)
        return True

    def _attempt(self, call: Callable[[], Any], what: str) -> Any:
        """Make one call to the plane. Never raises; ``None`` when it failed or the key is rejected."""
        if getattr(self._client, "key_state", None) == "invalid":
            return None
        try:
            result = call()
        except Exception:
            _LOG.warning("runbound: control plane %s failed", what, exc_info=True)
            self._record(False)
            return None
        ok = result is not None and result is not False
        self._record(ok)
        return result

    def _record(self, ok: bool) -> None:
        """Fold one call's outcome into the failure run."""
        with self._lock:
            self._failures = 0 if ok else self._failures + 1

    # --- lifecycle ------------------------------------------------------------------

    def observers(self) -> list:
        """The exporter, when it is exporting: the engine's only fleet observer."""
        exporter = self._exporter
        if exporter is None or not getattr(exporter, "include_events", True):
            return []
        return [exporter]

    def start(self, engine: Any) -> None:
        """Start the exporter and the heartbeat. Called by :func:`runbound.init` outside its lock."""
        try:
            if self._exporter is not None:
                self._exporter.start()
            poller = Poller(
                self._client,
                getattr(self._config, "control_plane_poll_s", 5.0),
                self.apply_hello,
                self._hello_payload,
                on_miss=self.record_miss,
            )
            self._poller = poller
            poller.start()
        except Exception:
            _LOG.warning(
                "runbound: could not start the control plane link; running local-only",
                exc_info=True,
            )

    def _hello_payload(self) -> dict:
        """What each heartbeat says: who we are and what we already know."""
        payload = {
            "service": self._config.service,
            "worker_id": self._config.resolved_worker_id(),
            "sdk_version": _sdk_version(),
            "policy_version_seen": self.policy_version,
            "active": _active_sessions(),
            "coverage": _coverage_payload(),
            # This worker's ack that it has seen and applied a halt as of the
            # last reply it processed. A halt holds until the plane lifts it.
            "halt_ack": self.halted(),
            # What this SDK enforces, on EVERY hello: the plane serves lines only to a worker that said so on its latest hello, and a
            # hello without it resets the worker to the legacy fields (stricter or equal).
            "policy_features": list(policy_features()),
            "policy_unsupported": self._unsupported_payload(),
            "boot_id": self._boot_id,
            "seq": self._next_seq(),
            "offline_window_s": self.offline_window_s(),  # the window this worker applies; null when it applies none
        }
        payload.update(self._evidence_payload())
        if getattr(self._config, "approval_bind", False):
            payload["capabilities"] = ["bind_v1"]  # this worker binds an approved request to an attempt before it runs it (D2); absent otherwise
        accepted = self._accepted_cfg()
        if accepted is not False:
            payload["accepted_cfg"] = accepted
        if self._rows_hash is not None:
            payload["rows_hash"] = self._rows_hash  # the rows held: the plane (T2) compares it to what it serves
        held = self.quota.report()
        if held:
            payload["quota_held"] = held
        executing = _executing_approvals()
        if executing:
            payload["renew_claims"] = executing[:20]  # approvals being run here: the plane extends their claims
        payload.update(self._tools_payload())
        return payload

    #: How often the telemetry counters ride the hello when nothing about the coverage STATE changed.
    EVIDENCE_STATS_EVERY_S = 3600.0

    def _evidence_state(self) -> dict:
        """Where this worker's evidence can go (AAI-1 FR-5, RFC section 7), as STATE, not counters: whether ``otel_endpoint`` is set, whether the customer has their own
        ``TracerProvider`` (read now, so a provider installed after ``init()`` is seen), and the events lane (``open``, ``stopped`` by a rejected key or the plan, or ``disabled`` by
        the customer's ``export_events=False``)."""
        from . import delivery, spans

        exporter = self._exporter
        if exporter is None or not getattr(exporter, "customer_events", getattr(exporter, "include_events", True)):
            lane = "disabled"
        elif not getattr(exporter, "include_events", True) or "events" in delivery.snapshot()["stopped_lanes"]:
            lane = "stopped"
        else:
            lane = "open"
        return {"otel_endpoint_set": bool(getattr(self._config, "otel_endpoint", None)), "tracer_provider_detected": spans._provider() is not None, "events_lane_state": lane}

    def _evidence_stats(self) -> dict:
        from . import delivery, spans

        mine = spans.exporter()
        events_dropped = sum(delivery.snapshot()["dropped"].get("events", {}).values())
        return {"sent": getattr(mine, "sent", 0), "dropped": getattr(mine, "dropped", 0), "rejected": getattr(mine, "rejected", 0), "pending": getattr(mine, "pending", 0), "events_dropped": events_dropped}

    def _evidence_payload(self) -> dict:
        """``coverage_digest`` on every hello (a hash of the three state fields only, so it moves when the state does and never when a counter does) and ``evidence_coverage``
        (the state plus ``telemetry_stats``) on the first hello, when the digest changes, and at most hourly: a plane stores a row on a change and the counters hourly. Reads
        local state only: no new network call, no decision. Never raises."""
        try:
            import hashlib
            import json

            state = self._evidence_state()
            digest = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            now = self._now()
            sent_digest, sent_at = self._evidence_sent
            out: dict = {"coverage_digest": digest}
            if digest != sent_digest or sent_at is None or now - sent_at >= self.EVIDENCE_STATS_EVERY_S:
                out["evidence_coverage"] = {**state, "telemetry_stats": self._evidence_stats()}
                self._evidence_inflight = (digest, now)
            return out
        except Exception:
            _LOG.debug("runbound: could not build the evidence coverage", exc_info=True)
            return {}

    def _next_seq(self) -> int:
        """This boot's next hello number: 1, 2, 3 ... strictly increasing, one per hello built (a hello that never reached the plane leaves a gap, which is fine:
        the plane only needs each number to be larger than the last it kept)."""
        with self._hello_seq_lock:
            self._hello_seq += 1
            return self._hello_seq

    def _accepted_cfg(self) -> "str | None | bool":
        """What the hello says this worker is enforcing NOW (WIRE-VPC 2): the configuration hash the plane served, echoed only once every row is parsed and applied.

        ``False`` when no policy has been applied yet (nothing to claim: the key is left out); ``None`` when any row or the service defaults was unreadable or
        unsupported (the worker is NOT enforcing what was served, so it must not say it is); ``""`` when a policy was applied that carried no rows hash (an older
        plane); otherwise the hash. Never recomputed here."""
        if not self._policy_held:
            return False
        if self.policy_unsupported():
            return None
        return self._cfg_hash or self._rows_hash or ""

    def _unsupported_payload(self) -> list:
        """``policy_unsupported`` as the hello carries it: ``[{tool, reason}]`` for each tool this worker refuses as unreadable."""
        return [{"tool": u["tool"], "reason": u["reason"]} for u in self.policy_unsupported() if u["tool"]]  # "*" is the service defaults

    def _tools_payload(self) -> dict:
        """``tools_hash`` every time, ``tools`` only when the plane needs it.

        The hash rides every heartbeat so the plane can always tell whether what
        it holds is current; the report itself rides only when its hash differs
        from the last one the plane acknowledged. The hash of a report that went
        out is only *pending* until a reply comes back. ``{}`` on any failure,
        so a heartbeat carries neither key rather than a hash it cannot back up.
        """
        try:
            from . import _coverage

            report = _coverage.tool_report()
            digest = _coverage.tool_report_hash(report)
            with self._lock:
                unsent = digest != self._tools_sent_hash
                if unsent:
                    self._tools_pending_hash = digest
            if unsent:
                return {"tools_hash": digest, "tools": report}
            return {"tools_hash": digest}
        except Exception:
            _LOG.debug("runbound: could not build the tool report", exc_info=True)
            return {}

    def approvals(self, method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> tuple:
        """A call to the plane's approval routes: ``(status, body)``, ``(None, None)`` on failure."""
        try:
            return self._client.approvals(method, path, body, timeout)
        except Exception:
            return None, None

    def pending_events(self) -> "int | None":
        """Records this worker still has queued to post, or ``None`` without an exporter."""
        exporter = self._exporter
        if exporter is None:
            return None
        try:
            return int(exporter.pending)
        except Exception:
            _LOG.debug("runbound: could not read the exporter's pending count", exc_info=True)
            return None

    def stop(self) -> None:
        """Stop the heartbeat and drain the exporter. Idempotent, bounded."""
        poller, self._poller = self._poller, None
        if poller is not None:
            try:
                poller.stop()
            except Exception:
                _LOG.warning("runbound: could not stop the plane poller", exc_info=True)
        if self._exporter is not None:
            try:
                self._exporter.stop()
            except Exception:
                _LOG.warning("runbound: could not stop the exporter", exc_info=True)


def build(config: Any) -> Any:
    """The shared state one configuration implies.

    :class:`LocalState` without a plane; with one, a :class:`RemoteState` over a
    fresh :class:`~runbound.plane.PlaneClient` and an
    :class:`~runbound.export.Exporter`. Nothing is started here;
    :meth:`RemoteState.start` does that once the engine exists. Fail-open: a
    client that cannot be built leaves the process local, with a warning.
    """
    mode = getattr(config, "plane_mode", None)
    if mode is None:
        mode = "self_hosted" if (getattr(config, "control_plane_url", None) or "").strip() else "off"
    if mode == "off":
        return LocalState()
    try:
        client = PlaneClient(
            config.control_plane_url,
            config.token,
            config.service,
            config.resolved_worker_id(),
        )
        exporter = Exporter(
            client,
            include_events=bool(config.export_events),
            send_session_keys=bool(config.send_session_keys),
            include_calls=not getattr(config, "otel_endpoint", None),
        )
        return RemoteState(client, exporter, config)
    except Exception:
        _LOG.warning(
            "runbound: could not build the control plane link; running local-only",
            exc_info=True,
        )
        return LocalState()


LEGACY_DOCUMENT_WARNING = (
    "runbound: this control plane still serves the legacy organisation policy document (deny/allow lists). "
    "It is deprecated: it is still enforced in this release and stops being read in a later one. "
    "Move its rules to per-tool rules in the console."
)


def _warn_legacy_document() -> None:
    """The plane served a non-null ``policy`` document: say once per process that it is deprecated. It is still enforced."""
    from .hooks import _warn_once

    _warn_once(("legacy-organisation-policy-document",), LEGACY_DOCUMENT_WARNING)


_UNREAD_WARNED_AT = [0.0]


def _warn_policy_unread() -> None:
    """One warning a minute that the plane could not read its rows (the marker ``rows_hash == ""``)."""
    now = time.monotonic()
    if now - _UNREAD_WARNED_AT[0] >= 60.0 or _UNREAD_WARNED_AT[0] == 0.0:
        _UNREAD_WARNED_AT[0] = now
        _LOG.warning("runbound: the control plane could not read its policy rows; keeping the rules held and trying again")


def _policy_envelope(data: dict, version: int) -> tuple[dict | None, int, bool]:
    """Split a policy response into ``(policy, version, dry_run)``.

    Two shapes arrive: the envelope ``{"version", "dry_run", "policy"}`` and the
    policy body itself. A malformed envelope degrades to "no policy".
    """
    if not isinstance(data, dict):
        return None, version, False
    if "policy" in data or "version" in data:
        body = data.get("policy")
        resolved = _as_int(data.get("version"), version)
        return (body if isinstance(body, dict) else None), resolved, bool(data.get("dry_run", False))
    return data, version, False


def _executing_approvals() -> list:
    try:
        from . import approvals

        return approvals.executing_ids()
    except Exception:
        return []


def _active_sessions() -> int:
    """Keyed sessions currently open in this process; 0 on any failure."""
    try:
        from . import api

        return api._active_count()
    except Exception:
        return 0


def _coverage_payload() -> dict:
    """The coverage numbers worth a heartbeat, or ``{}`` on failure."""
    try:
        from . import _coverage

        return _coverage.snapshot()
    except Exception:
        _LOG.debug("runbound: could not build the coverage payload", exc_info=True)
        return {}


def policy_features() -> tuple:
    """The R2 policy features this SDK actually enforces; it never reports one it does not (TR2-O2 AC1)."""
    from . import lines

    return lines.FEATURES


def _halted_tools(value: Any) -> dict:
    """``{tool: reasons}`` from the reply's ``halted_tools`` (``[{"tool", "reasons"}]``); anything else is ignored."""
    out: dict = {}
    for item in value if isinstance(value, list) else ():
        if isinstance(item, dict) and isinstance(item.get("tool"), str) and item["tool"]:
            reasons = item.get("reasons")
            out[item["tool"]] = [str(r) for r in reasons] if isinstance(reasons, list) else []
    return out


def _as_int(value: Any, default: int = 0) -> int:
    """``value`` as an int, ``default`` when it is not one (a bool is not)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _sdk_version() -> str:
    """This SDK's version, imported late so the package's own import order never matters."""
    try:
        from . import __version__

        return __version__
    except Exception:
        return "unknown"
