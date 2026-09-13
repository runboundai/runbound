"""The engine — the only place where an event turns into a consequence.

One pass per event: record it, ask every detector what it thinks, notify, then
act. Detection lives in :mod:`runbound.detectors`; the engine owns none of
it, it only sequences it. Delivery is not the engine's concern at all any
more (Wave 31) — the SDK detects, stops, refuses and reports; the control
plane routes and delivers. What "reports" means here is
:meth:`Engine._notify_anomaly`: every observer (telemetry export among them)
is told what happened, and that is how the plane learns of an anomaly.

Fail-open is enforced here, not hoped for: a detector that raises is logged and
skipped, an observer that raises is logged and skipped, a user callback that
raises is logged and swallowed. The single exception that leaves this module on
purpose is :class:`GuardrailTripped` under ``on_anomaly="raise"`` — that is the
product doing its job.
"""

import asyncio
import contextvars
import logging
import time
from collections.abc import Sequence

from .circuit import CircuitBreaker, is_provider_failure
from .config import GuardrailConfig
from .detectors import DEFAULT_DETECTORS, BudgetDetector, LoopDetector, SpikeDetector
from .events import PRIORITY, Anomaly, Event
from .exceptions import CircuitOpen, GuardrailTripped, PolicyViolation
from .policy import ToolCall, ToolPolicy, Violation, coerce, evaluate, merge
from .pricing import price_for
from .shared import LocalState
from .state import SessionState

_LOG = logging.getLogger("runbound")

LOOP_DETECTOR = LoopDetector.name
SPIKE_DETECTOR = SpikeDetector.name
BUDGET_DETECTOR = BudgetDetector.name

#: The detector name policy anomalies carry. Not a detector: nothing is
#: inferred, the customer stated the rule and we enforced it.
POLICY_DETECTOR = "policy"

#: The throttle delay this task's most recent loop anomaly asked for, under a
#: running event loop where `time.sleep` cannot be used without blocking the
#: whole worker. Scoped per asyncio task (a `contextvars.ContextVar` copies
#: forward into every task spawned from where it's read, but a `.set()` inside
#: one task is invisible to its siblings) — with one exception: a task that
#: *sets* the value and then spawns a child task before anything reads it
#: hands the child a context copy carrying that same pending delay, because the
#: copy is taken at spawn time, after the `.set()`. Stored as
#: ``(delay, set_at_monotonic)`` rather than a bare float so `take_pending_delay`
#: can recognize and drop a copy that stale: nothing owed it ever asked for it,
#: so it is not this child's to pay. `take_pending_delay` reads and clears it;
#: nothing else does.
_PENDING_DELAY: contextvars.ContextVar[tuple[float, float] | None] = contextvars.ContextVar(
    "runbound_pending_delay", default=None
)

#: A pending delay older than this was never collected by the task it was
#: stashed for (most often a sync ``@runbound.tool`` called directly on the
#: event-loop thread, which cannot await it) and leaked into a child task's
#: copied context instead. Dropping it after a second protects an unrelated
#: later call from sleeping a delay that was never its own.
_PENDING_DELAY_MAX_AGE_S = 1.0


def take_pending_delay() -> float:
    """Return, and clear, the throttle delay stashed for the current task.

    ``0.0`` when nothing is pending, or when what is pending is older than
    :data:`_PENDING_DELAY_MAX_AGE_S` — a leak from a task that set it and
    never came back to collect it, not a delay owed to whoever asks now. An
    async caller (the ``@runbound.tool`` wrapper, or a wrapped client's
    async request loop) calls this right after the event that might have set
    it and awaits ``asyncio.sleep`` on the result — the async equivalent of
    the synchronous path's ``time.sleep``, without blocking any other task on
    this worker.
    """
    pending = _PENDING_DELAY.get()
    if pending is None:
        return 0.0
    _PENDING_DELAY.set(None)
    delay, set_at = pending
    if _monotonic() - set_at > _PENDING_DELAY_MAX_AGE_S:
        return 0.0
    return delay

#: The detector name an open provider circuit carries. Also not a detector: it
#: is counted failures, not an inference about the agent.
CIRCUIT_DETECTOR = "circuit"

#: The detector name a refused call carries when the in-flight cap is full.
#: Not a detector either: the customer stated a number and we enforced it,
#: before the call went out.
INFLIGHT_DETECTOR = "inflight"

#: The detector name an org-wide halt carries. Not a detector either: the
#: control plane said stop, and this worker stopped.
HALT_DETECTOR = "halt"

#: The detector name a plane-loss refusal carries (``on_plane_loss="refuse"``
#: — the plane could not be asked at all). Not a detector either: it is the
#: outage itself, not an inference about the agent.
PLANE_DETECTOR = "plane"

#: An error string kept on an event is truncated to this many characters.
ERROR_MAX_CHARS = 500


def provider_host(provider: str) -> str:
    """The endpoint half of a provider label, or ``"default"``.

    Labels are ``"{shape}@{host}"`` — ``"openai@localhost:11434"`` — so that
    two endpoints of one shape are two circuits. Anomaly details carry the
    host on its own as well, because "which box" is the first question an
    on-call asks and splitting a string is not their job.
    """
    try:
        return provider.partition("@")[2] or "default"
    except Exception:
        return "default"


