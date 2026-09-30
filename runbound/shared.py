"""Shared session state — one key's budget across every worker that serves it.

Without a control plane a runbound process knows only what it did itself: a
$5 budget is $5 *per worker*, and a key eight replicas are serving can spend
$40. :class:`SharedState` is the seam that fixes that. :class:`LocalState` is
the single-process behavior, unchanged and free; :class:`RemoteState` asks a
control plane at four moments and nowhere else — a session opens, a session
closes, something trips, a provider circuit changes.

Three rules hold everywhere in this module:

* **The plane is an optimization, never a dependency.** Every method catches
  everything and falls back to the local answer. A plane that is down, slow or
  rejecting our key costs a session one 150 ms timeout, once, and then nothing
  at all: after :data:`DEGRADE_AFTER` failures in a row the link is *degraded*
  and stops calling, retrying one real call every :data:`RETRY_EVERY_S`.
* **Hashes and counts, never content.** A key travels as
  :func:`~runbound.plane_types.key_hash` of itself unless the customer opted
  into ``send_session_keys``; tags are scrubbed and capped; an anomaly is
  reduced to its wire form before it leaves.
* **No network under a lock.** The lock here guards a handful of fields for a
  few microseconds; every HTTP call is made outside it, so a slow plane can
  never serialize the workers that are not talking to it.

What the plane says is cached, not trusted forever: an entry decision is good
for :data:`DECISION_TTL_S`, and a fleet-wide halt we have not heard confirmed
for :data:`STALE_HALT_S` stops being enforced. A plane that vanishes must not
leave a fleet stopped.
"""

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from . import local_events, responses
from .export import Exporter
from .plane import WARN_INTERVAL_S, PlaneClient, Poller, _PeriodicWarning, warn_periodically
from .plane_types import (
    EntryDecision,
    ExitDelta,
    PlaneStatus,
    TripReport,
    anomaly_to_wire,
    key_hash,
    scrub_tags,
    to_wire,
)
from .state import PostureState, make_posture_state

_LOG = logging.getLogger("runbound")

#: Default for ``control_plane_cache_s``: how long one key's entry decision is
#: reused before the plane is asked again. Short enough that a fleet-wide
#: budget converges within a turn, long enough that a retry loop of
#: ``session()`` blocks costs one request, not a hundred.
DECISION_TTL_S = 5.0

#: Consecutive failed calls before the link is called degraded and stops
#: asking. Three, so a single dropped packet is not an outage.
DEGRADE_AFTER = 3

#: How often a degraded link spends one real call finding out if the plane is
#: back. Everything in between is answered locally, with no network at all.
RETRY_EVERY_S = 30.0

#: How long a halt outlives the last contact with the plane. A halt is the
#: heaviest thing the plane can say, so it is also the first thing we stop
#: believing when the plane goes quiet: after this, the fleet runs again.
STALE_HALT_S = 60.0

#: How far back the session-entry window looks. A heartbeat can succeed
#: throughout while most entries are actually being decided locally — a
#: plane slow enough to miss ``control_plane_timeout_s`` on the hot path but
#: fast enough to answer the background heartbeat — and the consecutive
#: failure count above never catches that: one entry timeout is a single
#: failure, and the next successful heartbeat resets it before three ever
#: land in a row. This window is a second, independent signal: how the last
#: minute's entries were actually decided.
ENTRY_WINDOW_S = 60.0

#: Fewest entries the window needs before its local share means anything —
#: a fleet that has opened two sessions in the last minute is not "half
#: local" because one of them fell back.
ENTRY_WINDOW_MIN_ENTRIES = 10

#: The local share above which the link is degraded even while heartbeats
#: succeed — more entries decided on this worker's own numbers than by the
#: plane and the cache combined.
ENTRY_LOCAL_SHARE_THRESHOLD = 0.5

#: The causes a "local" entry outcome can carry — why the plane could not
#: answer this particular session entry. ``"timeout"`` is the client giving
#: up quietly past ``control_plane_timeout_s`` (or the entry being skipped
#: outright on an already-degraded link — see :meth:`RemoteState._may_call`);
#: ``"plane_loss"`` is the plane naming
#: :data:`~runbound.plane_types.FLEET_STATE_UNAVAILABLE_CAUSE` on its 503 —
#: its own live store is unreachable, so it refuses to guess from defaults;
#: ``"plane_unavailable"`` is the same 503 *shape* with no such cause — the
#: plane answered but could not help for some other reason (a saturated
#: connection pool, a handler bug, any other failure its own generic
#: fail-open catches); ``"error"`` is everything else — a connection
#: refused, a malformed reply, any failure that never got a plane-shaped
#: answer at all. See :meth:`RemoteState._last_known_cause`.
#:
#: ``"plane_loss"`` and ``"plane_unavailable"`` look alike from the outside
#: (both are the plane itself answering 503) and used to be indistinguishable
#: on the wire — every 503 an SDK-path handler on the plane could produce
#: carried the identical body, so a plane whose database was merely
#: saturated read the same "fleet state unavailable" a real state outage
#: would. The plane's ``cause`` field (additive; a plane one version
#: behind this never sends it, and a body with no ``cause`` reads as
#: ``"plane_unavailable"`` here, never ``"plane_loss"``) is what splits them.
LOCAL_CAUSES = ("timeout", "plane_loss", "plane_unavailable", "error")

#: One human sentence per :data:`LOCAL_CAUSES` entry, read by
#: :meth:`RemoteState.status` when the entry window (not the older
#: consecutive-failure count) is what degrades the link — see
#: :meth:`RemoteState._window_reason`. ``"plane_loss"`` reads as the same
#: ``"fleet state unavailable"`` string the hello-based signal uses:
#: from the SDK's point of view a 503 naming that specific cause *is* the
#: plane saying its state is unavailable, whether that is learned from the
#: last heartbeat or from the entry call itself. ``"plane_unavailable"``
#: and ``"error"`` are the two reasons this task adds — plain words in the
#: same register as the other two, documented in
#: ``docs/guides/fleet-mode.md``'s reason table.
REASON_BY_LOCAL_CAUSE = {
    "timeout": "entry timeouts",
    "plane_loss": "fleet state unavailable",
    "plane_unavailable": "plane unavailable",
    "error": "plane errors",
}

#: The order ties break in when the entry window's local outcomes are split
#: evenly across more than one cause: ``"plane_loss"`` first (the plane
#: naming its own state as the problem is the most actionable fact to
#: surface), then ``"plane_unavailable"`` (the plane still answered, just
#: not with a specific cause), then ``"timeout"`` (a narrower,
#: already-documented case), then ``"error"`` (the catch-all — no answer
#: from the plane at all). See :meth:`RemoteState._window_reason`.
LOCAL_CAUSE_TIE_BREAK = ("plane_loss", "plane_unavailable", "timeout", "error")

#: Most keys the decision cache holds. Entries expire on their own; this is
#: the ceiling for a process churning through keys faster than that.
DECISION_CACHE_MAX = 1024

#: Most "refused and shown" entries one heartbeat carries — a
#: worker's own configuration only has a handful of Controls fields to
#: protect, so this is a sanity ceiling, not a real limit.
CONTROLS_REFUSED_MAX = 16

#: How often the plane's ``notice`` — the entitlement nudge a customer is meant
#: to act on — is written to the log. Once an hour: it is a billing fact, not
#: an incident, and a five-second heartbeat repeating it would be noise.
NOTICE_INTERVAL_S = 3600.0

#: How long a notice must be *continuously present* before it is written to
#: the log at all — twice the control plane's own heartbeat TTL, which is
#: 15 seconds. A rolling deploy of a single-worker (free-plan) customer
#: overlaps two heartbeats for up to that TTL, during which the plane
#: legitimately counts two live workers and denies
#: ``workers_synced_exceeded`` to both — a real entitlement flip, but a
#: deploy blip, not sustained overage. This constant only gates *logging*:
#: ``limited`` and the telemetry lanes still flip on the very first reply,
#: silently, so a customer genuinely over their limit is limited
#: immediately. It gates the existing :meth:`RemoteState._log_notice` path —
#: once satisfied, that path's own once-an-hour cadence takes over
#: unchanged; this is a gate in front of it, not a second logging mechanism.
NOTICE_DEBOUNCE_S: float = 30.0

#: Entitlement codes the plane sends in ``entitlements["denied"]``. The first
#: two turn the telemetry lanes off; the third puts this worker in ``limited``
#: mode, where entry questions are answered locally.
EVENTS_DENIED_CODES = ("events_denied", "events_over_cap")
WORKERS_DENIED_CODE = "workers_synced_exceeded"

#: The hosted control plane's URL, once there is one. ``None`` for now,
#: because there is no hosted plane yet — and a placeholder is worse than
#: nothing. An earlier cut of this named a domain that had been registered
#: but pointed at a parked host, which cost every ``init()`` with a token in
#: the environment a DNS lookup and a doomed TCP attempt, a ~45-line urllib
#: traceback at WARNING, and a re-probe every 30 seconds, purely because
#: nothing was listening. Name no domain here until one is ours and
#: serving. With this ``None``, a bare ``token`` and
#: no ``control_plane_url`` (see
#: :meth:`runbound.config.GuardrailConfig.validate`) logs one WARNING —
#: connect a self-hosted plane with ``control_plane_url``, or wait — and the
#: process stays local. The day hosting goes live this one constant changes
#: to the real URL and ``init(token=...)`` starts connecting on its own, with
#: no other code touched.
HOSTED_PLANE_URL = None


