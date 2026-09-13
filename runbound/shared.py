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
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from . import responses
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

#: Most keys the decision cache holds. Entries expire on their own; this is
#: the ceiling for a process churning through keys faster than that.
DECISION_CACHE_MAX = 1024

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
        """Is the whole fleet stopped right now?"""

    def policy(self) -> dict | None:  # pragma: no cover - protocol
        """The org's tool policy, as the plane last stated it."""

    def status(self) -> PlaneStatus:  # pragma: no cover - protocol
        """Where the link to the plane stands, for a health endpoint."""

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
        self._failures = 0
        self._last_success: float | None = None
        self._last_attempt: float | None = None
        self._last_failure_kind: str | None = None
        self._halt = False
        self._notice: str | None = None
        self._policy: dict | None = None
        self._policy_version = 0
        self._policy_dry_run = False
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
                return cached
            if self.limited:
                # Not plane loss: the plane is answering fine, this worker is
                # simply over its plan's synced-worker count and is told to
                # decide locally regardless of on_plane_loss — a customer who
                # opted into "refuse" wants safety when the plane cannot be
                # heard, not a fleet-wide outage because one plan limit was
                # hit. Guarding continues unaffected either way.
                return None
            if getattr(self._client, "key_state", None) == "invalid":
                return self._invalid_key()
            if not self._may_call():
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
        decision = self._attempt(lambda: self._client.enter(payload), "enter")
        if decision is None:
            with self._lock:
                kind = self._last_failure_kind or "error"
            return self._plane_loss_refusal(kind)
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
        """
        if not decision.halt:
            return
        with self._lock:
            self._halt = True

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
        """Report a provider circuit transition on the exporter's priority lane."""
        exporter = self._exporter
        if exporter is None:
            return
        try:
            exporter.on_circuit(label, state, failures, cooldown_s)
        except Exception:
            _LOG.warning("runbound: could not queue a circuit change", exc_info=True)

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
        try:
            with self._lock:
                if not self._halt:
                    return False
                last = self._last_success
            if last is None:
                return False
            if self._stale_halt_mode() == "hold":
                return True
            return self._now() - last <= STALE_HALT_S
        except Exception:
            _LOG.warning("runbound: could not read the fleet halt", exc_info=True)
            return False

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
        """
        try:
            failures = self._failure_run()
            with self._lock:
                last = self._last_success
                notice = self._notice
                limited = self._limited
                entitlements = dict(self._entitlements)
            age = None if last is None else max(0.0, self._now() - last)
            if self._is_degraded(failures):
                mode = "degraded"
            else:
                mode = "limited" if limited else "connected"
            halt_stale_s = age if self.halted() else None
            return PlaneStatus(
                mode=mode,
                last_contact_age_s=age,
                consecutive_failures=failures,
                notice=notice,
                entitlements=entitlements,
                halt_stale_s=halt_stale_s,
            )
        except Exception:
            _LOG.warning("runbound: could not read the plane status", exc_info=True)
            return PlaneStatus(mode="degraded")

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

        Five things can change: the fleet halt, the policy version (a new one
        is fetched here, on the poller's thread, never on a request's), the
        fleet-wide circuit instructions, the notice a customer sees in
        :func:`runbound.plane_status`, and the entitlements — what this
        org's plan still allows (see :meth:`_apply_entitlements`). Never
        raises: a heartbeat that cannot be applied is logged, and the next one
        is tried as if nothing happened.

        This runs only for a reply that actually arrived, which is what makes
        it the right place to settle the tool report: the hash that rode the
        last heartbeat becomes the acknowledged one, and a reply saying
        ``tools_known: false`` throws that away so the next heartbeat resends
        the report in full (see :meth:`_tools_payload`).
        """
        try:
            version = _as_int(getattr(reply, "policy_version", 0))
            entitlements = _entitlements(reply)
            notice = entitlements.get("notice") or getattr(reply, "notice", None)
            with self._lock:
                self._halt = bool(getattr(reply, "halt", False))
                self._notice = notice if isinstance(notice, str) else None
                self._entitlements = entitlements
                self._last_success = self._now()
                self._failures = 0
                changed = version != self._policy_version
                self._settle_tools_hash(reply)
            self._apply_entitlements(entitlements)
            if changed:
                self._fetch_policy(version)
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

    def _apply_circuits(self, circuits: Any) -> None:
        """Open or close provider circuits because the plane said so.

        Only transitions are applied: a circuit the plane keeps reporting as
        open is left alone, so a five-second heartbeat cannot keep resetting
        the cooldown and starve the half-open probe that would close it.
        """
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
            "circuits": self._circuit_states(),
            "active": self._active_sessions(),
            "coverage": self._coverage_payload(),
        }
        payload.update(self._tools_payload())
        return payload

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
        """``{label: "open"|"half_open"|"closed"}`` for this worker's circuits."""
        breaker = self.breaker
        if breaker is None:
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