class Engine:
    """Applies one configuration's detectors and reactions to a session.

    ``detectors`` are injectable: anything with ``check(state, event,
    config)`` plugs in without the engine knowing what it is. Detector
    instances are stateful (they fire once per session), so each engine
    gets its own.

    ``circuit`` is the provider circuit breaker, and is deliberately *not*
    per session: a provider is down for the whole process, not for one
    end-user, so every session's failures count towards the same breaker.

    ``observers`` are told what happened *after* it has happened — anything
    with ``on_event(session, event)`` and ``on_anomaly(session, anomaly,
    reacted)`` — and can never change it: one that raises is logged and
    skipped. ``shared`` is what the rest of the fleet knows
    (:class:`~runbound.shared.SharedState`); the default
    :class:`~runbound.shared.LocalState` knows nothing, which is the
    single-process behavior.
    """

    def __init__(
        self,
        config: GuardrailConfig,
        detectors: Sequence | None = None,
        observers: Sequence | None = None,
        shared=None,
    ) -> None:
        self.config = config
        self.detectors = (
            list(detectors) if detectors is not None else [cls() for cls in DEFAULT_DETECTORS]
        )
        self.observers = list(observers) if observers is not None else []
        self.shared = shared if shared is not None else LocalState()
        self.circuit = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            window_seconds=config.circuit_window_seconds,
            cooldown_seconds=config.circuit_cooldown_seconds,
            now=_monotonic,
        )
        self._alerted: set[tuple] = set()
        self._reported: set[tuple] = set()
        # A detector name events.PRIORITY has never heard of is warned about
        # exactly once per engine, not once per event it co-fires in.
        self._unranked_warned: set[str] = set()
        # A process with no plane must not pay for the fleet seam: reading a
        # circuit's state before every successful call is only worth it when
        # somebody is listening for the transition.
        self._reports_circuits = bool(getattr(self.shared, "fleet", True))
        self._merged_key: tuple | None = None
        self._merged: ToolPolicy | None = None
        # T136: models the admission budget estimate skipped for lack of a
        # price, warned about once per model per Engine — the same "once per
        # model, not process" scoping notify_door's alert-dedup set already
        # gives on_unpriced_model="refuse" (see _alert), kept separate here
        # because this is a plain log line, never an anomaly: no call was
        # refused, so there is nothing to alert observers about.
        self._admission_unpriced_warned: set[str] = set()

    def process(self, session: SessionState, event: Event) -> None:
        """Record ``event`` into ``session``, then detect, alert and react.

        The event is recorded *before* detection, so a detector sees the world
        including the action that is about to happen — the third identical tool
        call trips the loop detector before the tool runs.

        Alerts for every anomaly go out before any reaction, so an escalation
        is never lost to the exception that stops the agent, and each
        (session, detector) pair is alerted only once however often it fires.
        Only the most severe anomaly ("critical" over "warn") drives the
        reaction — a loop anomaly under a configured ``on_loop`` policy is
        reacted to by that policy instead of the global one, and a spike still
        at the warning stage is logged and alerted but never stops the agent.

        A session that has already been stopped by a critical anomaly stays
        stopped: the stored anomaly's reaction is re-applied here and detection
        is skipped entirely, so a host that catches the exception and keeps
        serving cannot spend its way past the wall. Re-applications never
        alert — the on-call was paged when the session first tripped. Under
        ``latch_ttl_seconds`` that wall has an expiry: an event arriving after
        it heals the session and is detected on normally.

        Raises :class:`GuardrailTripped` when ``on_anomaly="raise"`` and
        something tripped, or when a loop policy breaks the run; nothing else
        escapes.
        """
        session.record(event)
        self._notify_event(session, event)

        latched = _latched(session, self.config, self.detectors)
        if latched is not None:
            self._reapply(session, latched)
            return

        anomalies = self._detect(session, event)
        if not anomalies:
            return

        for anomaly in anomalies:
            self._alert(anomaly, session)

        worst = self._winner(anomalies)
        if worst.detector == LOOP_DETECTOR and self.config.on_loop is not None:
            # A loop policy must never shadow a co-firing non-loop critical:
            # fire-once detectors get no second chance to stop the run.
            others = [a for a in anomalies if a.detector != LOOP_DETECTOR]
            if others and self._winner(others).severity == "critical":
                self._react(self._winner(others), session)
                return
            self._react_to_loop(worst, session)
            return
        if worst.detector == SPIKE_DETECTOR:
            if worst.severity != "critical":
                # A heightened watch is a notice, never a stop: one odd model
                # call must not take down a chatbot, whatever on_anomaly says.
                # That holds for the ladder's session limit too — it costs the
                # session its allowance, not its next answer.
                if _detail(worst, "level", 0) == 2:
                    _LOG.warning("[runbound] session limited: %s", worst.message)
                else:
                    _LOG.warning("[runbound] %s", worst.message)
                return
            if not self._spike_stops(worst):
                # Thinking mode alone is legitimate behavior: a confirmed
                # spike notifies by default and stops the session only when
                # the user opted in.
                _LOG.warning("[runbound] confirmed, notifying only: %s", worst.message)
                return
        self._react(worst, session)

    def _spike_stops(self, anomaly: Anomaly) -> bool:
        """Does this critical spike stop the session, or only page someone?

        It stops when the user opted in — ``on_spike="trip"``, or the ladder's
        ``"limit"``, whose rollover and cooldown are served by the latch — and
        whenever an explicit per-call cap was breached, because a cap is a
        limit somebody stated rather than a learned baseline.
        """
        if self.config.on_spike in ("trip", "limit"):
            return True
        return _detail(anomaly, "cap", None) is not None

    # --- failed provider calls ----------------------------------------------

    def record_llm_error(
        self,
        session: SessionState,
        model: str | None,
        exc: BaseException,
        duration_s: float,
        provider: str,
    ) -> None:
        """Account one failed model call: to the session, then to the provider.

        Two independent consequences, in that order. The session gets an
        ``llm_error`` event and the detectors run on it, so a run of failures
        is a retry storm the configured ``on_anomaly`` reacts to — that is the
        agent's own behavior. The provider gets a mark against its circuit,
        which is process-wide and has nothing to do with which session was
        unlucky enough to make the call.

        The circuit accounting happens even when detection stops the session,
        because it must: under ``on_anomaly="raise"`` a latched session raises
        on every later event, and a breaker that stopped counting there would
        never notice the outage that is still going on.

        Raises :class:`GuardrailTripped` if this failure is the one that trips
        the session. Nothing about the circuit ever raises.
        """
        event = Event(
            kind="llm_error",
            ts=_event_ts(),
            step=session.next_step(),
            model=model,
            duration_s=duration_s,
            error=_error_text(exc),
        )
        try:
            self.process(session, event)
        finally:
            self._mark_provider(session, exc, provider)

    def _mark_provider(self, session: SessionState, exc: BaseException, provider: str) -> None:
        """Count a failure against ``provider``'s circuit and alert if it opened.

        Only the provider's own failures count: a 400 is our request being
        wrong, and opening a circuit over it would blame the provider for a bug
        in the caller's code. Alerting happens under both modes — an open
        circuit is news whether or not the customer asked us to act on it — and
        exactly once, because one outage is one incident.

        Never raises: the circuit is an optimization on top of the host's own
        error handling, and a bug in it must not replace the provider's
        exception with ours.
        """
        try:
            if not is_provider_failure(exc):
                return
            if not self.circuit.record_failure(provider):
                return
            anomaly = self._circuit_anomaly(provider)
            self._alert(anomaly, session)
            self._report_circuit(provider, "open", self.config.circuit_failure_threshold)
            _LOG.warning("[runbound] %s", anomaly.message)
        except Exception:
            _LOG.warning(
                "runbound could not update the circuit for provider %r",
                provider,
                exc_info=True,
            )

    def _circuit_anomaly(self, provider: str) -> Anomaly:
        """Describe a circuit that has just opened, in the mode it opened in."""
        config = self.config
        blocking = config.on_provider_failure == "open"
        tail = (
            " — calls fail fast until it recovers"
            if blocking
            else " (notify only: calls continue)"
        )
        return Anomaly(
            detector=CIRCUIT_DETECTOR,
            severity="critical",
            message=(
                f"Provider {provider!r} circuit opened: "
                f"{config.circuit_failure_threshold} failures in "
                f"{config.circuit_window_seconds:.0f}s; cooling down "
                f"{config.circuit_cooldown_seconds:.0f}s{tail}"
            ),
            details={
                "provider": provider,
                "host": provider_host(provider),
                "failures": config.circuit_failure_threshold,
                "window_seconds": config.circuit_window_seconds,
                "cooldown_seconds": config.circuit_cooldown_seconds,
                "on_provider_failure": config.on_provider_failure,
                "state": "open",
            },
        )

    def circuit_allows(self, provider: str) -> bool:
        """May a call to ``provider`` go out?

        Always yes under the default ``on_provider_failure="notify"``: failures
        are counted and reported, and nobody's traffic is refused unless the
        customer asked for that. Under ``"open"`` this is the breaker's own
        answer — no while it is open, yes for the single probe once the
        cooldown has passed. A breaker that cannot answer says yes: runbound
        never blocks a call because of its own bug.
        """
        if self.config.on_provider_failure != "open":
            return True
        try:
            return bool(self.circuit.allow(provider))
        except Exception:
            _LOG.warning(
                "runbound could not read the circuit for provider %r; allowing the call",
                provider,
                exc_info=True,
            )
            return True

    def admit(
        self,
        session: SessionState,
        provider: str,
        model: str | None,
        request: dict | None,
    ) -> None:
        """Circuit, unpriced model, in-flight cap, then — when
        ``config.budget_admission`` — the budget estimate. Raises
        ``GuardrailTripped``/``CircuitOpen``; never latches on an admission
        refusal.

        T136 gives a name to what already existed in embryo inside
        :meth:`~runbound.api._Hooks.before` — the circuit and unpriced-model
        checks move here unchanged — and adds the one thing that was missing:
        an opt-in estimate of what this call would cost, refused before it
        goes out rather than discovered after. The in-flight cap stays where
        it always lived, in the api's own registry (a process-wide resource
        this engine does not own), called by ``before`` immediately after
        this returns without raising — from the caller's side the four still
        run in the stated order, because a circuit or unpriced refusal here
        never lets ``before`` reach the in-flight check at all.

        Every phase is independently fail-open: a bug checking the circuit,
        the price table or the estimate logs a warning and lets the call
        through rather than skip the phases after it or refuse a call for a
        reason of our own making. Only a phase's own deliberate refusal
        raises.

        The budget estimate never latches on purpose: it is a guess, and a
        cheaper call minutes from now may fit even though this one would not
        have — latching here would turn an estimate into a wall, which is
        exactly the imprecision ``budget_admission`` is opt-in to avoid
        importing into the default path. The post-call ``budget`` detector is
        the wall; this is the door in front of it.
        """
        self._admit_circuit(provider)
        self._admit_unpriced(session, model)
        if self.config.budget_admission:
            self._admit_budget(session, model, request)

    def _admit_circuit(self, provider: str) -> None:
        """The first admission phase: is this provider's circuit open?

        Ported from the api's own ``_Hooks.before`` unchanged: only
        ``on_provider_failure="open"`` ever refuses here (see
        :meth:`circuit_allows`), and building the refusal's anomaly is
        covered by the same fail-open as reading the circuit itself — a
        broken describer must not block a call the breaker would have let
        through.
        """
        try:
            allowed = self.circuit_allows(provider)
            anomaly = None if allowed else self._circuit_open_anomaly(provider)
        except Exception:
            _LOG.warning(
                "runbound could not check the circuit for %r; the call proceeds",
                provider,
                exc_info=True,
            )
            return
        if anomaly is not None:
            raise CircuitOpen(anomaly, provider)

    def _circuit_open_anomaly(self, provider: str) -> Anomaly:
        """Describe the refusal :meth:`_admit_circuit` is about to raise."""
        state = self.circuit.state(provider)
        cooldown = self.config.circuit_cooldown_seconds
        return Anomaly(
            detector=CIRCUIT_DETECTOR,
            severity="critical",
            message=(
                f"Provider {provider!r} circuit is open; failing fast "
                f"(cooldown {cooldown:.0f}s)"
            ),
            details={
                "provider": provider,
                "host": provider_host(provider),
                "state": state,
                "cooldown_seconds": cooldown,
            },
        )

    def _admit_unpriced(self, session: SessionState, model: str | None) -> None:
        """The second admission phase: does ``on_unpriced_model="refuse"`` apply?

        Ported from the api's own ``_check_unpriced_refusal``/
        ``_alert_unpriced_model`` unchanged: a no-op unless that mode is set
        and ``model`` is known before the request goes out; latches nothing
        (nothing ran yet) and is alerted once per model per Engine, via the
        same ``notify_door``/``_alert`` dedup every other door refusal uses.
        A broken alert must not swallow the refusal itself, so alerting has
        its own, inner fail-open.
        """
        try:
            config = self.config
            if config.on_unpriced_model != "refuse" or not model:
                return
            if price_for(model, config.custom_prices) is not None:
                return
            anomaly = Anomaly(
                detector=BUDGET_DETECTOR,
                severity="critical",
                message=(
                    f"Model {model!r} has no known price and "
                    'on_unpriced_model="refuse"; refusing before the request '
                    "goes out"
                ),
                details={"reason": "unpriced_model", "model": model},
            )
        except Exception:
            _LOG.warning(
                "runbound could not check pricing for model %r; the call proceeds",
                model,
                exc_info=True,
            )
            return
        try:
            self.notify_door(session, anomaly)
            _LOG.warning("[runbound] %s", anomaly.message)
        except Exception:
            _LOG.warning(
                "runbound could not alert on an unpriced-model refusal", exc_info=True
            )
        raise GuardrailTripped(anomaly)

    def _admit_budget(
        self, session: SessionState, model: str | None, request: dict | None
    ) -> None:
        """The opt-in phase: would this call's estimated cost cross the budget?

        Estimate = ``estimated_tokens(chars of the request's messages)`` at
        the model's input rate, plus the request's own output-token cap (else
        ``config.admission_output_tokens``) at its output rate — the same
        price table :mod:`runbound.pricing` prices the call with after the
        fact. Remaining = ``budget_usd`` minus what this session (and the rest
        of the fleet, via ``spend_offset_usd``) has already spent. A no-op
        without ``budget_usd`` — there is nothing to estimate against — and
        for an unpriced model, warned once per model per Engine rather than
        refused: inventing a limit the customer never set is worse than
        skipping this one opt-in check for a call the post-call wall still
        watches.

        Never latches (see :meth:`admit`): raises straight from here, never
        through :meth:`_react`/:meth:`_latch`.

        ``price_for`` may return a 3-tuple when the model publishes a cached-
        input rate (T139); admission has no way to know before the call how
        many of its input tokens will be cache hits, so it estimates at the
        plain input rate (``price[:2]``) — the same conservative "assume no
        discount" the post-call price falls back to for an unpriced model,
        here applied to an unknown-yet split instead of an unknown rate.
        """
        config = self.config
        if config.budget_usd is None:
            return
        try:
            price = price_for(model, config.custom_prices)
            if price is None:
                self._warn_admission_unpriced(model)
                return
            price_in, price_out = price[0], price[1]
            with session.lock:
                remaining = config.budget_usd - (
                    session.total_cost_usd + session.spend_offset_usd
                )
            output_tokens = _admission_output_cap(request)
            if output_tokens is None:
                output_tokens = config.admission_output_tokens
            input_tokens = _estimated_tokens(_admission_request_chars(request))
            cost_in = (input_tokens / 1_000_000.0) * price_in
            cost_out = (output_tokens / 1_000_000.0) * price_out
            estimate = cost_in + cost_out
            if estimate <= remaining:
                return
            anomaly = _admission_anomaly(
                session, model, estimate, remaining, config.budget_usd
            )
        except Exception:
            _LOG.warning(
                "runbound could not estimate the admission cost for model %r; "
                "the call proceeds",
                model,
                exc_info=True,
            )
            return
        try:
            self.notify_door(session, anomaly)
            _LOG.warning("[runbound] %s", anomaly.message)
        except Exception:
            _LOG.warning(
                "runbound could not alert on an admission refusal", exc_info=True
            )
        raise GuardrailTripped(anomaly)

    def _warn_admission_unpriced(self, model: str | None) -> None:
        """Say once per model per Engine that admission skipped this call.

        A plain log line, not an anomaly: nothing was refused, so there is
        nothing for an observer to react to. ``model`` may be ``None`` (a
        provider that only reveals it in the response); that is its own
        single entry in the warned set, which is exactly right — one warning
        for "admission cannot see this call's model" is enough.
        """
        if model in self._admission_unpriced_warned:
            return
        self._admission_unpriced_warned.add(model)
        _LOG.warning(
            "runbound: no price known for model %r; skipping the admission "
            "budget estimate for this call (the post-call budget check still "
            "applies)",
            model,
        )

    def record_llm_success(self, provider: str) -> None:
        """Report a model call that worked: ``provider``'s circuit closes.

        A circuit that was not closed and now is, is a transition the fleet
        wants to hear about. Without a control plane the state is not even
        read: a healthy call must cost nothing it did not cost before.
        """
        try:
            before = self.circuit.state(provider) if self._reports_circuits else "closed"
            self.circuit.record_success(provider)
            if before != "closed":
                self._report_circuit(provider, "closed", 0)
        except Exception:
            _LOG.warning(
                "runbound could not close the circuit for provider %r",
                provider,
                exc_info=True,
            )

    def enforce_policy(self, session: SessionState, call: ToolCall) -> None:
        """Apply the configured action policy to one attempted tool call.

        Called from the tool hook after the ``tool_call`` event is recorded and
        before the tool body runs, so ``session.tool_calls`` already counts this
        attempt — which is what ``max_calls`` is measured against.

        The reaction is the policy's own ``on_violation``, never
        ``on_anomaly``: the customer stated this rule about their own agent, so
        an agent that tries a forbidden action is stopped whether or not
        detectors are configured to stop anything. ``"dry_run"`` logs and alerts
        what would have been refused and lets the call proceed; ``"block"``
        refuses this call; ``"block_and_latch"`` also stops the session,
        honoring ``on_trip``.

        Raises :class:`PolicyViolation` (a :class:`GuardrailTripped`) when the
        call is refused. Nothing else escapes: a policy we cannot evaluate is
        logged and the call proceeds, because a runbound bug must never take
        down a host. The deliberate exception is a customer predicate that
        raises — :func:`runbound.policy.evaluate` treats that as a refusal.
        """
        policy = self._policy()
        if policy is None:
            return
        with session.lock:
            calls_so_far = session.tool_calls.get(call.name, 0)
        try:
            violation = evaluate(policy, call, calls_so_far)
        except Exception:
            _LOG.warning(
                "runbound could not evaluate the tool policy for %r; "
                "the call was allowed",
                call.name,
                exc_info=True,
            )
            return
        if violation is None:
            return

        dry_run = policy.on_violation == "dry_run" or _is_dry_run(violation)
        anomaly = _policy_anomaly(session, violation, dry_run)
        self._alert(anomaly, session, "dry_run" if dry_run else "blocked")
        if dry_run:
            _LOG.warning("[runbound] %s", anomaly.message)
            return
        if policy.on_violation == "block_and_latch":
            self._latch(session, anomaly)
        raise PolicyViolation(anomaly, violation)

    def _policy(self) -> ToolPolicy | None:
        """The policy to enforce: the customer's, plus the org's if there is one.

        The two are combined by :func:`runbound.policy.merge`, which can only
        ever remove freedom, and the result is cached until the plane states a
        new version — so a policy change reaches a running fleet without an
        ``init()``, and an unchanged one costs nothing per tool call.

        ``None`` when there is nothing to enforce. A merge that cannot be
        honored (an org rule requiring approval where the customer configured
        no callback) is logged and the local policy alone applies: an org
        mistake must not start refusing an agent's every action.
        """
        local = self._local_policy()
        remote, version, dry_run = self._remote_policy()
        if remote is None:
            return local
        key = (version, dry_run, id(local))
        if self._merged_key == key:
            return self._merged
        try:
            merged = merge(local, remote, remote_dry_run=dry_run)
        except Exception:
            _LOG.warning(
                "runbound: the org tool policy could not be merged; "
                "enforcing the local one",
                exc_info=True,
            )
            merged = local
        self._merged_key = key
        self._merged = merged
        return merged

    def _local_policy(self) -> ToolPolicy | None:
        """The configured policy, coerced from a dict if it still is one.

        ``init()`` coerces a dict policy during validation; an engine built
        around an unvalidated config coerces here instead, and one built around
        something that is not a policy at all enforces nothing rather than
        failing every tool call the host makes.
        """
        policy = self.config.tool_policy
        if policy is None or isinstance(policy, ToolPolicy):
            return policy
        try:
            policy = coerce(policy)
        except ValueError:
            _LOG.warning(
                "runbound: tool_policy is not usable; no policy is enforced",
                exc_info=True,
            )
            return None
        self.config.tool_policy = policy
        return policy

    def _remote_policy(self) -> "tuple[dict | None, int, bool]":
        """The org policy the plane last stated: ``(body, version, dry_run)``."""
        shared = self.shared
        try:
            body = shared.policy()
        except Exception:
            _LOG.warning("runbound: could not read the org policy", exc_info=True)
            return None, 0, False
        if not isinstance(body, dict):
            return None, 0, False
        return (
            body,
            int(getattr(shared, "policy_version", 0) or 0),
            bool(getattr(shared, "policy_dry_run", False)),
        )

    def _winner(self, anomalies: list[Anomaly]) -> Anomaly:
        """The anomaly that drives the reaction (T135).

        Most severe first (any ``critical`` beats any ``warn``); a tie among
        anomalies of the same severity is broken by :data:`events.PRIORITY` —
        the one stated order — then by detector name, so the result never
        depends on which detector happened to run first in ``self.detectors``.
        Reversing ``DEFAULT_DETECTORS`` produces the same winner.
        """
        return min(anomalies, key=lambda a: _anomaly_sort_key(a, self._unranked_warned))

    def _detect(self, session: SessionState, event: Event) -> list[Anomaly]:
        """Run every detector; a broken one is logged and skipped."""
        anomalies: list[Anomaly] = []
        for detector in self.detectors:
            try:
                anomaly = detector.check(session, event, self.config)
            except Exception:
                _LOG.warning(
                    "runbound detector %r failed and was skipped",
                    getattr(detector, "name", detector),
                    exc_info=True,
                )
                continue
            if anomaly is not None:
                anomalies.append(anomaly)
        return anomalies

    def _alert(
        self, anomaly: Anomaly, session: SessionState, reacted: str | None = None
    ) -> None:
        """Notify the observers, once per (session, detector).

        Detectors that fire once per session are unaffected; the loop detector
        under a repeat-driven policy fires on every repeat, and this is what
        keeps that from becoming a notification storm.

        The observers are told what the engine is about to do about it
        (``reacted``). Callers that already know — a policy dry run, a
        refusal at the door — say so; the rest is worked out from the anomaly
        and the configuration. This is the whole of what used to be "alerting"
        from inside the SDK: delivery itself is the control plane's job now
        (Wave 31), and an observer — telemetry export among them — is how it
        learns that this happened.
        """
        # Keyed by severity too, so an escalation ("watching" -> confirmed
        # critical) still reaches the on-call instead of being deduped away —
        # and, on the abuse ladder, by which rung it is (``level``) and which
        # limit it belongs to (``episode``), so each rung pages once and a
        # session limited again after healing pages again.
        key = (
            getattr(session, "session_id", ""),
            anomaly.detector,
            anomaly.severity,
            _detail(anomaly, "level", None),
            _detail(anomaly, "episode", None),
        )
        if anomaly.detector in (CIRCUIT_DETECTOR, INFLIGHT_DETECTOR):
            # A circuit — and a full endpoint — belongs to a provider, not to
            # the session that happened to make the call: the session id is
            # dropped so one outage pages once however many end-users ran into
            # it, and two endpoints stay two incidents.
            key = (anomaly.detector, _detail(anomaly, "provider", None))
        if anomaly.detector == PLANE_DETECTOR:
            # A plane-loss refusal (`on_plane_loss="refuse"`) belongs to the
            # outage, not to whichever session's entry happened to hit it
            # first: the session id is dropped so one degraded link pages once
            # however many end-users are refused at the door while it lasts.
            key = (anomaly.detector, _detail(anomaly, "reason", None))
        if anomaly.detector == BUDGET_DETECTOR and _detail(anomaly, "reason", None) == (
            "unpriced_model"
        ):
            # An unpriced-model refusal belongs to the model, not to whoever
            # happened to ask for it first: one page per model per Engine
            # (this set is rebuilt by init(), not truly process-wide), however
            # many sessions or endpoints hit the same unpriced name.
            key = (anomaly.detector, "unpriced_model", _detail(anomaly, "model", None))
        if anomaly.detector == BUDGET_DETECTOR and _detail(anomaly, "rule", None) == (
            "admission"
        ):
            # An admission refusal (T136) is its own kind of "budget" news:
            # the ordinary key already includes the session id, but not the
            # rule, so without this an admission refusal and a later
            # post-call budget trip in the same session would dedupe against
            # each other — only the first of the two would ever reach an
            # observer. Adding "admission" keeps them apart; alerted once per
            # session either way, as the ordinary key already ensures.
            key += ("admission",)
        if anomaly.detector == POLICY_DETECTOR:
            # A policy anomaly is per rule and per tool: an agent refused a
            # second tool, or refused the same tool for a different reason, is
            # news; the same refusal on every retry is not.
            key += (_detail(anomaly, "rule", None), _detail(anomaly, "tool", None))
        if key in self._alerted:
            return
        self._alerted.add(key)
        self._notify_anomaly(session, anomaly, reacted or self._reacted_for(anomaly))

    def notify_door(self, session: SessionState, anomaly: Anomaly) -> None:
        """Report a refusal made at the door of a :func:`~runbound.session` block.

        Fan-out limits, the in-flight cap and a fleet-wide halt all refuse
        *before* anything has happened, so there is no event to hang the
        anomaly off — the api calls this instead. Deduped and reported to the
        observers exactly like any other anomaly, tagged ``"door"``.
        """
        self._alert(anomaly, session, "door")

    def _reacted_for(self, anomaly: Anomaly) -> str:
        """What this anomaly is about to cost the run, in one word.

        The vocabulary is the plane's: ``raise`` (the session is stopped with
        an exception), ``callback`` (the customer's handler decides),
        ``warn`` (logged and alerted, nothing stopped), plus ``blocked``,
        ``dry_run`` and ``door``, which their own callers pass in.

        It describes what this pass of the engine did to the run, so a warning
        that co-fired with a critical is reported as the stop it happened
        alongside. The detectors with a reaction of their own — a spike still
        being watched, a throttled loop — are the exceptions, and say so.
        """
        if anomaly.detector == SPIKE_DETECTOR and (
            anomaly.severity != "critical" or not self._spike_stops(anomaly)
        ):
            return "warn"
        if anomaly.detector == LOOP_DETECTOR and self.config.on_loop is not None:
            if self.config.on_loop == "break":
                return "raise"
            if self.config.on_loop == "escalate":
                return "raise" if anomaly.severity == "critical" else "warn"
            return "warn"  # throttling delays the call; it never stops the run
        if anomaly.detector == CIRCUIT_DETECTOR:
            return "blocked" if self.config.on_provider_failure == "open" else "warn"
        mode = self.config.on_anomaly
        if mode == "raise":
            return "raise"
        if mode == "callback" and self.config.callback is not None:
            return "callback"
        return "warn"

    def _notify_event(self, session: SessionState, event: Event) -> None:
        """Hand one recorded event to every observer. Never raises."""
        for observer in self.observers:
            try:
                observer.on_event(session, event)
            except Exception:
                _LOG.warning(
                    "runbound observer %s failed on an event",
                    type(observer).__name__,
                    exc_info=True,
                )

    def _notify_anomaly(
        self, session: SessionState, anomaly: Anomaly, reacted: str
    ) -> None:
        """Hand one verdict, and what it cost, to every observer. Never raises."""
        for observer in self.observers:
            try:
                observer.on_anomaly(session, anomaly, reacted)
            except Exception:
                _LOG.warning(
                    "runbound observer %s failed on an anomaly",
                    type(observer).__name__,
                    exc_info=True,
                )

    def _react_to_loop(self, anomaly: Anomaly, session: SessionState) -> None:
        """Apply ``on_loop`` to a loop anomaly, in place of the global mode.

        ``"break"`` stops the run; ``"escalate"`` logs while the detector
        still calls the loop a warning and stops the run once it calls it
        critical; ``"throttle"`` never raises and delays the repeated call.

        Under a running event loop, ``time.sleep`` would block every other
        request this worker is serving, so the delay is stashed in a
        contextvar instead of slept here — ``take_pending_delay()`` hands it
        to whichever async caller asks next (the ``@runbound.tool`` wrapper,
        or a wrapped client's async request loop), which ``await
        asyncio.sleep``s it without blocking anyone else. A synchronous call
        made directly on the event-loop thread — outside either of those
        async paths — has no way to await and genuinely cannot be throttled;
        that one case's own wrapper takes and discards the pending delay right
        away (rather than leave it stashed for some unrelated later task to
        find and sleep instead), skipping it rather than blocking the loop —
        the same trade-off the old "disabled under asyncio" warning described,
        now made silently because it is the exception rather than the rule.
        A delay nobody collects at all this way (an even older wrapper, say)
        is dropped by :data:`_PENDING_DELAY_MAX_AGE_S` instead of leaking into
        whatever task looks next.

        The two policies that stop the run latch the session; throttling and
        the escalate warn phase do not, because nothing was stopped.
        """
        policy = self.config.on_loop
        if policy == "break":
            self._latch(session, anomaly)
            raise GuardrailTripped(anomaly)
        if policy == "escalate":
            if anomaly.severity == "critical":
                self._latch(session, anomaly)
                raise GuardrailTripped(anomaly)
            _LOG.warning("[runbound] %s", anomaly.message)
            return

        delay = self._throttle_delay(anomaly)
        if _in_event_loop():
            _PENDING_DELAY.set((delay, _monotonic()))
            return

        _LOG.warning(
            "[runbound] throttling tool %r for %.1fs: %s",
            _detail(anomaly, "tool_name", "<unknown>"),
            delay,
            anomaly.message,
        )
        time.sleep(delay)

    def _throttle_delay(self, anomaly: Anomaly) -> float:
        """Seconds to sleep for this repeat: base doubled per repeat, capped.

        An anomaly that carries no usable ``count`` is treated as the first
        repeat over the threshold, i.e. the base delay.
        """
        threshold = self.config.loop_threshold
        count = _detail(anomaly, "count", threshold)
        try:
            over = max(0, int(count) - threshold)
        except (TypeError, ValueError):
            over = 0
        return min(
            self.config.throttle_base_seconds * 2**over, self.config.throttle_max_seconds
        )

    def _react(self, anomaly: Anomaly, session: SessionState) -> None:
        """Apply the configured reaction to the anomaly that matters most.

        A reaction that actually stops the session — a raise, or the user's
        callback being told to stop serving — latches it first, so the wall
        stays a wall. ``on_anomaly="warn"`` stops nothing and never latches,
        and neither does a "warn"-severity anomaly.
        """
        mode = self.config.on_anomaly
        if mode == "raise":
            self._latch(session, anomaly)
            raise GuardrailTripped(anomaly)
        if mode == "callback" and self.config.callback is not None:
            self._latch(session, anomaly)
            self._invoke_callback(anomaly)
            return
        _LOG.warning("[runbound] %s", anomaly.message)

    def _reapply(self, session: SessionState, anomaly: Anomaly) -> None:
        """Serve a latched session's stored anomaly again, without detecting.

        The same reaction the session was stopped with, minus the alert: the
        blocked user costs the business nothing from their next message on,
        and the on-call is not paged once per retry. Under
        ``on_anomaly="warn"`` this is a debug line, not a warning per event.

        The observers hear about it once per latch — a session refused all
        afternoon is one line of telemetry, not one per event, and the
        control plane already learned of the trip when it first happened.
        """
        self._report_blocked(session, anomaly)
        mode = self.config.on_anomaly
        if mode == "raise":
            raise GuardrailTripped(anomaly)
        if mode == "callback" and self.config.callback is not None:
            self._invoke_callback(anomaly)
            return
        _LOG.debug("[runbound] session still tripped: %s", anomaly.message)

    def _report_blocked(self, session: SessionState, anomaly: Anomaly) -> None:
        """Tell the observers, once, that this session is being served its wall."""
        if not self.observers:
            return
        key = (getattr(session, "session_id", ""), anomaly.detector, anomaly.severity)
        if key in self._reported:
            return
        self._reported.add(key)
        self._notify_anomaly(session, anomaly, "blocked")

    def _latch(self, session: SessionState, anomaly: Anomaly) -> None:
        """Stop this session on ``anomaly``, and tell the fleet if it took.

        The fleet is told only about a latch this worker actually made: a
        session already stopped by something else has already been reported,
        and ``on_trip="once"`` latches nothing at all.
        """
        if not _latch(session, anomaly, self.config):
            return
        self._report_trip(session, anomaly, door=False)

    def _report_trip(self, session: SessionState, anomaly: Anomaly, door: bool) -> None:
        """Hand a trip to the shared state, synchronously. Never raises."""
        try:
            with session.lock:
                ttl = session.latch_ttl_override
            if ttl is None:
                ttl = self.config.latch_ttl_seconds
            self.shared.trip(
                getattr(session, "key", None), session, anomaly, ttl, door
            )
        except Exception:
            _LOG.warning("runbound could not report a trip to the fleet", exc_info=True)

    def _report_circuit(self, provider: str, state: str, failures: int) -> None:
        """Hand a provider circuit transition to the shared state. Never raises."""
        try:
            self.shared.circuit(
                provider, state, failures, self.config.circuit_cooldown_seconds
            )
        except Exception:
            _LOG.warning(
                "runbound could not report a circuit change for %r",
                provider,
                exc_info=True,
            )

    def _invoke_callback(self, anomaly: Anomaly) -> None:
        """Hand the anomaly to the user's callback; a broken one is logged."""
        try:
            self.config.callback(anomaly)
        except Exception:
            _LOG.warning("runbound on_anomaly callback raised", exc_info=True)


