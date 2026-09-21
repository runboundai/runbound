"""Detectors — pure verdicts about a session, one detector per failure mode.

A detector answers a single question about the session as it stands after an
event has been recorded, and returns an :class:`Anomaly` or ``None``. It does
no I/O, sends no alerts and decides no policy: what happens to an anomaly is
the engine's business.

Several pieces of state are deliberate exceptions to that purity: each
detector instance remembers which sessions it has already fired for (a
sustained overrun must not re-alert on every following event),
:class:`SpikeDetector` additionally tracks each session's trailing abnormal
calls and its place on the abuse ladder, :class:`VelocityDetector` prunes
expired entries from the session's token window so a long-running session's
memory stays bounded, and :class:`LoopDetector` keeps its own bounded,
per-session windows of recent tool names and turns for the "sequence",
"retry" and "stall" shapes — the tool names and turn numbers ``recent_hashes``
does not carry.

Detectors are constructed once per session-scoped engine and called from
whatever thread the agent happens to be on, so every multi-value read of
``SessionState`` happens under ``state.lock``.
"""

import statistics
from collections import deque

from . import ladder
from .config import SPIKE_CONFIRM_MAX, GuardrailConfig
from .events import Anomaly, Decision, Event
from .ladder import Effect, Observation, Transition
from .plane_types import key_hash as _key_hash
from .state import ERROR_WINDOW_SECONDS, HASHED_KINDS, SessionState

VELOCITY_WINDOW_SECONDS = 60.0

#: Loop policies whose reaction applies to every repeat, not just the first.
REPEATING_LOOP_POLICIES = ("throttle", "escalate")


class _FireOnceDetector:
    """Shared bookkeeping: one anomaly per session, then silence.

    Subclasses implement ``_evaluate``; they are never called again for a
    session once they have returned an anomaly for it.
    """

    name: str = ""

    def __init__(self) -> None:
        self._fired: set[str] = set()

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """Return an anomaly if this detector's condition just tripped.

        Returns ``None`` when the detector's config knob is unset (disabled),
        when the condition does not hold, or when this detector has already
        reported on ``state.session_id``.
        """
        if state.session_id in self._fired:
            return None
        anomaly = self._evaluate(state, event, config)
        if anomaly is not None:
            self._fired.add(state.session_id)
        return anomaly

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        raise NotImplementedError

    def rearm(self, session_id: str) -> None:
        """Forget that ``session_id`` already fired.

        Called by the engine when a latch's ``latch_ttl_seconds`` expires and
        the session heals: the fire-once memo must not silence a condition
        that still holds, or the session would run with no wall at all until
        someone calls :func:`runbound.clear`. Rearming is not resetting —
        ``state.total_cost_usd`` and friends are untouched, so a session that
        is still over budget re-trips on its very next event, with the same
        detector. A session id this detector never fired for is a no-op.
        """
        self._fired.discard(session_id)


