"""Mutable per-session state — the single place counters live."""

import threading
import time
from collections import deque

from .events import Anomaly, Event

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


class SessionState:
    """Counters and sliding windows for one agent run.

    Agents run tools and model calls from threads, so every mutation happens
    under ``self.lock`` (reentrant, so callers may hold it across a
    ``record()`` when they need a consistent multi-attribute read).

    ``key`` and ``tags`` identify a keyed session (one chatbot end-user, say)
    and are ``None``/empty for the default session that guards a whole process.

    ``tool_calls`` counts attempts per tool name — the per-session number an
    action policy's ``max_calls`` is measured against. Only executed calls are
    counted there: what the model *asked* for is not what the agent did.

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
    the two numbers a retry storm shows up in.

    ``tripped_by`` is the latch: the anomaly that stopped this session, kept so
    that a host which catches the exception and keeps serving is stopped again
    on every later event instead of once, with ``tripped_at`` the monotonic
    time it was set — what ``latch_ttl_seconds`` measures the latch's age
    against. Both are written by the engine under ``self.lock``, and are
    otherwise cleared only by discarding the session.
    ``latch_ttl_override`` is this session's own expiry for that latch, which
    beats ``latch_ttl_seconds`` when set: it is how a rollover cooldown is
    served.

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

    ``spend_offset_usd`` and ``tokens_offset`` are what the *rest of the fleet*
    has already spent under this session's key, handed over by the control
    plane when the session was entered and added to the local counters by the
    budget detector — so a $5 budget is $5 across every worker, not $5 each.
    They stay zero without a plane, which is exactly the single-process
    behavior. ``fleet_generation`` is the plane's version of this key's state,
    kept so a stale answer can be recognized and ignored. All three are written
    by the api under ``self.lock``, and never by a detector.

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
        self.spike_allowance: int | None = None
        self.spike_allowance_base = spike_allowance_base
        self.spike_allowance_start: int | None = None
        self.strikes = strikes
        self.latch_ttl_override = latch_ttl_override
        self.spike_trigger: dict | None = spike_trigger
        self.spike_limited_at: float | None = spike_limited_at
        self.spike_closed_at: float | None = spike_closed_at
        self.healed_times: int = healed_times
        self.ladder_history: deque[tuple[int, int, float, str]] = deque(
            ladder_history or (), maxlen=10
        )
        self.step_count = 0
        self.tool_calls: dict[str, int] = {}
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self.estimated_cost_usd = 0.0
        self.spend_offset_usd = 0.0
        self.tokens_offset = 0
        self.fleet_generation: int | None = None
        self.started_at = time.monotonic()
        self.recent_hashes: deque[str] = deque(maxlen=loop_window)
        self.recent_calls: deque[tuple[float, int, float]] = deque(maxlen=spike_window)
        self.token_timestamps: deque[tuple[float, int]] = deque()
        self.error_timestamps: deque[float] = deque()
        self.consecutive_errors = 0
        self.tripped_by: Anomaly | None = None
        self.tripped_at: float | None = None
        self.lock = threading.RLock()
        self._steps = 0

    def next_step(self) -> int:
        """Allocate this session's next 1-based step number, atomically."""
        with self.lock:
            self._steps += 1
            return self._steps

    def record_ladder_transition(
        self,
        level_from: int,
        level_to: int,
        reason: str,
        trigger: tuple[str, float, float, float] | None = None,
    ) -> None:
        """Append one abuse-ladder transition and keep its derived facts in sync.

        Called by the spike detector and the api at the moment of each
        transition — ``reason`` one of ``"first_abnormal"``, ``"confirmed"``,
        ``"healed"``, ``"allowance_spent"``, ``"rollover"``, ``"blocked"`` or
        ``"cleared"`` — so a customer-facing ``why`` never has to be
        reconstructed after the fact by replaying events.

        ``healed_times`` increments on a ``"healed"`` transition;
        ``spike_limited_at``/``spike_closed_at`` are stamped when ``level_to``
        is 2 or 3. ``trigger`` — ``(metric, value, median, factor)`` — is only
        kept when this transition actually raised the level: a call that only
        spends allowance or heals the session is not what a customer asking
        "why" wants pointed to.
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
                metric, value, median, factor = trigger
                self.spike_trigger = {
                    "metric": metric,
                    "value": value,
                    "median": median,
                    "factor": factor,
                    "at": now,
                }

    def record(self, event: Event) -> None:
        """Fold one event into the session counters, atomically.

        ``step_count`` tracks the highest step seen rather than the number of
        events, so several events on one step (an llm_call plus its tool_call)
        do not inflate it. Only ``tool_call`` and ``tool_request`` events with a
        hash contribute to the loop window — and only when they are not
        ``loop_exempt`` (``@runbound.tool(repeatable=True)``, or a name in
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
        """
        tokens = event.tokens_in + event.tokens_out
        with self.lock:
            self.step_count = max(self.step_count, event.step)
            self.total_tokens += tokens
            self.total_cost_usd += event.cost_usd
            if event.priced == "estimated":
                self.estimated_cost_usd += event.cost_usd
            if event.kind in HASHED_KINDS and event.args_hash and not event.loop_exempt:
                self.recent_hashes.append(event.args_hash)
            if event.kind in ERROR_KINDS:
                self.consecutive_errors += 1
                self.error_timestamps.append(event.ts)
                self._prune_error_window(event.ts)
            elif event.kind == "llm_call":
                self.consecutive_errors = 0
            if event.kind == "tool_call" and event.tool_name:
                self.tool_calls[event.tool_name] = (
                    self.tool_calls.get(event.tool_name, 0) + 1
                )
            if event.kind == "llm_call":
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
