"""Detectors — pure verdicts about a session, one detector per failure mode.

A detector answers a single question about the session as it stands after an
event has been recorded, and returns an :class:`Anomaly` or ``None``. It does
no I/O, sends no alerts and decides no policy: what happens to an anomaly is
the engine's business.

Three pieces of state are deliberate exceptions to that purity: each detector
instance remembers which sessions it has already fired for (a sustained
overrun must not re-alert on every following event), :class:`SpikeDetector`
additionally tracks each session's trailing abnormal calls and its place on
the abuse ladder, and
:class:`VelocityDetector` prunes expired entries from the session's token
window so a long-running session's memory stays bounded.

Detectors are constructed once per session-scoped engine and called from
whatever thread the agent happens to be on, so every multi-value read of
``SessionState`` happens under ``state.lock``.
"""

import statistics
from collections import deque

from .config import SPIKE_CONFIRM_MAX, GuardrailConfig
from .events import Anomaly, Event
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
        """Forget that ``session_id`` already fired (T137).

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
    """Catches an agent repeating the same tool call with the same arguments.

    Only ``tool_call`` and ``tool_request`` events can trip it, and only via
    their ``args_hash``: an event without a hash, or a model call, is never a
    loop by itself. A model that keeps *asking* for the same tool is looping
    whether or not the developer dispatches the request, and because request
    hashes live in their own ``"req:"`` namespace the two streams are counted
    separately — three requests and three executions are two threes, not a six.

    It fires once per session like every other detector, except under the
    ``on_loop`` policies that react to each repeat ("throttle", "escalate"):
    those need a verdict on every event, so the fire-once memo is bypassed.
    """

    name = "loop"

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """Report the loop, once per session or on every repeat.

        Which one depends on ``config.on_loop``: the repeat-driven policies
        need one anomaly per repeating event, the others one per session.
        """
        if config.on_loop in REPEATING_LOOP_POLICIES:
            return self._evaluate(state, event, config)
        return super().check(state, event, config)

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        if event.kind not in HASHED_KINDS or not event.args_hash:
            return None

        with state.lock:
            count = state.recent_hashes.count(event.args_hash)

        if count < config.loop_threshold:
            return None

        tool = event.tool_name or "<unknown>"
        what = "model requested tool" if event.kind == "tool_request" else "tool"
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

    def _evaluate(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        if config.budget_usd is None and config.max_total_tokens is None:
            return None

        with state.lock:
            spend_offset = state.spend_offset_usd
            tokens_offset = state.tokens_offset
            total_cost = state.total_cost_usd + spend_offset
            total_tokens = state.total_tokens + tokens_offset
            estimated_cost = state.estimated_cost_usd

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

        if config.budget_usd is not None and total_cost > config.budget_usd:
            return Anomaly(
                detector=self.name,
                severity="critical",
                message=(
                    f"Budget exceeded: ${total_cost:.4f} spent, "
                    f"limit ${config.budget_usd:.4f}"
                ),
                details={**details, "limit_hit": "budget_usd"},
            )

        if config.max_total_tokens is not None and total_tokens > config.max_total_tokens:
            return Anomaly(
                detector=self.name,
                severity="critical",
                message=(
                    f"Token budget exceeded: {total_tokens} tokens used, "
                    f"limit {config.max_total_tokens}"
                ),
                details={**details, "limit_hit": "max_total_tokens"},
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

    An agent step is a model turn (T134): ``max_steps`` is measured against
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
    failures alike — which is what ``max_steps`` counted before T134
    redefined "step" to mean a model turn. Use this when what you actually
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
    returning chatbot user's tenth message does not inherit the age of their
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
        """Forget both clocks' fire-once memos for ``session_id`` (T137).

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
    calls, so a chatbot that normally answers in two seconds notices the answer
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

    def check(
        self, state: SessionState, event: Event, config: GuardrailConfig
    ) -> Anomaly | None:
        """Report on the model call that was just recorded.

        Returns ``None`` for anything but an ``llm_call``, when spike detection
        is off, when the session's call window is empty or unreadable, when the
        call looks like the session's others, when this session has already
        been reported at this severity, and once the ladder has closed it.
        """
        if event.kind != "llm_call" or not config.spike_detection:
            return None

        session_id = getattr(state, "session_id", "")
        if session_id in self._closed:
            return None

        calls = _recent_calls(state)
        if not calls:
            return None
        history, current = calls[:-1], calls[-1]

        flags = self._flags.setdefault(session_id, deque(maxlen=SPIKE_CONFIRM_MAX))
        baseline = _baseline(state, history, config, held=any(flags))
        cap = _cap_breach(current, config)
        abnormal, metric, value, median = _measure(history, baseline, current, config)
        flags.append(bool(abnormal or cap is not None))

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
            )

        if _ladder_active(state, config):
            return self._climb(state, config, session_id, abnormal, metric, value, median)

        if sum(self._flags[session_id]) >= config.spike_confirm:
            if not self._confirm(session_id):
                return None
            return _anomaly(
                state, config, "critical", metric, value, median, message_kind="confirmed"
            )

        if abnormal and session_id not in self._warned:
            self._warned.add(session_id)
            return _anomaly(
                state, config, "warn", metric, value, median, message_kind="watching"
            )
        return None

    def rearm(self, session_id: str) -> None:
        """Forget that ``session_id`` was already warned or confirmed (T137).

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
    ) -> Anomaly | None:
        """Where this call leaves the session on the ladder.

        Below the limit the rungs are the familiar ones — the first abnormal
        call is a notice (level 1, once per session), a confirmed spike is a
        *limit* (level 2) that stops nothing. From there
        :meth:`_at_limit` takes over.
        """
        confirmed = sum(self._flags[session_id]) >= config.spike_confirm
        with state.lock:
            level = int(getattr(state, "spike_level", 0) or 0)
        if level >= 2:
            return self._at_limit(
                state, config, session_id, abnormal, confirmed, metric, value, median
            )
        if confirmed:
            return self._limit(state, config, session_id, metric, value, median)
        if abnormal and session_id not in self._warned:
            self._warned.add(session_id)
            with state.lock:
                state.spike_level = 1
            state.record_ladder_transition(
                0, 1, "first_abnormal", trigger=(metric, value, median, config.spike_factor)
            )
            return _anomaly(
                state,
                config,
                "warn",
                metric,
                value,
                median,
                message_kind="watching",
                extra={"level": 1},
            )
        return None

    def _limit(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        metric: str,
        value: float,
        median: float,
    ) -> Anomaly:
        """Confirmed spiking: limit the session. Nothing is stopped yet.

        Re-emitted every time a healed session is confirmed again — that is
        new information for whoever is on call — with the allowance back at
        full, so the numbering of the limit (``episode``) is what keeps those
        alerts from deduping into one.
        """
        base = _base_allowance(state, config)
        episode = self._limits[session_id] = self._limits.get(session_id, 0) + 1
        self._warned.add(session_id)
        with state.lock:
            level_before = int(getattr(state, "spike_level", 0) or 0)
            state.spike_level = 2
            state.spike_allowance = base
            state.spike_allowance_start = base
            state.record_ladder_transition(
                level_before,
                2,
                "confirmed",
                trigger=(metric, value, median, config.spike_factor),
            )
        return _anomaly(
            state,
            config,
            "warn",
            metric,
            value,
            median,
            message_kind="limited",
            extra={
                "level": 2,
                "action": "limit",
                "allowance": base,
                "confirmed": True,
                "episode": episode,
            },
        )

    def _at_limit(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        abnormal: bool,
        confirmed: bool,
        metric: str,
        value: float,
        median: float,
    ) -> Anomaly | None:
        """A limited session's next call: burn, heal, or close.

        Every further abnormal call costs one of the session's allowance and
        is otherwise silent. A normal call whose trailing window no longer
        confirms a spike heals the session back to watching, allowance
        forgotten. An allowance spent to zero closes the session.
        """
        with state.lock:
            allowance = state.spike_allowance
            if allowance is None:
                allowance = _base_allowance(state, config)
            if not abnormal:
                if not confirmed:
                    state.spike_level = 1
                    state.spike_allowance = None
                    state.spike_allowance_start = None
                    state.record_ladder_transition(2, 1, "healed")
                return None
            allowance -= 1
            state.spike_allowance = allowance
            if allowance > 0:
                state.record_ladder_transition(2, 2, "allowance_spent")
                return None
            state.spike_level = 3
            state.record_ladder_transition(
                2,
                3,
                "allowance_spent",
                trigger=(metric, value, median, config.spike_factor),
            )
        return self._close(state, config, session_id, metric, value, median)

    def _close(
        self,
        state: SessionState,
        config: GuardrailConfig,
        session_id: str,
        metric: str,
        value: float,
        median: float,
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
                "level": 3,
                "action": "rollover",
                "allowance": base,
                "strikes": strikes,
                "cooldown_seconds": config.spike_cooldown_seconds,
                "max_strikes": config.spike_max_strikes,
                "episode": self._limits.get(session_id, 1),
            },
        )


def _ladder_active(state: SessionState, config: GuardrailConfig) -> bool:
    """Does the abuse ladder govern this session?

    Only under ``on_spike="limit"``, and only for a keyed session: the ladder
    ends in a rollover of the *key* to a fresh session, and the unkeyed default
    session — which guards a whole process — has no key to roll over, so it
    keeps behaving exactly like ``on_spike="trip"``.
    """
    return config.on_spike == "limit" and getattr(state, "key", None) is not None


def _base_allowance(state: SessionState, config: GuardrailConfig) -> int:
    """Abnormal calls a limit starts with: the session's own, else the config's.

    A session created after a rollover carries a tighter
    ``spike_allowance_base``; a first-offender session carries ``None`` and
    gets ``spike_limit_calls``.
    """
    base = getattr(state, "spike_allowance_base", None)
    if base is None:
        return config.spike_limit_calls
    try:
        return max(1, int(base))
    except (TypeError, ValueError):
        return config.spike_limit_calls


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
) -> tuple[float, float]:
    """The ``(duration, output)`` medians this call is judged against.

    ``held`` says the session's trailing calls already contain an abnormal one,
    and then the answer is ``state.spike_baseline``: the snapshot taken on the
    last call before that run began. Holding it is what stops an abuser's own
    spikes — which land in the session's history like any other call — from
    dragging the median up until they read as normal. A trailing window of
    nothing but ordinary calls refreshes the snapshot and hands judgement back
    to the live medians.

    Only a warmed-up, non-zero history is worth snapshotting, so a session
    flagged before it ever warmed up (a per-call cap breached on call #1) has
    nothing to hold and falls back to the live medians — as does one whose
    snapshot is unreadable.
    """
    live = (_median(history, "duration"), _median(history, "output_tokens"))
    if held:
        stored = _stored_baseline(state)
        return live if stored is None else stored
    if len(history) >= config.spike_warmup_calls and max(live) > 0:
        with state.lock:
            state.spike_baseline = live
    return live


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


def _measure(
    history: list[tuple[float, float, float]],
    baseline: tuple[float, float],
    current: tuple[float, float, float],
    config: GuardrailConfig,
) -> tuple[bool, str, float, float]:
    """``(abnormal, metric, value, median)`` for the call against its baseline.

    A baseline is armed only once the history is at least ``spike_warmup_calls``
    long and that metric's median is above zero — a session with no measured
    durations has no opinion about durations. The metric reported is the
    abnormal one, or, when neither is, the one that came closest.

    A call is abnormal only if it is *both* more than ``spike_factor`` times the
    median *and* higher than it by more than that metric's floor. Ratios alone
    are meaningless near zero: twelve times a 0.1s lookup is 1.2s, which no
    on-call human wants to hear about.
    """
    armed = len(history) >= config.spike_warmup_calls
    best = (False, 0.0, "duration", current[0], 0.0)
    for index, metric in enumerate(("duration", "output_tokens")):
        value = current[index]
        median = baseline[index] if armed else 0.0
        if median <= 0:
            continue
        abnormal = (
            value > config.spike_factor * median
            and value - median > _floor(metric, config)
        )
        candidate = (abnormal, value / median, metric, value, median)
        if candidate[:2] > best[:2]:
            best = candidate
    return best[0], best[2], best[3], best[4]


def _floor(metric: str, config: GuardrailConfig) -> float:
    """The absolute rise this metric must clear before its ratio counts."""
    if metric == "duration":
        return float(config.spike_min_duration_s)
    return float(config.spike_min_output_tokens)


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
) -> Anomaly:
    """Build the spike anomaly, identity and numbers included.

    ``extra`` carries the ladder's own fields (``level``, ``action``,
    ``allowance``, ``strikes``...) and is merged last, so a rung that reports
    a confirmed session at "warn" severity — the level-2 limit — can say so.
    """
    key = getattr(state, "key", None)
    details = {
        "session_id": getattr(state, "session_id", ""),
        "key": key,
        "tags": dict(getattr(state, "tags", None) or {}),
        "metric": metric,
        "value": value,
        "median": median,
        "factor": config.spike_factor,
        "confirmed": severity == "critical",
    }
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
    if kind == "limited":
        return (
            f"Spiking {label} limited: {observed}, {normal}; "
            f"{details['allowance']} more abnormal call{'' if details['allowance'] == 1 else 's'} close{'s' if details['allowance'] == 1 else ''} this session"
        )
    if kind == "confirmed":
        return f"Spike confirmed for {label}: {observed}, {normal}"
    return f"Unusual call for {label} (watching): {observed}, {normal}"


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