def _iso() -> str:
    """Now, as an ISO-8601 UTC stamp — the form every wire record carries."""
    stamp = datetime.now(timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SharedState(Protocol):
    """What the engine and the api know about the world outside this process.

    The methods on the hot paths — ``enter``, ``exit``, ``trip``, ``clear``,
    ``circuit``, ``halted`` — must be cheap and must never raise; the rest
    report (``policy``, ``status``, ``fleet_status``) or wire the background
    threads up and down (``observers``, ``start``, ``stop``).
    :class:`LocalState` implements every one of them as a no-op; that is the
    SDK's default and its single-process behavior.
    """

    def enter(
        self, key: str, state: Any, config: Any
    ) -> EntryDecision | None:  # pragma: no cover - protocol
        """The plane's answer for a session that is opening, or ``None``."""

    def exit(
        self, key: str, state: Any, delta: ExitDelta
    ) -> None:  # pragma: no cover - protocol
        """Report what one session block added."""

    def trip(
        self, key: str | None, state: Any, anomaly: Any, ttl: float | None, door: bool
    ) -> None:  # pragma: no cover - protocol
        """Tell the fleet that this key has tripped."""

    def clear(self, key: str) -> None:  # pragma: no cover - protocol
        """Forget everything the fleet remembers about one key."""

    def circuit(
        self, label: str, state: str, failures: int, cooldown_s: float
    ) -> None:  # pragma: no cover - protocol
        """Report a provider circuit transition."""

    def halted(self) -> bool:  # pragma: no cover - protocol
        """Is the whole fleet stopped right now, by either halt mode?"""

    def halt_mode(self) -> "str | None":  # pragma: no cover - protocol
        """``"stop"``/``"narrow"``, or ``None`` when no halt is in effect
        — see :meth:`RemoteState.halt_mode`."""

    def halt_posture_directive(self) -> "PostureState | None":  # pragma: no cover - protocol
        """The posture a fleet-wide Narrow halt states, or ``None``."""

    def posture_directive(self) -> "PostureState | None":  # pragma: no cover - protocol
        """The posture the plane states for this service, or ``None``."""
        ...

    def controls_directive(self) -> dict | None:  # pragma: no cover - protocol
        """The Controls body the plane last stated for this service,
        or ``None`` — no plane, nothing served, a ``dry_run`` row, or a link
        stale past the halt's own staleness rule. See :class:`RemoteState`."""

    def service_baseline(self) -> "tuple[float, float] | None":  # pragma: no cover - protocol
        """This service's median spike baseline, off the same
        Controls body ``controls_directive`` reads — but never gated on
        ``dry_run``: a peer baseline is data, not an enforcement directive.
        ``None`` without a plane, or when the plane has nothing for this
        service yet."""

    def policy(self) -> dict | None:  # pragma: no cover - protocol
        """The org's tool policy, as the plane last stated it."""

    def status(self) -> PlaneStatus:  # pragma: no cover - protocol
        """Where the link to the plane stands, for a health endpoint."""

    def pending_events(self) -> "int | None":  # pragma: no cover - protocol
        """State/telemetry records still queued to post, or ``None``
        without an exporter — see :class:`RemoteState`."""

    def fleet_status(self, key: str) -> dict | None:  # pragma: no cover - protocol
        """What the plane last said about one key, or ``None``."""

    #: Whether this state is backed by anything outside the process. ``False``
    #: for :class:`LocalState`, and what lets the engine and the api skip the
    #: fleet bookkeeping entirely when there is no plane to send it to.
    fleet: bool

    def observers(self) -> list:  # pragma: no cover - protocol
        """Engine observers this link needs installed (the exporter)."""

    def start(self, engine: Any) -> None:  # pragma: no cover - protocol
        """Attach to a live engine and start the background threads."""

    def stop(self) -> None:  # pragma: no cover - protocol
        """Stop those threads and drain what is queued."""


class LocalState:
    """No plane: every answer is the one this process already had.

    The default, and the whole of runbound's behavior before fleet mode
    existed. Nothing here allocates, locks, or reaches the network, so a
    process that never configures a control plane pays nothing for the seam.
    """

    #: Nothing outside this process is listening, so the engine and the api
    #: skip the fleet bookkeeping altogether: no circuit-state read before a
    #: successful model call, no exit delta computed when a block closes.
    fleet = False

    def enter(self, key: str, state: Any, config: Any) -> EntryDecision | None:
        """Nothing to say: the session's own counters are the whole truth."""
        return None

    def exit(self, key: str, state: Any, delta: ExitDelta) -> None:
        """Nobody to tell."""

    def trip(
        self, key: str | None, state: Any, anomaly: Any, ttl: float | None, door: bool
    ) -> None:
        """Nobody to tell."""

    def clear(self, key: str) -> None:
        """Nothing outside this process remembers the key."""

    def circuit(self, label: str, state: str, failures: int, cooldown_s: float) -> None:
        """Nobody to tell."""

    def halted(self) -> bool:
        """A fleet of one is never halted from outside."""
        return False

    def halt_mode(self) -> "str | None":
        """A fleet of one has no halt to have a mode."""
        return None

    def halt_posture_directive(self) -> "PostureState | None":
        """A fleet of one is never narrowed by a halt."""
        return None

    def posture_directive(self) -> "PostureState | None":
        """A fleet of one is never narrowed from outside."""
        return None

    def controls_directive(self) -> dict | None:
        """A fleet of one has no Controls to be tightened by."""
        return None

    def service_baseline(self) -> "tuple[float, float] | None":
        """A fleet of one has no peers to be judged against."""
        return None

    def policy(self) -> dict | None:
        """Only the local ``tool_policy`` applies."""
        return None

    @property
    def policy_version(self) -> int:
        """No remote policy, so version zero, forever."""
        return 0

    @property
    def policy_dry_run(self) -> bool:
        """Nothing is being rolled out from anywhere."""
        return False

    def status(self) -> PlaneStatus:
        """``mode="local"`` — the honest answer for "no plane configured"."""
        return PlaneStatus(mode="local")

    def pending_events(self) -> "int | None":
        """A fleet of one has no exporter to queue anything in."""
        return None

    def fleet_status(self, key: str) -> dict | None:
        """There is no fleet."""
        return None

    def observers(self) -> list:
        """Nothing to observe with."""
        return []

    def start(self, engine: Any) -> None:
        """No threads to start."""

    def stop(self) -> None:
        """No threads to stop."""


class RemoteState:
    """Shared state backed by a control plane, and by local answers when not.

    ``client`` is a :class:`~runbound.plane.PlaneClient` (or anything with
    its methods), ``exporter`` an :class:`~runbound.export.Exporter` — the
    state lane, carrying exits, circuits and refused trips whatever
    ``export_events`` says, and ``None`` only in a test — and ``now`` the
    monotonic clock, injected so tests can move it.

    Entry decisions are cached per key for :data:`DECISION_TTL_S`; failures are
    counted and, past :data:`DEGRADE_AFTER`, stop the calling entirely until
    the next :data:`RETRY_EVERY_S` probe. ``breaker`` is the engine's circuit
    breaker, attached by :meth:`start`, and is what a fleet-wide circuit
    instruction acts on.
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
        self._cache: "OrderedDict[str, tuple[float, EntryDecision]]" = OrderedDict()
        #: The trailing :data:`ENTRY_WINDOW_S` of session-entry outcomes,
        #: oldest first — each a ``(timestamp, "plane" | "cache" | "local",
        #: cause, seq)`` tuple. ``cause`` is ``None`` for ``"plane"``/``"cache"``
        #: and one of :data:`LOCAL_CAUSES` for ``"local"`` — see
        #: :meth:`_last_known_cause`. ``seq`` orders outcomes independently of
        #: the clock, so "recorded before the plane answered again" never
        #: depends on two timestamps differing. Written by
        #: :meth:`_record_entry_outcome`, read (and pruned) by
        #: :meth:`_entry_window_counts`. Under ``self._lock`` like every
        #: other field here.
        self._entry_outcomes: "deque[tuple[float, str, str | None, int]]" = deque()
        self._entry_seq = 0
        #: Local decisions caused by lost fleet state with a ``seq`` below
        #: this stop counting toward a degraded link: the plane has since
        #: answered an entry, or a heartbeat has said its state is back, so
        #: that outage is over. They stay in :meth:`_entry_window_counts` —
        #: the window reports what happened; ``status()`` reports what is
        #: failing now. Timeouts and other errors are never superseded this
        #: way: one good answer does not prove a slow plane is fast again.
        self._state_loss_superseded_seq = 0
        self._failures = 0
        self._last_success: float | None = None
        self._last_attempt: float | None = None
        self._last_failure_kind: str | None = None
        self._halt = False
        #: Which kind of halt ``self._halt`` is, once it is true --
        #: "stop" until a heartbeat's ``halt_mode`` says otherwise, and reset
        #: to "stop" the moment ``self._halt`` goes false, so a stale, no
        #: longer meaningful mode never lingers into the next halt. See
        #: :meth:`halt_mode` and :meth:`_halt_state`.
        self._halt_mode: str = "stop"
        #: The Narrow halt's own posture slot, entirely separate from
        #: ``self._posture`` below (the Controls-stated one) -- see
        #: :meth:`_absorb_halt_posture` and its own warning about the two
        #: plane-driven postures clobbering each other.
        self._halt_posture: PostureState | None = None
        self._posture: PostureState | None = None
        self._notice: str | None = None
        self._policy: dict | None = None
        self._policy_version = 0
        self._policy_dry_run = False
        #: The Controls body the plane last served for this service,
        #: its version and whether that row is still ``dry_run`` — see
        #: :meth:`controls_directive`. Fetched the same way ``_policy`` is:
        #: only when ``HelloReply.controls_version`` changes.
        self._controls_body: dict | None = None
        self._controls_version = 0
        self._controls_dry_run = False
        #: The worker whose ``on_anomaly`` this heartbeat reports as
        #: ``can_stop`` — set by :meth:`start`, read-only otherwise.
        self._engine: Any = None
        self._entitlements: dict = {}
        self._limited = False
        #: Monotonic timestamp of when a notice was first seen continuously
        #: present, or ``None`` while no notice is present. Read and written
        #: only from :meth:`_track_notice_presence`, under ``self._lock``.
        self._notice_since: float | None = None
        self._notice_warning = _PeriodicWarning(NOTICE_INTERVAL_S, now)
        self._invalid_key_warning = _PeriodicWarning(WARN_INTERVAL_S, now)
        self._poller: Any = None
        #: The tool-report hash the plane has acknowledged, and the one riding
        #: a heartbeat whose reply has not come back yet. Both under
        #: ``self._lock``; see :meth:`_tools_payload`.
        self._tools_sent_hash: str | None = None
        self._tools_pending_hash: str | None = None
        self.breaker: Any = None
        #: Is the plane's own fleet state (its live store) currently
        #: unavailable -- the last hello said so? Set and cleared only by
        #: :meth:`apply_hello`, under ``self._lock``. While true, a halt,
        #: its Narrow posture, the Controls-stated posture and the policy/
        #: controls versions are held exactly as they were rather than
        #: absorbed from the reply -- see :meth:`apply_hello`.
        self._fleet_state_unavailable = False
        #: Monotonic timestamp of the moment fleet state was *first* seen
        #: unavailable in the current streak, or ``None`` while it is not.
        #: This, not ``self._last_success`` (the link itself keeps
        #: succeeding throughout), is what ``stale_halt`` measures a held
        #: halt's staleness from — see :meth:`_staleness_clock_locked`.
        self._state_unavailable_since: float | None = None

    # --- the customer's settings, read defensively ---------------------------

    def _cache_ttl_s(self) -> float:
        """How long an entry decision is reused: ``control_plane_cache_s``."""
        ttl = getattr(self._config, "control_plane_cache_s", DECISION_TTL_S)
        return float(ttl) if isinstance(ttl, (int, float)) else DECISION_TTL_S

    def _send_session_keys(self) -> bool:
        """Has the customer opted into putting raw keys on the wire?"""
        return bool(getattr(self._config, "send_session_keys", False))

    def _stale_halt_mode(self) -> str:
        """``"release"`` (default) or ``"hold"`` — read defensively, config.py

        may not validate this field yet. An unrecognized value behaves like
        the documented default rather than raising.
        """
        mode = getattr(self._config, "stale_halt", "release")
        return mode if mode in ("release", "hold") else "release"

    def _on_plane_loss_mode(self) -> str:
        """``"guard_locally"`` (default) or ``"refuse"`` — read defensively,

        same reasoning as :meth:`_stale_halt_mode`.
        """
        mode = getattr(self._config, "on_plane_loss", "guard_locally")
        return mode if mode in ("guard_locally", "refuse") else "guard_locally"

    # --- entering a session ------------------------------------------------

    def enter(self, key: str, state: Any, config: Any) -> EntryDecision | None:
        """Ask the plane what the fleet already knows about ``key``.

        ``None`` means "decide locally". Under ``on_plane_loss="guard_locally"``
        (the default) that is the answer for every way this can go wrong: a
        cached miss on a degraded link, a timeout, a rejected key, a session
        whose counters cannot be read, or a plan whose synced worker count has
        run out (:attr:`limited`). Under ``on_plane_loss="refuse"`` a degraded
        link, a timeout or an error instead returns a refusal decision from
        :meth:`_plane_loss_refusal` — see there for what is deliberately
        exempt. The caller applies a decision it gets and is unchanged by one
        it does not.

        An invalid API key is not plane loss at all — it is a configuration
        error, terminal until the key is fixed and :meth:`PlaneClient.reset_key`
        runs — so it guards locally in *both* modes rather than refusing every
        session forever, with a warning at most once a minute naming the real
        cause (see :meth:`_invalid_key`).

        A decision already in the cache is still served in ``limited`` mode
        and under ``on_plane_loss="refuse"`` alike: the plane raises the floor
        of what this worker knows and is never allowed to lower it, so a latch
        we were told about does not evaporate because the org went a worker
        over its plan, and a fresh answer is not replaced by a manufactured
        refusal just because the *next* call would have failed.

        A hit in the decision cache costs no network at all, which is what
        makes a retry loop of ``session()`` blocks affordable.
        """
        try:
            digest = key_hash(key)
            cached = self._cached(digest)
            if cached is not None:
                self._record_entry_outcome("cache")
                return cached
            if self.limited:
                # Not plane loss: the plane is answering fine, this worker is
                # simply over its plan's synced-worker count and is told to
                # decide locally regardless of on_plane_loss — a customer who
                # opted into "refuse" wants safety when the plane cannot be
                # heard, not a fleet-wide outage because one plan limit was
                # hit. Guarding continues unaffected either way. Not folded
                # into the entry window either: that window is a health
                # signal about the plane link, and a plan limit is neither
                # a timeout nor an error.
                return None
            if getattr(self._client, "key_state", None) == "invalid":
                return self._invalid_key()
            if not self._may_call():
                self._record_entry_outcome("local", cause=self._last_known_cause())
                return self._plane_loss_refusal("degraded")
            payload = self._entry_payload(key, digest, state, config)
        except Exception:
            # Our bug (key_hash, the cache, or payload construction raised),
            # not plane loss: stay fail-open and let the caller decide
            # locally, same as always.
            _LOG.warning(
                "runbound: could not ask the control plane about a session",
                exc_info=True,
            )
            return None
        entry_kind: str | None = None

        def call_plane() -> EntryDecision | None:
            # Prefer enter_with_kind's own, synchronous answer over reading
            # last_failure_kind back afterward (what _last_known_cause does,
            # the fallback below): that shared attribute is whole-client, so
            # a concurrent call on the same client -- the heartbeat, always,
            # in practice -- can clear or overwrite it before this call's
            # caller gets to look. A duck-typed client without the method
            # (every test double outside this module) falls back to the old
            # path unchanged.
            nonlocal entry_kind
            entering = getattr(self._client, "enter_with_kind", None)
            if entering is None:
                return self._client.enter(payload)
            decision, entry_kind = entering(payload)
            return decision

        decision = self._attempt(call_plane, "enter")
        if decision is None:
            cause = entry_kind if entry_kind in LOCAL_CAUSES else self._last_known_cause()
            self._record_entry_outcome("local", cause=cause)
            return self._plane_loss_refusal(cause)
        self._record_entry_outcome("plane")
        self._remember(digest, decision)
        self._absorb(decision)
        return decision

    def _invalid_key(self) -> None:
        """Guard locally because the plane rejected our API key.

        A rejected key is a configuration mistake on this worker, not the
        plane being unreachable — the plane answered, and its answer was
        "no". Refusing every session forever over it (what ``on_plane_loss=
        "refuse"`` would otherwise do, since :meth:`_may_call` also returns
        ``False`` here) would turn a typo'd key into a full outage; instead
        this guards locally in both modes, exactly as ``guard_locally``
        always has, and logs at most once a minute so the real cause is
        findable instead of a generic "unreachable" message repeating.
        """
        warn_periodically(
            self._invalid_key_warning,
            "runbound: the control plane rejected this API key; deciding "
            "locally until reconfigured",
        )
        return None

    def _plane_loss_refusal(self, mode: str) -> EntryDecision | None:
        """The answer when the plane could not be asked or did not answer.

        ``"guard_locally"`` returns ``None`` here — decide on this worker's
        own numbers, exactly as with no plane at all. ``"refuse"`` instead
        hands back a refusal :class:`~runbound.plane_types.EntryDecision`:
        not remembered in the decision cache (the next entry asks again),
        and carrying none of the fleet facts (``latch``, ``strikes``,
        ``generation``, ``halt`` all stay at their defaults) so a caller that
        applies it changes nothing about this session's fleet state — only
        the one entry question in front of it is refused.
        """
        if self._on_plane_loss_mode() != "refuse":
            return None
        return EntryDecision(
            allow=False,
            refusal={
                "detector": "plane",
                "severity": "critical",
                "message": (
                    "Control plane unreachable; refusing session entry "
                    "(on_plane_loss='refuse')"
                ),
                "details": {"reason": "plane_unreachable", "mode": mode},
            },
        )

    def _last_known_cause(self) -> str:
        """A best-effort guess at why the plane could not be reached, for
        the one path in :meth:`enter` that has no call of its own to ask —
        the link is already degraded, so nothing was attempted this time
        (:meth:`_may_call` said no) — plus any duck-typed client that has
        no :meth:`~runbound.plane.PlaneClient.enter_with_kind` at all.

        :class:`~runbound.plane.PlaneClient` classifies its own failures
        (``last_failure_kind``: ``"timeout"``, ``"error"`` or
        ``"plane_loss"`` — the last for a 503 carrying the plane-loss body,
        see its module docstring); read first, since it is the real client's
        own, more specific answer. Whole-client state, though — a concurrent
        call on the same client (the heartbeat, in practice) can already
        have overwritten it by the time this reads it, which is exactly why
        :meth:`enter` prefers ``enter_with_kind``'s own, race-free answer
        for the one path that actually attempted a call this time and
        reaches this only as its fallback. A duck-typed client that does
        not track ``last_failure_kind`` at all (every test double in this
        module's tests) falls back further, to this object's own coarser
        record (:attr:`_last_failure_kind`, set by :meth:`_record` from
        whether the call raised or merely returned ``None`` —
        ``"error"``/``"timeout"`` respectively), and finally to ``"error"``
        if nothing has failed yet at all — reachable only from the
        already-degraded branch of :meth:`enter` on a link that somehow
        never recorded a failure kind, which :meth:`_may_call` cannot
        produce in practice (three real failures always run through
        :meth:`_record` first).
        """
        with self._lock:
            kind = self._last_failure_kind
        client_kind = getattr(self._client, "last_failure_kind", None)
        cause = client_kind or kind or "error"
        return cause if cause in LOCAL_CAUSES else "error"

    def _entry_payload(self, key: str, digest: str, state: Any, config: Any) -> dict:
        """What one session entry tells the plane: who, where, how much so far."""
        lock = getattr(state, "lock", None)
        if lock is not None:
            with lock:
                spend = float(getattr(state, "total_cost_usd", 0.0))
                tokens = int(getattr(state, "total_tokens", 0))
        else:  # pragma: no cover - a session double without a lock
            spend, tokens = 0.0, 0
        payload = {
            "key_hash": digest,
            "tags": scrub_tags(getattr(state, "tags", None)),
            "service": config.service,
            "worker_id": config.resolved_worker_id(),
            "budget_usd": config.budget_usd,
            "local_spend_usd": spend,
            "local_total_tokens": tokens,
        }
        if config.send_session_keys:
            payload["key"] = key
        return payload

    def _absorb(self, decision: EntryDecision) -> None:
        """Take the fleet-wide facts an entry decision carries in passing.

        A decision can *raise* a halt the heartbeat has not reported yet, but
        never lift one: ``halt`` is false by default on the wire, and a plane
        that only sets it on the heartbeat must not have every session entry
        quietly cancel it. Lifting a halt is the heartbeat's job.

        ``EntryDecision`` has no mode of its own: it only ever means
        "stop" -- a Narrow halt never refuses at the door, so it never has a
        reason to ride ``/enter``. Setting ``_halt_mode`` here too keeps a
        halt this session discovered early from reading as "narrow" (and
        therefore posture-only, non-refusing) before the next heartbeat has
        even said what kind it is.
        """
        if not decision.halt:
            return
        with self._lock:
            self._halt = True
            self._halt_mode = "stop"

    # --- the decision cache -------------------------------------------------

    def _cached(self, digest: str) -> EntryDecision | None:
        """This key's decision if it is still fresh. Expired entries are dropped."""
        now = self._now()
        with self._lock:
            entry = self._cache.get(digest)
            if entry is None:
                return None
            at, decision = entry
            if now - at >= self._cache_ttl_s():
                self._cache.pop(digest, None)
                return None
            self._cache.move_to_end(digest)
            return decision

    def _remember(self, digest: str, decision: EntryDecision) -> None:
        with self._lock:
            self._cache[digest] = (self._now(), decision)
            self._cache.move_to_end(digest)
            while len(self._cache) > DECISION_CACHE_MAX:
                self._cache.popitem(last=False)

    # --- the rolling entry window -------------------------------------------

    def _record_entry_outcome(self, outcome: str, cause: "str | None" = None) -> None:
        """Fold one session-entry decision into the trailing window.

        ``outcome`` is ``"plane"`` (a fresh answer over the network),
        ``"cache"`` (a decision cache hit) or ``"local"`` (decided on this
        worker's own numbers because the plane could not be asked, or did
        not answer in time). ``cause`` is one of :data:`LOCAL_CAUSES` for a
        ``"local"`` outcome — ``None`` (the default) for the other two, and
        normalized to ``"error"`` if a caller passes anything else, so a bad
        value never breaks the majority-cause arithmetic in
        :meth:`_window_reason`. Never raises: a window this cannot update is
        a diagnostic gap, not a reason to fail the entry it describes.
        """
        try:
            now = self._now()
            recorded = cause if cause in LOCAL_CAUSES else (None if cause is None else "error")
            with self._lock:
                self._entry_seq += 1
                self._entry_outcomes.append((now, outcome, recorded, self._entry_seq))
                if outcome == "plane":
                    # A fresh answer needs the plane's own state, so any
                    # state loss recorded before it has ended.
                    self._state_loss_superseded_seq = self._entry_seq
                self._prune_entry_window(now)
        except Exception:
            _LOG.debug("runbound: could not record an entry outcome", exc_info=True)

    def _prune_entry_window(self, now: float) -> None:
        """Drop outcomes older than :data:`ENTRY_WINDOW_S`. Caller holds ``self._lock``."""
        window = self._entry_outcomes
        while window and now - window[0][0] >= ENTRY_WINDOW_S:
            window.popleft()

    def _entry_window_counts(self) -> dict:
        """``{"plane": n, "cache": n, "local": n, "local_causes": {...}}``
        over the live window.

        The three top-level counts are unchanged from before this method
        gained ``local_causes`` — every existing reader of them sees the
        same numbers it always did. ``local_causes`` has one zero-filled
        entry per :data:`LOCAL_CAUSES` (``"timeout"``, ``"plane_loss"``,
        ``"plane_unavailable"``, ``"error"``), so a caller never has to
        guard a missing key, and it always sums to the top-level
        ``"local"`` count.

        Pruned on read as well as on write, so a process that has gone idle
        for a while does not keep reporting stale outcomes just because
        nothing new has come in to trigger a prune.
        """
        try:
            now = self._now()
            with self._lock:
                self._prune_entry_window(now)
                counts = {"plane": 0, "cache": 0, "local": 0}
                causes = {cause: 0 for cause in LOCAL_CAUSES}
                for _, outcome, cause, _seq in self._entry_outcomes:
                    if outcome in counts:
                        counts[outcome] += 1
                    if outcome == "local" and cause in causes:
                        causes[cause] += 1
            counts["local_causes"] = causes
            return counts
        except Exception:
            _LOG.debug("runbound: could not read the entry window", exc_info=True)
            return {"plane": 0, "cache": 0, "local": 0, "local_causes": dict.fromkeys(LOCAL_CAUSES, 0)}

    def _live_entry_counts(self) -> tuple:
        """``(total, local, local_causes)`` over the window, as evidence of
        what is failing *now*: the same outcomes as
        :meth:`_entry_window_counts` minus local decisions caused by lost
        fleet state that the plane has since superseded (see
        ``_state_loss_superseded_seq``). Those are left out of the total
        too, not counted as successes: an outage that is over is simply no
        longer evidence either way. Never raises.
        """
        try:
            now = self._now()
            with self._lock:
                self._prune_entry_window(now)
                superseded = self._state_loss_superseded_seq
                total = local = 0
                causes = {cause: 0 for cause in LOCAL_CAUSES}
                for _, outcome, cause, seq in self._entry_outcomes:
                    if outcome == "local" and cause == "plane_loss" and seq < superseded:
                        continue
                    total += 1
                    if outcome == "local":
                        local += 1
                        if cause in causes:
                            causes[cause] += 1
            return total, local, causes
        except Exception:
            _LOG.debug("runbound: could not read the live entry window", exc_info=True)
            return 0, 0, dict.fromkeys(LOCAL_CAUSES, 0)

    def fleet_status(self, key: str) -> dict | None:
        """What the plane last said about ``key``, or ``None`` if nothing fresh.

        Plain data for a dashboard or a support tool::

            {"fleet_spend_usd": 4.8, "fleet_tokens": 9000, "strikes": 1,
             "generation": 3, "halt": False, "latched": True,
             "policy_version": 4, "age_s": 1.2}

        Reads the cache only — asking this never opens a socket, so a status
        endpoint polling it cannot slow the plane down or be slowed by it.
        """
        try:
            digest = key_hash(key)
            now = self._now()
            with self._lock:
                entry = self._cache.get(digest)
                version = self._policy_version
            if entry is None:
                return None
            at, decision = entry
            age = now - at
            if age >= self._cache_ttl_s():
                return None
            return {
                "fleet_spend_usd": decision.fleet_spend_usd,
                "fleet_tokens": decision.fleet_tokens,
                "strikes": decision.strikes,
                "generation": decision.generation,
                "halt": decision.halt,
                "latched": decision.latch is not None,
                "policy_version": version,
                "age_s": age,
            }
        except Exception:
            _LOG.warning("runbound: could not read the fleet status", exc_info=True)
            return None

    # --- leaving a session, tripping, circuits ------------------------------

    def exit(self, key: str, state: Any, delta: ExitDelta) -> None:
        """Hand one session's delta to the exporter, which posts it in batches.

        Asynchronous on purpose: closing a ``session()`` block is on the
        request path, and a token count is not worth a millisecond of it.
        """
        exporter = self._exporter
        if exporter is None:
            return
        try:
            exporter.on_exit(delta)
        except Exception:
            _LOG.warning("runbound: could not queue a session exit", exc_info=True)

    def trip(
        self, key: str | None, state: Any, anomaly: Any, ttl: float | None, door: bool
    ) -> None:
        """Tell the fleet about a trip, synchronously, then fall back.

        This is the one report that cannot wait for a batch: the point of a
        fleet-wide latch is that the *other* seven workers refuse this key on
        their next request, and a trip sitting in an export queue for a second
        is seven more turns of spending. It is still bounded by the client's
        entry timeout, and a plane that does not take it loses nothing — the
        record goes to the exporter instead, ahead of ordinary events.
        """
        try:
            digest = None if key is None else key_hash(key)
            reacted = "door" if door else "raise"
            report = self._trip_report(digest, state, anomaly, ttl, door, reacted, key)
        except Exception:
            _LOG.warning("runbound: could not describe a trip", exc_info=True)
            return
        sent = False
        if self._may_call():
            sent = bool(self._attempt(lambda: self._client.trip(report), "trip"))
        if sent or self._exporter is None:
            return
        try:
            self._queue_trip(state, anomaly, reacted)
        except Exception:
            _LOG.warning("runbound: could not queue a trip", exc_info=True)

    def _queue_trip(self, state: Any, anomaly: Any, reacted: str) -> None:
        """Hand a refused trip to the exporter's trip lane.

        :meth:`~runbound.export.Exporter.on_trip` queues whatever
        ``export_events`` says, because a trip is fleet state rather than
        telemetry. An observer without that method — a double, or an older
        exporter — is offered ``on_anomaly`` instead.
        """
        queue = getattr(self._exporter, "on_trip", None)
        if queue is None:
            queue = self._exporter.on_anomaly
        queue(state, anomaly, reacted)

    def _trip_report(
        self,
        digest: str | None,
        state: Any,
        anomaly: Any,
        ttl: float | None,
        door: bool,
        reacted: str,
        key: str | None = None,
    ) -> TripReport:
        wire = anomaly_to_wire(
            anomaly,
            reacted,
            digest,
            _iso(),
            send_session_keys=self._send_session_keys(),
            key=key,
        )
        return TripReport(
            key_hash=digest,
            anomaly=to_wire(wire),
            latch_ttl_s=None if ttl is None else float(ttl),
            strikes=int(getattr(state, "strikes", 0) or 0),
            generation=int(getattr(state, "fleet_generation", 0) or 0),
            refused_at_door=bool(door),
            # The same anomaly goes to the exporter as telemetry; the id
            # riding both is how the plane files one refusal, not two.
            anomaly_id=wire.anomaly_id,
        )

    def circuit(self, label: str, state: str, failures: int, cooldown_s: float) -> None:
        """Report a provider circuit transition on the exporter's priority lane.

        A no-op under ``circuit_fleet=False``: this worker's own
        transitions never leave the process, so it never contributes to (or
        shows up in) the fleet's per-service fold. The other half of opting
        out is :meth:`_apply_circuits`, which stops taking fleet instructions
        in either direction.
        """
        if not self._circuit_fleet():
            return
        exporter = self._exporter
        if exporter is None:
            return
        try:
            exporter.on_circuit(label, state, failures, cooldown_s)
        except Exception:
            _LOG.warning("runbound: could not queue a circuit change", exc_info=True)

    def _circuit_fleet(self) -> bool:
        """``circuit_fleet`` (a real ``init()`` keyword: it opts this
        worker in or out of a fold that only ever does anything once there
        is a plane to fold with, so it was never actually a control the
        plane needed to state), read defensively like every other knob
        here a duck-typed config may not carry."""
        return bool(getattr(self._config, "circuit_fleet", True))

    def clear(self, key: str) -> None:
        """Forget ``key`` here and ask the plane to forget it everywhere."""
        try:
            digest = key_hash(key)
            with self._lock:
                self._cache.pop(digest, None)
            if not self._may_call():
                return
            self._attempt(lambda: self._client.clear(digest), "clear")
        except Exception:
            _LOG.warning("runbound: could not clear a key on the plane", exc_info=True)

    # --- what the plane tells us --------------------------------------------

    def halted(self) -> bool:
        """Is the fleet stopped? Depends on ``stale_halt`` once contact is stale.

        A halt is only ever *set* by a successful call (a heartbeat, or an
        entry decision via :meth:`_absorb`) and only ever *lifted* by a
        heartbeat that says ``halt=False`` — see :meth:`_absorb` and
        :meth:`apply_hello`. What differs by ``stale_halt`` is what happens
        while the link stays degraded without a heartbeat to lift it:

        * ``"release"`` (the default): more than :data:`STALE_HALT_S` without
          a successful call and the answer goes back to ``False``. A control
          plane that dies must not take the fleet down with it — that is the
          whole fail-open promise, applied to the heaviest instruction it can
          give.
        * ``"hold"``: a halt already received stays enforced for as long as
          the link is degraded, however long that is — until a successful
          heartbeat says otherwise. For a customer whose incident *is* the
          runaway spend a halt exists to stop, a control plane going quiet is
          not a reason to let the fleet start spending again.
        """
        return self._halt_state()[0]

    def _staleness_clock_locked(self) -> "float | None":
        """The timestamp every ``stale_halt`` decision measures its window
        from. Caller holds ``self._lock``.

        Ordinarily ``self._last_success`` — the last time the plane
        actually answered. While the plane's own fleet state is
        unavailable (:attr:`_fleet_state_unavailable`),
        the link itself can keep succeeding on every heartbeat (that is the
        whole point — "plane loss for state, not for the link"), so
        ``_last_success`` would never age and a held halt would look fresh
        forever. The clock instead starts at
        :attr:`_state_unavailable_since`, the moment that state was first
        found unavailable, so ``stale_halt="release"`` still lifts a held
        halt :data:`STALE_HALT_S` after the plane stopped being able to
        vouch for it, exactly as it would after a dead link.
        """
        if self._fleet_state_unavailable and self._state_unavailable_since is not None:
            return self._state_unavailable_since
        return self._last_success

    def _halt_state(self) -> "tuple[bool, str | None]":
        """``(halted, mode)``, the one staleness rule :meth:`halted` and
        :meth:`halt_mode` both need, computed once.

        ``mode`` is ``None`` whenever ``halted`` is ``False`` — a halt that
        is not enforced has no kind, so :meth:`halt_posture_directive` and
        the api's own door check never have to separately ask "but is it
        actually on."
        """
        try:
            with self._lock:
                if not self._halt:
                    return False, None
                mode = self._halt_mode
                clock = self._staleness_clock_locked()
            if clock is None:
                return False, None
            if self._stale_halt_mode() == "hold":
                return True, mode
            return (True, mode) if self._now() - clock <= STALE_HALT_S else (False, None)
        except Exception:
            _LOG.warning("runbound: could not read the fleet halt", exc_info=True)
            return False, None

    def halt_mode(self) -> "str | None":
        """``"stop"`` or ``"narrow"`` while a halt is enforced, else ``None``
        — the halt's own staleness rule (:meth:`halted`) applies here
        too, so a mode never outlives the halt it belongs to."""
        return self._halt_state()[1]

    def _absorb_halt_posture(self, mode: "str | None") -> None:
        """Set or lift the Narrow halt's own posture slot. Caller holds
        ``self._lock`` (called from :meth:`apply_hello`).

        Mirrors :meth:`_absorb_posture`'s stability rule so the two behave
        identically to a reader: entering "narrow" once stamps the reason
        and the timestamp, and repeating it keeps that stamp rather than
        churning it every heartbeat. Anything but ``"narrow"`` (no halt at
        all, or a Stop) lifts this slot -- deliberately its *own* slot,
        never ``self._posture`` (the Controls-stated one): the two
        are independent plane sources (see :data:`runbound.state.
        POSTURE_SOURCES`'s ``"halt"`` entry) that must tighten together in
        :meth:`~runbound.engine.Engine.effective_posture` and lift on their
        own — sharing one slot would mean lifting a Narrow halt erases a
        Controls ``restricted`` an operator set separately, or the reverse.
        """
        if mode != "narrow":
            self._halt_posture = None
        elif self._halt_posture is None:
            self._halt_posture = make_posture_state(
                "restricted", "a fleet-wide Narrow halt", "halt"
            )

    def halt_posture_directive(self) -> "PostureState | None":
        """The posture a fleet-wide Narrow halt states, or ``None``.

        ``None`` for every case :meth:`halt_mode` would not answer
        ``"narrow"``: no plane, no halt, a Stop, or (``stale_halt="release"``,
        the default) a halt whose contact has gone quiet past
        :data:`STALE_HALT_S` — the same staleness rule :meth:`posture_
        directive`/:meth:`controls_directive` already apply to the plane's
        other two directives, for the same reason: a dead plane must not
        keep a fleet narrowed forever unless the customer chose that
        (``stale_halt="hold"``).
        """
        try:
            with self._lock:
                state = self._halt_posture if self._halt else None
                last = self._staleness_clock_locked()
            if state is None or last is None:
                return None
            if self._stale_halt_mode() == "hold":
                return state
            return state if self._now() - last <= STALE_HALT_S else None
        except Exception:
            _LOG.warning("runbound: could not read the fleet halt's posture", exc_info=True)
            return None

    def _absorb_posture(self, name: object) -> None:
        """Set or lift the plane's posture slot. Caller holds ``self._lock``.

        The first heartbeat naming a posture stamps the entry; later heartbeats
        naming the same one keep that stamp, so the reason and the timestamp do
        not churn. A different name replaces it, and ``None`` lifts this slot
        only: a worker's manual narrowing lives on its engine and its sessions,
        which the plane never touches.
        """
        if not isinstance(name, str) or not name:
            self._posture = None
        elif self._posture is None or self._posture.name != name:
            self._posture = make_posture_state(name, "stated by the control plane", "plane")

    def posture_directive(self) -> "PostureState | None":
        """The plane's posture for this service, with the halt's staleness rule.

        A narrowing the plane stated is kept while contact is fresh, and past
        :data:`STALE_HALT_S` without a successful call only under
        ``stale_halt="hold"`` — the same answer :meth:`halted` gives, for the
        same reason: a dead plane must not keep a fleet narrowed forever unless
        the customer chose that.
        """
        try:
            with self._lock:
                state = self._posture
                last = self._staleness_clock_locked()
            if state is None or last is None:
                return None
            if self._stale_halt_mode() == "hold":
                return state
            return state if self._now() - last <= STALE_HALT_S else None
        except Exception:
            _LOG.warning("runbound: could not read the plane's posture", exc_info=True)
            return None

    def controls_directive(self) -> dict | None:
        """The Controls body the plane last served, with the halt's own
        staleness rule — the same reasoning as :meth:`posture_directive`.

        ``None`` when there is nothing to apply: no plane, no served row for
        this scope, a row still ``dry_run`` (carried and shown, but never
        enforced — the same "shadow observes, enforce acts" rule the row's
        own rollout state applies elsewhere), or
        (``stale_halt="release"``, the default) contact has gone quiet past
        :data:`STALE_HALT_S`. A worker's own :class:`~runbound.engine.Engine`
        pulls this on demand rather than being pushed to, so staleness is
        re-evaluated on every read, not just at the moment contact was lost.
        """
        try:
            with self._lock:
                body = self._controls_body
                dry_run = self._controls_dry_run
                last = self._staleness_clock_locked()
            if body is None or dry_run or last is None:
                return None
            if self._stale_halt_mode() == "hold":
                return body
            return body if self._now() - last <= STALE_HALT_S else None
        except Exception:
            _LOG.warning("runbound: could not read the plane's controls", exc_info=True)
            return None

    def service_baseline(self) -> "tuple[float, float] | None":
        """This service's median spike baseline, off the same Controls body
        :meth:`controls_directive` reads.

        Deliberately does not share :meth:`controls_directive`'s ``dry_run``
        or staleness gate: a peer baseline is advisory context for a call
        already happening in this process, not an enforcement directive
        that a rollout state needs to guard, and reading it a little stale
        after a plane outage is no worse than reading a stale ``budget()``
        would be — it costs nothing to admit, only to compare against.
        ``None`` when there is no plane, nothing has been served for this
        scope yet, or the served body has no service baseline in it
        (``service_baseline_keys`` absent or ``0`` — no worker has reported
        any key's baseline for this service yet).
        """
        try:
            with self._lock:
                body = self._controls_body
            if not isinstance(body, dict):
                return None
            keys = body.get("service_baseline_keys")
            if not isinstance(keys, (int, float)) or isinstance(keys, bool) or keys <= 0:
                return None
            duration = float(body.get("service_baseline_duration_s") or 0.0)
            output = float(body.get("service_baseline_output_tokens") or 0.0)
            if max(duration, output) <= 0:
                return None
            return duration, output
        except Exception:
            _LOG.warning("runbound: could not read the plane's service baseline", exc_info=True)
            return None

    @property
    def controls_version(self) -> int:
        """The version of the Controls body :meth:`controls_directive` reads
        from, whatever its ``dry_run``/staleness state — zero for none."""
        with self._lock:
            return self._controls_version

    def policy(self) -> dict | None:
        """The org policy the plane last stated, or ``None``."""
        with self._lock:
            policy = self._policy
        return None if policy is None else dict(policy)

    @property
    def policy_version(self) -> int:
        """The version of the policy :meth:`policy` returns. Zero for none."""
        with self._lock:
            return self._policy_version

    @property
    def policy_dry_run(self) -> bool:
        """Is the org policy being rolled out — logged rather than enforced?"""
        with self._lock:
            return self._policy_dry_run

    def status(self) -> PlaneStatus:
        """Where the link stands: ``"connected"``, ``"limited"`` or ``"degraded"``.

        ``last_contact_age_s`` is ``None`` until the first call succeeds. A
        link that has not failed yet reads ``"connected"`` — a plane that is
        configured and has said nothing wrong is the normal case, and calling
        it degraded before it has been asked anything would be a false alarm.

        ``"limited"`` is the plan talking rather than the network: the plane is
        answering, but this worker is over the synced-worker limit, so entry
        questions are being decided locally. ``"degraded"`` wins over it when
        both are true — a link we have stopped calling at all is the more
        urgent fact, and it already implies local decisions. ``notice`` and
        ``entitlements`` carry the plane's own words either way.

        ``halt_stale_s`` is seconds since the last successful contact while a
        halt is being enforced (see :meth:`halted`), and ``None`` the rest of
        the time — no halt at all, or one that ``stale_halt="release"`` has
        already stopped enforcing.

        ``mode`` also reads ``"degraded"`` when the consecutive-failure count
        is fine but most of the last minute's session entries were decided
        locally anyway (:data:`ENTRY_WINDOW_MIN_ENTRIES` or more entries,
        more than :data:`ENTRY_LOCAL_SHARE_THRESHOLD` of them local) — a
        plane that answers every heartbeat but keeps missing
        ``control_plane_timeout_s`` on the hot path. It also reads
        ``"degraded"`` the moment the last hello carried ``fleet_state:
        "unavailable"`` — the plane's own live store, not the link, is
        the thing that is gone. ``reason`` says which of the three is
        failing: ``"heartbeat failures"`` (the link itself has stopped
        answering anything, which wins when more than one is true — the
        most urgent fact), ``"fleet state unavailable"`` (next — the link
        is fine but the plane cannot vouch for its own state, known within
        one heartbeat rather than waiting for the entry window to fill), or
        — when it is the entry window that degrades the link — the majority
        cause behind the window's own ``"local"`` entries
        (:meth:`_window_reason`): ``"entry timeouts"`` for genuine timeouts,
        ``"fleet state unavailable"`` again for a window full of 503s
        specifically naming the plane's state as the problem (those are
        the plane's state being lost, discovered at the entry door instead
        of the heartbeat, and must read the same as the hello-based case
        above, never the older, misleading "entry timeouts"), ``"plane
        unavailable"`` for a window full of 503s shaped like plane loss but
        naming no such cause (a saturated connection pool, a handler bug —
        the plane answered, just not usefully, and must never be conflated
        with its state being gone), or ``"plane errors"`` for anything else
        (no plane-shaped answer at all: a connection refused, a malformed
        reply). ``None`` while nothing is wrong.

        ``halt_stale_s`` measures from the same clock a held halt's own
        staleness does (:meth:`_staleness_clock_locked`) — ordinarily the
        last successful contact, but the moment fleet state was found
        unavailable while it still is, so this number keeps growing even
        though the heartbeat itself keeps succeeding.
        """
        try:
            failures = self._failure_run()
            counts = self._entry_window_counts()
            total = counts["plane"] + counts["cache"] + counts["local"]
            local_share = (counts["local"] / total) if total else 0.0
            live_total, live_local, live_causes = self._live_entry_counts()
            live_share = (live_local / live_total) if live_total else 0.0
            with self._lock:
                last = self._last_success
                notice = self._notice
                limited = self._limited
                entitlements = dict(self._entitlements)
                fleet_state_unavailable = self._fleet_state_unavailable
                staleness_clock = self._staleness_clock_locked()
            age = None if last is None else max(0.0, self._now() - last)
            halt_age = None if staleness_clock is None else max(0.0, self._now() - staleness_clock)
            reason = None
            if self._is_degraded(failures):
                mode = "degraded"
                reason = "heartbeat failures"
            elif fleet_state_unavailable:
                mode = "degraded"
                reason = "fleet state unavailable"
            elif live_total >= ENTRY_WINDOW_MIN_ENTRIES and live_share > ENTRY_LOCAL_SHARE_THRESHOLD:
                mode = "degraded"
                reason = self._window_reason(live_causes)
            else:
                mode = "limited" if limited else "connected"
            halt_stale_s = halt_age if self.halted() else None
            return PlaneStatus(
                mode=mode,
                last_contact_age_s=age,
                consecutive_failures=failures,
                notice=notice,
                entitlements=entitlements,
                halt_stale_s=halt_stale_s,
                entries_window=counts,
                entries_local_share=local_share,
                reason=reason,
            )
        except Exception:
            _LOG.warning("runbound: could not read the plane status", exc_info=True)
            return PlaneStatus(mode="degraded")

    def _window_reason(self, causes: dict) -> str:
        """The reason string for a link degraded by the entry window.

        Before this method existed, ``status()`` hardcoded ``"entry
        timeouts"`` here regardless of why the window's local entries were
        actually decided locally — the mislabel a state outage's own run log flagged: for
        up to a minute after a state outage recovers, the window still holds
        entries that were really 503s from lost fleet state, and the old
        code named them "entry timeouts" anyway.

        Instead, this reads the majority cause off ``causes`` (one entry
        per :data:`LOCAL_CAUSES`, from :meth:`_entry_window_counts`) and
        maps it through :data:`REASON_BY_LOCAL_CAUSE`. Ties break by
        :data:`LOCAL_CAUSE_TIE_BREAK`: ``max`` with a ``key`` that pairs each
        cause's count with its *negative* tie-break rank picks the highest
        count first and, among equal counts, the earliest cause in that
        tuple — so an even split always resolves the same way, not by
        whatever order ``dict`` happens to iterate in. A window with no
        local entries at all (should not happen: this is only called once
        the window's own local share has already cleared the degrade
        threshold) falls back to ``"entry timeouts"``, the original,
        least-alarming default.
        """
        if not any(causes.values()):
            return REASON_BY_LOCAL_CAUSE["timeout"]
        rank = {cause: i for i, cause in enumerate(LOCAL_CAUSE_TIE_BREAK)}
        winner = max(causes, key=lambda cause: (causes[cause], -rank.get(cause, len(rank))))
        return REASON_BY_LOCAL_CAUSE.get(winner, REASON_BY_LOCAL_CAUSE["timeout"])

    def _is_degraded(self, failures: int) -> bool:
        """Degraded when we have given up calling: too many failures, or a bad key."""
        if getattr(self._client, "key_state", None) == "invalid":
            return True
        return failures >= DEGRADE_AFTER

    def _failure_run(self) -> int:
        """How many calls in a row have failed, ours and the client's.

        The client counts the heartbeat's failures too, which is the point: a
        plane that is down is usually discovered by the poller long before a
        session entry runs into it, and the entry should degrade on what the
        poller already knows rather than spending three more timeouts learning
        it for itself.
        """
        theirs = getattr(self._client, "consecutive_failures", 0)
        if not isinstance(theirs, int) or isinstance(theirs, bool) or theirs < 0:
            theirs = 0
        with self._lock:
            return max(self._failures, theirs)

    def apply_hello(self, reply: Any) -> None:
        """Apply one heartbeat reply. The :class:`~runbound.plane.Poller` calls this.

        Five things can change: the fleet halt (and, with it, which
        *kind* the halt is — see :meth:`_absorb_halt_posture`), the policy
        version (a new one is fetched here, on the poller's thread, never on
        a request's), the fleet-wide circuit instructions, the notice a
        customer sees in :func:`runbound.plane_status`, and the
        entitlements — what this org's plan still allows (see
        :meth:`_apply_entitlements`). Never raises: a heartbeat that cannot
        be applied is logged, and the next one is tried as if nothing
        happened.

        ``reply.fleet_state == "unavailable"`` means the plane itself
        answered (the link is fine — the *this reply arrived at all* half
        of this method still runs) but could not read its own live store, so
        none of it can say anything true about the halt, either posture
        slot, or the policy/Controls versions — every one of those fields
        rides the wire at its own "nothing to report" default in that case
        (see ``routers/sdk.py::hello``), and a plane's silence must never
        read as "no halt." Those five are left exactly as they were instead
        of being absorbed from the reply; the link's own liveness
        (``_last_success``, ``_failures``) and the entitlements/notice
        (read from Postgres on the plane, unaffected by its Redis being
        gone) still update normally either way.

        This runs only for a reply that actually arrived, which is what makes
        it the right place to settle the tool report: the hash that rode the
        last heartbeat becomes the acknowledged one, and a reply saying
        ``tools_known: false`` throws that away so the next heartbeat resends
        the report in full (see :meth:`_tools_payload`) — unaffected by
        ``fleet_state`` either, since the tool inventory lives on the
        plane's ledger, not its live store.
        """
        try:
            fleet_state = getattr(reply, "fleet_state", "ok")
            unavailable = fleet_state == "unavailable"
            version = _as_int(getattr(reply, "policy_version", 0))
            controls_version = _as_int(getattr(reply, "controls_version", 0))
            entitlements = _entitlements(reply)
            notice = entitlements.get("notice") or getattr(reply, "notice", None)
            halt = bool(getattr(reply, "halt", False))
            mode = getattr(reply, "halt_mode", "stop")
            mode = mode if mode in ("stop", "narrow") else "stop"
            with self._lock:
                if unavailable:
                    if not self._fleet_state_unavailable:
                        self._state_unavailable_since = self._now()
                    self._fleet_state_unavailable = True
                    changed = False
                    controls_changed = False
                else:
                    if self._fleet_state_unavailable:
                        # The heartbeat says the state is back: every local
                        # decision the outage caused is now history.
                        self._state_loss_superseded_seq = self._entry_seq + 1
                    self._fleet_state_unavailable = False
                    self._state_unavailable_since = None
                    self._halt = halt
                    self._halt_mode = mode if halt else "stop"
                    self._absorb_halt_posture(mode if halt else None)
                    self._absorb_posture(getattr(reply, "posture", None))
                    changed = version != self._policy_version
                    controls_changed = controls_version != self._controls_version
                self._notice = notice if isinstance(notice, str) else None
                self._entitlements = entitlements
                self._last_success = self._now()
                self._failures = 0
                self._settle_tools_hash(reply)
            self._apply_entitlements(entitlements)
            if changed:
                self._fetch_policy(version)
            if controls_changed:
                self._fetch_controls(controls_version)
            if not unavailable:
                self._apply_circuits(getattr(reply, "circuits", None))
        except Exception:
            _LOG.warning(
                "runbound: could not apply the control plane's reply", exc_info=True
            )

    def _settle_tools_hash(self, reply: Any) -> None:
        """Promote the pending tool-report hash, unless the plane wants it again.

        Called from :meth:`apply_hello` with ``self._lock`` already held.
        ``tools_known`` is read with a default of ``True`` because an older
        plane does not send the field at all — see :class:`HelloReply`.
        """
        if self._tools_pending_hash is not None:
            self._tools_sent_hash = self._tools_pending_hash
            self._tools_pending_hash = None
        if not getattr(reply, "tools_known", True):
            self._tools_sent_hash = None

    def _apply_entitlements(self, entitlements: dict) -> None:
        """Do what the plan says this worker may still do.

        Two denials have an effect here and both are reversible — the plane
        withdrawing a code puts the worker straight back:

        * ``"events_denied"`` / ``"events_over_cap"`` close the telemetry
          lanes, exactly as ``export_events=False`` does. The exits, the
          circuits and the trips keep flowing, because those are fleet state:
          a worker that stopped reporting its exits would stop contributing to
          the shared budget, and a plan limit must not quietly break the thing
          the customer is paying for.
        * ``"workers_synced_exceeded"`` puts the worker in ``limited`` mode:
          entry questions are answered locally, the heartbeat carries on, and
          trips and exits still go out. Guarding is unchanged — every
          detector, latch and cap runs as it does with no plane at all.

        Never enforced against the customer's own settings: telemetry the
        customer switched off stays off when the denial is lifted.

        A third thing happens here that touches nothing above: whether the
        notice this reply carries is old enough to actually write to the
        log. A rolling deploy of a single-worker customer can overlap two
        heartbeats for up to the plane's heartbeat TTL, briefly denying
        ``workers_synced_exceeded`` to an otherwise in-plan customer — real
        for the moment it lasts, but not worth an alarm. So ``limited`` and
        the telemetry lanes above still flip on this very reply, silently;
        only :meth:`_log_notice` is gated, by :data:`NOTICE_DEBOUNCE_S` of
        continuous presence (see :meth:`_track_notice_presence`).
        """
        codes = _denied_codes(entitlements)
        limited = WORKERS_DENIED_CODE in codes
        with self._lock:
            self._limited = limited
            notice = self._notice
        continuously_present_s = self._track_notice_presence(notice)
        self._set_include_events(not codes.intersection(EVENTS_DENIED_CODES))
        if codes and continuously_present_s >= NOTICE_DEBOUNCE_S:
            self._log_notice()

    def _track_notice_presence(self, notice: str | None) -> float:
        """How long a notice has been continuously present, in seconds.

        Presence, not text, is what is tracked: a notice whose wording
        changes between polls (the plane rephrasing a nudge) still counts as
        the same continuous stretch, so a reworded notice never gets a free
        reset of the debounce window that a rolling deploy relies on. A
        reply carrying no notice at all (``notice`` falsy) clears the clock
        outright, because the condition being timed has ended.

        The clock is ``self._now`` — real time in production, a test's hand
        moved one in tests — and it is trusted to advance, but never trusted
        not to go backwards: a clock that regresses must never manufacture
        elapsed time (an ``abs()`` of a negative difference would do exactly
        that), so a negative gap floors at zero rather than counting as
        continuous presence of any duration.
        """
        now = self._now()
        with self._lock:
            if not notice:
                self._notice_since = None
                return 0.0
            if self._notice_since is None:
                self._notice_since = now
                return 0.0
            since = self._notice_since
        return max(0.0, now - since)

    def _set_include_events(self, allowed: bool) -> None:
        """Open or close the exporter's telemetry lanes, never past the config."""
        exporter = self._exporter
        if exporter is None:
            return
        wanted = bool(allowed and getattr(self._config, "export_events", True))
        if bool(getattr(exporter, "include_events", True)) != wanted:
            exporter.include_events = wanted

    def _log_notice(self) -> None:
        """Say once an hour what the plan is refusing and what to do about it."""
        with self._lock:
            notice = self._notice
        if notice:
            warn_periodically(self._notice_warning, "runbound: %s", notice)

    @property
    def limited(self) -> bool:
        """Is this worker over its plan's synced-worker limit?

        True means entry questions are answered locally. Nothing else changes:
        the heartbeat, the trips and the exit deltas all carry on, so the
        fleet's totals stay right and the moment the plan allows this worker
        again it is back to asking.
        """
        with self._lock:
            return self._limited

    def _fetch_policy(self, version: int) -> None:
        """Pull the org policy for this service; keep the old one on failure.

        The plane may answer with an envelope — ``{"version", "dry_run",
        "policy", "refusals"}`` — or with the policy body itself; both are
        accepted, and the envelope's version wins over the one the heartbeat
        announced. The refusal profile travels in the same envelope but is
        not policy: it is installed separately, into
        :mod:`runbound.responses`, on every fetch — including one where it
        is absent, which is how a profile withdrawn on the plane is withdrawn
        here.
        """
        data = self._attempt(lambda: self._client.policy(self._config.service), "policy")
        if data is None:
            return
        body, resolved, dry_run, refusals = _policy_envelope(data, version)
        with self._lock:
            self._policy = body
            self._policy_version = resolved
            self._policy_dry_run = dry_run
        _install_remote_refusals(refusals)
        _note_version("policy_version", resolved)

    def _fetch_controls(self, version: int) -> None:
        """Pull this service's Controls body; keep the old one on failure.

        Mirrors :meth:`_fetch_policy`'s shape and its "keep the old one on
        failure" rule: a plane that cannot be reached right now must never
        revert this worker to no Controls at all — that would *loosen* the
        moment the tightened value stopped being applied, which is exactly
        what fail-open must not do to a control that is already in effect.
        Applying the body itself happens lazily, when
        :class:`~runbound.engine.Engine` next reads :meth:`controls_directive`
        — nothing here touches the engine.
        """
        data = self._attempt(lambda: self._client.controls(self._config.service), "controls")
        if data is None:
            return
        body, resolved, dry_run = _controls_envelope(data, version)
        with self._lock:
            self._controls_body = body
            self._controls_version = resolved
            self._controls_dry_run = dry_run
        _note_version("controls_version", resolved)

    def _apply_circuits(self, circuits: Any) -> None:
        """Open or close provider circuits because the plane said so.

        Only transitions are applied: a circuit the plane keeps reporting as
        open is left alone, so a five-second heartbeat cannot keep resetting
        the cooldown and starve the half-open probe that would close it.

        A no-op under ``circuit_fleet=False``: this worker opted out
        of the fleet fold, so a plane instruction — including a fresh
        worker's very first hello, joining a circuit that is already open
        elsewhere — is never forced onto its local breaker; it stays purely
        local, exactly as it behaved before the fleet circuit existed.
        """
        if not self._circuit_fleet():
            return
        breaker = self.breaker
        if breaker is None or not isinstance(circuits, dict):
            return
        for label, spec in circuits.items():
            try:
                wanted, until_s = _circuit_spec(spec)
                if wanted not in ("open", "closed"):
                    continue
                current = breaker.state(str(label))
                if wanted == "open" and current != "open":
                    breaker.force_open(str(label), until_s)
                elif wanted == "closed" and current != "closed":
                    breaker.force_close(str(label))
            except Exception:
                _LOG.warning(
                    "runbound: could not apply the fleet circuit for %r",
                    label,
                    exc_info=True,
                )

    # --- calling, and giving up on, the plane -------------------------------

    def _may_call(self) -> bool:
        """Is it worth opening a socket right now?

        No while the key is rejected — that is terminal until the process is
        reconfigured — and no on a degraded link except for one probe every
        :data:`RETRY_EVERY_S`, which is what lets a recovered plane be noticed
        without the fleet hammering it while it is down.
        """
        try:
            if getattr(self._client, "key_state", None) == "invalid":
                return False
            failures = self._failure_run()
            now = self._now()
            with self._lock:
                if failures < DEGRADE_AFTER:
                    return True
                last = self._last_attempt
                if last is None:
                    # Degraded on the heartbeat's evidence rather than our
                    # own: the retry window starts here, and this caller is
                    # answered locally instead of paying to confirm it.
                    self._last_attempt = now
                    return False
                if now - last < RETRY_EVERY_S:
                    return False
                self._last_attempt = now
                return True
        except Exception:
            return False

    def _attempt(self, call: Callable[[], Any], what: str) -> Any:
        """Make one call to the plane, counting how it went. Never raises.

        A ``None`` (or ``False``) answer is a failure: the client has already
        logged why, and what matters here is only the run of them that turns
        the link degraded. ``_last_failure_kind`` records which of the two
        shapes a failure took — "error" (the call raised) or "timeout" (the
        client gave up quietly and returned ``None``/``False``) — for
        :meth:`enter` to name in a plane-loss refusal's details; it is not
        used for anything that affects fail-open behavior.
        """
        now = self._now()
        with self._lock:
            self._last_attempt = now
        try:
            result = call()
        except Exception:
            _LOG.warning("runbound: control plane %s failed", what, exc_info=True)
            self._record(False, kind="error")
            return None
        ok = result is not None and result is not False
        self._record(ok, kind=None if ok else "timeout")
        return result

    def _record(self, ok: bool, kind: str | None = None) -> None:
        """Fold one call's outcome into the failure run."""
        with self._lock:
            if ok:
                self._failures = 0
                self._last_success = self._now()
            else:
                self._failures += 1
                if kind is not None:
                    self._last_failure_kind = kind

    # --- lifecycle ----------------------------------------------------------

    def observers(self) -> list:
        """The exporter, when it is exporting: the engine's only fleet observer.

        With ``export_events`` off the exporter is still here — it carries the
        exits, the circuits and the trips — but it has nothing to hear from
        the engine, so it is not installed as an observer at all and the
        observation path costs a process with telemetry off exactly nothing.
        """
        exporter = self._exporter
        if exporter is None or not getattr(exporter, "include_events", True):
            return []
        return [exporter]

    def start(self, engine: Any) -> None:
        """Attach to ``engine`` and start the exporter and the heartbeat.

        Called by :func:`runbound.init` outside its lock, because both of
        these start threads and the first heartbeat goes out immediately.
        """
        try:
            self.breaker = getattr(engine, "circuit", None)
            self._engine = engine
            if self._exporter is not None:
                self._exporter.start()
            self._start_poller()
        except Exception:
            _LOG.warning(
                "runbound: could not start the control plane link; running local-only",
                exc_info=True,
            )

    def _start_poller(self) -> None:
        poller = Poller(
            self._client,
            getattr(self._config, "control_plane_poll_s", 5.0),
            self.apply_hello,
            self._hello_payload,
        )
        self._poller = poller
        poller.start()

    def _hello_payload(self) -> dict:
        """What each heartbeat says: who we are and what we already know."""
        payload = {
            "service": self._config.service,
            "worker_id": self._config.resolved_worker_id(),
            "sdk_version": _sdk_version(),
            "policy_version_seen": self.policy_version,
            "controls_version_seen": self.controls_version,
            "circuits": self._circuit_states(),
            "active": self._active_sessions(),
            "coverage": self._coverage_payload(),
            "can_stop": self._can_stop(),
            # This worker's ack that it has seen and applied *a* halt
            # (either mode) as of the last reply it actually processed --
            # ``self.halted()`` already applies the halt's own staleness
            # rule, so a worker that gave up enforcing a stale halt
            # (``stale_halt="release"``, the default) stops acking it too,
            # rather than reporting a compliance it is no longer providing.
            # Sent on every heartbeat while true, not only the first --
            # simplest and most robust on this side (no state to track
            # about which halt was last acked). The plane is the one that
            # only *writes* the first ack of each halt episode
            # (``ledger.writer.record_halt_ack``): recording every repeat
            # would let a worker's own ack keep sliding forward for as
            # long as it keeps heartbeating, and the reported convergence
            # time would slide forward with it.
            "halt_ack": self.halted(),
        }
        refused = self._controls_refused_payload()
        if refused:
            payload["controls_refused"] = refused
        envelope = self._envelope_payload()
        if envelope is not None:
            payload["envelope"] = envelope
        local_share = self._entries_local_share_payload()
        if local_share is not None:
            payload["entries_local_share"] = local_share
        payload.update(self._tools_payload())
        return payload

    def _entries_local_share_payload(self) -> float | None:
        """This worker's local-decision share over the last minute, for the
        plane to store per worker — so a console can show how many of a
        service's workers are deciding locally right now.

        ``None`` with fewer than :data:`ENTRY_WINDOW_MIN_ENTRIES` entries in
        the window: a worker that has barely opened any sessions does not
        have a share worth storing, and ``0.0`` would misreport "definitely
        fine" from a sample of one.
        """
        try:
            counts = self._entry_window_counts()
            total = counts["plane"] + counts["cache"] + counts["local"]
            if total < ENTRY_WINDOW_MIN_ENTRIES:
                return None
            return counts["local"] / total
        except Exception:
            _LOG.debug("runbound: could not build the entries_local_share payload", exc_info=True)
            return None

    def pending_events(self) -> "int | None":
        """State/telemetry records this worker still has queued to post,
        or ``None`` without an exporter. Read the same way
        :meth:`status` is — safe for a health endpoint, never opens a
        socket."""
        exporter = self._exporter
        if exporter is None:
            return None
        try:
            return int(exporter.pending)
        except Exception:
            _LOG.debug("runbound: could not read the exporter's pending count", exc_info=True)
            return None

    @staticmethod
    def _envelope_payload() -> dict | None:
        """The current, unkeyed session's own execution envelope (kept on
        the heartbeat even though ``runbound.envelope()`` is public too),
        or ``None`` before an engine exists or on any
        failure -- the plane can show a worker's own budget/posture/
        capabilities picture without the customer's process calling
        anything for it, and it is the same object :func:`runbound.envelope`
        returns.

        Imported from :mod:`runbound.api` inside the call, not at module
        level, for the same reason :meth:`_coverage_payload` already
        does this: ``api`` imports :mod:`runbound.shared` to build this
        class, so importing back at module load time would be a cycle.
        """
        try:
            from . import api

            return api.envelope(None)
        except Exception:
            _LOG.debug("runbound: could not build the heartbeat's envelope", exc_info=True)
            return None

    def _can_stop(self) -> bool | None:
        """``can_stop``: can this worker's own ``on_anomaly`` actually
        stop it? ``None`` ("unknown") before an engine is attached, or on
        any failure — never rendered by the plane as "cannot stop"."""
        engine = self._engine
        if engine is None:
            return None
        try:
            return bool(getattr(engine.config, "on_anomaly", "warn") in ("raise", "callback"))
        except Exception:
            return None

    def _controls_refused_payload(self) -> list:
        """"Refused and shown": the Controls fields this worker's own
        configuration kept because the plane's own value would have
        loosened it, capped and best-effort like every other heartbeat
        extra — see :meth:`runbound.engine.Engine.controls_refusals`."""
        engine = self._engine
        if engine is None:
            return []
        try:
            refused = engine.controls_refusals()
        except Exception:
            return []
        return refused[:CONTROLS_REFUSED_MAX] if isinstance(refused, list) else []

    def _tools_payload(self) -> dict:
        """``tools_hash`` every time, ``tools`` only when the plane needs it.

        The hash rides every heartbeat so the plane can always tell whether
        what it holds is current; the report itself rides only when its hash
        differs from the last one the plane acknowledged — which covers both a
        deploy that changed the tools and a plane that answered
        ``tools_known: false`` (:meth:`apply_hello` clears the acknowledged
        hash, so the very next heartbeat differs from it).

        The hash of a report that went out is only *pending* until a reply
        comes back: a heartbeat that was never answered proves nothing about
        what the plane stored, so the next one sends the report again.

        ``{}`` on any failure, so a heartbeat carries **neither** key rather
        than a hash it cannot back up — a plane that had a hash and no report
        would keep answering ``tools_known: false`` at a worker that cannot
        build one. Same fail-open contract as :meth:`_coverage_payload`, and
        the same in-call import, for the same cycle.
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

    @staticmethod
    def _coverage_payload() -> dict:
        """The coverage numbers worth a heartbeat, or ``{}`` on failure.

        ``decorated_tool_names`` rides along as a bounded list (see
        :data:`runbound._coverage.DECORATED_TOOL_NAMES_MAX`) rather than a
        count: the Services page already has ``decorated_tools`` for "how
        many," and the names are what let it say *which* tools a service has,
        without ever carrying a tool's arguments or return value.

        Imported from :mod:`runbound._coverage`/:mod:`runbound.api`/
        :mod:`runbound.autowrap` inside the call, not at module level, for
        the same reason as :meth:`_active_sessions` — those modules import
        :mod:`runbound.shared` to build this class, so importing back at
        module load time would be a cycle. Mirrors the arguments
        :func:`runbound.api.coverage` passes to ``snapshot`` so the fleet
        sees the same numbers a local ``runbound.coverage()`` call would.
        Fails open to ``{}``: a heartbeat must never break because its own
        diagnostics did.
        """
        try:
            from . import _coverage, api, autowrap

            report = _coverage.snapshot(autowrap.patched(), **api._coverage_kwargs())
            return {
                "guarded_calls": report["guarded_calls"],
                "decorated_tools": report["decorated_tools"],
                "decorated_tool_names": report.get("decorated_tool_names", []),
                "providers_imported": report["providers_imported"],
                "providers_unguarded": report["providers_unguarded"],
            }
        except Exception:
            return {}

    @staticmethod
    def _active_sessions() -> int:
        """How many keyed session blocks are open in this process right now.

        Imported from :mod:`runbound.api` inside the call, not at module
        level: ``api`` imports :mod:`runbound.shared` to build this class,
        so importing back would be a cycle. Fails open to ``0`` — a heartbeat
        must never fail because the fleet couldn't be told how busy we are.
        """
        try:
            from . import api

            return int(api.active_sessions())
        except Exception:
            return 0

    def _circuit_states(self) -> dict:
        """``{label: "open"|"half_open"|"closed"}`` for this worker's circuits.

        ``{}`` under ``circuit_fleet=False`` too — nothing about a
        worker that opted out of the fold rides the wire, this field
        included, even though the plane does not currently read it back.
        """
        breaker = self.breaker
        if breaker is None or not self._circuit_fleet():
            return {}
        try:
            return {
                label: entry.get("state", "closed")
                for label, entry in breaker.snapshot().items()
            }
        except Exception:
            return {}

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

    :class:`LocalState` without ``control_plane_url`` — the default, and the
    behavior of every runbound before fleet mode. With one, a
    :class:`RemoteState` over a fresh :class:`~runbound.plane.PlaneClient`,
    always carrying an :class:`~runbound.export.Exporter`: ``export_events``
    decides what that exporter *accepts*, not whether it exists, because the
    exits it ships are how this worker's spend reaches the fleet's total and a
    worker that stopped shipping them would quietly stop sharing a budget.
    Nothing is started here; :meth:`RemoteState.start` does that once the
    engine exists.

    Fail-open like everything else: a client that cannot be built at all
    leaves the process local, with a warning, rather than failing ``init()``.
    """
    # One question, asked one way. `plane_mode` is the settled answer
    # validate() wrote down ("off" | "hosted" | "self_hosted"); a duck-typed
    # config that never went through validate() falls back to the url, read
    # the same way validate() reads it, so a blank string is "no plane" on
    # both paths rather than "a plane" on one of them.
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
            timeout_s=config.control_plane_timeout_s,
        )
        exporter = Exporter(
            client,
            include_events=bool(config.export_events),
            send_session_keys=bool(config.send_session_keys),
        )
        return RemoteState(client, exporter, config)
    except Exception:
        _LOG.warning(
            "runbound: could not build the control plane link; running local-only",
            exc_info=True,
        )
        return LocalState()


def _note_version(what: str, version: Any) -> None:
    """Note a newly applied policy or Controls version, recording a change
    from the one this worker ran on before. Never raises."""
    try:
        local_events.note_runtime_value(what, version)
    except Exception:
        _LOG.warning("runbound: could not note the %s", what, exc_info=True)


def _policy_envelope(data: dict, version: int) -> tuple[dict | None, int, bool, dict | None]:
    """Split a policy response into ``(policy, version, dry_run, refusals)``.

    Accepts both shapes the plane may answer with: an envelope carrying the
    policy under ``"policy"``, or the policy body itself with ``"version"``
    and ``"dry_run"`` mixed in. A body that is not a dict — ``null`` for "no
    org policy" — becomes ``None``, which is how a policy is withdrawn.

    ``"refusals"`` — the merged org/service refusal profile, see
    :mod:`runbound.responses` — travels in the same envelope but is not part
    of the policy body: it is stripped out here and returned separately, so a
    policy body that never carried it is byte-for-byte what it always was. Its
    absence (or an explicit ``null``) is reported as ``None``, which is how a
    profile is withdrawn.
    """
    if not isinstance(data, dict):
        return None, version, False, None
    if "policy" in data:
        body = data.get("policy")
        return (
            dict(body) if isinstance(body, dict) else None,
            _as_int(data.get("version", version), version),
            bool(data.get("dry_run", False)),
            _refusals_of(data),
        )
    refusals = _refusals_of(data)
    body = {
        key: value
        for key, value in data.items()
        if key not in ("version", "dry_run", "refusals")
    }
    return (
        body or None,
        _as_int(data.get("version", version), version),
        bool(data.get("dry_run", False)),
        refusals,
    )


def _controls_envelope(data: dict, version: int) -> tuple[dict | None, int, bool]:
    """Split a ``/v1/controls`` response into ``(controls, version, dry_run)``.

    ``data`` is always the envelope shape (``{"version", "dry_run",
    "controls"}``) the plane answers with — unlike :func:`_policy_envelope`,
    there is no older bare-body shape to also tolerate, since ``/v1/controls``
    is new in this phase. ``controls`` not a dict (``None`` for "nothing
    served") becomes ``None``, which :meth:`RemoteState.controls_directive`
    reads as "nothing to apply".
    """
    if not isinstance(data, dict):
        return None, version, False
    body = data.get("controls")
    return (
        dict(body) if isinstance(body, dict) else None,
        _as_int(data.get("version", version), version),
        bool(data.get("dry_run", False)),
    )


def _refusals_of(data: dict) -> dict | None:
    """The ``"refusals"`` profile out of a policy envelope, or ``None``."""
    refusals = data.get("refusals")
    return dict(refusals) if isinstance(refusals, dict) else None


def _install_remote_refusals(profile: dict | None) -> None:
    """Hand a fetched refusal profile to :mod:`runbound.responses`.

    Fail-open like everything else here: a broken ``responses`` module must
    cost this worker nothing beyond a debug line, never a policy fetch.
    """
    try:
        responses.set_remote(profile)
    except Exception:
        _LOG.debug("runbound could not install the fleet's refusal profile", exc_info=True)


def _circuit_spec(spec: Any) -> tuple[str | None, float | None]:
    """One fleet circuit instruction: ``("open", 30.0)`` or ``("closed", None)``.

    The plane may state a bare string or a dict carrying how long the hold
    should last; anything else is not an instruction and is ignored.
    """
    if isinstance(spec, str):
        return spec, None
    if not isinstance(spec, dict):
        return None, None
    state = spec.get("state")
    until = spec.get("until_s", spec.get("cooldown_s"))
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        until = None
    return (state if isinstance(state, str) else None), (
        None if until is None else float(until)
    )


def _entitlements(reply: Any) -> dict:
    """The entitlements dict a hello reply carries, or ``{}``.

    ``{"plan": "team", "limits": {...}, "denied": ["events_over_cap"],
    "notice": "…"}`` is the shape; anything else the plane sends is data we
    keep and do not act on, which is what lets the plane grow codes the SDK
    has never heard of without breaking this worker.
    """
    entitlements = getattr(reply, "entitlements", None)
    return dict(entitlements) if isinstance(entitlements, dict) else {}


def _denied_codes(entitlements: dict) -> set:
    """The entitlement codes the plane is currently refusing, as a set of str."""
    denied = entitlements.get("denied")
    if not isinstance(denied, (list, tuple, set)):
        return set()
    return {code for code in denied if isinstance(code, str)}


def _as_int(value: Any, default: int = 0) -> int:
    """An int from whatever the plane sent, or ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _sdk_version() -> str:
    """The installed SDK version, read lazily so import order never matters."""
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover - only during a broken import
        return "unknown"
