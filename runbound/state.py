"""Mutable per-session state — the single place counters live."""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import local_events
from .events import Anomaly, Event

_LOG = logging.getLogger("runbound")

#: How far back the token window is worth keeping: nothing reads an entry
#: older than this, because velocity is a per-minute measure.
TOKEN_WINDOW_SECONDS = 60.0

#: How far back failed calls are remembered. A retry storm is a burst, not a
#: tally: ten failures over an afternoon are a flaky provider, ten in a minute
#: are an agent hammering one.
ERROR_WINDOW_SECONDS = 60.0

#: Event kinds that count as a failure of the work the session was doing.
ERROR_KINDS = ("llm_error", "tool_error")

#: Event kinds whose ``args_hash`` belongs in the loop window. A model's tool
#: *request* loops exactly as an executed call does, and its hashes live in
#: their own ``"req:"`` namespace, so the two streams never merge.
HASHED_KINDS = ("tool_call", "tool_request")

#: Entries the token window may hold before :meth:`SessionState.record` starts
#: dropping expired ones. Pruning costs a comparison per entry dropped, and a
#: session with a handful of entries has nothing worth reclaiming; the point of
#: the prune is the long run, where a busy session would otherwise retain every
#: token-bearing event it ever saw.
PRUNE_AFTER = 64

#: Every way into safe mode. ``manual`` is the customer's own switch;
#: the others are automatic, and an automatic exit never lifts a manual entry.
#: ``"halt"`` is a fleet-wide Narrow, kept apart from ``"plane"`` (the
#: Controls-stated posture) on purpose — both are plane-driven, but
#: they are independent sources that must tighten together and lift
#: independently. Sharing one slot would mean lifting a Narrow halt could
#: erase a Controls ``restricted`` the operator set separately, or the
#: reverse.
POSTURE_SOURCES = ("budget_soft", "ladder", "circuit", "plane", "manual", "halt")

#: How much of a customer-supplied reason is kept.
POSTURE_REASON_MAX = 200

#: A keyed session with a real budget_usd and no budget_window is warned
#: about once it has lived this long -- unbounded lifetime accumulation on a
#: long-lived key is almost never what a customer meant to configure.
BUDGET_WINDOW_AGE_WARN_SECONDS = 24 * 60 * 60.0


_ONE_HOUR = timedelta(hours=1)
_ONE_DAY = timedelta(days=1)


def _next_month(start: datetime) -> datetime:
    """The first instant of the calendar month after ``start``'s."""
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _calendar_bucket(window: str, wall_now: float) -> tuple:
    """Which UTC hour/day/month ``wall_now`` falls in, as a plain tuple.

    A tuple compares by equality, which is all a caller needs to know "is
    this still the same bucket as last time" -- no arithmetic on it, no
    meaning beyond identity.
    """
    now = datetime.fromtimestamp(wall_now, tz=timezone.utc)
    if window == "hour":
        return (now.year, now.month, now.day, now.hour)
    if window == "month":
        return (now.year, now.month)
    return (now.year, now.month, now.day)  # "day", and the only other name


@dataclass(frozen=True)
class PostureState:
    """Which posture a session, the process or the fleet is in, why, and since when.

    ``name`` is a posture name (:mod:`runbound.posture`); the state carries it
    as a string rather than a :class:`~runbound.posture.Posture` so the table
    an override changes is read once, where the decision is made, and never
    frozen into a session. ``entered_at`` is wall-clock (``time.time()``) and
    for display only; nothing measures a window against it.
    """

    name: str
    reason: str
    source: str
    entered_at: float

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "reason": self.reason,
            "source": self.source,
            "entered_at": self.entered_at,
        }


def make_posture_state(name: str, reason: object, source: str) -> PostureState:
    """A :class:`PostureState` stamped now. Raises ``ValueError`` on a bad source.

    The posture *name* is validated where the posture table is known (the
    engine), not here: an override can name a posture this module has never
    heard of.
    """
    if source not in POSTURE_SOURCES:
        raise ValueError(f"posture source must be one of {POSTURE_SOURCES}, got {source!r}")
    if not isinstance(name, str) or not name:
        raise ValueError(f"posture name must be a non-empty string, got {name!r}")
    text = reason if isinstance(reason, str) else str(reason)
    return PostureState(name, text[:POSTURE_REASON_MAX], source, time.time())