class LoopDetector(_FireOnceDetector):
    """Catches an agent in one of four deterministic, content-blind loop shapes.

    ``"repeat"`` is the original mechanism, unchanged: the current event's
    ``args_hash`` appears at least ``loop_threshold`` times in
    ``state.recent_hashes`` (the trailing hash sequence, maxlen
    ``loop_window``). Only ``tool_call`` and ``tool_request`` events can trip
    it, and only via that hash: an event without one, or a model call, is
    never a loop by itself. A model that keeps *asking* for the same tool is
    looping whether or not the developer dispatches the request, and because
    request hashes live in their own ``"req:"`` namespace the two streams are
    counted separately — three requests and three executions are two threes,
    not a six.

    The other three shapes are additive, the loops agents actually
    fall into beyond one call repeated verbatim:

    ``"sequence"``
        A period-k cycle of *tool names* (not exact arguments),
        ``2 <= k <= loop_max_period``, repeated ``loop_threshold`` times back
        to back — the edit-test-edit cycle, not ``search("x")`` three times.
        Found by suffix comparison over this detector's own per-session
        window of ``(turn, tool_name, args_hash)`` triples, built one event
        at a time in :meth:`_track`; see :meth:`_evaluate_sequence` for why
        it compares names rather than hashes. The window mirrors
        ``state.recent_hashes``'s own maxlen and exemption rule exactly
        (``HASHED_KINDS``, a hash present, not ``loop_exempt``), so a
        ``polling=True`` tool inside an otherwise-looping sequence is as
        invisible to "sequence" as it is to "repeat" — what is left after
        removing it is whatever the other tools were doing on their own.
    ``"retry"``
        One tool's ``tool_error`` events reaching ``loop_threshold`` (twice
        that, a declared grace, for a ``retryable=True`` tool — see
        :meth:`_evaluate_retry`) within the trailing ``loop_window`` failures
        of *that* tool — distinct from ``error_storm``, which counts every
        failure, any tool, any kind (``llm_error`` too), in a trailing
        60-second window rather than a trailing action count. A
        ``retryable`` tool's grace only has room to matter when
        ``loop_window >= 2 * loop_threshold``; a smaller window still evicts
        the earlier failures before the doubled bar is reached.
    ``"stall"``
        ``loop_stall_turns`` consecutive turns that introduce no hash this
        session has not already seen at least once before — the agent going
        quiet rather than looping. Deliberately not in the default
        ``loop_shapes`` (:data:`runbound.config.LOOP_SHAPES`): opt in to it.

    ``config.loop_shapes`` says which shapes run and in what order — the
    first one that matches on a given event wins, so reordering the tuple
    changes precedence, not just membership.

    Beyond the fire-once memo every detector keeps, this one keeps three more
    per-session structures — the same kind of deliberate exception
    :class:`SpikeDetector` and :class:`VelocityDetector` already keep for
    their own trailing windows: a bounded deque of recent ``(turn, tool,
    hash)`` triples per session for "sequence", a bounded deque of recent
    ``(turn, tool)`` failures per session for "retry", and the turn of the
    last never-seen hash per session for "stall". All three are folded in by
    :meth:`_track`, called once per event exactly as the engine calls
    :meth:`check` in production. "repeat" needs none of this — it reads
    ``state.recent_hashes`` directly — which is why a caller that replays a
    batch of ``state.record`` calls and then calls :meth:`check` only once
    (several of this module's own tests for "repeat" do exactly that) still
    sees "repeat" correctly even though "sequence"/"retry"/"stall" saw only
    that one event.

    It fires once per session like every other detector, except under the
    ``on_loop`` policies that react to each repeat ("throttle", "escalate"):
    those need a verdict on every event, so the fire-once memo is bypassed —
    and, exactly as before 0.4.0, only for "repeat"; the other three shapes
    are never affected by ``on_loop``.
    """

    name = "loop"

    def __init__(self) -> None:
        super().__init__()
        #: session_id -> deque[(turn, tool_name, args_hash)], maxlen loop_window.
        self._windows: dict[str, deque] = {}
        #: session_id -> every non-exempt hash ever seen this session (for "stall").
        self._all_hashes: dict[str, set] = {}
        #: session_id -> turn of the last hash this session had never seen before.
        self._last_new_turn: dict[str, int] = {}
        #: session_id -> deque[(turn, tool_name)] of tool_error events, maxlen loop_window.
        self._errors: dict[str, deque] = {}

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """Report the loop, once per session or on every repeat.

        Which one depends on ``config.on_loop``: the repeat-driven policies
        need one anomaly per repeating event, and only cover the "repeat"
        shape (unchanged from before 0.4.0); every other case — including
        "repeat" when no such policy is set — fires at most once per session.
        """
        self._track(state, event, config)
        if config.on_loop in REPEATING_LOOP_POLICIES and "repeat" in getattr(
            config, "loop_shapes", ("repeat",)
        ):
            return self._evaluate(state, event, config)
        if state.session_id in self._fired:
            return None
        anomaly = self._evaluate_shapes(state, event, config)
        if anomaly is not None:
            self._fired.add(state.session_id)
        return anomaly

    def _track(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> None:
        """Fold one event into this detector's own per-session history.

        The hashed window mirrors ``SessionState.record``'s own filter for
        ``recent_hashes`` exactly (``HASHED_KINDS``, a hash present, not
        ``loop_exempt``) so its length and order always agree with
        ``recent_hashes``'s — the only things added are the tool name and the
        turn, neither of which ``recent_hashes`` carries and neither of which
        this task's ownership extends to adding there (``state.py``).
        """
        session_id = state.session_id
        turn = state.turns
        if event.kind in HASHED_KINDS and event.args_hash and not event.loop_exempt:
            window = self._windows.setdefault(
                session_id, deque(maxlen=config.loop_window)
            )
            window.append((turn, event.tool_name or "<unknown>", event.args_hash))
            seen = self._all_hashes.setdefault(session_id, set())
            if event.args_hash not in seen:
                seen.add(event.args_hash)
                self._last_new_turn[session_id] = turn
        if event.kind == "tool_error" and event.tool_name:
            errors = self._errors.setdefault(
                session_id, deque(maxlen=config.loop_window)
            )
            errors.append((turn, event.tool_name))

    def _evaluate_shapes(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """The first shape in the effective ``loop_shapes`` that matches, or
        ``None``. ``config.loop_shapes`` is not a
        :class:`~runbound.config.GuardrailConfig` field with a static
        default -- it is set on the shim :meth:`~runbound.engine.Engine.
        _config_for_detection` builds only when the plane's Controls enable
        shapes beyond "repeat"; absent that, nothing beyond "repeat" is ever
        evaluated here."""
        for shape in getattr(config, "loop_shapes", ("repeat",)):
            anomaly = _SHAPE_EVALUATORS[shape](self, state, event, config)
            if anomaly is not None:
                return anomaly
        return None

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """The "repeat" shape: unchanged detection, enriched details."""
        if event.kind not in HASHED_KINDS or not event.args_hash:
            return None

        with state.lock:
            count = state.recent_hashes.count(event.args_hash)

        if count < config.loop_threshold:
            return None

        tool = event.tool_name or "<unknown>"
        what = "model requested tool" if event.kind == "tool_request" else "tool"
        started_turn = self._started_turn(state, event.args_hash)
        return Anomaly(
            detector=self.name,
            severity=self._severity(count, config),
            message=(
                f"Loop detected: {what} {tool!r} repeated {count}x "
                f"in last {config.loop_window} actions"
            ),
            details={
                "session_id": state.session_id,
                "tool_name": event.tool_name,
                "args_hash": event.args_hash,
                "count": count,
                "threshold": config.loop_threshold,
                "window": config.loop_window,
                "shape": "repeat",
                "period_tools": [tool],
                "repeats": count,
                "started_turn": started_turn,
                "usd_inside_loop": _usd_inside_loop(state, started_turn),
            },
        )

    def _started_turn(self, state: SessionState, args_hash: str) -> int:
        """The earliest turn this detector's own window has ``args_hash`` at.

        Falls back to the session's current turn when this detector's window
        has nothing for this session — a caller that fed ``state`` by
        replaying ``record`` calls directly and never called :meth:`check`
        on the earlier ones (see the class docstring) — so "repeat" always
        has a defined ``started_turn`` rather than raising.
        """
        window = self._windows.get(state.session_id)
        if window:
            for turn, _, h in window:
                if h == args_hash:
                    return turn
        return state.turns

    def _evaluate_sequence(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """The "sequence" shape: a period-k *tool-name* cycle, repeated ``loop_threshold`` times.

        The comparison is on tool **name**, not ``args_hash``: the loops
        agents fall into repeat the same rotation of tools with *different*
        arguments each cycle (a different file, a different query) — see the
        class and module docstrings for why "repeat"'s exact-hash matching
        cannot be the same signal as this one. Only a hashed, non-exempt
        ``tool_call``/``tool_request`` can be the event that completes a
        cycle, and a candidate period whose unit is a single repeated name
        (a degenerate "period" that is really just one tool spammed with
        different arguments — not one of the four shapes this detector
        claims) is skipped.

        Checks periods ``2..loop_max_period`` in increasing order and returns
        the first (smallest genuine) match, so a period-4 "ABAB" pattern is
        reported as period-2. Cost per call is bounded: the tail examined for
        period ``k`` is exactly ``k * loop_threshold`` long, so total work
        across every period is ``O(loop_threshold * loop_max_period**2)`` —
        O(window), since ``loop_max_period`` is a small, fixed, customer-set
        constant and ``loop_threshold <= loop_window`` is enforced at
        ``validate()``.
        """
        if event.kind not in HASHED_KINDS or not event.args_hash or event.loop_exempt:
            return None
        window = self._windows.get(state.session_id)
        if not window:
            return None
        items = list(window)
        threshold = config.loop_threshold
        for period in range(2, config.loop_max_period + 1):
            block = period * threshold
            if block > len(items):
                break
            tail = items[-block:]
            unit = [name for _, name, _ in tail[-period:]]
            if len(set(unit)) < 2:
                continue  # degenerate: really period-1, not a genuine cycle
            if not all(tail[i][1] == tail[i % period][1] for i in range(block)):
                continue
            started_turn = tail[0][0]
            return Anomaly(
                detector=self.name,
                severity="critical",
                message=(
                    f"Loop detected: a period-{period} sequence {unit!r} "
                    f"repeated {threshold}x in last {config.loop_window} actions"
                ),
                details={
                    "session_id": state.session_id,
                    "shape": "sequence",
                    "period": period,
                    "period_tools": unit,
                    "repeats": threshold,
                    "threshold": threshold,
                    "window": config.loop_window,
                    "started_turn": started_turn,
                    "usd_inside_loop": _usd_inside_loop(state, started_turn),
                },
            )
        return None

    def _evaluate_retry(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """The "retry" shape: one tool's failures reaching a threshold.

        Only the ``tool_error`` event that reaches the threshold can trip
        this — the same "fires on the event that completes it" convention
        "repeat" and "sequence" follow.

        ``@runbound.tool(retryable=True)`` grants that tool a grace:
        its failures are not ignored (they still count, and still feed
        ``error_storm`` exactly as any tool's do), but the retry shape does
        not fire on them until they reach *twice* ``loop_threshold`` rather
        than once — "a retryable tool's retries do not count toward the
        retry shape until loop_threshold" i.e. the ordinary bar has already
        been crossed and the grace is spent. This mirrors the house pattern in
        :meth:`GuardrailConfig.hard_loop_threshold` (``on_loop="escalate"``'s
        own ``2 * loop_threshold`` default). ``retryable`` is read off the
        *triggering* event — the decorator declares it once, so every
        ``tool_error`` for that tool carries the same value.
        """
        if event.kind != "tool_error" or not event.tool_name:
            return None
        errors = self._errors.get(state.session_id)
        if not errors:
            return None
        matches = [turn for turn, name in errors if name == event.tool_name]
        count = len(matches)
        threshold = config.loop_threshold * 2 if event.retryable else config.loop_threshold
        if count < threshold:
            return None
        started_turn = matches[0]
        return Anomaly(
            detector=self.name,
            severity="critical",
            message=(
                f"Loop detected: tool {event.tool_name!r} failed and was "
                f"retried {count}x in last {config.loop_window} actions"
            ),
            details={
                "session_id": state.session_id,
                "shape": "retry",
                "tool_name": event.tool_name,
                "period_tools": [event.tool_name],
                "repeats": count,
                "threshold": threshold,
                "window": config.loop_window,
                "started_turn": started_turn,
                "usd_inside_loop": _usd_inside_loop(state, started_turn),
            },
        )

    def _evaluate_stall(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """The "stall" shape: ``loop_stall_turns`` turns adding no new hash.

        Unlike the other three shapes this can trip on any event kind,
        ``llm_call`` included — a run that stops trying anything new is a
        stall whether or not it is still calling tools.
        """
        turn = state.turns
        if turn < config.loop_stall_turns:
            return None
        last_new = self._last_new_turn.get(state.session_id, 0)
        stalled = turn - last_new
        if stalled < config.loop_stall_turns:
            return None
        started_turn = last_new + 1
        return Anomaly(
            detector=self.name,
            severity="critical",
            message=(
                f"Loop detected: a {stalled}-turn stall, no new tool call in "
                f"the last {config.loop_stall_turns} turns"
            ),
            details={
                "session_id": state.session_id,
                "shape": "stall",
                "period_tools": [],
                "repeats": stalled,
                "threshold": config.loop_stall_turns,
                "window": config.loop_window,
                "started_turn": started_turn,
                "usd_inside_loop": _usd_inside_loop(state, started_turn),
            },
        )

    @staticmethod
    def _severity(count: int, config: GuardrailConfig) -> str:
        """A loop is critical, except while ``escalate`` is still warning.

        Under ``on_loop="escalate"`` repeats below the hard threshold are a
        "warn" the engine only logs; at the hard threshold they turn
        "critical" and stop the agent.
        """
        if config.on_loop == "escalate" and count < config.hard_loop_threshold():
            return "warn"
        return "critical"


#: Dispatch table for :meth:`LoopDetector._evaluate_shapes`, keyed by the
#: names ``config.loop_shapes`` (validated against ``config.LOOP_SHAPES``)
#: may contain. A module-level dict rather than a per-instance one: the
#: methods it names are looked up fresh on every call, so subclassing
#: LoopDetector and overriding one of them is honored.
_SHAPE_EVALUATORS = {
    "repeat": LoopDetector._evaluate,
    "sequence": LoopDetector._evaluate_sequence,
    "retry": LoopDetector._evaluate_retry,
    "stall": LoopDetector._evaluate_stall,
}


def _usd_inside_loop(state: SessionState, started_turn: int) -> float:
    """The model spend since ``started_turn`` — bounded, honestly, by the spike window.

    ``SessionState.recent_calls`` is the only per-call cost history a session
    keeps, and it is a ``deque(maxlen=spike_window)`` (default 50) of this
    session's most recent ``llm_call`` events as ``(duration_s, tokens_out,
    cost_usd)`` — one entry per call, and ``SessionState.turns`` increments on
    exactly those same events, one for one. So entry ``-i`` (1-based from the
    end) is turn ``state.turns - i + 1``, and this function can only ever see
    as far back as ``len(recent_calls)`` turns, however old ``started_turn``
    really is.

    When the loop began within that trailing window, the sum returned is
    exact. When it began longer ago — more turns back than ``spike_window``
    — ``state`` no longer holds the earlier calls' cost at all, and this
    returns the spend inside the trailing window only: a documented lower
    bound on the loop's true spend, never a wrong number reported as an exact
    one. Zero for a session with no model calls yet.
    """
    with state.lock:
        turns = state.turns
        calls = list(state.recent_calls)
    if turns <= 0 or not calls:
        return 0.0
    wanted = max(turns - started_turn + 1, 0)
    available = min(wanted, len(calls))
    if available <= 0:
        return 0.0
    return sum(cost for _, _, cost in calls[-available:])


def _tighter_crossing(
    key_used: float, key_limit: "float | None", run_used: float, run_limit: "float | None"
) -> tuple:
    """Has either the key's own cumulative total or the run's own crossed its
    limit — and if both have, which one is the tighter bind?

    ``(crossed, used, limit, level)``. Pure arithmetic, no lock, no I/O — the
    same "which scope actually bound this" question
    ``runbound.engine._tighter_budget`` answers for the admission door,
    asked here for the post-call wall instead: a call already happened, so
    this reports whichever ceiling it actually broke, preferring the one
    whose overage is larger when both did (a tie goes to the key, the
    identity that outlives any one run).
    """
    key_over = key_limit is not None and key_used > key_limit
    run_over = run_limit is not None and run_used > run_limit
    if run_over and not key_over:
        return True, run_used, run_limit, "run"
    if key_over and not run_over:
        return True, key_used, key_limit, "key"
    if key_over and run_over:
        if (run_limit - run_used) < (key_limit - key_used):
            return True, run_used, run_limit, "run"
        return True, key_used, key_limit, "key"
    return False, None, None, None


class BudgetDetector(_FireOnceDetector):
    """Catches a session spending more money or tokens than it was allowed.

    Both limits are optional and checked independently; cost is reported
    first when a single event blows through both.

    The numbers measured are the session's own totals plus the fleet offsets
    the control plane handed over at entry (zero without a plane), so a budget
    is a budget across every worker sharing the key rather than per process.
    The offsets appear in ``details`` only when they are not zero — a
    single-process trip says nothing about a fleet.

    ``details["estimated_cost_usd"]`` likewise appears only when it is not
    zero: the slice of ``total_cost_usd`` that came from an unpriced model
    priced by the ``on_unpriced_model`` fallback rather than a real price
    (:attr:`~runbound.state.SessionState.estimated_cost_usd`) — informative,
    not part of the comparison against ``budget_usd``.
    """

    name = "budget"

    def __init__(self) -> None:
        super().__init__()
        #: Sessions the soft line has already warned about. Its own memo,
        #: so a warning can never silence the wall, and not cleared by
        #: :meth:`rearm`: a warning already sent is not news the second time.
        self._soft_fired: set[str] = set()

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """The wall first; the soft line only when the wall has nothing to say.

        A call that crosses both at once trips the wall, and the soft line then
        stays quiet for this session — a warning after the stop is noise.
        """
        if state.session_id in self._fired:
            return None
        anomaly = self._evaluate(state, event, config)
        if anomaly is not None:
            self._fired.add(state.session_id)
            self._soft_fired.add(state.session_id)
            return anomaly
        return self._soft_line(state, config)

    def _soft_line(self, state: SessionState, config: GuardrailConfig) -> Anomaly | None:
        """One ``warn`` at the first call over ``budget_soft``; safe mode if asked.

        Strictly greater than the line, like the wall. Under
        ``on_budget_soft="safe_mode"`` the crossing narrows the session
        (``source="budget_soft"``) and spend back under the line lifts it —
        only that entry, never a manual one. Nothing here latches.
        """
        soft = getattr(config, "budget_soft", None)
        if config.budget_usd is None or soft is None:
            return None
        state.roll_budget_window(getattr(config, "budget_window", None))
        line = config.budget_usd * soft
        narrowing = getattr(config, "on_budget_soft", "notify") == "safe_mode"
        with state.lock:
            total = state.total_cost_usd + state.spend_offset_usd
        if total <= line:
            if narrowing:
                leave = getattr(state, "_exit_posture", None)
                if leave is not None:
                    leave(source="budget_soft")
            return None
        if state.session_id in self._soft_fired:
            return None
        self._soft_fired.add(state.session_id)
        if narrowing:
            enter = getattr(state, "_enter_posture", None)
            if enter is not None:
                enter(
                    "restricted",
                    f"budget soft line: ${total:.4f} of ${config.budget_usd:.4f}",
                    source="budget_soft",
                )
        return Anomaly(
            detector=self.name,
            severity="warn",
            message=(
                f"Budget soft line crossed: ${total:.4f} spent, soft line "
                f"${line:.4f} ({soft:.0%} of ${config.budget_usd:.4f})"
            ),
            details={
                "session_id": state.session_id,
                "total_cost_usd": total,
                "budget_usd": config.budget_usd,
                "budget_soft": soft,
                "soft_at_usd": line,
                "on_budget_soft": getattr(config, "on_budget_soft", "notify"),
                "limit_hit": "budget_soft",
            },
        )

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        run_budget_usd = getattr(config, "run_budget_usd", None)
        run_max_total_tokens = getattr(config, "run_max_total_tokens", None)
        if (
            config.budget_usd is None and config.max_total_tokens is None
            and run_budget_usd is None and run_max_total_tokens is None
        ):
            return None
        if config.budget_usd is not None:
            state.roll_budget_window(getattr(config, "budget_window", None))

        with state.lock:
            spend_offset = state.spend_offset_usd
            tokens_offset = state.tokens_offset
            total_cost = state.total_cost_usd + spend_offset
            total_tokens = state.total_tokens + tokens_offset
            estimated_cost = state.estimated_cost_usd
            run_cost = state.run_cost_usd
            run_tokens = state.run_tokens

        details = {
            "session_id": state.session_id,
            "total_cost_usd": total_cost,
            "budget_usd": config.budget_usd,
            "total_tokens": total_tokens,
            "max_total_tokens": config.max_total_tokens,
        }
        if spend_offset:
            details["fleet_spend_offset_usd"] = spend_offset
        if tokens_offset:
            details["fleet_tokens_offset"] = tokens_offset
        if estimated_cost:
            # Omitted when zero so an exact-priced fleet's anomaly details are
            # unchanged from before this field existed.
            details["estimated_cost_usd"] = estimated_cost

        cost_over, cost_used, cost_limit, cost_level = _tighter_crossing(
            total_cost, config.budget_usd, run_cost, run_budget_usd
        )
        if cost_over:
            decision = Decision(
                verdict="deny",
                kind="model_call",
                boundary="money",
                level=cost_level,
                detector=self.name,
                reason=(
                    f"budget exceeded: ${cost_used:.4f} spent, limit "
                    f"${cost_limit:.4f}; the call that crossed it "
                    "already happened and its result is withheld"
                ),
                evaluation={
                    "limit": cost_limit,
                    "used": cost_used,
                    "provider_called": True,
                },
            )
            return Anomaly(
                detector=self.name,
                severity="critical",
                message=(
                    f"Budget exceeded: ${cost_used:.4f} spent, "
                    f"limit ${cost_limit:.4f}"
                ),
                details={
                    **details,
                    "limit_hit": "budget_usd" if cost_level == "key" else "run_budget_usd",
                    "level": cost_level,
                    "decision": decision.as_dict(),
                    "key_hash": _key_hash(state.key) if state.key else None,
                },
            )

        tokens_over, tokens_used, tokens_limit, tokens_level = _tighter_crossing(
            total_tokens, config.max_total_tokens, run_tokens, run_max_total_tokens
        )
        if tokens_over:
            decision = Decision(
                verdict="deny",
                kind="model_call",
                boundary="tokens",
                level=tokens_level,
                detector=self.name,
                reason=(
                    f"token budget exceeded: {tokens_used} tokens used, limit "
                    f"{tokens_limit}; the call that crossed it "
                    "already happened and its result is withheld"
                ),
                evaluation={
                    "limit": tokens_limit,
                    "used": tokens_used,
                    "provider_called": True,
                },
            )
            return Anomaly(
                detector=self.name,
                severity="critical",
                message=(
                    f"Token budget exceeded: {tokens_used} tokens used, "
                    f"limit {tokens_limit}"
                ),
                details={
                    **details,
                    "limit_hit": "max_total_tokens" if tokens_level == "key" else "run_max_total_tokens",
                    "level": tokens_level,
                    "decision": decision.as_dict(),
                    "key_hash": _key_hash(state.key) if state.key else None,
                },
            )

        return None


class VelocityDetector(_FireOnceDetector):
    """Catches a session burning tokens too fast, regardless of the total.

    Entries older than the 60s window are dropped from
    ``state.token_timestamps`` on every check, measured against the current
    event's timestamp rather than the clock, so a session that runs for hours
    does not accumulate a deque of dead entries. An entry exactly 60s old is
    still inside the window.
    """

    name = "velocity"

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        if config.tokens_per_minute_limit is None:
            return None

        cutoff = event.ts - VELOCITY_WINDOW_SECONDS
        with state.lock:
            timestamps = state.token_timestamps
            while timestamps and timestamps[0][0] < cutoff:
                timestamps.popleft()
            recent_tokens = sum(tokens for _, tokens in timestamps)

        if recent_tokens <= config.tokens_per_minute_limit:
            return None

        return Anomaly(
            detector=self.name,
            severity="warn",
            message=(
                f"Token velocity high: {recent_tokens} tokens in the last 60s, "
                f"limit {config.tokens_per_minute_limit}"
            ),
            details={
                "session_id": state.session_id,
                "tokens_last_60s": recent_tokens,
                "tokens_per_minute_limit": config.tokens_per_minute_limit,
                "window_seconds": VELOCITY_WINDOW_SECONDS,
            },
        )


class StepDetector(_FireOnceDetector):
    """Catches an agent that keeps taking steps past its allowed budget.

    An agent step is a model turn: ``max_steps`` is measured against
    ``state.turns`` (the count of ``llm_call`` events), not every recorded
    event — a turn that makes three tool calls is one step, not four. See
    :class:`EventsDetector` for the raw every-event count this used to mean.
    """

    name = "steps"

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        if config.max_steps is None:
            return None

        with state.lock:
            turns = state.turns

        if turns <= config.max_steps:
            return None

        return Anomaly(
            detector=self.name,
            severity="critical",
            message=(
                f"Step limit exceeded: {turns} steps (model turns) taken, "
                f"limit {config.max_steps}"
            ),
            details={
                "session_id": state.session_id,
                "turns": turns,
                "max_steps": config.max_steps,
            },
        )


class EventsDetector(_FireOnceDetector):
    """Catches a session that keeps generating events past its allowed budget.

    Every recorded event counts — model calls, tool calls, tool requests, and
    failures alike — which is what ``max_steps`` counted before "step"
    was redefined to mean a model turn. Use this when what you actually
    want bounded is total recorded activity, not just how many times the
    model itself was called.
    """

    name = "events"

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        if config.max_events is None:
            return None

        with state.lock:
            event_count = state.event_count

        if event_count <= config.max_events:
            return None

        return Anomaly(
            detector=self.name,
            severity="critical",
            message=(
                f"Event limit exceeded: {event_count} events recorded, "
                f"limit {config.max_events}"
            ),
            details={
                "session_id": state.session_id,
                "event_count": event_count,
                "max_events": config.max_events,
            },
        )


class ErrorStormDetector(_FireOnceDetector):
    """Catches an agent retrying into a wall.

    The money blunder here is not the failures themselves — those are free —
    it is the retry loop around them: a provider answering 429 while the agent,
    its framework and the loop above it all retry, burning latency, quota and
    (on every attempt that half-succeeds) real dollars, with nothing that could
    possibly work happening. Counting failures in a trailing minute is what
    tells that apart from a flaky afternoon.

    Model calls and tool calls both count: from the session's point of view a
    tool that fails forty times a minute is the same incident as a provider
    that does. ``error_storm_limit`` is the number the session may reach and
    stay quiet at; the one past it fires, once per session, as critical.
    """

    name = "error_storm"

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        limit = config.error_storm_limit
        if limit is None:
            return None

        cutoff = event.ts - ERROR_WINDOW_SECONDS
        with state.lock:
            failures = state.error_timestamps
            while failures and failures[0] < cutoff:
                failures.popleft()
            count = len(failures)

        if count <= limit:
            return None

        key = getattr(state, "key", None)
        whose = f" for session {key!r}" if key else ""
        return Anomaly(
            detector=self.name,
            severity="critical",
            message=(
                f"Error storm{whose}: {count} failed calls in the last "
                f"{ERROR_WINDOW_SECONDS:.0f}s, limit {limit}"
            ),
            details={
                "session_id": getattr(state, "session_id", ""),
                "key": key,
                "tags": dict(getattr(state, "tags", None) or {}),
                "errors_last_60s": count,
                "limit": limit,
                "window_seconds": ERROR_WINDOW_SECONDS,
            },
        )


class TimeoutDetector(_FireOnceDetector):
    """Catches a session that has simply been running too long.

    The operational blunder here has no single expensive call in it: an agent
    stuck in a slow loop, a run waiting on something that will never arrive, a
    sub-agent nobody is watching. Every other number can look reasonable while
    the wall clock says this run stopped being work an hour ago.

    Any event kind can trip it — a session is running whether it is calling a
    model, running a tool or failing. Two independent clocks, two independent
    limits, each fired once per session:

    ``max_session_seconds`` (``scope="run"``) measures from
    ``state.run_started_at``, which the api resets on every entry of a keyed
    :func:`~runbound.session` block — this is the *run's* clock, so a
    returning caller's tenth request does not inherit the age of their
    first. For the default (unkeyed) session, which has no entry to reset on,
    this is simply the process's own age.

    ``max_session_lifetime_seconds`` (``scope="lifetime"``) measures from
    ``state.started_at``, which is never reset — the session's age since it
    was first created, the old identity-scoped meaning, for a customer who
    wants it back. Off by default.

    Both read the monotonic clock, never the system one, so a clock change
    cannot fake either. The two fire independently: a session that already
    tripped the run wall can still trip the lifetime wall later, and vice
    versa.
    """

    name = "timeout"

    def __init__(self) -> None:
        super().__init__()
        # A second, independent fire-once memo: the base class's ``_fired``
        # is used for the run-scoped wall, this one for the lifetime-scoped
        # wall, so either can trip without silencing the other.
        self._fired_lifetime: set[str] = set()

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """Report a run timeout, else a lifetime timeout, each once per session.

        The run wall is checked first: on an event that could trip both (an
        unkeyed session with both knobs set to the same number, say) the run
        anomaly is the one reported, since it is the wall enabled by default
        and the one every existing caller expects.
        """
        session_id = getattr(state, "session_id", "")
        if session_id not in self._fired:
            anomaly = self._evaluate_scope(
                state, event, config.max_session_seconds, "run_started_at", "run"
            )
            if anomaly is not None:
                self._fired.add(session_id)
                return anomaly
        if session_id not in self._fired_lifetime:
            anomaly = self._evaluate_scope(
                state,
                event,
                config.max_session_lifetime_seconds,
                "started_at",
                "lifetime",
            )
            if anomaly is not None:
                self._fired_lifetime.add(session_id)
                return anomaly
        return None

    def rearm(self, session_id: str) -> None:
        """Forget both clocks' fire-once memos for ``session_id``.

        Overrides :meth:`_FireOnceDetector.rearm` because this detector keeps
        a second memo (``_fired_lifetime``) the base class does not know
        about; a heal must clear both walls, not just the run-scoped one.
        """
        super().rearm(session_id)
        self._fired_lifetime.discard(session_id)

    @staticmethod
    def _evaluate_scope(
        state: SessionState,
        event: Event,
        limit: float | None,
        clock_attr: str,
        scope: str,
    ) -> Anomaly | None:
        """One clock's verdict: ``None`` when off, unreadable, or not yet over."""
        if limit is None:
            return None

        try:
            elapsed = float(event.ts) - float(getattr(state, clock_attr, 0.0))
        except (TypeError, ValueError):
            return None  # fail-open: an unreadable clock is not an incident
        if elapsed <= limit:
            return None

        key = getattr(state, "key", None)
        whose = f" for session {key!r}" if key else ""
        return Anomaly(
            detector=TimeoutDetector.name,
            severity="critical",
            message=(
                f"Session timeout{whose} ({scope}): running for {elapsed:.0f}s, "
                f"limit {limit:.0f}s"
            ),
            details={
                "session_id": getattr(state, "session_id", ""),
                "key": key,
                "tags": dict(getattr(state, "tags", None) or {}),
                "elapsed_s": elapsed,
                "limit": float(limit),
                "scope": scope,
            },
        )


class SpikeDetector:
    """Catches a session whose model calls stop looking like themselves.

    Every other detector needs a number from the user; this one learns one. It
    compares each model call against the median of that session's own recent
    calls, so a service that normally answers in two seconds notices the answer
    that took seventy-four — with nothing configured.

    A ratio is not enough on its own: the call must also be slower (or larger)
    than the median by an absolute amount — ``spike_min_duration_s`` seconds,
    ``spike_min_output_tokens`` tokens — so a session whose calls take
    milliseconds is not paged about every jitter.

    One outlier is only a "warn": models are noisy and a single slow call is
    not an incident. Once ``spike_confirm`` of the trailing
    ``SPIKE_CONFIRM_MAX`` calls look abnormal, the session is confirmed and the
    anomaly turns "critical". The optional per-call caps
    (``max_call_seconds``, ``max_tokens_out_per_call``) skip all of that: they
    are limits somebody stated, so breaching one is critical from call #1 with
    no warmup.

    The median it compares against is *held* for as long as the session's
    trailing calls contain an abnormal one (see :func:`_baseline`): spikes land
    in the session's own history, and a live median would climb under sustained
    abuse until the abuse read as that session's new normal.

    It is not a :class:`_FireOnceDetector`: it has two phases, and each of them
    is reported once per session (``self._warned``, ``self._confirmed``).

    Under ``on_spike="limit"`` a *keyed* session climbs the abuse ladder
    instead of being confirmed: see :meth:`_climb`. The unkeyed default
    session, which guards a whole process and has no key to roll over, keeps
    the two-phase behavior exactly as described above.
    """

    name = "spike"

    def __init__(self) -> None:
        self._warned: set[str] = set()
        self._confirmed: set[str] = set()
        self._closed: set[str] = set()
        self._limits: dict[str, int] = {}
        self._flags: dict[str, deque[bool]] = {}
        #: Consecutive calls, most recent first, that were individually
        #: neither abnormal nor a cap breach — reset to 0 the instant one
        #: is, incremented otherwise, capped at ``spike_window``. This is
        #: what backs ``state.spike_baseline_samples``: ``_flags``
        #: above only remembers the trailing ``SPIKE_CONFIRM_MAX`` (5) calls
        #: (it exists to *confirm* a spike, not to count support), so a
        #: session's up-to-``spike_window`` (50) call history can still hold
        #: an abnormal call from more than 5 calls ago even once ``held`` has
        #: lapsed — the median is robust to that one outlier (it stays a
        #: minority), but the *count* reported as this baseline's support
        #: must not include it.
        self._clean: dict[str, int] = {}

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """Report on the model call that was just recorded.

        Returns ``None`` for anything but an ``llm_call``, when the session's
        call window is empty or unreadable, when the call looks like the
        session's others, when this session has already been reported at
        this severity, and once the ladder has closed it.

        The learned baseline, the ratio-based "abnormal" judgement and
        the abuse ladder are gated on
        ``spike_enabled`` when present — the resolved, code-tightened-by-
        plane value :meth:`~runbound.engine.Engine._effective_config`
        stamps onto every config it hands a detector — falling back to the
        real ``spike_detection`` field (default ``True``) for a bare
        config built and checked directly, never through an ``Engine``
        (most of this file's own unit tests). Gated *after* the per-call
        hard-ceiling check (``_cap_breach``), which stays free and keeps
        firing from call one whatever this flag says. The bookkeeping just
        above that gate (the trailing-abnormality window, the clean-call
        streak) keeps running either way: it costs nothing to maintain,
        and it is what lets a session already mid-flight pick up its own
        history the moment the detector is turned on, rather than starting
        cold.
        """
        if event.kind != "llm_call":
            return None

        session_id = getattr(state, "session_id", "")
        if session_id in self._closed:
            return None

        calls = _recent_calls(state)
        if not calls:
            return None
        history, current = calls[:-1], calls[-1]

        flags = self._flags.setdefault(session_id, deque(maxlen=SPIKE_CONFIRM_MAX))
        # `clean_before` is the streak as it stood *before* this call — the
        # count of calls actually in `history` that were individually clean
        # — which is what a baseline computed from `history` right now may
        # honestly claim as its support.
        clean_before = self._clean.get(session_id, 0)
        baseline, source, armed = _baseline(state, history, config, held=any(flags), clean_samples=clean_before)
        cap = _cap_breach(current, config)
        abnormal, metric, value, median = _measure(history, baseline, current, config, armed)
        flags.append(bool(abnormal or cap is not None))
        self._clean[session_id] = (
            0 if (abnormal or cap is not None) else min(clean_before + 1, getattr(config, "spike_window", 50))
        )

        if cap is not None:
            cap_metric, cap_value, limit = cap
            if not self._confirm(session_id):
                return None
            return _anomaly(
                state,
                config,
                "critical",
                cap_metric,
                cap_value,
                _cap_median(baseline, cap_metric),
                message_kind="cap",
                cap=limit,
                source=source,
                vs_service=_vs_service(state, cap_metric, cap_value),
            )

        spike_enabled = getattr(config, "spike_enabled", None)
        if spike_enabled is None:
            spike_enabled = config.spike_detection
        if not spike_enabled:
            return None

        vs_service = _vs_service(state, metric, value)

        if _ladder_active(state, config):
            return self._climb(
                state, config, session_id, abnormal, metric, value, median, source, vs_service
            )

        if sum(self._flags[session_id]) >= getattr(config, "spike_confirm", 2):
            if not self._confirm(session_id):
                return None
            return _anomaly(
                state,
                config,
                "critical",
                metric,
                value,
                median,
                message_kind="confirmed",
                source=source,
                vs_service=vs_service,
            )

        if abnormal and session_id not in self._warned:
            self._warned.add(session_id)
            return _anomaly(
                state,
                config,
                "warn",
                metric,
                value,
                median,
                message_kind="watching",
                source=source,
                vs_service=vs_service,
            )
        return None

    def rearm(self, session_id: str) -> None:
        """Forget that ``session_id`` was already warned or confirmed.

        Only the two-phase fire-once memos are cleared — ``_warned`` and
        ``_confirmed`` — so a session whose calls are still abnormal reports
        again on its next one. ``_flags`` (the trailing abnormality window)
        is left alone: it describes the *current* condition, not this
        detector's memory of having reported it, and a healed session should
        be judged on the calls it has actually just made, not made to warm up
        from an empty window. ``_limits`` (the ladder's episode counter) and
        ``_closed`` are also left alone: a ladder-closed session is retired by
        :func:`runbound.session`'s own rollover, on its own cooldown, never by
        ``latch_ttl_seconds`` — see :func:`_ladder_active`.
        """
        self._warned.discard(session_id)
        self._confirmed.discard(session_id)

    def _confirm(self, session_id: str) -> bool:
        """Claim the critical phase for a session; False if already claimed.

        Claiming it also closes the warning phase: a session that has already
        been escalated must not fall back to "watching" later.
        """
        if session_id in self._confirmed:
            return False
        self._confirmed.add(session_id)
        self._warned.add(session_id)
        return True

    # --- the abuse ladder (on_spike="limit", keyed sessions) ----------------

    def _climb(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        abnormal: bool,
        metric: str,
        value: float,
        median: float,
        source: str,
        vs_service: float | None,
    ) -> Anomaly | None:
        """Where this call leaves the session on the ladder.

        Which rung, and why, is :func:`runbound.ladder.transition`'s to say;
        this applies what it says — the allowance, the level, the session's
        history entry — and reports the rungs that page a human.

        The one thing left here is arithmetic the machine cannot do without
        the session: spending the allowance, and noticing it has run out,
        which is itself a fresh observation
        (:attr:`~runbound.ladder.Observation.ALLOWANCE_GONE`) for the machine
        to answer in its turn.
        """
        seen = ladder.observation(
            abnormal=abnormal,
            abnormal_recent=sum(self._flags[session_id]),
            noticed=session_id in self._warned,
            config=config,
        )
        with state.lock:
            level = int(getattr(state, "spike_level", 0) or 0)
            move = ladder.transition(level, seen, config)
            spending = Effect.SPEND_ALLOWANCE in move.effects
            if spending and self._spend(state, config) <= 0:
                move = ladder.transition(
                    move.next_level, Observation.ALLOWANCE_GONE, config
                )
            if Effect.SET_ALLOWANCE in move.effects:
                base = _base_allowance(state, config)
                state.spike_allowance = base
                state.spike_allowance_start = base
            if Effect.HEAL in move.effects:
                state.spike_allowance = None
                state.spike_allowance_start = None
            _apply_posture(state, move)
            if not move.moved:
                return None
            state.spike_level = move.next_level
            state.record_ladder_transition(
                level,
                move.next_level,
                move.reason,
                trigger=(metric, value, median, getattr(config, "spike_factor", 10.0), vs_service),
            )
        return self._report(
            state, config, session_id, move, metric, value, median, source, vs_service
        )

    def _spend(self, state: SessionState, config: GuardrailConfig) -> int:
        """Charge one abnormal call to the limit; returns what is left.

        The caller holds ``state.lock``. A session limited before this
        detector instance ever saw it carries no allowance of its own, so it
        starts from its base.
        """
        allowance = state.spike_allowance
        if allowance is None:
            allowance = _base_allowance(state, config)
        allowance -= 1
        state.spike_allowance = allowance
        return allowance

    def _report(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        move: Transition,
        metric: str,
        value: float,
        median: float,
        source: str,
        vs_service: float | None,
    ) -> Anomaly | None:
        """The anomaly a transition owes an on-call human, if any.

        Three rungs speak: the first abnormal call (a notice, once per
        session), the limit, and the close. Spending allowance and healing
        are silent — they are the ladder working, not news.
        """
        if Effect.CLOSE in move.effects:
            return self._close(
                state, config, session_id, move, metric, value, median, source, vs_service
            )
        if Effect.SET_ALLOWANCE in move.effects:
            return self._limit(
                state, config, session_id, move, metric, value, median, source, vs_service
            )
        if move.reason == "first_abnormal":
            self._warned.add(session_id)
            return _anomaly(
                state,
                config,
                "warn",
                metric,
                value,
                median,
                message_kind="watching",
                extra={"level": move.next_level},
                source=source,
                vs_service=vs_service,
            )
        return None

    def _limit(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        move: Transition,
        metric: str,
        value: float,
        median: float,
        source: str,
        vs_service: float | None,
    ) -> Anomaly:
        """Confirmed spiking: say so. Nothing is stopped yet.

        Re-emitted every time a healed session is confirmed again — that is
        new information for whoever is on call — with the allowance back at
        full, so the numbering of the limit (``episode``) is what keeps those
        alerts from deduping into one.
        """
        base = _base_allowance(state, config)
        episode = self._limits[session_id] = self._limits.get(session_id, 0) + 1
        self._warned.add(session_id)
        return _anomaly(
            state,
            config,
            "warn",
            metric,
            value,
            median,
            message_kind="limited",
            extra={
                "level": move.next_level,
                "action": "limit",
                "allowance": base,
                "confirmed": True,
                "episode": episode,
            },
            source=source,
            vs_service=vs_service,
        )

    def _close(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        move: Transition,
        metric: str,
        value: float,
        median: float,
        source: str,
        vs_service: float | None,
    ) -> Anomaly:
        """The allowance ran out: close the session and ask for a rollover.

        ``action="rollover"`` is the contract with the api: the engine latches
        the session on this anomaly, and the next entry for the key retires
        the session, counts the strike and starts the next one on tighter
        terms after the cooldown. The detector says nothing more about this
        session id.
        """
        self._closed.add(session_id)
        self._confirmed.add(session_id)
        base = _base_allowance(state, config)
        with state.lock:
            strikes = int(getattr(state, "strikes", 0) or 0) + 1
        return _anomaly(
            state,
            config,
            "critical",
            metric,
            value,
            median,
            message_kind="rollover",
            extra={
                "level": move.next_level,
                "action": "rollover",
                "allowance": base,
                "strikes": strikes,
                "cooldown_seconds": getattr(config, "spike_cooldown_seconds", 300.0),
                "max_strikes": getattr(config, "spike_max_strikes", 3),
                "episode": self._limits.get(session_id, 1),
            },
            source=source,
            vs_service=vs_service,
        )


def _ladder_active(state: SessionState, config: GuardrailConfig) -> bool:
    """Does the abuse ladder govern this session?

    Only under ``on_spike="limit"``, and only for a keyed session: the ladder
    ends in a rollover of the *key* to a fresh session, and the unkeyed default
    session — which guards a whole process — has no key to roll over, so it
    keeps behaving exactly like ``on_spike="trip"``.
    """
    return getattr(config, "on_spike", "notify") == "limit" and getattr(state, "key", None) is not None


#: What a ladder rung says when it narrows a session, in a customer's words.
_LADDER_REASONS = {
    "confirmed": "limited: a confirmed spike on this session",
    "allowance_spent": "closed: this session spent its limit",
}


def _apply_posture(state: SessionState, move: Transition) -> None:
    """Narrow or restore the posture a ladder transition decided.

    The caller holds ``state.lock``. A state without the methods (a test's
    stand-in) is left alone: the ladder's allowance still works without it.
    Only the ladder's own entry is ever lifted here, never a manual one.
    """
    if Effect.SET_POSTURE in move.effects and move.posture:
        enter = getattr(state, "_enter_posture", None)
        if enter is not None:
            enter(move.posture, _LADDER_REASONS.get(move.reason, move.reason), source="ladder")
    if Effect.CLEAR_POSTURE in move.effects:
        leave = getattr(state, "_exit_posture", None)
        if leave is not None:
            leave(source="ladder")


def _base_allowance(state: SessionState, config: GuardrailConfig) -> int:
    """Abnormal calls a limit starts with: the session's own, else the config's.

    A session created after a rollover carries a tighter
    ``spike_allowance_base``; a first-offender session carries ``None`` and
    gets ``spike_limit_calls``.
    """
    base = getattr(state, "spike_allowance_base", None)
    if base is None:
        return getattr(config, "spike_limit_calls", 5)
    try:
        return max(1, int(base))
    except (TypeError, ValueError):
        return getattr(config, "spike_limit_calls", 5)


def _recent_calls(state: SessionState) -> list[tuple[float, float, float]]:
    """``(duration, output work, cost)`` per recent model call, oldest first.

    Returns ``[]`` — the silent answer — for a session that carries no readable
    call window, so a malformed state makes the detector say nothing rather
    than raise inside the host agent.
    """
    window = getattr(state, "recent_calls", None)
    lock = getattr(state, "lock", None)
    if window is None or lock is None:
        return []
    with lock:
        snapshot = list(window)
    try:
        return [
            (float(call[0]), float(call[1]), float(call[2])) for call in snapshot
        ]
    except (TypeError, ValueError, IndexError, KeyError):
        return []


def _cap_breach(
    current: tuple[float, float, float], config: GuardrailConfig
) -> tuple[str, float, float] | None:
    """``(metric, value, limit)`` for the per-call cap this call broke, if any.

    Three caps, checked in the order a human would read them off an incident:
    the call took too long, produced too much, or cost too much. Each is a
    number the customer stated, so breaching one needs no warmup and no
    baseline — one call is enough.
    """
    duration, output_work, cost = current
    if config.max_call_seconds is not None and duration > config.max_call_seconds:
        return "duration", duration, float(config.max_call_seconds)
    if (
        config.max_tokens_out_per_call is not None
        and output_work > config.max_tokens_out_per_call
    ):
        return "output_tokens", output_work, float(config.max_tokens_out_per_call)
    if (
        config.max_cost_per_call_usd is not None
        and cost > config.max_cost_per_call_usd
    ):
        return "cost_usd", cost, float(config.max_cost_per_call_usd)
    return None


def _cap_median(baseline: tuple[float, float], metric: str) -> float:
    """What this session's normal looks like for the metric a cap named.

    Duration and output have learned medians; cost has none — the session's
    baseline is about the shape of its calls, not their price — so a dollar
    cap reports ``0.0`` and lets the cap and the value tell the story.
    """
    if metric == "duration":
        return baseline[0]
    if metric == "output_tokens":
        return baseline[1]
    return 0.0


def _baseline(
    state: SessionState,
    history: list[tuple[float, float, float]],
    config: GuardrailConfig,
    held: bool,
    clean_samples: int,
) -> tuple[tuple[float, float], str, bool]:
    """The ``(duration, output)`` medians this call is judged against, where
    they came from (``"local"``, ``"restored"`` or ``"peer"``), and
    whether there is enough of a baseline to judge a call abnormal at all.

    ``held`` says the session's trailing calls already contain an abnormal
    one, and then the answer is ``state.spike_baseline``: the snapshot taken
    on the last call before that run began, whatever its source. Holding it
    is what stops a repeat offender's own spikes — which land in the
    session's history like any other call — from dragging the median up
    until they read as normal. A trailing window of nothing but ordinary
    calls refreshes the snapshot and hands judgement back to the live
    medians.

    Only a warmed-up, non-zero history is worth snapshotting as this
    session's own ("local") baseline. Short of that, the fallback: a
    baseline the control plane restored for this exact key — this worker, or
    another one, already learned it — stands in ("restored"); failing that,
    the service-wide median does ("peer"), so a brand-new key is judged from
    its first call instead of served a free warmup nobody else got. Either
    one is written into ``state.spike_baseline`` the moment it is used, so a
    sustained spike train on a session that never got to warm up locally is
    held against that external baseline exactly as a local one would be,
    never against its own drifting, spike-contaminated history.

    A session with neither a warmed local history nor anything external is
    simply not armed: ``armed`` is ``False``, and the caller must not call any
    call abnormal against it (the plane-less behavior).

    ``clean_samples`` is how many of the calls in ``history`` were
    individually clean — not ``len(history)``, and not the ladder's own
    ``held`` window (``SPIKE_CONFIRM_MAX``, 5 calls, there only to confirm a
    spike): a lone abnormal call more than 5 calls back can still sit inside
    ``history`` once ``held`` has lapsed, and the median is robust to it
    (it stays a minority), but ``state.spike_baseline_samples`` — what the
    exit delta reports as this baseline's support — must not count it.
    """
    live = (_median(history, "duration"), _median(history, "output_tokens"))
    if held:
        stored = _stored_baseline(state)
        if stored is None:
            return live, "local", True
        with state.lock:
            source = getattr(state, "spike_baseline_source", None) or "local"
        return stored, source, True
    if len(history) >= getattr(config, "spike_warmup_calls", 4) and max(live) > 0:
        with state.lock:
            state.spike_baseline = live
            state.spike_baseline_source = "local"
            state.spike_baseline_samples = max(0, min(clean_samples, getattr(config, "spike_window", 50)))
        return live, "local", True
    external = _external_baseline(state)
    if external is not None:
        baseline, source = external
        with state.lock:
            state.spike_baseline = baseline
            state.spike_baseline_source = source
            # Not learned by this worker: the exit delta must never re-report
            # someone else's (or the whole service's) numbers as if this
            # worker had measured this key's own calls itself.
            state.spike_baseline_samples = None
        return baseline, source, True
    return live, "local", False


def _stored_baseline(state: SessionState) -> tuple[float, float] | None:
    """The session's baseline snapshot, or ``None`` if there is no usable one.

    Fail-open: a snapshot that is not a readable pair of numbers is treated as
    absent rather than raised over inside the host agent.
    """
    with state.lock:
        stored = getattr(state, "spike_baseline", None)
    try:
        duration, output_work = stored
        return float(duration), float(output_work)
    except (TypeError, ValueError):
        return None


def _external_baseline(state: SessionState) -> tuple[tuple[float, float], str] | None:
    """This key's own restored baseline, else the service's, else ``None``.

    Both are delivered by the control plane and seeded onto ``state`` at
    session entry (:mod:`runbound.api`) — this function only reads what is
    already there, so it is exactly as fail-open as any other state read.
    ``None`` when the plane never delivered either:
    a plane-less process, or a service the plane has not answered for yet.
    """
    own = _stored_baseline(state)
    with state.lock:
        own_source = getattr(state, "spike_baseline_source", None)
        service = getattr(state, "service_baseline", None)
    if own is not None and own_source in ("restored", "peer"):
        return own, own_source
    try:
        duration, output_work = float(service[0]), float(service[1])
    except (TypeError, ValueError, IndexError):
        return None
    if max(duration, output_work) <= 0:
        return None
    return (duration, output_work), "peer"


def _vs_service(state: SessionState, metric: str, value: float) -> float | None:
    """This call's ratio to the service-wide median for ``metric``.

    Reported alongside whatever baseline the call was actually judged
    against, so the dashboard can show both "N× its own normal" and "M× this
    service's" together. ``None`` without a plane-delivered service
    baseline, or when that metric's median is not (yet) above zero — the
    same "no opinion near zero" rule the session's own baseline follows.
    """
    with state.lock:
        service = getattr(state, "service_baseline", None)
    if service is None:
        return None
    index = 0 if metric == "duration" else 1
    try:
        median = float(service[index])
    except (TypeError, ValueError, IndexError):
        return None
    if median <= 0:
        return None
    return value / median


def _measure(
    history: list[tuple[float, float, float]],
    baseline: tuple[float, float],
    current: tuple[float, float, float],
    config: GuardrailConfig,
    armed: bool,
) -> tuple[bool, str, float, float]:
    """``(abnormal, metric, value, median)`` for the call against its baseline.

    ``armed`` — the caller's to decide (:func:`_baseline`) — is ``True`` once
    there is *something* to judge a call against: this session's own history
    reached ``spike_warmup_calls``, or a plane-delivered baseline (this key's
    own "restored", or the service's "peer") stands in for a session
    that has not warmed up locally at all. A session with neither is simply
    not armed, and this never calls anything abnormal against a baseline that
    is not really known yet — unchanged without a
    plane. A metric whose median is not above zero has no opinion about that
    metric regardless of ``armed``: a session with no measured durations has
    nothing to say about durations. The metric reported is the abnormal one,
    or, when neither is, the one that came closest.

    A call is abnormal only if it is *both* more than ``spike_factor`` times the
    median *and* higher than it by more than that metric's floor. Ratios alone
    are meaningless near zero: twelve times a 0.1s lookup is 1.2s, which no
    on-call human wants to hear about.
    """
    best = (False, 0.0, "duration", current[0], 0.0)
    for index, metric in enumerate(("duration", "output_tokens")):
        value = current[index]
        median = baseline[index] if armed else 0.0
        if median <= 0:
            continue
        abnormal = (
            value > getattr(config, "spike_factor", 10.0) * median
            and value - median > _floor(metric, config)
        )
        candidate = (abnormal, value / median, metric, value, median)
        if candidate[:2] > best[:2]:
            best = candidate
    return best[0], best[2], best[3], best[4]


def _floor(metric: str, config: GuardrailConfig) -> float:
    """The absolute rise this metric must clear before its ratio counts."""
    if metric == "duration":
        return float(getattr(config, "spike_min_duration_s", 2.0))
    return float(getattr(config, "spike_min_output_tokens", 500))


def _median(history: list[tuple[float, float, float]], metric: str) -> float:
    """The history's median for one metric; 0.0 when there is no history."""
    if not history:
        return 0.0
    index = 0 if metric == "duration" else 1
    return float(statistics.median([call[index] for call in history]))


def _anomaly(
    state: SessionState,
    config: GuardrailConfig,
    severity: str,
    metric: str,
    value: float,
    median: float,
    message_kind: str,
    cap: float | None = None,
    extra: dict | None = None,
    source: str | None = None,
    vs_service: float | None = None,
) -> Anomaly:
    """Build the spike anomaly, identity and numbers included.

    ``extra`` carries the ladder's own fields (``level``, ``action``,
    ``allowance``, ``strikes``...) and is merged last, so a rung that reports
    a confirmed session at "warn" severity — the level-2 limit — can say so.

    ``source`` is ``"local"``, ``"restored"`` or ``"peer"`` — where
    ``median`` above came from — and ``vs_service`` this call's ratio to the
    service-wide median for ``metric``, ``None`` when no service baseline is
    known. Both ride ``details`` so a dashboard can render "N× its own
    normal, M× this service's" from one anomaly without a second lookup.
    """
    key = getattr(state, "key", None)
    details = {
        "session_id": getattr(state, "session_id", ""),
        "key": key,
        "tags": dict(getattr(state, "tags", None) or {}),
        "metric": metric,
        "value": value,
        "median": median,
        "factor": getattr(config, "spike_factor", 10.0),
        "confirmed": severity == "critical",
    }
    if source is not None:
        details["baseline_source"] = source
    if vs_service is not None:
        details["vs_service"] = vs_service
    if cap is not None:
        details["cap"] = cap
    if extra:
        details.update(extra)
    return Anomaly(
        detector=SpikeDetector.name,
        severity=severity,
        message=_message(message_kind, key, details, metric, value, median, cap),
        details=details,
    )


def _message(
    kind: str,
    key: str | None,
    details: dict,
    metric: str,
    value: float,
    median: float,
    cap: float | None,
) -> str:
    """One line an on-call human can act on: which session, what changed."""
    label = f"session {key!r}" if key else f"session {details.get('session_id')!r}"
    observed = _amount(metric, value)
    if kind == "cap":
        limit = _amount(metric, cap, bare=True)
        return f"Per-call cap exceeded for {label}: {observed}, cap {limit}"
    if kind == "rollover":
        return (
            f"Sustained spiking for {label}: {details['allowance']} abnormal calls "
            f"past the limit; closing this session "
            f"(cooldown {details['cooldown_seconds']:.0f}s; "
            f"strike {details['strikes']} of {details['max_strikes']})"
        )
    normal = f"this session's normal is {_amount(metric, median, bare=True)}"
    normal += _ratio_suffix(value, median, details.get("vs_service"))
    if kind == "limited":
        return (
            f"Spiking {label} limited: {observed}, {normal}; "
            f"{details['allowance']} more abnormal call{'' if details['allowance'] == 1 else 's'} close{'s' if details['allowance'] == 1 else ''} this session"
        )
    if kind == "confirmed":
        return f"Spike confirmed for {label}: {observed}, {normal}"
    return f"Unusual call for {label} (watching): {observed}, {normal}"


def _ratio_suffix(value: float, median: float, vs_service: float | None) -> str:
    """" (Nx its own normal, Mx this service's)" — or "" without a service
    baseline. Never replaces the existing "this session's normal is
    ..." phrase; only ever appended to it, so a message that already
    named that number keeps meaning exactly what it always did.
    """
    if vs_service is None or median <= 0:
        return ""
    own_ratio = value / median
    return f" ({own_ratio:.1f}x its own normal, {vs_service:.1f}x this service's)"


def _amount(metric: str, value: float, bare: bool = False) -> str:
    """Render one measurement, with or without the "call ..." lead-in."""
    if metric == "duration":
        return f"{value:.1f}s" if bare else f"call took {value:.1f}s"
    if metric == "cost_usd":
        return f"${value:.4f}" if bare else f"call cost ${value:.4f}"
    return f"{value:.0f} output tokens" if bare else f"call produced {value:.0f} output tokens"


DEFAULT_DETECTORS = [
    LoopDetector,
    BudgetDetector,
    VelocityDetector,
    StepDetector,
    EventsDetector,
    SpikeDetector,
    ErrorStormDetector,
    TimeoutDetector,
]