def _policy_anomaly(
    session: SessionState, violation: Violation, dry_run: bool
) -> Anomaly:
    """Describe a refused (or would-be-refused) tool call.

    Names the session, the tool and the rule; never the arguments. A dry run
    is a "warn" — nothing was actually stopped — and everything else is
    critical, because the customer's own rule was broken.
    """
    lead = "Policy dry-run: would block tool" if dry_run else "Policy blocked tool"
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=POLICY_DETECTOR,
        severity="warn" if dry_run else "critical",
        message=f"{lead} {violation.tool!r}{whose}: {violation.reason}",
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", {}) or {}),
            "tool": violation.tool,
            "rule": violation.rule,
            **violation.details,
        },
    )


def _admission_anomaly(
    session: SessionState,
    model: str | None,
    estimate: float,
    remaining: float,
    budget_usd: float,
) -> Anomaly:
    """Describe the refusal :meth:`Engine._admit_budget` is about to raise."""
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=BUDGET_DETECTOR,
        severity="critical",
        message=(
            f"Admission refused{whose}: estimated ${estimate:.4f} for model "
            f"{model!r} would exceed budget_usd (${remaining:.4f} left of "
            f"${budget_usd:.4f})"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "reason": "admission",
            "rule": "admission",
            "model": model,
            "estimated_cost_usd": estimate,
            "remaining_usd": remaining,
            "budget_usd": budget_usd,
        },
    )