class Hold:
    """Money (or another resource) reserved for the lifetime of one call.

    ``Engine.admit`` returns one of these instead of nothing: the estimate it
    checked against the budget is held on ``owner.reserved`` from the moment
    admission passes until whichever close-out path this call takes gives it
    back, so ``settled + reserved`` — never settled alone — is what the next
    concurrent call is compared against (INVARIANTS.md, invariant 2).

    ``resource`` names the ledger key (``"usd"`` today), ``amount`` is how
    much was reserved, and ``owner`` is the :class:`SessionState` it was taken
    from — ``None`` for a hold built without one, which then holds nothing and
    exists only so a caller need not special-case "there was no session to
    reserve against". ``released`` is true once :meth:`release` has run;
    ``actual`` and ``delta`` are ``None`` until :meth:`settle` records them.
    """

    def __init__(
        self, resource: str, amount: float, owner: "SessionState | None" = None
    ) -> None:
        self.resource = resource
        self.amount = float(amount)
        self.owner = owner
        self.released = False
        self.actual: float | None = None
        self.delta: float | None = None

    def release(self) -> None:
        """Give the reservation back, once, floored at zero. Never raises.

        Idempotent — a second call is a no-op — because a stream can reach
        both a failure path and a close path for the same call, and each must
        be free to release without knowing whether the other already did.
        ``released`` is set *before* the ledger is touched, so an owner whose
        ledger cannot be written at all still ends up marked released rather
        than retried by every close-out path that thinks it still owes one. Floored: ``owner.reserved[resource]`` is popped
        entirely rather than left at or below zero, so an untouched ledger
        reads back as ``{}`` and never accumulates drift from repeated
        floor-and-clamp arithmetic.
        """
        if self.released:
            return
        self.released = True
        owner = self.owner
        if owner is None:
            return
        try:
            with owner.lock:
                remaining = owner.reserved.get(self.resource, 0.0) - self.amount
                if remaining <= 0.0:
                    owner.reserved.pop(self.resource, None)
                else:
                    owner.reserved[self.resource] = remaining
        except Exception:
            _LOG.warning(
                "runbound could not release a %r hold of %.4f; the session may "
                "be left holding money that was already given back",
                self.resource,
                self.amount,
                exc_info=True,
            )

    def settle(self, actual: object) -> None:
        """Record ``actual`` for evidence, then release the hold in full.

        The ledger only ever gives back ``amount`` — the reservation, never
        ``actual`` — so a call that cost more than its cap can never
        over-credit the session, and one that cost less can never
        under-credit it either; the delta is informational only. ``actual``
        that is not a plain number (an edge that could not read one cleanly)
        records nothing but still releases. A hold that is already released
        records nothing and returns: the evidence belongs to whichever
        close-out path got here first.
        """
        if self.released:
            return
        try:
            self.actual = float(actual)
            self.delta = self.actual - self.amount
        except (TypeError, ValueError):
            pass
        self.release()

    def adjust(self, amount: float) -> None:
        """No-op today; the seam a future progressive reconcile will use.

        A call whose true cost becomes clear before it ends — a streaming
        response that has already emitted more output than its cap assumed —
        will eventually want to grow or shrink its own reservation in place
        instead of releasing and re-admitting. Nothing here calls this yet; it
        exists so that later change is additive rather than a new method on
        every caller.
        """
        return


class SessionState:
    """Counters and sliding windows for one agent run.

    Agents run tools and model calls from threads, so every mutation happens
    under ``self.lock`` (reentrant, so callers may hold it across a
    ``record()`` when they need a consistent multi-attribute read).

    ``key`` and ``tags`` identify a keyed session (one run, one job, one
    caller, say) and are ``None``/empty for the default session that guards a
    whole process.

    ``tool_calls`` counts attempts per tool name — the per-session number an
    action policy's ``max_calls`` is measured against. Only executed calls are
    counted there: what the model *asked* for is not what the agent did.

    ``requested_actions`` is the same distinction taken session-wide
    rather than per tool name: every ``tool_request`` event, written by
    :meth:`record` under ``self.lock`` -- a model asking for a tool, before
    anyone dispatches it.

    ``admitted_actions``, ``executed_actions`` and ``refused_actions``
    are the outcome of an *attempt* to run a tool, once admission (posture, a
    capability class rule, ``max_actions_per_run``) and the action policy
    have both had their say: :meth:`mark_action_admitted` raises the first two
    together (nothing in this codebase can refuse a call between "admitted"
    and "its body starts", so they always agree today; kept as two counters
    because the public contract names both), :meth:`mark_action_refused`
    the third. Unlike ``tool_calls`` and the loop window -- which count every
    *attempt*, admitted or not, because a refused repeat is still a loop --
    ``max_actions_per_run`` (``runbound.admission.actions``) is measured
    against ``executed_actions`` specifically: a refused attempt must not
    itself count as having executed, or the cap would refuse one attempt
    earlier than it says it does.

    ``depth``, ``parent_key`` and ``children`` are this session's place in a
    fan-out: how many :func:`~runbound.session` blocks enclose it, which key
    it was first opened under (``None`` for a top-level or default session),
    and how many distinct child sessions have been opened inside it. They are
    written once, by the api, when a session is first entered under a parent —
    ``children`` under ``self.lock`` like every other counter — and are what
    ``max_session_depth`` and ``max_child_sessions`` are measured against.

    ``error_timestamps`` holds the failures of the trailing minute (model calls
    and tools alike), pruned as they are appended, and ``consecutive_errors``
    is how many failures have happened since the last model call that worked —
    the two numbers a retry storm shows up in. ``total_errors`` is a
    third, unpruned count: the session's lifetime total of the same
    ``llm_error``/``tool_error`` events, diffed into
    :class:`~runbound.plane_types.ExitDelta`'s ``errors_delta`` the way
    ``total_tokens`` already diffs into ``tokens_delta`` — a storm window and
    a running total answer different questions, and only the second survives
    being read twice a minute apart.

    ``tripped_by`` is the latch: the anomaly that stopped this session, kept so
    that a host which catches the exception and keeps serving is stopped again
    on every later event instead of once, with ``tripped_at`` the monotonic
    time it was set — what ``latch_ttl_seconds`` measures the latch's age
    against. Both are written by the engine under ``self.lock``, and are
    otherwise cleared only by discarding the session.
    ``latch_ttl_override`` is this session's own expiry for that latch, which
    beats ``latch_ttl_seconds`` when set: it is how a rollover cooldown is
    served.
    ``returns_from`` names the posture whose stop this session is the cooldown
    for, on a session the ladder started after closing its predecessor or that
    another worker's rollover has latched, and is ``None`` on every other
    session (a budget or wall latch has an expiry of its own but no stop to
    return from). It is what makes serving the cooldown a return.
    ``return_strikes`` is the strike that rollover carried, and
    ``returned_strikes`` the highest strike this session has already recorded a
    return for, so one rollover is recorded once however often the plane
    repeats it.

    ``spike_baseline`` is the ``(median_duration, median_output)`` the spike
    detector last trusted, written by it under ``self.lock``. It is the
    session's *held* baseline: while the detector's trailing window still holds
    an abnormal call, that snapshot — taken on the last call before the run of
    abnormal ones — is what the next call is judged against, so a sustained
    spike train cannot teach a session that spikes are its normal. ``None``
    means no snapshot has been taken yet (the session has not warmed up), and
    the detector falls back to the live medians.

    ``estimated_cost_usd`` is the slice of ``total_cost_usd`` that came from an
    unpriced model priced by the ``on_unpriced_model="estimate"`` (or
    ``"refuse"``-recorded) fallback rather than a real price — the sum of
    ``cost_usd`` over events whose ``priced == "estimated"``. It is informative
    only: nothing compares a budget against it, so a customer running an
    exact-priced fleet next to an experimental unpriced model can see how much
    of the total is a guess.

    ``run_started_at`` is the clock ``max_session_seconds`` measures against:
    the monotonic instant this *run* began. For the default (unkeyed) session
    it is set once, at creation, because that session guards the whole
    process and *is* the run. For a keyed session it is reset by the api on
    every entry of :func:`~runbound.session` (before the fleet sync), because
    a returning key's next request is a new run, not a continuation of the
    first one it ever sent — a session reused across requests must not read
    as "running" since its very first request. ``started_at`` keeps its
    original meaning (the session's *lifetime*, from creation) for
    ``max_session_lifetime_seconds``, the opt-in cap for a customer who wants
    the old, identity-scoped meaning back.

    ``spend_offset_usd`` and ``tokens_offset`` are what the *rest of the fleet*
    has already spent under this session's key, handed over by the control
    plane when the session was entered and added to the local counters by the
    budget detector — so a $5 budget is $5 across every worker, not $5 each.
    They stay zero without a plane, which is exactly the single-process
    behavior. ``fleet_generation`` is the plane's version of this key's state,
    kept so a stale answer can be recognized and ignored. All three are written
    by the api under ``self.lock``, and never by a detector. ``reserved``
    (below) is the opposite kind of local: it is never folded into the fleet
    total and never sent to the plane, because it is this worker's own
    in-flight money, gone the moment its calls close out.

    ``reserved`` is what calls *in flight on this worker* are holding —
    ``{"usd": 1.0}`` while one ``max_tokens=1`` call worth a dollar is still
    running — written only under ``self.lock``, only by :meth:`hold` and by
    :class:`Hold.release`. It is worker-local, the same way ``spend_offset_usd``
    is fleet-wide: a reservation is a promise this process is about to spend
    money, not money spent, so it is never folded into fleet spend and never
    sent to the plane. ``settled + reserved`` — ``total_cost_usd +
    spend_offset_usd + reserved.get("usd", 0.0)`` — is what a budget is
    compared against at the door (:meth:`Engine._admit_budget`), which is the
    whole point: a call admitted while another is still in flight must see the
    other's money as already spoken for.

    The remaining four attributes are the abuse ladder (``on_spike="limit"``),
    written by the spike detector under ``self.lock`` and read by the api:
    ``spike_level`` (0 quiet, 1 watching, 2 limited, 3 closed),
    ``spike_allowance`` (abnormal calls left before the session is closed),
    ``spike_allowance_base`` (the allowance this session starts each limit
    with; ``None`` means ``config.spike_limit_calls``) and ``strikes`` (how
    many times this key has already been rolled over).

    The following attributes exist for one reason: so a customer can answer
    "why was this user limited?" without reading our code. They are the raw
    facts behind ``session_status()``'s ``why`` and ``history``, written by
    the spike detector and the api under ``self.lock`` at the moment each one
    changes, and read back (converted from a monotonic instant to "seconds
    ago") by the api.

    ``spike_trigger`` is the abnormal call that last *moved the level up* —
    ``{"metric", "value", "median", "factor", "at"}`` — so the number on the
    ladder is never a mystery: this is the call that put it there. It is left
    alone by a call that only spends allowance or heals, and it survives a
    rollover, because the question "why is this key on strike 2" is about the
    call that started it, not about the fresh session's still-empty history.

    ``spike_limited_at`` and ``spike_closed_at`` are the monotonic instants
    this session last entered level 2 and level 3 — ``None`` until it has.
    Both survive a rollover for the same reason ``spike_trigger`` does.

    ``spike_allowance_start`` is the allowance the *current* limit began with
    (what ``spike_allowance`` is counting down from); ``None`` when the
    session is not currently limited — set alongside ``spike_allowance`` and
    cleared alongside it on a heal, never carried across a rollover, because a
    fresh session's own limit has not started yet. A closed (level 3) session
    keeps the allowance its last limit began with — only a heal back to level
    1 clears it, and a closed session never heals.

    ``healed_times`` counts this key's "healed" transitions — level 2 falling
    back to 1 — and, unlike the allowance, does survive a rollover: it is a
    fact about the key's whole history, not about one limited episode.

    ``spike_baseline_source`` names where :attr:`spike_baseline` came
    from: ``"local"`` (this session's own live or held median), ``"restored"``
    (this key's own baseline, delivered by
    the control plane because another worker — or this one, before a
    restart — already learned it), or ``"peer"`` (no baseline for this key
    exists anywhere, so the service-wide median stands in for it). ``None``
    until the detector has judged a call. ``spike_baseline_samples`` is how
    many calls :attr:`spike_baseline` is a median of, when this worker
    learned it itself (``source == "local"``) — what the exit delta reports
    to the plane as the baseline's own sample count; ``None`` for a
    restored or peer baseline, which this worker did not learn and must not
    re-report as if it had.

    ``service_baseline`` is the ``(median_duration, median_output)`` the
    control plane last delivered for this session's *service* — a median of
    other keys' own baselines, never a call-weighted average, so one heavy
    key cannot define what "normal" means for a key that has never called
    before. ``None`` without a plane, or before one has answered. Read by
    the spike detector to report ``details["vs_service"]`` alongside
    whatever baseline it actually judged the call against, and to give a
    brand-new key something to be judged against from its first call
    (``source == "peer"``) rather than a several-call warmup.

    ``ladder_history`` is a bounded (``maxlen=10``) deque of every transition
    this key's ladder has made, oldest first, as raw
    ``(level_from, level_to, monotonic_instant, reason)`` tuples. It survives
    a rollover, the new session's own entry appended alongside what it
    inherited, so the story reads as one continuous climb rather than
    resetting at every strike. None of these six survive :func:`clear`, or a
    key's first-ever session: forgiveness there is total, and a session that
    has never been on the ladder has nothing to explain.
    """

    def __init__(
        self,
        session_id: str,
        loop_window: int = 20,
        spike_window: int = 50,
        key: str | None = None,
        tags: dict | None = None,
        strikes: int = 0,
        spike_allowance_base: int | None = None,
        latch_ttl_override: float | None = None,
        depth: int = 0,
        parent_key: str | None = None,
        spike_trigger: dict | None = None,
        spike_limited_at: float | None = None,
        spike_closed_at: float | None = None,
        healed_times: int = 0,
        ladder_history: "deque | None" = None,
    ) -> None:
        self.session_id = session_id
        self.key = key
        self.depth = depth
        self.parent_key = parent_key
        self.children = 0
        self.tags: dict = dict(tags) if tags else {}
        self.spike_level = 0
        self.spike_baseline: tuple[float, float] | None = None
        self.spike_baseline_source: str | None = None
        self.spike_baseline_samples: int | None = None
        self.service_baseline: tuple[float, float] | None = None
        self.spike_allowance: int | None = None
        self.spike_allowance_base = spike_allowance_base
        self.spike_allowance_start: int | None = None
        self.strikes = strikes
        self.latch_ttl_override = latch_ttl_override
        self.returns_from: str | None = None
        self.return_strikes = 0
        self.returned_strikes = 0
        self.spike_trigger: dict | None = spike_trigger
        self.spike_limited_at: float | None = spike_limited_at
        self.spike_closed_at: float | None = spike_closed_at
        self.healed_times: int = healed_times
        self.ladder_history: deque[tuple[int, int, float, str]] = deque(
            ladder_history or (), maxlen=10
        )
        self.event_count = 0
        self.turns = 0
        # How many tool actions the model has asked for (tool_request
        # events) and how many actually ran (tool_call events), counted
        # session-wide rather than per tool name the way tool_calls below is
        # — what runbound.envelope()'s actions_remaining and
        # max_actions_per_run's door check both read. loop_exempt calls still
        # count here (they are real requested/executed actions; only the
        # loop window itself ignores them), exactly like tool_calls.
        self.requested_actions = 0
        self.admitted_actions = 0
        self.executed_actions = 0
        self.refused_actions = 0
        self.tool_calls: dict[str, int] = {}
        self.total_tokens = 0
        self.tokens_cached_in = 0  # running total of Event.tokens_cached_in
        self.total_cost_usd = 0.0
        self.estimated_cost_usd = 0.0
        self.spend_offset_usd = 0.0
        self.tokens_offset = 0
        self.reserved: dict[str, float] = {}
        self.fleet_generation: int | None = None
        self.started_at = time.monotonic()
        self.run_started_at = self.started_at
        # The run's own spend, reset alongside run_started_at on every
        # session() entry -- what run_budget_usd/run_max_total_tokens are
        # measured against, independent of the key's own (cumulative,
        # possibly windowed) total_cost_usd/total_tokens.
        self.run_cost_usd = 0.0
        self.run_tokens = 0
        # budget_window's own bookkeeping: a calendar bucket (a plain tuple,
        # e.g. (2026, 9, 21) for "day") for a named window, or a monotonic
        # start instant for a seconds window. None until the first check.
        self.budget_window_marker: object = None
        self._budget_window_age_warned = False
        self.recent_hashes: deque[str] = deque(maxlen=loop_window)
        self.recent_calls: deque[tuple[float, int, float]] = deque(maxlen=spike_window)
        self.token_timestamps: deque[tuple[float, int]] = deque()
        self.error_timestamps: deque[float] = deque()
        self.consecutive_errors = 0
        self.total_errors = 0  # lifetime count, for ExitDelta.errors_delta
        self.tripped_by: Anomaly | None = None
        self.posture: PostureState | None = None
        self.tripped_at: float | None = None
        self.lock = threading.RLock()
        self._steps = 0

    def enter_safe_mode(self, reason: str = "manual", posture: str = "restricted") -> None:
        """Narrow this session to ``posture`` by hand.

        From now until :meth:`exit_safe_mode` or :func:`runbound.clear`, a
        ``@runbound.tool`` whose declared classes that posture denies is
        refused before its body runs; model calls are served as before. A
        manual entry replaces an automatic one, so no automatic exit can lift
        it. The posture name is checked by the caller that knows the table
        (:func:`runbound.enter_safe_mode`).
        """
        self._enter_posture(posture, reason, source="manual")

    def exit_safe_mode(self) -> None:
        """Put this session back to ``full``, whatever narrowed it."""
        self._exit_posture()

    def _enter_posture(
        self, name: str, reason: object, source: str, level: str | None = None
    ) -> bool:
        """Narrow to ``name`` from ``source``. True if this call changed the state.

        Three rules, in this order. A source may always replace **its own**
        entry, because the same driver escalating its own decision is the
        ladder going from limited to closed. ``manual`` replaces an automatic
        entry. An automatic source never replaces a *different* source's entry,
        manual or automatic, so two drivers cannot fight over one session.

        Every real move is recorded as a session-scoped posture transition,
        with the posture it replaced and, from the ladder, the rung's name in
        ``level`` (see :func:`runbound.local_events.record_posture`).
        """
        state = make_posture_state(name, reason, source)
        with self.lock:
            current = self.posture
            if current is not None and current.source != source and source != "manual":
                return False
            if current is not None and current.source == source and current.name == name:
                return False  # nothing moved; do not restamp the reason
            self.posture = state
            self._record_posture(source, name, state.reason, current, level)
            return True

    def _exit_posture(
        self,
        source: str | None = None,
        reason: str = "exit_safe_mode",
        level: str | None = None,
    ) -> bool:
        """Go back to ``full``. With ``source``, only that source's entry is lifted.

        True if this call changed the state. ``source=None`` is the manual exit
        and lifts anything. A real move is recorded like :meth:`_enter_posture`'s,
        under the source of the entry it lifted.
        """
        with self.lock:
            current = self.posture
            if current is None or (source is not None and current.source != source):
                return False
            self.posture = None
            self._record_posture(current.source, "full", reason, current, level)
            return True

    def record_return(self, previous: str, source: str, reason: str, level: str | None) -> None:
        """Record this session coming back from ``previous`` to the posture it
        holds now, for a move that happened by replacing the session rather
        than lifting its posture (a stopped session's cooldown served).

        ``to`` is read from the session, not assumed: a session that holds no
        posture is at ``"full"``.
        """
        with self.lock:
            held = self.posture
            self._record_posture(
                source, "full" if held is None else held.name, reason, previous, level
            )

    def _record_posture(
        self,
        source: str,
        name: str,
        reason: object,
        previous: "PostureState | str | None",
        level: str | None,
    ) -> None:
        """Record one session posture move. Never raises: a record that
        cannot be written must not undo or block the move itself.

        ``previous`` is the posture replaced: its state, or just its name."""
        try:
            local_events.record_posture(
                source,
                name,
                reason,
                self.session_id,
                previous=previous if isinstance(previous, str) or previous is None else previous.name,
                scope="session",
                level=level,
                key=self.key,
            )
        except Exception:
            _LOG.warning("runbound could not record a posture change; continuing", exc_info=True)

    def hold(self, resource: str, amount: float) -> Hold:
        """Reserve ``amount`` of ``resource`` for one call's lifetime.

        Raises ``self.reserved[resource]`` by ``amount`` under ``self.lock``
        and returns a :class:`Hold` bound to this session, which gives it back
        (see :meth:`Hold.release`) however the call the reservation was for
        ends. ``self.lock`` is reentrant, so a caller already holding it — the
        engine's compare-and-hold section — may call this without releasing
        first, keeping the whole "is there room, and if so take it" decision
        inside one lock section.
        """
        with self.lock:
            self.reserved[resource] = self.reserved.get(resource, 0.0) + amount
            return Hold(resource, amount, owner=self)

    def roll_budget_window(self, window: "str | float | None") -> bool:
        """Reset this key's cumulative counters if ``window`` has rolled over.

        Called before any read of ``total_cost_usd``/``total_tokens`` that
        feeds a budget decision (the reservation door, the post-call wall,
        ``budget()``'s own report) -- so every one of them sees the same,
        current window, whichever asks first. A no-op, cheaply, with no lock
        taken, when ``window`` is ``None`` (the pre-existing, unbounded
        behavior).

        A named window (``"hour"``/``"day"``/``"month"``) is a calendar UTC
        boundary: wall time is read here specifically because a customer who
        wrote ``budget_window="day"`` means the calendar day, not "the
        86400 seconds since we last checked" -- the two would silently
        disagree after a single restart or a clock adjustment. Any other
        window is a plain float/int, a rolling number of seconds measured on
        the monotonic clock, exactly as every other window/rate figure in
        this codebase is.

        Returns ``True`` when this call rolled the window over (the counters
        just reset); ``False`` when nothing moved, including on the very
        first call for a session (there is nothing to roll *from* yet, only
        a marker to establish).
        """
        if window is None:
            return False
        with self.lock:
            if isinstance(window, str):
                bucket = _calendar_bucket(window, time.time())
                rolled = self.budget_window_marker is not None and self.budget_window_marker != bucket
                self.budget_window_marker = bucket
            else:
                now = time.monotonic()
                start = self.budget_window_marker
                if start is None:
                    self.budget_window_marker = now
                    return False
                rolled = (now - start) >= window
                if rolled:
                    self.budget_window_marker = now
            if rolled:
                self.total_cost_usd = 0.0
                self.total_tokens = 0
                self.tokens_cached_in = 0
                self.estimated_cost_usd = 0.0
            return rolled

    def budget_window_resets_at(self, window: "str | float | None") -> "float | None":
        """When the current window ends, as a wall-clock (``time.time()``)
        timestamp — for display only, the same way ``PostureState.
        entered_at`` is. ``None`` without a window, or before
        :meth:`roll_budget_window` has ever run (nothing to compute the end
        of yet).

        A calendar window's end is the wall-clock boundary of the bucket
        :meth:`roll_budget_window` last recorded — the *next* hour, day or
        UTC midnight, regardless of when within the current one this is
        read. A seconds window's end is ``window`` seconds after the
        monotonic instant that window started, translated to a wall-clock
        reading by the same offset every other monotonic-to-wall conversion
        in this codebase uses (``now_wall - now_monotonic``).
        """
        if window is None:
            return None
        with self.lock:
            marker = self.budget_window_marker
        if marker is None:
            return None
        if isinstance(window, str):
            year, month, day = marker[0], marker[1], marker[2]
            hour = marker[3] if window == "hour" else 0
            start = datetime(year, month, day, hour, tzinfo=timezone.utc)
            if window == "hour":
                end = start + _ONE_HOUR
            elif window == "month":
                end = _next_month(start)
            else:
                end = start + _ONE_DAY
            return end.timestamp()
        now_monotonic = time.monotonic()
        now_wall = time.time()
        return (marker + window) + (now_wall - now_monotonic)

    def mark_action_admitted(self) -> None:
        """Count one tool attempt that cleared admission and the action policy.

        Called once, after both have had their say and before the tool's own
        body runs -- so a refusal from either never reaches here.
        Raises ``admitted_actions`` and ``executed_actions`` together: this
        codebase has nothing that can stop a call between "admitted" and "its
        body starts", so the two always agree, but they are kept as separate
        counters because the contract states both.
        """
        with self.lock:
            self.admitted_actions += 1
            self.executed_actions += 1

    def mark_action_refused(self) -> None:
        """Count one tool attempt that admission or the action policy refused.

        Called from the single ``except`` around both checks, so
        every refusal site -- a posture denial, a capability class rule, the
        ``max_actions_per_run`` cap, or a tool-policy violation -- raises
        this exactly once, whichever one fired.
        """
        with self.lock:
            self.refused_actions += 1

    def next_step(self) -> int:
        """Allocate this session's next 1-based step number, atomically."""
        with self.lock:
            self._steps += 1
            return self._steps

    @property
    def step_count(self) -> int:
        """Deprecated read-only alias for ``event_count``.

        Before this release "steps" meant every recorded event; it now means
        model turns (:attr:`turns`), and ``max_steps`` is measured against
        that instead. This alias is kept for one release for anyone already
        reading ``session.step_count`` expecting the old, every-event count —
        assign to :attr:`event_count` instead, this attribute cannot be set.
        """
        return self.event_count

    def record_ladder_transition(
        self,
        level_from: int,
        level_to: int,
        reason: str,
        trigger: tuple[str, float, float, float] | tuple[str, float, float, float, float | None] | None = None,
    ) -> None:
        """Append one abuse-ladder transition and keep its derived facts in sync.

        Called by the spike detector and the api at the moment of each
        transition — ``reason`` one of ``"first_abnormal"``, ``"confirmed"``,
        ``"healed"``, ``"allowance_spent"``, ``"rollover"``, ``"blocked"``,
        ``"cleared"`` or ``"restored"`` (a worker restart re-entering this
        key at the rung the plane last knew it on) — so a
        customer-facing ``why`` never has to be
        reconstructed after the fact by replaying events.

        ``healed_times`` increments on a ``"healed"`` transition;
        ``spike_limited_at``/``spike_closed_at`` are stamped when ``level_to``
        is 2 or 3. ``trigger`` — ``(metric, value, median, factor)``, or the
        same four plus a fifth ``vs_service`` (this call's ratio to the
        service-wide median, or ``None`` when no service baseline was known)
        — is only kept when this transition actually raised the level: a call
        that only spends allowance or heals the session is not what a
        customer asking "why" wants pointed to.
        """
        with self.lock:
            now = time.monotonic()
            self.ladder_history.append((level_from, level_to, now, reason))
            if reason == "healed":
                self.healed_times += 1
            if level_to == 2:
                self.spike_limited_at = now
            if level_to == 3:
                self.spike_closed_at = now
            if trigger is not None and level_to > level_from:
                metric, value, median, factor = trigger[:4]
                vs_service = trigger[4] if len(trigger) > 4 else None
                self.spike_trigger = {
                    "metric": metric,
                    "value": value,
                    "median": median,
                    "factor": factor,
                    "vs_service": vs_service,
                    "at": now,
                }

    def record(self, event: Event) -> None:
        """Fold one event into the session counters, atomically.

        ``event_count`` tracks the highest step seen rather than the number
        of events folded so far, so several events sharing one step (an
        llm_call plus its tool_call) do not inflate it; ``max_events`` is
        measured against it. ``turns`` counts ``llm_call`` events only — one
        agent step is one model turn — and ``max_steps`` is measured against
        that instead (``step_count`` is a read-only alias for
        ``event_count``, kept for one release). Only ``tool_call`` and
        ``tool_request`` events with a hash contribute to the loop window —
        and only when they are not
        ``loop_exempt`` (``@runbound.tool(polling=True)``, or a name in
        ``loop_ignore_tools``): such a call is marked "supposed to repeat" and
        never feeds the window, but still raises ``tool_calls`` below, since it
        happened and a policy's ``max_calls`` must still see it. Only named
        ``tool_call`` events raise that tool's entry in ``tool_calls`` (what a
        policy's per-session ``max_calls`` counts), and only token-bearing
        events are timestamped for velocity.

        A failed call (``llm_error``, ``tool_error``) is timestamped into the
        trailing-minute error window and raises ``consecutive_errors``; a model
        call that worked puts that counter back to zero and leaves the window
        alone, because a minute of failures is a storm however it ended.

        Only ``llm_call`` events land in ``recent_calls``, as
        ``(duration_s, output work, cost_usd)`` — output work being the completion
        token count (which already includes reasoning), the number a spike shows up in.
        An event whose ``priced == "estimated"`` also raises ``estimated_cost_usd``
        by its ``cost_usd``, whatever its kind — a partial call reported by an
        abandoned stream's finalizer can be priced this way too.

        The token window is bounded here rather than by whoever reads it: a
        session running for hours with velocity detection switched off — the
        default — would otherwise keep every token-bearing event it ever saw.

        ``tokens_cached_in`` is a running total of ``event.tokens_cached_in``
        — a subset of what already landed in ``total_tokens`` via
        ``tokens_in``, never additional, kept apart so a caller (or a future
        exit delta) can see how much of a session's spend was the discounted
        kind without re-deriving it from raw events.
        """
        tokens = event.tokens_in + event.tokens_out
        with self.lock:
            self.event_count = max(self.event_count, event.step)
            self.total_tokens += tokens
            self.run_tokens += tokens
            self.tokens_cached_in += event.tokens_cached_in
            self.total_cost_usd += event.cost_usd
            self.run_cost_usd += event.cost_usd
            if event.priced == "estimated":
                self.estimated_cost_usd += event.cost_usd
            if event.kind in HASHED_KINDS and event.args_hash and not event.loop_exempt:
                self.recent_hashes.append(event.args_hash)
            if event.kind in ERROR_KINDS:
                self.consecutive_errors += 1
                self.total_errors += 1
                self.error_timestamps.append(event.ts)
                self._prune_error_window(event.ts)
            elif event.kind == "llm_call":
                self.consecutive_errors = 0
            if event.kind == "tool_call" and event.tool_name:
                self.tool_calls[event.tool_name] = (
                    self.tool_calls.get(event.tool_name, 0) + 1
                )
            if event.kind == "tool_request":
                self.requested_actions += 1
            # ``executed_actions`` no longer increments here. This
            # event is recorded before admission runs (on purpose -- an
            # attempt must feed the loop window and tool_calls() whether or
            # not it is then refused), so counting it here counted a refused
            # attempt as executed. What actually ran is now tracked by
            # mark_action_admitted()/mark_action_refused(), called once the
            # outcome of admission (and the tool policy) is known.
            if event.kind == "llm_call":
                self.turns += 1
                # Providers already include reasoning/thinking tokens inside
                # the completion count, so tokens_out IS the output work —
                # adding tokens_reasoning again would double-count and make
                # max_tokens_out_per_call fire at half the configured value.
                self.recent_calls.append(
                    (event.duration_s, event.tokens_out, event.cost_usd)
                )
            if tokens > 0:
                self.token_timestamps.append((event.ts, tokens))
                self._prune_token_window(event.ts)

    def _prune_error_window(self, now: float) -> None:
        """Drop failures older than a minute. Caller holds ``self.lock``.

        Pruned on every append rather than in bulk: the window is only ever a
        storm's worth of entries, and the detector that reads it wants a count
        it can trust without pruning first. ``now`` is the incoming event's own
        timestamp, so a back-dated event prunes nothing on its way past.
        """
        cutoff = now - ERROR_WINDOW_SECONDS
        while self.error_timestamps and self.error_timestamps[0] < cutoff:
            self.error_timestamps.popleft()

    def _prune_token_window(self, now: float) -> None:
        """Drop token entries older than a minute. Caller holds ``self.lock``.

        ``now`` is the incoming event's own timestamp, so replayed or
        back-dated events prune against the world they describe; an event that
        arrives out of order simply prunes nothing on its way past.
        """
        if len(self.token_timestamps) <= PRUNE_AFTER:
            return
        cutoff = now - TOKEN_WINDOW_SECONDS
        while self.token_timestamps and self.token_timestamps[0][0] < cutoff:
            self.token_timestamps.popleft()