#: Characters an estimated token stands for (T136's own copy of the constant
#: `wrappers.CHARS_PER_TOKEN` uses — duplicated, not imported, because engine.py
#: must not depend on the wrapper package; see `_admission_request_chars`).
_ADMISSION_CHARS_PER_TOKEN = 4

#: Output-token cap fields, checked in the order a request is likeliest to
#: carry one: `max_tokens` (Anthropic, and OpenAI's older chat completions),
#: `max_completion_tokens` (OpenAI's newer chat completions), then
#: `max_output_tokens` (OpenAI's Responses API). Mirrors
#: `wrappers.openai_wrapper.request_output_cap` and
#: `wrappers.anthropic_wrapper.request_output_cap`.
_ADMISSION_OUTPUT_CAP_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _estimated_tokens(chars: int) -> int:
    """``ceil(chars / 4)`` for the admission estimate — T136's own copy of
    ``wrappers.estimated_tokens`` (see ``_admission_request_chars``)."""
    try:
        return -(-max(int(chars), 0) // _ADMISSION_CHARS_PER_TOKEN)
    except (TypeError, ValueError):
        return 0


def _admission_field(obj, name: str):
    """Read ``name`` off an attribute-style or mapping-style object.

    ``None`` for anything missing or that raises — a request is someone
    else's dict (or SDK param object), and may be shaped any way at all.
    """
    try:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)
    except Exception:
        return None


def _admission_request_chars(request: dict | None) -> int:
    """Characters of message text an admission estimate is based on.

    Deliberately narrow: only the ``messages`` shape both OpenAI's chat
    completions and Anthropic's ``messages.create`` use, because an admission
    estimate is stated as one (see ``budget_admission`` in config.py) — a
    request shaped differently (the Responses API's ``input``, say) simply
    estimates 0 input chars rather than guessing at a shape this module was
    not taught. This is engine.py's own minimal reader, not
    ``wrappers.messages_chars``: the wrapper package imports the engine
    (indirectly, through the api), so the engine must not import it back —
    see the module docstring's dependency direction.
    """
    if not isinstance(request, dict):
        return 0
    messages = request.get("messages")
    if not isinstance(messages, (list, tuple)):
        return 0
    total = 0
    for message in messages:
        content = _admission_field(message, "content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, (list, tuple)):
            for part in content:
                text = _admission_field(part, "text")
                if isinstance(text, str):
                    total += len(text)
    return total


def _admission_output_cap(request: dict | None) -> int | None:
    """The output-token cap ``request`` stated, if any (T136).

    Checked in :data:`_ADMISSION_OUTPUT_CAP_FIELDS` order; the first present,
    positive value wins. ``None`` for a request with no cap at all, or one
    that cannot be read — the caller's definition of "the request stated no
    limit," which is exactly when ``config.admission_output_tokens`` applies.
    """
    if not isinstance(request, dict):
        return None
    for field_name in _ADMISSION_OUTPUT_CAP_FIELDS:
        value = request.get(field_name)
        if value is None:
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _is_dry_run(violation: Violation) -> bool:
    """Is this violation one the org is still rolling out?

    An org rule under a dry run is reported and never enforced, while the
    customer's own rules on the same policy keep blocking — so "dry run" is a
    property of the violation, not of the merged policy's mode.
    """
    details = violation.details
    return isinstance(details, dict) and details.get("dry_run") is True


def _error_text(exc: BaseException) -> str:
    """One failed call's message, truncated; its type when it has no message.

    Someone else's exception may raise from ``__str__``; an event that says
    ``TimeoutError`` is worth more than an event we could not build.
    """
    try:
        return str(exc)[:ERROR_MAX_CHARS]
    except Exception:
        return type(exc).__name__


def _event_ts() -> float:
    """The timestamp an engine-built event carries: monotonic, never missing.

    A clock that refuses to answer costs the event its place in the trailing
    windows, not its existence.
    """
    now = _now()
    return 0.0 if now is None else now


def _monotonic() -> float:
    """The clock the provider circuit runs on.

    Read off this module's ``time`` at call time, like :func:`_now`, so a test
    that swaps the engine's clock moves the circuit's cooldown with it.
    """
    return time.monotonic()


def _in_event_loop() -> bool:
    """True if this thread is running inside an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _latch(session: SessionState, anomaly: Anomaly, config: GuardrailConfig) -> bool:
    """Remember that ``anomaly`` stopped ``session``, if nothing else has.

    Only a critical anomaly latches — a warning stops nothing — and the first
    one wins, so the session keeps reporting why it was actually stopped
    rather than whatever fired last. The moment it was stopped is stamped
    alongside, which is what ``latch_ttl_seconds`` later measures.

    Under ``on_trip="once"`` nothing is remembered: the customer chose to
    stop that one call only and evaluate the next one afresh.

    Returns whether this call is the one that stopped the session, which is
    what the fleet is told about.
    """
    if anomaly.severity != "critical" or config.on_trip != "latch":
        return False
    with session.lock:
        if session.tripped_by is not None:
            return False
        session.tripped_by = anomaly
        session.tripped_at = _now()
    return True


def _latched(
    session: SessionState,
    config: GuardrailConfig | None = None,
    detectors: Sequence | None = None,
) -> Anomaly | None:
    """The anomaly ``session`` is latched on, or ``None`` if it is healthy.

    With a ``config`` that sets ``latch_ttl_seconds``, a latch older than that
    many seconds expires here: it is cleared and every detector is re-armed
    for this session id (T137), so the session is judged fresh on its very
    next event. Without one — the default — the latch is permanent, exactly
    as it has always been.

    This is re-admission, not a clean slate: nothing here resets a counter.
    ``latch_ttl_seconds`` re-admits a session and its next event is judged on
    the same cumulative counters, so a session still over budget re-trips
    immediately, with the same detector — the wall that promise requires. A
    windowed budget that actually zeroes on a schedule is a different,
    unbuilt feature. Only ``clear()`` resets the counters themselves. The
    latch is armed again regardless, so a *different* critical anomaly can
    still stop the session even where the same one no longer applies.

    ``detectors`` is the live engine's own list, handed in so the rearm
    reaches the same instances ``process()`` is about to call ``check()`` on
    next; callers that only want to *read* the latch's state without an
    engine at hand (or that healed it moments ago through another call site)
    may omit it — the latch still clears, just with no detector to rearm,
    which only matters the first time any caller observes the expiry.
    """
    with session.lock:
        anomaly = session.tripped_by
        if anomaly is None or not _latch_expired(session, config):
            return anomaly
        session.tripped_by = None
        session.tripped_at = None
    _rearm_detectors(detectors, session.session_id)
    _LOG.info(
        "runbound: latch expired for session %s; resuming", session.session_id
    )
    return None


def _rearm_detectors(detectors: Sequence | None, session_id: str) -> None:
    """Tell every detector that can rearm itself to forget ``session_id``.

    Fail-open, per detector: one broken ``rearm`` must not stop the others
    from clearing their own memory, and must never propagate into the caller
    healing the latch. A detector with no ``rearm`` (a customer's own, or a
    test double) is silently skipped rather than required to implement it.
    """
    if not detectors:
        return
    for detector in detectors:
        rearm = getattr(detector, "rearm", None)
        if rearm is None:
            continue
        try:
            rearm(session_id)
        except Exception:
            _LOG.warning(
                "runbound detector %r could not rearm for session %s",
                getattr(detector, "name", detector),
                session_id,
                exc_info=True,
            )


def _latch_expired(session: SessionState, config: GuardrailConfig | None) -> bool:
    """Has the latch outlived its ttl? Caller holds the lock.

    The session's own ``latch_ttl_override`` wins when it is set — that is how
    a rollover cooldown expires on its own schedule — and otherwise the
    configured ``latch_ttl_seconds`` applies, or nothing at all.
    """
    ttl = getattr(session, "latch_ttl_override", None)
    if ttl is None:
        ttl = config.latch_ttl_seconds if config is not None else None
    if ttl is None or session.tripped_at is None:
        return False
    now = _now()
    return now is not None and now - session.tripped_at > ttl


def _now() -> float | None:
    """Monotonic seconds, or ``None`` when the clock cannot be read.

    Read through the module's ``time`` so a test can inject a clock. A clock
    that refuses to answer costs the latch its expiry — it stays permanent,
    the behavior without a ttl — and never costs the host its request.
    """
    try:
        return time.monotonic()
    except Exception:
        _LOG.debug("runbound could not read the monotonic clock", exc_info=True)
        return None


def _detail(anomaly: Anomaly, key: str, default):
    """One value out of an anomaly's details, tolerating a malformed one."""
    details = anomaly.details
    if not isinstance(details, dict):
        return default
    value = details.get(key, default)
    return default if value is None else value


#: Severity always outranks priority: any critical anomaly beats any warn
#: one, whatever their detectors' declared order. Lower rank wins.
_SEVERITY_RANK = {"critical": 0, "warn": 1}

#: Rank handed to a detector name events.PRIORITY has no entry for — after
#: every named one, so an unranked detector loses every tie it is in.
_UNRANKED_PRIORITY = len(PRIORITY)


def _priority_rank(detector: str, warned: set[str]) -> int:
    """``detector``'s tie-break rank from :data:`events.PRIORITY`.

    A detector this table does not know about — a customer's own, or one
    the table has not caught up with yet — must never crash the winner
    selection: it sorts last, and this warns about it exactly once (``warned``
    is one engine's own memo, so a chatty session does not repeat itself).
    """
    rank = PRIORITY.get(detector)
    if rank is not None:
        return rank
    if detector not in warned:
        warned.add(detector)
        _LOG.warning(
            "runbound: detector %r has no entry in events.PRIORITY; it will "
            "lose every tie against a ranked detector until one is added",
            detector,
        )
    return _UNRANKED_PRIORITY


def _anomaly_sort_key(anomaly: Anomaly, warned: set[str]) -> tuple:
    """``(severity_rank, priority_rank, detector_name)`` — lower sorts first."""
    severity_rank = _SEVERITY_RANK.get(anomaly.severity, len(_SEVERITY_RANK))
    return (severity_rank, _priority_rank(anomaly.detector, warned), anomaly.detector)
