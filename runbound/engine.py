"""The engine — the only place where an event turns into a consequence.

One pass per event: record it, ask every detector what it thinks, notify, then
act. Detection lives in :mod:`runbound.detectors`; the engine owns none of
it, it only sequences it. Delivery is not the engine's concern at all —
the SDK detects, stops, refuses and reports; the control plane routes and
delivers. What "reports" means here is
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
import dataclasses
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, NamedTuple

from . import admission
from . import controls_merge
from . import ladder
from . import local_events
from .circuit import CIRCUIT_FAULTS, PROVIDER, CircuitBreaker, classify_failure
from .config import GuardrailConfig
from .detectors import (
    DEFAULT_DETECTORS,
    BudgetDetector,
    LoopDetector,
    SpikeDetector,
    StepDetector,
    TimeoutDetector,
)
from .events import ALLOW, PRIORITY, Anomaly, Decision, Event
from .exceptions import CircuitOpen, GuardrailTripped, PolicyViolation, SafeModeViolation
from . import _coverage
from .policy import (
    ToolCall,
    ToolPolicy,
    Violation,
    coerce,
    conflicts,
    evaluate,
    from_decorators,
    merge,
)
from .plane_types import key_hash as _key_hash_fn
from .pricing import admission_worst_case, price_for, request_chars
from .quota import MAX_COOLDOWN_S, Quota, cooldown_for, headers_of, read_quota
from .shared import LocalState
from . import posture as posture_module
from .posture import Posture
from .state import Hold, PostureState, SessionState, make_posture_state

_LOG = logging.getLogger("runbound")

LOOP_DETECTOR = LoopDetector.name

#: How many identical refusals one session records individually, per
#: ``(detector, rule, tool)``. A refusal is evidence, so every one of them is
#: exported as its own anomaly and Decision, up to this many; a session that
#: keeps hammering the same refused action past it is counted, and one summary
#: anomaly per ``(detector, rule, tool)`` carrying ``details["suppressed_count"]``
#: is reported when the session block exits. Two rules, or two tools, storming
#: at once never hide each other: the count is per rule and per tool, not per
#: session.
REFUSAL_RECORD_CAP = 100

#: How many ``(session, detector, rule, tool)`` counters an engine keeps.
_REFUSAL_COUNTERS_MAX = 20_000
SPIKE_DETECTOR = SpikeDetector.name
BUDGET_DETECTOR = BudgetDetector.name
STEP_DETECTOR = StepDetector.name
TIMEOUT_DETECTOR = TimeoutDetector.name

#: The detector name the envelope's own ``max_actions_per_run`` refusal
#: carries — the same name the api's fan-out door refusals use
#: (:data:`runbound.api.FANOUT_DETECTOR`), not imported back from there (the
#: dependency runs the other way) but kept as one literal string, "fanout",
#: reused wherever this module builds that anomaly.
FANOUT_DETECTOR = "fanout"

#: The detector name policy anomalies carry. Not a detector: nothing is
#: inferred, the customer stated the rule and we enforced it.
POLICY_DETECTOR = "policy"

#: ``VelocityDetector.name`` — never imported as a class (this module reads
#: none of its behavior), only its name, for :meth:`Engine._local_detector_action`:
#: it is always ``severity="warn"`` and so can never stop a run, whatever any
#: control says.
VELOCITY_DETECTOR = "velocity"

#: Sentinel: no Controls body has ever been merged yet — distinct from
#: ``None`` (a plane that is connected but currently states nothing), so
#: the very first read always computes the merge once rather than trusting
#: an accidental cache hit against Python's own ``None is None``.
_UNSET = object()


class _ControlsSnapshot(NamedTuple):
    """:meth:`Engine._controls_snapshot`'s own return shape, by name rather
    than position — an earlier revision added three fields to this in one
    afternoon and a positional ``(*_rest, a, b, c, d)`` unpack silently read
    the wrong ones twice in a row when the tuple grew out from under it.
    Every reader below names the field it wants; adding a twelfth costs no
    other method a line.
    """

    limits: dict
    capabilities: dict
    envelope: "bool | None"
    detectors: dict
    violations: list
    circuit_rate: "dict | None"
    loop_shapes: "dict | None"
    budget_soft: "dict | None"
    max_actions_per_run: "int | None"
    circuit_posture: bool
    spike_enabled: bool
    spike: "dict | None"

#: The detector a posture refusal carries ("safe mode" is the product
#: word for any posture but ``full``). Not a detector either: a tool refused
#: because the run may think but not act, on a session that goes on.
SAFE_MODE_DETECTOR = "safe_mode"

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
    caller, so every session's failures count towards the same breaker.

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
        # Rate mode, its own knobs, the fleet fold and circuit-driven
        # posture are not read from `config` when the breaker is
        # constructed here. The breaker always starts in count mode, the
        # free four's own behavior
        # unchanged; `_sync_circuit_from_controls` reconfigures it in place
        # whenever the plane's Controls deliver a `circuit_rate` bundle for
        # this service, and puts it straight back the moment one stops being
        # delivered (a stale plane, a dry_run row, no plane at all).
        self.circuit = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            window_seconds=config.circuit_window_seconds,
            cooldown_seconds=config.circuit_cooldown_seconds,
            now=_monotonic,
            mode="count",
            min_calls=1,
            failure_rate=1.0,
            slow_rate=1.0,
            half_open_calls=1,
        )
        self._circuit_rate_active: dict | None = None
        self._alerted: set[tuple] = set()
        # Per (session id, detector, rule, tool): [refusals seen, refusals
        # suppressed since the last summary, reacted]. See REFUSAL_RECORD_CAP.
        self._refusal_counts: "OrderedDict[tuple, list]" = OrderedDict()
        self._refusal_lock = threading.Lock()
        # The process's own narrowing, set by runbound.enter_safe_mode().
        self._posture: PostureState | None = None
        self._posture_lock = threading.Lock()
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
        # The configured policy with the @runbound.tool rules folded onto it,
        # cached on (how many times the decorator registry has changed, which
        # object config.tool_policy is). Decorators run after init(), so the
        # fold has to be live; the cache is what keeps it from rebuilding —
        # and re-keying self._merged above, which keys on id(local) — on every
        # tool call the process makes.
        self._local_key: tuple | None = None
        self._local: ToolPolicy | None = None
        # A tool whose rule both a decorator and init(tool_policy=...) state is
        # warned about once per engine, not once per tool call.
        self._policy_conflicts_warned: set[str] = set()
        # Models the admission budget estimate skipped for lack of a
        # price, warned about once per model per Engine — the same "once per
        # model, not process" scoping notify_door's alert-dedup set already
        # gives on_unpriced_model="refuse" (see _alert), kept separate here
        # because this is a plain log line, never an anomaly: no call was
        # refused, so there is nothing to alert observers about.
        self._admission_unpriced_warned: set[str] = set()
        # This worker's own init() configuration, snapshotted once here —
        # never touched again — so every later Controls merge tightens
        # against what the code *actually* set, not against a value a
        # previous merge may have already narrowed. See
        # ``_controls_snapshot`` and INVARIANTS.md's tighten-only bound.
        self._code_limits: dict = {
            "budget_usd": config.budget_usd,
            "max_steps": config.max_steps,
            "max_events": config.max_events,
            "loop_threshold": config.loop_threshold,
            "max_cost_per_call_usd": config.max_cost_per_call_usd,
            "max_call_seconds": config.max_call_seconds,
            "max_tokens_out_per_call": config.max_tokens_out_per_call,
        }
        self._code_capabilities: dict = dict(getattr(config, "capabilities", None) or {})
        self._code_envelope: bool | None = config.envelope
        # Every one of these is a real, local ``init()`` value, snapshotted
        # here the same way ``_code_limits`` above already is, so the plane
        # can only tighten what this worker's own code actually configured.
        self._code_circuit_rate: "dict | None" = (
            {
                "mode": "rate",
                "min_calls": config.circuit_min_calls,
                "failure_rate": config.circuit_failure_rate,
                "slow_call_seconds": config.circuit_slow_call_seconds,
                "slow_rate": config.circuit_slow_rate,
                "half_open_calls": config.circuit_half_open_calls,
            }
            if config.circuit_mode == "rate"
            else None
        )
        self._code_circuit_posture: bool = bool(config.circuit_posture)
        self._code_loop_shapes: dict = {
            "shapes": tuple(config.loop_shapes),
            "max_period": config.loop_max_period,
            "stall_turns": config.loop_stall_turns,
        }
        self._code_budget_soft: "dict | None" = (
            {"fraction": config.budget_soft, "reaction": config.on_budget_soft}
            if config.budget_soft is not None
            else None
        )
        self._code_max_actions_per_run: "int | None" = config.max_actions_per_run
        self._code_spike_enabled: bool = bool(config.spike_detection)
        self._code_spike: dict = {
            "mode": config.on_spike,
            "limit_calls": config.spike_limit_calls,
            "cooldown_seconds": config.spike_cooldown_seconds,
            "max_strikes": config.spike_max_strikes,
            "warmup_calls": config.spike_warmup_calls,
            "min_duration_s": config.spike_min_duration_s,
            "min_output_tokens": config.spike_min_output_tokens,
            "window": config.spike_window,
            "factor": config.spike_factor,
            "confirm": config.spike_confirm,
        }
        self._controls_lock = threading.Lock()
        # Identity-cached against the last Controls body actually read from
        # the plane (``is``, not equality — the plane link only ever hands
        # over a *new* dict object when something changed), so a hot
        # detection/admission path pays for the merge once per delivered
        # Controls, not once per event.
        self._controls_last_body: Any = _UNSET
        self._controls_cache = _ControlsSnapshot(
            limits=dict(self._code_limits),
            capabilities=dict(self._code_capabilities),
            envelope=self._code_envelope,
            detectors={},
            violations=[],
            circuit_rate=dict(self._code_circuit_rate) if self._code_circuit_rate else None,
            loop_shapes=dict(self._code_loop_shapes),
            budget_soft=dict(self._code_budget_soft) if self._code_budget_soft else None,
            max_actions_per_run=self._code_max_actions_per_run,
            circuit_posture=self._code_circuit_posture,
            spike_enabled=self._code_spike_enabled,
            spike=dict(self._code_spike),
        )

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
        if worst.detector == LOOP_DETECTOR and (
            self.config.on_loop is not None or _detail(worst, "policy", None) == "graded"
        ):
            # A loop policy must never shadow a co-firing non-loop critical:
            # fire-once detectors get no second chance to stop the run.
            others = [a for a in anomalies if a.detector != LOOP_DETECTOR]
            if others and self._winner(others).severity == "critical":
                self._react(self._winner(others), session)
                return
            if self.config.is_graded_loop():
                self._react_graded(worst, session)
            else:
                self._react_to_loop(worst, session)
            return
        if worst.detector == BUDGET_DETECTOR and worst.severity != "critical":
            # The soft line is a notice under the wall, never a stop —
            # whatever on_anomaly says, and it latches nothing.
            _LOG.warning("[runbound] %s", worst.message)
            return
        if worst.detector == SPIKE_DETECTOR:
            if worst.severity != "critical":
                # A heightened watch is a notice, never a stop: one odd model
                # call must not take down a service, whatever on_anomaly says.
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
        if worst.severity != "critical":
            # ``on_anomaly`` is the reaction to a *critical* anomaly: a
            # warn-severity winner (``velocity``, say) is a notice, never a
            # stop, whatever the mode says, and it latches nothing. The
            # budget soft line and the spike watch above are the same rule
            # with their own wording.
            _LOG.warning("[runbound] %s", worst.message)
            return
        self._react(worst, session)

    def _spike_stops(self, anomaly: Anomaly) -> bool:
        """Does this critical spike stop the session, or only page someone?

        It stops when the user opted in — ``on_spike="trip"``, or the ladder's
        ``"limit"``, whose rollover and cooldown are served by the latch — and
        whenever an explicit per-call cap was breached, because a cap is a
        limit somebody stated rather than a learned baseline.
        """
        if self._effective_config().on_spike in ("trip", "limit"):
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

        Only failures that say the *next* call cannot succeed count — the
        provider's own (a 503, a timeout) and the network's (a reset, a refused
        connection, DNS, TLS). A 400 is our request being wrong and a
        ``TypeError`` is a bug in the caller's code; opening a circuit over
        either would stop calls to a provider that is answering perfectly, and
        a cancelled call did not fail at all. See
        :func:`~runbound.circuit.classify_failure`, whose verdict travels on
        the anomaly as ``details["fault"]`` so an operator can see why the
        circuit opened.

        Alerting happens under both modes — an open circuit is news whether or
        not the customer asked us to act on it — and exactly once, because one
        outage is one incident.

        Never raises: the circuit is an optimization on top of the host's own
        error handling, and a bug in it must not replace the provider's
        exception with ours.
        """
        try:
            fault = classify_failure(exc)
            if fault not in CIRCUIT_FAULTS:
                return
            self._sync_circuit_from_controls()
            quota = self._read_quota(exc)
            if self.circuit.record_failure(provider):
                cooldown = self._retry_after_hold(provider, quota)
                self._announce_circuit(
                    session,
                    provider,
                    fault,
                    reason="failures",
                    failures=self.config.circuit_failure_threshold,
                    cooldown_s=cooldown,
                )
                return
            self._open_on_quota(session, provider, quota)
        except Exception:
            _LOG.warning(
                "runbound could not update the circuit for provider %r",
                provider,
                exc_info=True,
            )

    def note_quota(self, provider: str, headers: Any) -> None:
        """Read a response's quota headers and pre-emptively open if spent.

        No-op unless ``config.circuit_reads_quota``. Never raises: a bug
        here must not replace a provider's answer with ours.

        Called from a wrapper's success path, which has no session to hand
        over — and needs none: a circuit belongs to a provider, not to
        whichever run happened to make the call, and the anomaly is deduped
        by provider for exactly that reason. ``headers`` is what
        :func:`~runbound.quota.headers_of` found on the response, which is
        ``None`` for the plain parsed model an ordinary call returns; a
        wrapper only calls this when there was something to read.
        """
        try:
            if not self.config.circuit_reads_quota:
                return
            self._open_on_quota(None, provider, read_quota(headers))
        except Exception:
            _LOG.warning(
                "runbound could not read the quota headers for provider %r",
                provider,
                exc_info=True,
            )

    def _read_quota(self, exc: BaseException) -> Quota:
        """What the failed response's headers said, or nothing at all.

        ``Quota()`` — every field ``None`` — whenever the customer has not
        opted in, the exception carries no readable headers (a plain
        ``TimeoutError`` never does), or reading them goes wrong.
        """
        try:
            if not self.config.circuit_reads_quota:
                return Quota()
            headers = headers_of(exc)
            return Quota() if headers is None else read_quota(headers)
        except Exception:
            _LOG.debug("runbound could not read a failure's headers", exc_info=True)
            return Quota()

    def _retry_after_hold(self, provider: str, quota: Quota) -> float:
        """Re-time an opening the provider itself put a clock on.

        A 429 says when to come back. When it does, that beats the configured
        ``circuit_cooldown_seconds`` guess — capped at
        :data:`~runbound.quota.MAX_COOLDOWN_S`, because no header gets to hold
        a provider shut for a day. Returns the cooldown now in force, so the
        anomaly reports the number that is actually being used.
        """
        cooldown = self.config.circuit_cooldown_seconds
        if quota.retry_after_s is None:
            return cooldown
        hold = min(float(quota.retry_after_s), MAX_COOLDOWN_S)
        self.circuit.force_open(provider, hold)
        return hold

    def _open_on_quota(
        self, session: "SessionState | None", provider: str, quota: Quota
    ) -> None:
        """Open ``provider`` pre-emptively when its headers said it is spent.

        ``Retry-After`` wins over the reset when both are readable: on a 429
        it is the provider's own instruction, and the bucket's reset is only
        the calendar. A ``remaining`` of ``None`` — no readable header, a
        proxy that stripped them — opens nothing.
        """
        deadline = quota.retry_after_s if quota.retry_after_s is not None else quota.reset_s
        if not self.circuit.note_quota(provider, quota.remaining, deadline):
            return
        self._announce_circuit(
            session,
            provider,
            PROVIDER,
            reason="quota",
            failures=0,
            cooldown_s=cooldown_for(deadline, self.config.circuit_cooldown_seconds),
        )

    def _announce_circuit(
        self,
        session: "SessionState | None",
        provider: str,
        fault: str,
        *,
        reason: str,
        failures: int,
        cooldown_s: float,
    ) -> None:
        """Alert, report and log one circuit that has just opened. Once."""
        anomaly = self._circuit_anomaly(
            provider, fault, reason=reason, cooldown_s=cooldown_s
        )
        self._alert(anomaly, session)
        self._report_circuit(provider, "open", failures)
        _LOG.warning("[runbound] %s", anomaly.message)

    def _circuit_anomaly(
        self,
        provider: str,
        fault: str,
        *,
        reason: str = "failures",
        cooldown_s: float | None = None,
    ) -> Anomaly:
        """Describe a circuit that has just opened, in the mode it opened in.

        ``fault`` is the class of the failure that opened it — ``"provider"``
        or ``"transport"`` — and rides along in the details, because "the
        provider is answering 503" and "we cannot reach the provider" are
        different incidents with different first moves.

        ``reason`` says what opened it: ``"failures"``, the count reaching the
        threshold, or ``"quota"``, the provider's own headers saying
        the next call is going to be refused. ``cooldown_s`` is how long it is
        actually shut for, which is the configured cooldown unless a 429's
        ``Retry-After`` or a bucket's reset replaced it.

        Nothing a header *said* appears here — only numbers derived from it.
        A header's text never leaves the process.
        """
        config = self.config
        blocking = config.on_provider_failure == "open"
        tail = (
            " — calls fail fast until it recovers"
            if blocking
            else " (notify only: calls continue)"
        )
        cooldown = config.circuit_cooldown_seconds if cooldown_s is None else cooldown_s
        cause = (
            f"{config.circuit_failure_threshold} failures in "
            f"{config.circuit_window_seconds:.0f}s"
            if reason == "failures"
            else "the provider reports no quota left"
        )
        return Anomaly(
            detector=CIRCUIT_DETECTOR,
            severity="critical",
            message=(
                f"Provider {provider!r} circuit opened: {cause}; "
                f"cooling down {cooldown:.0f}s{tail}"
            ),
            details={
                "provider": provider,
                "host": provider_host(provider),
                "failures": config.circuit_failure_threshold if reason == "failures" else 0,
                "window_seconds": config.circuit_window_seconds,
                "cooldown_seconds": cooldown,
                "on_provider_failure": config.on_provider_failure,
                "state": "open",
                "fault": fault,
                "reason": reason,
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
            self._sync_circuit_from_controls()
            return bool(self.circuit.allow(provider))
        except Exception:
            _LOG.warning(
                "runbound could not read the circuit for provider %r; allowing the call",
                provider,
                exc_info=True,
            )
            return True

    def refuse(
        self,
        session: "SessionState | None",
        anomaly: Anomaly,
        decision: Decision,
        *,
        exc: type = GuardrailTripped,
        reacted: str | None = None,
        latch: bool = False,
        provider_called: bool = False,
        **exc_kwargs: Any,
    ) -> None:
        """The single exit for a refusal site: stamp, alert, latch, raise.

        Every admission stage ends here — the circuit, unpriced
        model, money/reservation, posture, capability, policy, and the new
        steps/run-time/tokens/actions door stages all call this to raise.
        ``decision`` is stamped onto ``anomaly.details["decision"]`` via
        :func:`_stamp_decision`, which also folds ``provider_called`` into
        ``decision.evaluation`` and this session's key hash into
        ``details["key_hash"]`` — what ``exc.provider_called`` and
        ``exc.scope`` read (see :mod:`runbound.exceptions`). ``provider_called``
        defaults ``False`` because every site that calls this raises *before*
        the provider is ever touched; the one exception (a post-call budget
        crossing) does not go through here at all — see
        :class:`~runbound.detectors.BudgetDetector`. :func:`_stamp_decision`
        keeps the anomaly's own ``anomaly_id`` rather than minting a new one,
        since ``anomaly_id`` is a defaulted field and
        ``dataclasses.replace`` copies the value already on the instance. The
        observers are told with ``reacted`` (a broken observer, or a bug in
        ``_alert`` itself, is logged and swallowed — a refusal must never be
        lost to a broken alerter); ``latch`` stops the session first when this
        is one of the deterministic envelope denies that latches "exactly as
        the wall would have" (steps, run time, blast radius, a stopped
        posture set by the abuse ladder's own closed rung); then ``exc`` (a
        :class:`GuardrailTripped` subclass) is raised with the stamped
        anomaly and any extra constructor arguments (``violation=``,
        ``provider=``). Never returns.

        Two shipped sites stay outside it on purpose, not by oversight:
        :meth:`_admit_circuit` (see its own docstring — routing it through
        here would provably add no new alert) and :meth:`enforce_policy`'s
        dry-run branch, which structurally cannot call something that
        always raises, because a dry run never refuses anything.
        """
        stamped = _stamp_decision(anomaly, decision, session=session, provider_called=provider_called)
        if latch and session is not None:
            self._latch(session, stamped)
        try:
            self._alert(stamped, session, reacted, refusal=True)
            _LOG.warning("[runbound] %s", stamped.message)
        except Exception:
            _LOG.warning(
                "runbound could not alert on a refusal; the refusal still stands",
                exc_info=True,
            )
        raise exc(stamped, **exc_kwargs)

    def _react_at_door(
        self, session: SessionState, anomaly: Anomaly, decision: Decision, *, latches: bool
    ) -> None:
        """Apply ``on_anomaly`` to a door stage standing in front of a
        pre-existing wall (steps, run time, tokens).

        These three stages are not a new control the customer opted into;
        they are the *same* ``max_steps``/``max_session_seconds``/
        ``max_total_tokens`` walls, checked one call earlier. So only
        **"raise"** actually refuses here, via :meth:`refuse`
        (``reacted="door"``, ``latches`` — the boundary's own, separate
        decision: steps and run time latch, tokens never does). **"warn" and "callback" both let the call through** and only
        alert (``reacted="warn"``) — neither latches and neither invokes the
        customer's callback *here*.

        That last part is deliberate, not an oversight: the call this door
        let through still gets its own event recorded and processed in the
        ordinary way, and the wall behind this door (`StepDetector` /
        `TimeoutDetector` / the tokens half of `BudgetDetector`) will detect
        the very same crossing on that event, for real, and react to it —
        latching and invoking the callback under `"callback"`, or just
        logging under `"warn"` — exactly once, exactly as it always has. If
        this method also latched or invoked the callback, that reaction
        would happen *twice* for one crossing: once here, from a projection,
        and again when the session's own latch makes the call's event
        replay through :meth:`_reapply` instead of fresh detection. Letting
        the wall be the only thing that ever latches or calls back is what
        "exactly as if the wall had fired one call later" actually means for
        the call sitting at the door. The door's
        alert here is still real, distinct news (a stated cap or an
        already-expired clock projects a crossing before it is confirmed),
        which is why it still reports one — with ``rule="envelope"`` so it
        never dedupes away the wall's own, separate anomaly when that fires.

        A plane Controls override on this anomaly's detector — see
        :meth:`controls_detector_override` — is checked ahead of
        ``on_anomaly`` too, in both directions: "notify" or shadow-"stop"
        means the door itself must never refuse, whatever ``on_anomaly``
        says, because the wall behind it will not latch on this crossing
        either; a plane "stop" refuses right here even under
        ``on_anomaly="warn"``/``"callback"``.
        """
        override = self.controls_detector_override(anomaly.detector)
        refuses = override == "stop" or (self.config.on_anomaly == "raise" and override is None)
        if refuses:
            self.refuse(session, anomaly, decision, exc=GuardrailTripped, reacted="door", latch=latches)
            return  # pragma: no cover - refuse() always raises
        stamped = _stamp_decision(anomaly, decision, session=session)
        try:
            reacted = override if override in ("warn", "dry_run") else "warn"
            self._alert(stamped, session, reacted)
            _LOG.warning("[runbound] %s", stamped.message)
        except Exception:
            _LOG.warning(
                "runbound could not alert at the door; the call still proceeds "
                "and the wall behind it still watches",
                exc_info=True,
            )

    def admit(
        self,
        session: SessionState,
        provider: str | None = None,
        model: str | None = None,
        request: dict | None = None,
        *,
        kind: str = "model_call",
        tool: str | None = None,
        effects: frozenset | None = None,
        call: Any = None,
        decorated: bool = False,
    ) -> "Hold | None":
        """One request through the door: ``kind="model_call"`` or ``"action"``.

        ``kind="action"`` (a decorated tool call) runs the action stages —
        posture, then a capability class rule, then (``config.envelope``
        only) the blast-radius/``max_actions_per_run`` cap — via
        :meth:`_admit_action`, and never returns a hold. ``tool``, ``effects``
        and ``decorated`` are that call's own.

        ``call`` is **reserved and unused** — the signature names it for a
        possible future policy stage, but the tool policy is not folded
        into ``admit`` yet (it stays :meth:`enforce_policy`, called by
        :func:`~runbound.api._admit_action` right after this method
        returns, on the same :class:`~runbound.policy.ToolCall` that caller
        already built). Nothing in this module reads ``call``; do not go
        looking for its call site.

        ``kind="model_call"`` (the default, and the only kind every existing
        caller of ``admit`` used before the action stages existed) is
        unchanged in spirit:

        Posture first — before even the circuit: while the effective posture is
        ``stopped`` no provider is ever touched, whatever else is configured
        (see :meth:`_admit_stopped`). Then circuit, unpriced model, in-flight
        cap, then — when ``config.budget_admission`` — the budget estimate.
        Raises ``GuardrailTripped``/``CircuitOpen``; the posture stage latches
        only when the ladder's closed rung is what set ``stopped`` (it is
        already latched by the anomaly that closed it) and never on a manual
        or plane-directed one; every other admission refusal here still never
        latches.

        This gives a name to what already existed in embryo inside
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

        The money hold ``_admit_budget`` may take is the *last* stage,
        by design: authorization runs before reservation (INVARIANTS.md)
        says every cheap, deterministic check runs before any resource is
        held, so there
        is nothing after this one for a hold to be released *for* — a call
        that gets this far either is refused with nothing held, or is let
        through holding exactly the money it might cost. Returns ``None``
        when nothing was held (no budget, an uncapped call under
        ``"capped"``, ``budget_admission`` off, an unpriced model). The
        caller — ultimately :meth:`~runbound.api._Hooks.before` — owns the
        hold from here and must give it back on every close-out path this
        call can take, however it ends.

        The envelope inserts three ``config.envelope``-gated door stages
        between the unpriced check and the money hold: steps, run time,
        then tokens (the same order — cheap and deterministic before
        anything that holds a resource). Each is independently fail-open
        like every other phase here. ``envelope=False`` skips all three and
        this method is then byte-for-byte the sequence without them.
        """
        if kind == "action":
            self._admit_action(session, tool, effects, decorated)
            return None
        self._admit_stopped(session)
        self._admit_circuit(provider, session)
        self._admit_unpriced(session, model)
        if self._effective_envelope():
            self._admit_steps(session)
            self._admit_run_time(session)
            self._admit_tokens(session, model, request)
        if self.config.budget_admission:
            return self._admit_budget(session, model, request)
        return None

    def _admit_action(
        self,
        session: "SessionState | None",
        tool: str | None,
        effects: "frozenset | None",
        decorated: bool,
    ) -> None:
        """The action stages: posture, a capability class rule, blast radius.

        Only a *decorated* tool declares effects, so an undecorated call
        (there is currently no such caller of ``kind="action"``, but the
        signature allows one) skips posture and the class rule outright — the
        same guard :meth:`~runbound.api._admit_action` already applied
        before this stage was folded in here. ``max_actions_per_run`` (the
        envelope's own boundary) applies to every action either way, and
        only under
        ``config.envelope`` — it is a new control, silent unless both it and
        the envelope are configured.

        Reading the posture is fail-open, exactly as it was in the api's own
        ``_admit_posture`` before this stage moved here: a bug judging the
        action logs and lets the call through. The refusals themselves (:meth:`refuse_by_posture`,
        :meth:`_admit_actions`) are raised on purpose.
        """
        if session is None:
            return
        if decorated:
            try:
                judgment = self.judge_action(session, effects or frozenset())
            except Exception:
                _LOG.warning(
                    "runbound could not read the posture for %r; the call was allowed",
                    tool,
                    exc_info=True,
                )
                judgment = None
            if judgment is not None and judgment["verdict"] != "allow":
                self.refuse_by_posture(session, tool or "<tool>", effects or frozenset(), judgment)
        if self._effective_envelope():
            self._admit_actions(session)

    def _admit_stopped(self, session: "SessionState | None") -> None:
        """The first admission phase for a model call: is the run ``stopped``?

        Every other posture (``restricted``, ``read_only``,
        ``no_side_effects``) keeps serving model calls — only a tool's
        capability classes are judged by them, which is what
        :meth:`_admit_action` already enforces. ``stopped`` is different: the
        posture table says "the run is latched; nothing runs", so a model
        call must never reach the provider while the *effective* posture
        (session, process, plane and halt, tightened together) is
        ``stopped`` — by hand, by the abuse ladder's closed rung, or by a
        connected control plane. This is the reported bug's own reproduction: a
        process put in ``stopped`` still served a model call, because no
        admission stage ever asked.

        Raises :class:`SafeModeViolation` is *not* used here on purpose — a
        model call carries no capability classes to name, so this is a plain
        :class:`GuardrailTripped` with ``detector="safe_mode"``,
        ``boundary="posture"``, the same vocabulary
        :meth:`refuse_by_posture` uses for a tool. It latches only when the
        ladder's own closed rung is what set ``stopped``: that path already
        latched the session the moment the ladder closed it (the critical
        anomaly that did so went through the ordinary :meth:`_react`/
        :meth:`_latch` path), so this call latching too is consistent with,
        not a change to, that existing behavior. A manual
        (``session.enter_safe_mode``/``runbound.enter_safe_mode``) or a
        plane-directed ``stopped`` never latches here: lifting it is exactly
        what un-stops the run, and a local latch would survive that lift.

        Reading the posture is fail-open like every other admission phase: a
        bug here must cost this call nothing but a warning and let it
        through, never refuse a call for a reason of our own making.
        """
        if session is None:
            return
        try:
            effective = self.effective_posture(session)
            is_stopped = effective.name == "stopped"
            if not is_stopped:
                return
            state = self.posture_state(session)
            source = state.source if state is not None else "posture"
            reason = state.reason if state is not None else effective.name
        except Exception:
            _LOG.warning(
                "runbound could not read the posture before a model call; "
                "the call proceeds",
                exc_info=True,
            )
            return
        decision = admission.stopped(True, effective.name, source, reason)
        anomaly = _stopped_door_anomaly(session, decision, source, reason)
        self.refuse(
            session, anomaly, decision, exc=GuardrailTripped, reacted="door",
            latch=(source == "ladder"),
        )

    def _admit_circuit(self, provider: "str | None", session: "SessionState | None" = None) -> None:
        """The first admission phase: is this provider's circuit open?

        Ported from the api's own ``_Hooks.before`` unchanged: only
        ``on_provider_failure="open"`` ever refuses here (see
        :meth:`circuit_allows`), and building the refusal's anomaly is
        covered by the same fail-open as reading the circuit itself — a
        broken describer must not block a call the breaker would have let
        through.

        Stays outside :meth:`refuse` on purpose — but not for the reason an
        earlier draft of this comment gave (that routing it through
        :meth:`refuse` would page once per refused call): ``_alert`` dedupes
        a circuit anomaly on ``(detector, provider)`` with the session id
        dropped, so in practice it would add **at most one** extra alert per
        provider per Engine, and empirically zero — a circuit can only be
        open here because :meth:`_announce_circuit` already claimed that
        exact key when it opened, so :meth:`refuse`'s own alert call would
        always be deduped away. The real reason to leave this call
        (`Decision`-stamped via :func:`_stamp_decision`, never alerted to
        *external* observers here) outside :meth:`refuse` is narrower:
        :meth:`refuse` is the exit for *new* refusal sites this task adds,
        and changing an already-shipped site's external-alerting path for a
        change that provably alerts nothing new is churn without a
        behavior to point to, not a fix.

        The local ring is a different question from external
        alerting, though (every denial must be explainable from its own
        Decision) — a circuit refusal
        never reaches an observer via ``_alert``, and skipping
        ``local_events`` too would silently leave the one denial kind this
        module raises without going through :meth:`refuse` invisible to
        ``runbound.events()``/``decisions()``. So this records the stamped
        anomaly and its Decision directly, once, right before raising —
        the same fail-open :meth:`_notify_anomaly` gives every other
        anomaly, without the duplicate-observer-alert :meth:`refuse` would
        add.

        Also where a half-open breaker is noticed for ``circuit_posture``
        (see :meth:`_sync_circuit_posture`) — checked here, ahead of
        ``circuit_allows``, so the posture reflects a half-open circuit
        whether ``on_provider_failure`` is ``"notify"`` (which never even
        reads the breaker below) or ``"open"``. Skipped entirely with
        ``circuit_posture`` off, which is the default: a process that never
        opted in must not pay for reading a state it will not use.
        """
        if provider is not None and self._effective_circuit_posture():
            try:
                self._sync_circuit_posture(provider, self.circuit.state(provider))
            except Exception:
                _LOG.warning(
                    "runbound could not read the circuit state for %r; the call proceeds",
                    provider,
                    exc_info=True,
                )
        try:
            allowed = self.circuit_allows(provider)
            if allowed:
                return
            circuit_state = self.circuit.state(provider)
            decision = admission.circuit(
                False, provider, circuit_state, self.config.circuit_cooldown_seconds
            )
            anomaly = _stamp_decision(self._circuit_open_anomaly(provider), decision, session=session)
        except Exception:
            _LOG.warning(
                "runbound could not check the circuit for %r; the call proceeds",
                provider,
                exc_info=True,
            )
            return
        try:
            # Snapshotted here, not inside CircuitOpen.retry_after
            # itself -- a bug reading it must cost this refusal nothing but
            # a None retry_after, never the refusal.
            retry_snapshot = self.circuit.retry_snapshot(provider)
        except Exception:
            retry_snapshot = None
        try:
            local_events.record_anomaly(getattr(session, "session_id", None), anomaly, "raise")
        except Exception:
            _LOG.warning(
                "runbound could not record a local event for a circuit refusal",
                exc_info=True,
            )
        raise CircuitOpen(anomaly, provider, retry_snapshot)

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
            decision = admission.unpriced(False, model)
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
        self.refuse(session, anomaly, decision, exc=GuardrailTripped, reacted="door")

    def _admit_budget(
        self, session: SessionState, model: str | None, request: dict | None
    ) -> "Hold | None":
        """The opt-in phase: would this call's estimated cost cross the budget?

        Estimate = ``estimated_tokens(chars of the request's messages)`` at
        the model's input rate, plus the request's own output-token cap (else
        ``config.admission_output_tokens``) at its output rate — the same
        price table :mod:`runbound.pricing` prices the call with after the
        fact — computed by :func:`_admission_worst_case`, pure arithmetic that
        touches no lock. A no-op (returns ``None``, holds nothing) without
        ``budget_usd`` — there is nothing to estimate against — and for an
        unpriced model, warned once per model per Engine rather than refused:
        inventing a limit the customer never set is worse than skipping this
        one opt-in check for a call the post-call wall still watches.

        Remaining = ``budget_usd`` minus what this session (and the rest of
        the fleet, via ``spend_offset_usd``) has already settled, minus what
        this worker already has reserved. That read and the compare
        against the estimate happen in one ``session.lock`` section together
        with taking the hold itself — ``session.hold()`` may be called while
        already holding ``session.lock`` because it is an ``RLock`` — so no
        second call's admission can land between this one's compare and its
        write. A passing estimate returns the :class:`~runbound.state.Hold`
        it took, whether the cap came from ``budget_admission="capped"`` (a
        stated cap) or ``budget_admission=True`` (the assumed
        ``admission_output_tokens`` cap); under ``"capped"`` with no stated
        cap, nothing is checked and nothing is held, exactly as the
        original budget_admission check did. The
        caller (:meth:`admit`, ultimately :meth:`~runbound.api._Hooks.before`)
        owns giving the hold back.

        Never latches (see :meth:`admit`): raises straight from here, never
        through :meth:`_react`/:meth:`_latch`.

        Run and key budgets: with ``config.run_budget_usd`` also (or only)
        set, ``estimate`` is checked against whichever of the run's own
        remaining (``run_budget_usd - run_cost_usd``, reset fresh on every
        :func:`~runbound.api.session` entry) and the key's remaining (the
        pre-existing computation above, now against a possibly windowed
        ``total_cost_usd`` — see :meth:`~runbound.state.SessionState.
        roll_budget_window`) is *tighter* — the same money reservation
        (``session.reserved``) covers both, since it is the same dollar
        either way. ``Decision.level`` names which one actually bound the
        call, ``"run"`` or ``"key"``, so a customer reading the refusal is
        never left guessing which budget to raise.
        """
        config = self.config
        budget_usd = self._limit("budget_usd")
        run_budget_usd = config.run_budget_usd
        if budget_usd is None and run_budget_usd is None:
            return None
        stated_cap = _admission_output_cap(request)
        reserving = config.budget_admission == "capped"
        if reserving and stated_cap is None:
            # No stated cap, nothing exact to reserve against. The
            # post-call wall is the only check, exactly as in 0.3.0.
            return None
        try:
            price = price_for(model, config.custom_prices)
            if price is None:
                self._warn_admission_unpriced(model)
                return None
            estimate = _admission_worst_case(
                price, stated_cap, config.admission_output_tokens, request
            )
            with session.lock:
                if budget_usd is not None:
                    session.roll_budget_window(config.budget_window)
                reserved = session.reserved.get("usd", 0.0)
                key_remaining = (
                    None
                    if budget_usd is None
                    else budget_usd
                    - (session.total_cost_usd + session.spend_offset_usd)
                    - reserved
                )
                run_remaining = (
                    None
                    if run_budget_usd is None
                    else run_budget_usd - session.run_cost_usd - reserved
                )
                remaining, level, limit = _tighter_budget(key_remaining, run_remaining, budget_usd, run_budget_usd)
                if estimate <= remaining:
                    return session.hold("usd", estimate)
            decision = admission.money(
                estimate, remaining, limit=limit, reserved=reserved, level=level
            )
            if reserving:
                anomaly = _reservation_anomaly(
                    session, model, stated_cap, estimate, remaining, reserved, limit, level
                )
            else:
                anomaly = _admission_anomaly(
                    session, model, estimate, remaining, reserved, budget_usd
                )
        except Exception:
            _LOG.warning(
                "runbound could not estimate the admission cost for model %r; "
                "the call proceeds",
                model,
                exc_info=True,
            )
            return None
        self.refuse(session, anomaly, decision, exc=GuardrailTripped, reacted="door")

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

    # --- the envelope's own door stages (config.envelope only) ------------

    def _admit_steps(self, session: SessionState) -> None:
        """Stand in front of ``StepDetector``, one call earlier.

        Snapshots ``turns`` under ``session.lock`` once and hands it to the
        pure :func:`admission.steps`, which reports the same ``turns`` number
        the wall's own anomaly would carry. This is not a new control — it is
        ``max_steps`` itself, checked before the offending call instead of
        after — so the reaction is :meth:`_react_at_door`: "raise" refuses
        and latches here; "warn" and "callback" both only alert and let the
        call through, leaving the wall itself to detect the same crossing on
        this call's own event and react (including invoking the customer's
        callback) exactly once. Fail-open like every other admission phase.
        """
        try:
            max_steps = self._limit("max_steps")
            with session.lock:
                turns = session.turns
            decision = admission.steps(turns, max_steps)
        except Exception:
            _LOG.warning(
                "runbound could not check the step limit at the door; the call proceeds",
                exc_info=True,
            )
            return
        if decision is ALLOW:
            return
        anomaly = _steps_door_anomaly(session, decision, max_steps)
        self._react_at_door(session, anomaly, decision, latches=True)

    def _admit_run_time(self, session: SessionState) -> None:
        """Stand in front of ``TimeoutDetector``'s run-scoped clock.

        Mirrors ``run_started_at``, checked one call earlier — see
        :func:`admission.run_time`. Not a new control, so :meth:`_react_at_door`
        applies: "raise" refuses and latches here; "warn"/"callback" only
        alert and let the call through, leaving the wall to react once the
        call's own event confirms the same crossing. The lifetime-scoped
        clock (``max_session_lifetime_seconds``) is not checked at the door:
        it is the identity-scoped, opt-in wall for a customer who wants the
        old meaning back, not a per-call gate.
        """
        try:
            with session.lock:
                run_started_at = session.run_started_at
            elapsed = _monotonic() - run_started_at
            decision = admission.run_time(elapsed, self.config.max_session_seconds)
        except Exception:
            _LOG.warning(
                "runbound could not check the run-time limit at the door; the call proceeds",
                exc_info=True,
            )
            return
        if decision is ALLOW:
            return
        anomaly = _run_time_door_anomaly(session, decision)
        self._react_at_door(session, anomaly, decision, latches=True)

    def _admit_tokens(
        self, session: SessionState, model: str | None, request: dict | None
    ) -> None:
        """Stand in front of the tokens half of ``BudgetDetector``.

        Only a request that states its own cap can be checked exactly (see
        :func:`admission.tokens`); an uncapped call is not checked here at
        all, exactly like the money reservation's own ``"capped"`` shape.
        Not a new control, so :meth:`_react_at_door` applies ``on_anomaly``
        like the wall does — but never latches regardless of which branch
        runs (CONTROLS §2.5): a projection from a stated cap says nothing
        about the next call, the same reasoning that keeps the money
        estimate from latching.
        """
        try:
            config = self.config
            if config.max_total_tokens is None:
                return
            stated_cap = _admission_output_cap(request)
            with session.lock:
                total_tokens = session.total_tokens + session.tokens_offset
            decision = admission.tokens(total_tokens, stated_cap, config.max_total_tokens)
        except Exception:
            _LOG.warning(
                "runbound could not check the token limit at the door; the call proceeds",
                exc_info=True,
            )
            return
        if decision is ALLOW:
            return
        anomaly = _tokens_door_anomaly(session, model, decision)
        self._react_at_door(session, anomaly, decision, latches=False)

    def _admit_actions(self, session: SessionState) -> None:
        """Refuse the ``(max_actions_per_run + 1)``\\ th *executed* tool action.

        Read *before* this attempt is counted as anything (
        ``session.executed_actions`` only rises once admission and the
        action policy have both let an attempt through — see
        :meth:`~runbound.api._admit_action` and
        :meth:`~runbound.state.SessionState.mark_action_admitted`), so this
        stage's own refusal never counts itself as having executed — see
        :func:`admission.actions`. Unlike steps/run-time/tokens above,
        ``max_actions_per_run`` is a brand-new control with no prior wall to
        stay faithful to, so it always refuses through :meth:`refuse`
        directly rather than :meth:`_react_at_door` — there is no
        "``on_anomaly='warn'`` used to mean nothing was ever refused" promise
        to keep here. Latches like the other deterministic envelope denies.
        """
        try:
            with session.lock:
                executed = session.executed_actions
            decision = admission.actions(executed, self._effective_max_actions_per_run())
        except Exception:
            _LOG.warning(
                "runbound could not check the action limit at the door; the call proceeds",
                exc_info=True,
            )
            return
        if decision is ALLOW:
            return
        anomaly = _actions_door_anomaly(session, decision)
        self.refuse(session, anomaly, decision, exc=GuardrailTripped, reacted="door", latch=True)

    def record_llm_success(self, provider: str, duration_s: float = 0.0) -> None:
        """Report a model call that worked: ``provider``'s circuit closes.

        A circuit that was not closed and now is, is a transition the fleet
        wants to hear about. Without a control plane the state is not even
        read: a healthy call must cost nothing it did not cost before.

        ``duration_s`` only matters under ``circuit_mode="rate"`` with
        ``circuit_slow_call_seconds`` set: a call slower than that,
        however it ended, is marked ``slow`` for the breaker's rate window,
        even though it succeeded. Count mode never reads either knob, so a
        slow call there costs exactly what it always did: nothing extra.
        """
        try:
            self._sync_circuit_from_controls()
            before = self.circuit.state(provider) if self._reports_circuits else "closed"
            slow = self._is_slow_call(duration_s)
            self.circuit.record_success(provider, slow=slow)
            if before != "closed":
                self._report_circuit(provider, "closed", 0)
            if self._effective_circuit_posture():
                self._sync_circuit_posture(provider, self.circuit.state(provider))
        except Exception:
            _LOG.warning(
                "runbound could not close the circuit for provider %r",
                provider,
                exc_info=True,
            )

    def _is_slow_call(self, duration_s: float) -> bool:
        """Does ``duration_s`` cross ``circuit_slow_call_seconds``?

        Always ``False`` outside ``circuit_mode="rate"`` or with no
        ``circuit_slow_call_seconds`` configured, so count mode's behavior
        never changes by so much as a comparison it did not make before.
        """
        rate = self._effective_circuit_rate()
        if rate is None or rate.get("slow_call_seconds") is None:
            return False
        return float(duration_s) > rate["slow_call_seconds"]

    def _sync_circuit_posture(self, provider: str, state: str) -> None:
        """Track a half-open circuit against the process posture.

        Gated on ``circuit_posture`` (off by default). Half-open narrows the
        *process* to "restricted" (source ``"circuit"`` in
        :data:`runbound.state.POSTURE_SOURCES`); a full close lifts that same
        source and nothing else, exactly like every other posture source
        (what opens must close, and closing must leave a manual, ladder,
        plane or halt narrowing standing).
        Deliberately does nothing while merely ``"open"`` — a failed probe's
        fresh cooldown reads as ``"open"`` again, and re-entering nothing
        there is what keeps this from flapping the posture on every retry;
        the restriction, once set at half-open, simply stays set until a
        real close lifts it.

        There is no per-provider *tool* scoping in this codebase — tools
        declare capability classes (``effects=``), never a provider — so
        "restricted for that provider's tools" is honoured the only way the
        posture model actually can: process-wide, the same mechanism the
        ladder, a manual call, and the plane all share.

        Never raises: a bug here must not touch the call it was reporting.
        """
        if not self._effective_circuit_posture():
            return
        try:
            if state == "half_open":
                self.enter_safe_mode(
                    f"provider {provider!r} circuit half-open",
                    "restricted",
                    source="circuit",
                )
            elif state == "closed":
                self.exit_safe_mode(source="circuit")
        except Exception:
            _LOG.warning(
                "runbound could not sync the circuit posture for provider %r",
                provider,
                exc_info=True,
            )

    # --- postures ------------------------------------------------------------

    def resolve_posture(self, name: str) -> Posture:
        """The posture called ``name``, with ``init(postures=...)`` applied.

        Raises ``ValueError`` for a name nothing defines, which is how
        :func:`runbound.enter_safe_mode` refuses a typo at the door.
        """
        return posture_module.resolve(name, getattr(self.config, "postures", None))

    def enter_safe_mode(
        self, reason: object = "manual", name: str = "restricted", source: str = "manual"
    ) -> bool:
        """Narrow the whole process to ``name``. True if this call changed it.

        Same precedence as a session's: a manual entry replaces an automatic
        one, an automatic entry never replaces a manual one.
        """
        self.resolve_posture(name)  # refuse an unknown posture before storing it
        state = make_posture_state(name, reason, source)
        with self._posture_lock:
            current = self._posture
            if current is not None and (current.source == "manual" or source != "manual"):
                return False
            self._posture = state
        local_events.record_posture(
            source,
            name,
            reason,
            previous=None if current is None else current.name,
            scope="process",
        )
        return True

    def exit_safe_mode(self, source: str | None = None) -> bool:
        """Put the process back to ``full``; with ``source``, only that entry."""
        with self._posture_lock:
            current = self._posture
            if current is None or (source is not None and current.source != source):
                return False
            self._posture = None
        local_events.record_posture(
            current.source, "full", "exit_safe_mode", previous=current.name, scope="process"
        )
        return True

    def process_posture(self) -> "PostureState | None":
        """The process's own narrowing, or ``None``."""
        with self._posture_lock:
            return self._posture

    def plane_posture(self) -> "PostureState | None":
        """The posture the plane states for this service, or ``None`` with no plane."""
        read = getattr(self.shared, "posture_directive", None)
        return None if read is None else read()

    def halt_posture(self) -> "PostureState | None":
        """The posture a fleet-wide Narrow halt states, or ``None``.

        Gated on ``on_halt``, the halt's own local knob: ``"raise"`` (the
        default) honours it, same as any other plane posture. ``"warn"``
        never installs it at all — the *whole* posture, not just this one
        read — so a worker configured "say it, keep serving" for a Stop
        halt gets exactly that for a Narrow one too: no ``SafeModeViolation``
        ever reaches code that asked never to be stopped by the kill switch
        (the same fix applied for a detector's own stop, at the tool door
        instead of the session door). Deliberately its own method, read
        independently of :meth:`plane_posture` (the plane's Controls-stated
        posture) in :meth:`effective_posture` — the two are
        separate plane sources that must tighten together and lift on
        their own; see :data:`runbound.state.POSTURE_SOURCES`'s ``"halt"``
        entry.
        """
        if self.config.on_halt != "raise":
            return None
        read = getattr(self.shared, "halt_posture_directive", None)
        return None if read is None else read()

    def posture_state(self, session: SessionState | None) -> "PostureState | None":
        """The local narrowing a refusal names: the session's own, else the
        process's, else the plane's Controls-stated one, else the halt's.

        For the message only. The posture an action is *judged* under is
        :meth:`effective_posture`, which tightens all four.
        """
        own = getattr(session, "posture", None) if session is not None else None
        return own or self.process_posture() or self.plane_posture() or self.halt_posture()

    def effective_posture(self, session: SessionState | None) -> Posture:
        """The posture an action on ``session`` is judged under.

        The session's own narrowing, the process's, the plane's Controls-
        stated one and a fleet-wide Narrow halt's, tightened
        together — the strictest wins, so a ladder narrowing one session can
        never loosen a process an operator narrowed further, and the plane
        can narrow a worker but never widen one. The last two are
        independent plane sources on purpose: lifting one
        must never lift the other. Class rules are *not* folded in here:
        they hold whatever the posture is, and folding them would rename it.
        """
        own = getattr(session, "posture", None) if session is not None else None
        effective = posture_module.FULL
        for state in (own, self.process_posture(), self.plane_posture(), self.halt_posture()):
            if state is not None:
                effective = posture_module.tighten(effective, self.resolve_posture(state.name))
        return effective

    def judge_action(self, session: SessionState | None, effects: frozenset) -> dict:
        """What may happen to a tool carrying ``effects``, and who decided.

        One verdict, from :meth:`Posture.allows` and the configured class
        rules, with the stricter of the two winning and naming itself — so a
        refusal can always say which class was denied and what denied it.
        """
        effective = self.effective_posture(session)
        verdict = effective.allows(effects)
        rules = posture_module.class_rules(self._effective_capabilities())
        rule_verdict = rules.allows(effects)
        state = self.posture_state(session)
        if posture_module.stricter(verdict, rule_verdict) == rule_verdict and rule_verdict != verdict:
            return {
                "verdict": rule_verdict,
                "denied_class": rules.denied_class(effects),
                "source": "class_rule",
                "posture": effective.name,
                "reason": "a capability class rule on init()",
            }
        return {
            "verdict": verdict,
            "denied_class": effective.denied_class(effects),
            "source": state.source if state is not None else "posture",
            "posture": effective.name,
            "reason": state.reason if state is not None else effective.name,
        }

    def refuse_by_posture(
        self, session: SessionState, tool: str, effects: frozenset, judgment: dict
    ) -> None:
        """Refuse a tool the posture or a class rule denies. Always raises.

        Raises :class:`SafeModeViolation`. Latches nothing and costs no strike:
        the run goes on with less autonomy, which is the whole point. The
        anomaly is ``warn``, once per session, and never carries arguments.
        """
        approval = judgment["verdict"] == "approve"
        denied = judgment["denied_class"]
        detail = (
            f"it needs approval for its {denied} capability, and this process has "
            "no approval queue"
            if approval
            else f"posture {judgment['posture']!r} denies its {denied} capability"
        )
        violation = Violation(
            tool,
            SAFE_MODE_DETECTOR,
            f"{detail} ({judgment['source']}: {judgment['reason']})",
            {
                "posture": judgment["posture"],
                "denied_class": denied,
                "effects": sorted(effects or ()),
                "verdict": judgment["verdict"],
                "reason": judgment["reason"],
                "source": judgment["source"],
            },
        )
        anomaly = _posture_anomaly(session, violation)
        if judgment["source"] == "class_rule":
            decision = admission.capability(judgment["verdict"], denied)
        else:
            decision = admission.posture(
                judgment["verdict"], denied, judgment["posture"], judgment["source"], violation.reason
            )
        self.refuse(session, anomaly, decision, exc=SafeModeViolation, reacted="blocked", violation=violation)

    # --- Controls --------------------------------------------------------
    #
    # The plane's Controls body, tightened against this worker's own
    # ``init()`` configuration — never the reverse. Everything below reads
    # ``self.shared.controls_directive()`` fresh each time (never pushed to
    # by the poller thread directly), which is what makes staleness free:
    # once the link goes quiet past the halt's own rule, the directive
    # reverts to ``None`` on its own and the very next read here falls back
    # to this worker's own configuration, unchanged. A malformed or partial
    # body degrades to "nothing stated" per field, never a crash and never
    # a looser worker (this control's own fail-open rule).

    def _controls_snapshot(self) -> tuple:
        """``(limits, capabilities, envelope, detectors, violations,
        circuit_rate, loop_shapes, budget_soft, max_actions_per_run,
        circuit_posture, spike_enabled, spike)``, every field tightened
        against this worker's own configuration (all of them are real,
        local values, not just the first three), recomputed only
        when the plane's Controls body has actually changed (a cheap
        identity check, not a deep compare — see ``_controls_last_body``).
        See :meth:`_merge_controls`.
        """
        body = None
        read = getattr(self.shared, "controls_directive", None)
        if read is not None:
            try:
                body = read()
            except Exception:
                _LOG.warning(
                    "runbound: could not read the control plane's Controls; "
                    "keeping this worker's own configuration",
                    exc_info=True,
                )
        with self._controls_lock:
            if body is self._controls_last_body:
                return self._controls_cache
            cache = self._merge_controls(body)
            self._controls_last_body = body
            self._controls_cache = cache
            return cache

    def _merge_controls(self, body: Any) -> "_ControlsSnapshot":
        """The pure part of :meth:`_controls_snapshot`: one Controls body in,
        a :class:`_ControlsSnapshot` out (``violations`` already plain
        dicts, ready for :meth:`controls_refusals`). Never raises — any
        failure here (a malformed body, a bug in this merge) falls all the
        way back to this worker's own configuration with nothing refused,
        which is the fail-open this control promises.

        Every field from ``circuit_rate`` on is a real, local control:
        each is this worker's own ``config``
        tightened by whatever the plane states, the same shape
        ``limits``/``capabilities``/``envelope`` above already use — see
        each ``controls_merge.effective_*`` function's own docstring for
        its field's "stricter" direction.
        """
        try:
            safe_body = body if isinstance(body, dict) else {}
            limits, limit_violations = controls_merge.effective_limits(
                self._code_limits, safe_body.get("limits") if isinstance(safe_body.get("limits"), dict) else {}
            )
            capabilities, cap_violations = controls_merge.effective_capabilities(
                self._code_capabilities,
                safe_body.get("capabilities") if isinstance(safe_body.get("capabilities"), dict) else {},
            )
            envelope, env_violation = controls_merge.effective_envelope(
                self._code_envelope, safe_body.get("envelope"), stated="envelope" in safe_body
            )
            # spike/spike_enabled are resolved before _merge_detectors, not
            # after: _local_detector_action("spike") needs this call's own
            # on_spike mode, and it must never get there by calling back
            # into self._effective_config()/self._controls_snapshot() --
            # this method already runs *inside* _controls_snapshot()'s own
            # lock, and that lock is not reentrant (a second acquire from
            # the same thread deadlocks forever, not just recomputes).
            spike_enabled, spike_enabled_violation = controls_merge.effective_spike_enabled(
                self._code_spike_enabled,
                safe_body.get("spike_enabled"),
                stated="spike_enabled" in safe_body,
            )
            spike, spike_violations = controls_merge.effective_spike(
                self._code_spike, safe_body.get("spike")
            )
            resolved_on_spike = spike["mode"]
            detectors, det_violations = self._merge_detectors(
                safe_body.get("detectors") if isinstance(safe_body.get("detectors"), dict) else {},
                resolved_on_spike,
            )
            circuit_rate, circuit_rate_violations = controls_merge.effective_circuit_rate(
                self._code_circuit_rate, safe_body.get("circuit_rate")
            )
            circuit_posture, circuit_posture_violation = controls_merge.effective_circuit_posture(
                self._code_circuit_posture,
                safe_body.get("circuit_posture"),
                stated="circuit_posture" in safe_body,
            )
            loop_shapes, loop_shapes_violations = controls_merge.effective_loop_shapes(
                self._code_loop_shapes, safe_body.get("loop_shapes")
            )
            budget_soft, budget_soft_violations = controls_merge.effective_budget_soft(
                self._code_budget_soft, safe_body.get("budget_soft")
            )
            max_actions_per_run, actions_violations = controls_merge.effective_max_actions_per_run(
                self._code_max_actions_per_run, safe_body.get("max_actions_per_run")
            )
            violations = [v.as_dict() for v in limit_violations]
            violations += [v.as_dict() for v in cap_violations]
            if env_violation is not None:
                violations.append(env_violation.as_dict())
            violations += det_violations
            if spike_enabled_violation is not None:
                violations.append(spike_enabled_violation.as_dict())
            violations += [v.as_dict() for v in spike_violations]
            violations += [v.as_dict() for v in circuit_rate_violations]
            if circuit_posture_violation is not None:
                violations.append(circuit_posture_violation.as_dict())
            violations += [v.as_dict() for v in loop_shapes_violations]
            violations += [v.as_dict() for v in budget_soft_violations]
            violations += [v.as_dict() for v in actions_violations]
            return _ControlsSnapshot(
                limits=limits,
                capabilities=capabilities,
                envelope=envelope,
                detectors=detectors,
                violations=violations,
                circuit_rate=circuit_rate,
                loop_shapes=loop_shapes,
                budget_soft=budget_soft,
                max_actions_per_run=max_actions_per_run,
                circuit_posture=circuit_posture,
                spike_enabled=spike_enabled,
                spike=spike,
            )
        except Exception:
            _LOG.warning(
                "runbound: could not apply the control plane's Controls; "
                "keeping this worker's own configuration",
                exc_info=True,
            )
            return _ControlsSnapshot(
                limits=dict(self._code_limits),
                capabilities=dict(self._code_capabilities),
                envelope=self._code_envelope,
                detectors={},
                violations=[],
                circuit_rate=dict(self._code_circuit_rate) if self._code_circuit_rate else None,
                loop_shapes=dict(self._code_loop_shapes),
                budget_soft=dict(self._code_budget_soft) if self._code_budget_soft else None,
                max_actions_per_run=self._code_max_actions_per_run,
                circuit_posture=self._code_circuit_posture,
                spike_enabled=self._code_spike_enabled,
                spike=dict(self._code_spike),
            )

    def _merge_detectors(self, plane_detectors: dict, resolved_on_spike: str) -> tuple:
        """``({name: {"action", "mode"}}, [dict, ...])`` for every detector
        name the plane's body actually names — a name it does not mention
        gets no entry at all, which is how :meth:`controls_detector_override`
        knows to leave that detector's ordinary ``on_anomaly`` behavior
        alone.

        ``resolved_on_spike`` is this same :meth:`_merge_controls` call's
        own resolved spike mode, threaded straight into
        :meth:`_local_detector_action` — never re-derived by calling back
        through :meth:`_effective_config`/:meth:`_controls_snapshot`, whose
        lock this method is already running inside (see
        :meth:`_merge_controls`'s own comment on why that would deadlock).

        Two things the pure :func:`~runbound.controls_merge.effective_detector`
        cannot know are decided here, both about ``can_stop``:
        whether a merged ``"stop"``/``"enforce"`` this worker's own
        configuration did not already commit to can actually be applied —
        never, unless this process can stop at all
        (``on_anomaly in ("raise", "callback")``) — and, when it cannot, one
        more reported entry distinct from an ordinary loosening violation:
        the plane asked to *tighten* past a "notify" baseline and could
        not, not a refused loosening, so it carries ``"reason":
        "cannot_stop"`` rather than the ordinary shape, for the dashboard's
        badge and this report to tell the same story.
        """
        overrides: dict[str, dict] = {}
        violations: list[dict] = []
        can_stop = self._can_stop()
        for name, spec in plane_detectors.items():
            if not isinstance(name, str):
                continue
            local_action = self._local_detector_action(name, resolved_on_spike=resolved_on_spike)
            effective, field_violations = controls_merge.effective_detector(name, local_action, spec)
            if effective is None:
                continue
            overrides[name] = effective
            violations.extend(v.as_dict() for v in field_violations)
            if (
                local_action == "notify"
                and effective.get("action") == "stop"
                and effective.get("mode") == "enforce"
                and not can_stop
            ):
                violations.append(
                    {
                        "path": f"detectors.{name}.action",
                        "base": "notify",
                        "candidate": "stop",
                        "reason": "cannot_stop",
                    }
                )
        return overrides, violations

    def _can_stop(self) -> bool:
        """Can this worker's code actually stop a
        run at all? ``on_anomaly in ("raise", "callback")`` — the same
        answer :func:`runbound.coverage`'s ``can_stop`` key and the
        heartbeat's own field give. The gate a plane ``"stop"`` on a
        detector this worker's own configuration would only notify about
        must pass before it is ever applied (see :meth:`_merge_detectors`,
        :meth:`controls_detector_override`): a worker in warn mode has no
        path that expects a `GuardrailTripped`, so forcing one from a
        dashboard is exactly the bug fixed for the local case, done
        remotely instead.
        """
        return self.config.on_anomaly in ("raise", "callback")

    def _local_detector_action(self, name: str, resolved_on_spike: "str | None" = None) -> str:
        """``"stop"`` or ``"notify"``: what this worker's own configuration
        already does for detector ``name``, absent any plane Controls — the
        baseline invariant 3 protects against a plane *loosening* it. Table,
        not a single global rule, because several detectors read their own
        knob instead of (or as well as) ``on_anomaly`` (see ``config.py``
        and ``engine.py``'s own reaction logic, not guessed at).

        ``resolved_on_spike``, when given, is used for the ``spike`` branch
        instead of calling :meth:`_effective_config` — :meth:`_merge_detectors`
        passes its own already-resolved mode here because it runs inside
        :meth:`_controls_snapshot`'s lock, which :meth:`_effective_config`
        would otherwise deadlock re-entering. A direct call (a test, or any
        caller outside the merge) omits it and gets the ordinary resolved
        value.

        * ``circuit`` — ``on_provider_failure``: ``"open"`` stops, anything
          else (default ``"notify"``) does not, whatever ``on_anomaly`` is.
        * ``loop`` — ``on_loop``: ``"throttle"`` never stops; ``"break"``/
          ``"escalate"`` always eventually do (:meth:`_react_to_loop`
          raises unconditionally under ``"break"``, and at
          ``loop_hard_threshold`` under ``"escalate"``), whatever
          ``on_anomaly`` is; ``None`` (the default) falls back to
          ``on_anomaly`` like an ordinary detector.
        * ``spike`` — ``on_spike``: stops only under ``"trip"``/``"limit"``
          (:meth:`_spike_stops`); the default ``"notify"`` never does on its
          own, whatever ``on_anomaly`` is.
        * ``velocity`` — always ``severity="warn"`` (:class:`~runbound.
          detectors.VelocityDetector`), so it can never stop a run at all,
          whatever ``on_anomaly`` or any control says.
        * every other name (``budget``, ``steps``, ``events``,
          ``error_storm``, ``timeout``, and the door stages that reuse
          those same three names) is an "ordinary" detector with no knob of
          its own: it follows ``on_anomaly`` exactly.

        A plane ``"notify"``/``"shadow"`` against a ``"stop"`` baseline here
        is refused (invariant 3); a plane ``"stop"`` against a ``"notify"``
        baseline is a real tightening but is only ever *applied* when
        :meth:`_can_stop` says this worker's code can act on it — see
        :meth:`_merge_detectors`.
        """
        if name == CIRCUIT_DETECTOR:
            return "stop" if self.config.on_provider_failure == "open" else "notify"
        if name == LOOP_DETECTOR:
            if self.config.on_loop == "throttle":
                return "notify"
            if self.config.on_loop in ("break", "escalate"):
                return "stop"
            # on_loop is None: ordinary, falls through to on_anomaly below.
        elif name == SPIKE_DETECTOR:
            on_spike = (
                resolved_on_spike if resolved_on_spike is not None else self._effective_config().on_spike
            )
            return "stop" if on_spike in ("trip", "limit") else "notify"
        elif name == VELOCITY_DETECTOR:
            return "notify"
        return "stop" if self.config.on_anomaly in ("raise", "callback") else "notify"

    def _limit(self, name: str) -> "float | int | None":
        """This worker's effective value for one Controls limit field —
        tightened against the plane, or this worker's own configuration
        unchanged with no plane (or a stale/dry_run one)."""
        return self._controls_snapshot().limits.get(name, self._code_limits.get(name))

    def _effective_capabilities(self) -> dict:
        """The class rules :meth:`judge_action` reads — this worker's own
        ``init(capabilities=...)`` tightened against the plane's."""
        return self._controls_snapshot().capabilities

    def _effective_envelope(self) -> bool:
        """Whether the envelope's own door stages run at all — this
        worker's own ``envelope=`` tightened against the plane's."""
        return bool(self._controls_snapshot().envelope)

    def controls_detector_override(self, detector: str) -> str | None:
        """How the plane's Controls change ``detector``'s reaction, or
        ``None`` for no change at all (the overwhelming common case: no
        plane, or the plane says nothing about this detector).

        ``"warn"``: the merged ``action`` is ``"notify"`` — logs and alerts,
        never latches, whatever ``on_anomaly`` says. ``"dry_run"``: the
        merged ``action`` is ``"stop"`` but ``mode`` is ``"shadow"`` — this
        detector would have stopped the run and did not, which is also the
        wire's own vocabulary for a policy dry run (both mean "refused and
        shown, not enforced"), whatever :meth:`_can_stop` says: shadow mode
        never actually stops anything, so there is nothing to gate.
        ``"stop"``: the merged ``action`` is ``"stop"``, ``mode`` is
        ``"enforce"``, **and this worker can actually stop**
        (:meth:`_can_stop`) — applied for real, whatever ``on_anomaly``
        says (the tighten direction that escalates a detector this
        worker's own configuration would only notify about — for
        example: code ``on_spike="notify"``, a plane ``spike: stop``
        actually stops it, on a worker whose ``on_anomaly`` can raise at
        all). ``None`` here too — this worker's own, unaffected reaction
        applies — when the merged action is ``"stop"``/``"enforce"`` but
        :meth:`_can_stop` says this worker's code cannot act on it: forcing
        a stop nothing catches is exactly the bug fixed for the local
        case; see :meth:`_merge_detectors`
        for the report that tells an operator why. Read by
        :meth:`_reacted_for` (what to *tell* an observer),
        :meth:`_react`/:meth:`_react_to_loop` (whether to actually stop) and
        :meth:`_react_at_door` (whether the door itself may refuse).
        """
        spec = self._controls_snapshot().detectors.get(detector)
        if spec is None:
            return None
        if spec.get("action") == "notify":
            return "warn"
        if spec.get("mode") == "shadow":
            return "dry_run"
        if not self._can_stop():
            return None
        return "stop"

    def controls_refusals(self) -> list[dict]:
        """"Refused and shown": every Controls field this worker's
        own configuration kept, either because the plane's own value would
        have loosened it (``{"path", "base", "candidate"}``) or because a
        real tightening could not be applied at all (the same shape plus
        ``"reason": "cannot_stop"`` — see :meth:`_merge_detectors`), as
        plain dicts for the heartbeat to carry back."""
        return list(self._controls_snapshot().violations)

    def _effective_config(self) -> GuardrailConfig:
        """A shallow copy of ``self.config`` carrying the handful of fields
        the wall detectors, the ladder and the session-lifecycle
        bookkeeping in ``api.py`` read straight off a ``config`` argument
        (``max_events``, ``loop_threshold``, the three per-call caps, and
        every ``loop_shapes``/``budget_soft``/spike/ladder attribute) —
        rebuilt fresh from the live ``self.config`` every time, never from
        a frozen snapshot, so an unrelated in-place mutation elsewhere on
        the config (``tool_policy``'s lazy coercion, say) is never
        reverted by this.

        Every one of these has a real, local field on ``GuardrailConfig``
        — this shim's job is
        no longer "there is nowhere else to put this", only "compose this
        worker's own configuration with whatever the plane tightened it
        to" (:meth:`_controls_snapshot`), so a reader still only has to
        look in one place. Spike attributes are set unconditionally, never
        gated on ``spike_enabled`` here: a session's own bookkeeping (its
        allowance base, its baseline window) needs a number whether or not
        the detector is currently gated on — the gate itself lives in
        :class:`~runbound.detectors.SpikeDetector.check`, read via
        :meth:`_effective_spike_enabled`.
        """
        snapshot = self._controls_snapshot()
        limits = snapshot.limits
        loop_shapes = snapshot.loop_shapes
        budget_soft = snapshot.budget_soft
        spike = snapshot.spike
        cfg = dataclasses.replace(
            self.config,
            max_events=limits.get("max_events", self.config.max_events),
            loop_threshold=limits.get("loop_threshold", self.config.loop_threshold),
            max_call_seconds=limits.get("max_call_seconds", self.config.max_call_seconds),
            max_tokens_out_per_call=limits.get(
                "max_tokens_out_per_call", self.config.max_tokens_out_per_call
            ),
            max_cost_per_call_usd=limits.get(
                "max_cost_per_call_usd", self.config.max_cost_per_call_usd
            ),
        )
        # The plane's Controls can say a loop is notify-only: the graded policy's
        # contain rung then does not touch the ladder.
        cfg.loop_contain_allowed = self.controls_detector_override(LOOP_DETECTOR) not in ("warn", "dry_run")
        cfg.loop_shapes = loop_shapes["shapes"]
        cfg.loop_max_period = loop_shapes["max_period"]
        cfg.loop_stall_turns = loop_shapes["stall_turns"]
        if budget_soft is not None:
            cfg.budget_soft = budget_soft["fraction"]
            cfg.on_budget_soft = budget_soft["reaction"]
        cfg.spike_enabled = snapshot.spike_enabled
        cfg.on_spike = spike["mode"]
        cfg.spike_limit_calls = spike["limit_calls"]
        cfg.spike_cooldown_seconds = spike["cooldown_seconds"]
        cfg.spike_max_strikes = spike["max_strikes"]
        cfg.spike_warmup_calls = spike["warmup_calls"]
        cfg.spike_min_duration_s = spike["min_duration_s"]
        cfg.spike_min_output_tokens = spike["min_output_tokens"]
        cfg.spike_window = spike["window"]
        cfg.spike_factor = spike["factor"]
        cfg.spike_confirm = spike["confirm"]
        return cfg

    def _effective_circuit_rate(self) -> dict | None:
        """This worker's rate-mode bundle, tightened by the plane —
        ``None`` when neither this worker's own ``circuit_mode="rate"`` nor
        the plane's ``circuit_rate`` turns it on (count mode, the free
        four's own unchanged behavior). ``{"mode": "rate", "min_calls",
        "failure_rate", "slow_call_seconds", "slow_rate", "half_open_calls"}``
        otherwise.
        """
        return self._controls_snapshot().circuit_rate

    def _effective_circuit_posture(self) -> bool:
        """Does a half-open circuit narrow this process to ``restricted``?
        This worker's own ``circuit_posture`` (default ``False``),
        tightened by the plane's — independent of rate mode (a
        count-mode breaker narrows exactly like a rate-mode one)."""
        return self._controls_snapshot().circuit_posture

    def _effective_spike_enabled(self) -> bool:
        """Does the spike detector's ratio-based judgement and the abuse
        ladder run at all for this service? This worker's own
        ``spike_detection`` (default ``True``), tightened by the
        plane's ``spike_enabled``. Deliberately does not gate the per-call
        hard ceilings, which stay free — see
        :class:`~runbound.detectors.SpikeDetector.check`."""
        return self._controls_snapshot().spike_enabled

    def _sync_circuit_from_controls(self) -> None:
        """Reconfigure the live :class:`~runbound.circuit.CircuitBreaker` to
        match the currently-effective ``circuit_rate`` (this
        worker's own ``circuit_mode="rate"``, tightened by the plane's), or
        put it back to count mode the moment neither states one any more
        (a plane that goes quiet, a row that reverts to ``dry_run``, a
        fresh process with no local rate mode either).

        Cheap and idempotent: called before every circuit read/write so the
        breaker is never judged under a stale mode, and a no-op — no lock,
        no comparison worth doing twice — when nothing has changed since the
        last call (the identity-cached ``_controls_snapshot`` already makes
        that check free). The breaker's own lock guards its failure/call
        history, never these plain config attributes, so setting them here
        from whatever thread last synced is the same benign, rarely-changing
        write every other Controls-derived config value in this class is.
        """
        rate = self._effective_circuit_rate()
        if rate == self._circuit_rate_active:
            return
        self._circuit_rate_active = rate
        if rate is None:
            self.circuit.mode = "count"
            self.circuit.min_calls = 1
            self.circuit.failure_rate = 1.0
            self.circuit.slow_rate = 1.0
            self.circuit.half_open_calls = 1
            return
        self.circuit.mode = "rate"
        self.circuit.min_calls = int(rate.get("min_calls", 1))
        self.circuit.failure_rate = float(rate.get("failure_rate", 1.0))
        self.circuit.slow_rate = float(rate.get("slow_rate", 1.0))
        self.circuit.half_open_calls = int(rate.get("half_open_calls", 1))

    def _effective_loop_shapes(self) -> "tuple[str, ...]":
        """Which :data:`~runbound.config.LOOP_SHAPES` this worker's
        ``LoopDetector`` actually checks, in order: this worker's own
        ``loop_shapes`` (``("repeat", "sequence", "retry")`` by default),
        unioned with whatever the plane's Controls add for this service —
        a plane can only add a shape, never drop one this worker's own
        code already checks.
        """
        return self._controls_snapshot().loop_shapes["shapes"]

    def _effective_budget_soft(self) -> "float | None":
        """The soft-line fraction in effect for this service: this
        worker's own ``budget_soft``, tightened by the plane's — ``None``
        when neither states one."""
        budget_soft = self._controls_snapshot().budget_soft
        return None if budget_soft is None else budget_soft["fraction"]

    def _effective_max_actions_per_run(self) -> "int | None":
        """How many tool actions one run may make: this worker's own
        ``max_actions_per_run``, tightened by the plane's — ``None`` (no
        cap) when neither states one."""
        return self._controls_snapshot().max_actions_per_run

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
        decision = _policy_decision(violation)
        if dry_run:
            stamped = _stamp_decision(anomaly, decision, session=session)
            self._alert(stamped, session, "dry_run")
            _LOG.warning("[runbound] %s", stamped.message)
            return
        latch = policy.on_violation == "block_and_latch"
        self.refuse(
            session, anomaly, decision, exc=PolicyViolation, reacted="blocked",
            latch=latch, violation=violation,
        )

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
        """The service's own policy: what the decorators say, over what ``init()`` said.

        A rule lives on the tool, as a keyword on ``@runbound.tool``, and those
        decorators run *after* ``init()`` in every real app — the module
        configures runbound at the top and defines its tools below. So the fold
        happens here, on demand, rather than once at start-up where it would
        see an empty registry and enforce nothing.

        Cached on the registry's version and the configured policy's identity,
        and the *same object* comes back while neither has moved: :meth:`_policy`
        keys its org-policy merge on ``id()`` of what this returns, and a fresh
        object per tool call would thrash it.

        Fail-open at the seam: a fold that goes wrong leaves the configured
        policy enforcing alone — the decorator's own mistakes were already
        raised where they were written, at decoration time.
        """
        configured = self._configured_policy()
        try:
            key = (_coverage.tool_rules_version(), id(configured))
            if self._local_key == key:
                return self._local
            policy = self._fold(configured)
        except Exception:
            _LOG.warning(
                "runbound: the decorator rules could not be folded into the "
                "tool policy; enforcing the configured one",
                exc_info=True,
            )
            return configured
        # The value before the key: a reader racing this sees either the old
        # key (and refolds, harmlessly) or a key whose policy is already there,
        # never a key paired with the previous fold's policy.
        self._local = policy
        self._local_key = key
        return policy

    def _fold(self, configured: ToolPolicy | None) -> ToolPolicy | None:
        """``configured`` with every ``@runbound.tool`` rule folded onto it."""
        rules = _coverage.tool_rules()
        self._warn_conflicts(rules, configured)
        return from_decorators(rules, configured)

    def _warn_conflicts(self, rules: dict, configured: ToolPolicy | None) -> None:
        """Say, once per tool, that a decorator is overruling ``init()``.

        Silence would be the wrong answer: the customer wrote a rule on
        ``init(tool_policy=...)`` and it is not the one being enforced.
        """
        for tool in conflicts(rules, configured):
            if tool in self._policy_conflicts_warned:
                continue
            self._policy_conflicts_warned.add(tool)
            _LOG.warning(
                "runbound: tool %r has a rule on both @runbound.tool and "
                "init(tool_policy=...); the decorator's rule is the one "
                "enforced",
                tool,
            )

    def _configured_policy(self) -> ToolPolicy | None:
        """The ``init()`` policy, coerced from a dict if it still is one.

        ``init()`` coerces a dict policy during validation; an engine built
        around an unvalidated config coerces here instead, and one built around
        something that is not a policy at all contributes nothing rather than
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
        """The anomaly that drives the reaction.

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
        config = self._effective_config()
        for detector in self.detectors:
            try:
                anomaly = detector.check(session, event, config)
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
        self,
        anomaly: Anomaly,
        session: SessionState,
        reacted: str | None = None,
        *,
        refusal: bool = False,
    ) -> None:
        """Notify the observers, once per (session, detector).

        Detectors that fire once per session are unaffected; the loop detector
        under a repeat-driven policy fires on every repeat, and this is what
        keeps that from becoming a notification storm.

        **A refusal is not a notification.** ``refusal=True`` (from
        :meth:`refuse`) means a tool or a call was actually turned away, and
        every one of those is evidence: it is recorded and exported as its own
        anomaly and Decision, up to :data:`REFUSAL_RECORD_CAP` per
        ``(session, detector, rule, tool)``, past which they are counted and
        summarised (:meth:`flush_refusal_summaries`). The once-per-key memory
        below then gates only what it always gated for everything else: paging.
        Before this, the second refusal in a session was dropped whole, even
        one of a different tool.

        The observers are told what the engine is about to do about it
        (``reacted``). Callers that already know — a policy dry run, a
        refusal at the door — say so; the rest is worked out from the anomaly
        and the configuration. This is the whole of what used to be "alerting"
        from inside the SDK: delivery itself is the control plane's job now
        instead, and an observer — telemetry export among them — is how it
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
            # The graded loop policy's rungs (log, alert, contain) are three
            # different things to tell someone, whatever their severity.
            _detail(anomaly, "rung", None),
        )
        if anomaly.detector in (CIRCUIT_DETECTOR, INFLIGHT_DETECTOR):
            # A circuit — and a full endpoint — belongs to a provider, not to
            # the session that happened to make the call: the session id is
            # dropped so one outage pages once however many callers ran into
            # it, and two endpoints stay two incidents.
            key = (anomaly.detector, _detail(anomaly, "provider", None))
        if anomaly.detector == PLANE_DETECTOR:
            # A plane-loss refusal (`on_plane_loss="refuse"`) belongs to the
            # outage, not to whichever session's entry happened to hit it
            # first: the session id is dropped so one degraded link pages once
            # however many callers are refused at the door while it lasts.
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
            # An admission refusal is its own kind of "budget" news:
            # the ordinary key already includes the session id, but not the
            # rule, so without this an admission refusal and a later
            # post-call budget trip in the same session would dedupe against
            # each other — only the first of the two would ever reach an
            # observer. Adding "admission" keeps them apart; alerted once per
            # session either way, as the ordinary key already ensures.
            key += ("admission",)
        if _detail(anomaly, "rule", None) == "envelope":
            # The envelope's own door stages (steps, run time, the tokens half of
            # budget) reuse the wall's own detector name — on purpose, so a
            # customer's refusal profile for "steps"/"timeout"/"budget"
            # keeps applying — but that means, without this, a door refusal
            # and a *later* post-call trip by the same wall on the same
            # session would collide on the ordinary key (same detector, same
            # severity, nothing else distinguishing) and only the first of
            # the two would ever reach an observer. Adding "envelope" is the
            # same fix made for "admission" above, generalized to every
            # detector an envelope door stage can reuse (not only budget).
            key += ("envelope",)
        if anomaly.detector == POLICY_DETECTOR:
            # A policy anomaly is per rule and per tool: an agent refused a
            # second tool, or refused the same tool for a different reason, is
            # news; the same refusal on every retry is not.
            key += (_detail(anomaly, "rule", None), _detail(anomaly, "tool", None))
        if refusal:
            self._alerted.add(key)
            if not self._admit_refusal(session, anomaly, reacted):
                return
        elif key in self._alerted:
            return
        else:
            self._alerted.add(key)
        self._notify_anomaly(session, anomaly, reacted or self._reacted_for(anomaly))

    def _refusal_counter_key(self, session: SessionState, anomaly: Anomaly) -> tuple:
        return (
            getattr(session, "session_id", ""),
            anomaly.detector,
            _detail(anomaly, "rule", None),
            _detail(anomaly, "tool", None),
        )

    def _admit_refusal(
        self, session: SessionState, anomaly: Anomaly, reacted: str | None
    ) -> bool:
        """Count one refusal; True while it is still within the record cap."""
        key = self._refusal_counter_key(session, anomaly)
        with self._refusal_lock:
            entry = self._refusal_counts.get(key)
            if entry is None:
                entry = [0, 0, reacted or "blocked", _refusal_shape(anomaly)]
                self._refusal_counts[key] = entry
                while len(self._refusal_counts) > _REFUSAL_COUNTERS_MAX:
                    self._refusal_counts.popitem(last=False)
            else:
                self._refusal_counts.move_to_end(key)
            entry[0] += 1
            if entry[0] <= REFUSAL_RECORD_CAP:
                return True
            entry[1] += 1
            return False

    def flush_refusal_summaries(self, session: SessionState) -> None:
        """Report what this session's refusals suppressed since the last call.

        One summary anomaly per ``(detector, rule, tool)`` with something
        suppressed, ``details["suppressed_count"]`` being the number suppressed
        since the previous summary (so summaries add up), reacted like the
        refusals it stands for. Called as a session block exits, before its
        exit record is queued. Never raises.
        """
        try:
            sid = getattr(session, "session_id", "")
            with self._refusal_lock:
                due = []
                for key, entry in self._refusal_counts.items():
                    if key[0] == sid and entry[1] > 0:
                        due.append((key, entry[1], entry[2], entry[3]))
                        entry[1] = 0
            for key, count, reacted, shape in due:
                summary = _refusal_summary(shape, count)
                self._notify_anomaly(session, summary, reacted)
        except Exception:
            _LOG.warning("runbound could not summarise suppressed refusals", exc_info=True)

    def notify_door(self, session: SessionState, anomaly: Anomaly) -> None:
        """Report a refusal made at the door of a :func:`~runbound.session` block.

        Fan-out limits, the in-flight cap and a fleet-wide halt all refuse
        *before* anything has happened, so there is no event to hang the
        anomaly off — the api calls this instead. Deduped and reported to the
        observers exactly like any other anomaly, tagged ``"door"``.
        """
        self._alert(anomaly, session, "door")

    def record_door_refusal(self, session: SessionState, anomaly: Anomaly) -> None:
        """Record one refusal made at the door of a :func:`~runbound.session`
        block that has no event to hang it off (a fan-out limit), as its own
        record: the capped per-(session, detector, rule, tool) path every
        refusal takes, tagged ``"door"``. Unlike :meth:`notify_door` (a knock on
        a key that is already stopped, reported per knock through the trip),
        each of these is a distinct refused branch."""
        self._alert(anomaly, session, "door", refusal=True)

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

        Checked first, ahead of every other rule here, but **only for a
        critical anomaly**: a plane Controls override on this detector
        — see :meth:`controls_detector_override`. A ``"warn"``-
        severity anomaly (a spike still watching, an escalating loop not
        yet confirmed, ``velocity``, always) never stops the run on its
        own account, whatever any control says — that is the rule for
        every mode, and a plane override must not manufacture a
        stop this anomaly was never going to cause. For a critical one the
        override wins over every local reaction below, in both directions,
        because it is stated *for this anomaly's own detector name*, not
        for the session's mode: ``"warn"``/``"dry_run"`` report exactly
        that; ``"stop"`` (a plane escalating past this worker's own
        configuration, and only when :meth:`_can_stop` allows it) reports
        as ``"raise"``, the vocabulary's own word for "the session was
        stopped with an exception".
        """
        if anomaly.severity == "critical":
            override = self.controls_detector_override(anomaly.detector)
            if override == "stop":
                return "raise"
            if override is not None:
                return override
        if anomaly.detector == SPIKE_DETECTOR and (
            anomaly.severity != "critical" or not self._spike_stops(anomaly)
        ):
            return "warn"
        if anomaly.detector == BUDGET_DETECTOR and anomaly.severity != "critical":
            return "warn"  # the soft line never stops the run
        if (
            anomaly.detector == LOOP_DETECTOR
            and self.config.is_graded_loop()
            and _detail(anomaly, "policy", None) == "graded"
        ):
            # The graded policy: only the ladder's close stops the session;
            # its limit is the ladder's warn; the log, the page and a contain
            # the ladder cannot perform are notices ("notify"): recorded, paged
            # if critical, never latched.
            action = _detail(anomaly, "action", None)
            if action == "rollover":
                return "raise"
            return "warn" if action == "limit" else "notify"
        if anomaly.detector == LOOP_DETECTOR and self.config.on_loop is not None:
            if self.config.on_loop == "break":
                return "raise"
            if self.config.on_loop == "escalate":
                return "raise" if anomaly.severity == "critical" else "warn"
            return "warn"  # throttling delays the call; it never stops the run
        if anomaly.detector == CIRCUIT_DETECTOR:
            return "blocked" if self.config.on_provider_failure == "open" else "warn"
        if anomaly.severity != "critical":
            return "warn"  # a warn-severity anomaly is a notice in every mode
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
        try:
            session_id = getattr(session, "session_id", None)
            local_events.record_anomaly(session_id, anomaly, reacted)
        except Exception:
            _LOG.warning("runbound could not record a local event", exc_info=True)
        for observer in self.observers:
            try:
                observer.on_anomaly(session, anomaly, reacted)
            except Exception:
                _LOG.warning(
                    "runbound observer %s failed on an anomaly",
                    type(observer).__name__,
                    exc_info=True,
                )

    def _react_graded(self, anomaly: Anomaly, session: SessionState) -> None:
        """React to one rung of the graded loop policy.

        The log and the page (and a contain the ladder could not perform) are
        notices: logged here, exported and paged by their severity, never a
        stop. The ladder's limit is a warn, logged. Only its close, the
        rollover, stops the session: it latches it (the cooldown, strikes and
        fleet return are the ladder's) and raises on the call that closed it.
        A plane Controls override on ``"loop"`` applies to a critical rung as
        it does under the legacy policies. An anomaly that carries no rung (the
        opt-in "stall" shape) is reacted to as any other detector's.
        """
        if _detail(anomaly, "policy", None) != "graded":
            if anomaly.severity == "critical":
                self._react(anomaly, session)
            else:
                _LOG.warning("[runbound] %s", anomaly.message)
            return
        if anomaly.severity == "critical":
            override = self.controls_detector_override(anomaly.detector)
            if override in ("warn", "dry_run"):
                _LOG.warning("[runbound] %s", anomaly.message)
                return
            if override == "stop" or _detail(anomaly, "action", None) == "rollover":
                self._latch(session, anomaly)
                raise GuardrailTripped(anomaly)
        _LOG.warning("[runbound] %s", anomaly.message)

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

        Checked first, but **only for a critical anomaly** — a warn-severity
        loop (the escalate policy's own "not yet confirmed" stage) never
        stops on its own account, whatever any control says, the same
        guard :meth:`_reacted_for` applies: a plane Controls override on
        ``"loop"`` — see :meth:`controls_detector_override`.
        ``"notify"``/shadow-``"stop"`` both mean this anomaly logs and
        alerts but never latches, whatever ``on_loop`` says; a plane
        ``"stop"`` (only when :meth:`_can_stop` allows it) stops it here
        even under ``on_loop=None``/``"throttle"``.
        """
        if anomaly.severity == "critical":
            override = self.controls_detector_override(anomaly.detector)
            if override in ("warn", "dry_run"):
                _LOG.warning("[runbound] %s", anomaly.message)
                return
            if override == "stop":
                self._latch(session, anomaly)
                raise GuardrailTripped(anomaly)
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
        threshold = self._limit("loop_threshold")
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

        Checked first: a plane Controls override on this anomaly's detector
        — see :meth:`controls_detector_override`. It wins over
        ``on_anomaly`` entirely, in both directions: "notify" and
        shadow-"stop" both mean this anomaly logs and alerts but never
        latches; a plane "stop" latches and raises even under
        ``on_anomaly="warn"``.
        """
        override = self.controls_detector_override(anomaly.detector)
        if override in ("warn", "dry_run"):
            _LOG.warning("[runbound] %s", anomaly.message)
            return
        if override == "stop":
            self._latch(session, anomaly)
            raise GuardrailTripped(anomaly)
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
            ttl = self._trip_ttl(session, anomaly)
            self.shared.trip(
                getattr(session, "key", None), session, anomaly, ttl, door
            )
        except Exception:
            _LOG.warning("runbound could not report a trip to the fleet", exc_info=True)

    def _trip_ttl(self, session: SessionState, anomaly: Anomaly) -> float | None:
        """How long the fleet should hold the latch this trip makes.

        The session's own expiry if it has one; else, for the ladder's close,
        the cooldown the key's next session will serve (so every worker lets
        the key back in when that worker would have), except at the last
        strike, where the key is blocked until ``clear()`` and there is no
        expiry; else ``latch_ttl_seconds``.
        """
        with session.lock:
            ttl = session.latch_ttl_override
        if ttl is not None:
            return ttl
        details = anomaly.details if isinstance(anomaly.details, dict) else {}
        if details.get("action") == "rollover":
            config = self._effective_config()
            try:
                strikes = int(details.get("strikes"))
            except (TypeError, ValueError):
                strikes = 0
            if ladder.entry_observation(strikes, config) != ladder.Observation.ENTRY_OUT_OF_STRIKES:
                cooldown = details.get("cooldown_seconds")
                usable = (
                    isinstance(cooldown, (int, float))
                    and not isinstance(cooldown, bool)
                    and math.isfinite(cooldown)
                    and cooldown > 0
                )
                return float(cooldown) if usable else float(config.spike_cooldown_seconds)
        return self.config.latch_ttl_seconds

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


def _posture_anomaly(session: SessionState, violation: Violation) -> Anomaly:
    """Describe a tool a posture refused. Names the class, never the arguments."""
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    details = violation.details
    return Anomaly(
        detector=SAFE_MODE_DETECTOR,
        severity="warn",
        message=(
            f"Refused tool {violation.tool!r}{whose}: {violation.reason}"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", {}) or {}),
            "tool": violation.tool,
            "rule": violation.rule,
            **details,
        },
    )


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


def _policy_decision(violation: Violation) -> Decision:
    """The :class:`Decision` behind one policy violation.

    ``evaluation`` carries whatever numbers the violation itself stated
    (``count``/``limit`` for ``max_calls``, nothing for ``deny``) — never
    call arguments, which ``violation.details`` never holds either.
    """
    details = violation.details if isinstance(violation.details, dict) else {}
    evaluation = {k: v for k, v in details.items() if k in ("count", "limit")}
    evaluation["rule"] = violation.rule
    return Decision(
        verdict="deny",
        kind="action",
        boundary="policy",
        detector=POLICY_DETECTOR,
        reason=violation.reason,
        evaluation=evaluation,
    )


def _limit_field_for(level: str) -> str:
    """Which config field ``level`` names, in a customer's own words."""
    return "run_budget_usd" if level == "run" else "budget_usd"


def _tighter_budget(
    key_remaining: "float | None",
    run_remaining: "float | None",
    key_limit: "float | None",
    run_limit: "float | None",
) -> tuple:
    """``(remaining, level, limit)``: whichever of the run's own budget and
    the key's is the binding constraint for this call.

    Pure arithmetic, no lock, no I/O — the compare-and-hold section that
    calls this already holds ``session.lock``. Either input may be ``None``
    (that budget is not configured at all); at least one must not be, since
    the caller never reaches here otherwise. When both are configured, the
    smaller ``remaining`` wins — the actual bottleneck this call is about to
    hit — and a tie goes to ``"key"``, since the key is the identity that
    outlives any one run and is the more consequential one to have run out.
    """
    if run_remaining is None:
        return key_remaining, "key", key_limit
    if key_remaining is None:
        return run_remaining, "run", run_limit
    if run_remaining < key_remaining:
        return run_remaining, "run", run_limit
    return key_remaining, "key", key_limit


def _reservation_anomaly(
    session: SessionState,
    model: str | None,
    cap_tokens: int,
    worst_case: float,
    remaining: float,
    reserved: float,
    limit: float,
    level: str = "key",
) -> Anomaly:
    """Describe a call refused because its stated cap could cross the budget.

    ``worst_case_usd`` is the stated cap priced at the model's output rate
    plus the input estimate: an upper bound on the output side, an estimate on
    the input side. ``reserved_usd`` is what other in-flight calls on this
    worker are already holding; ``remaining_usd`` already has it
    subtracted. ``level`` names which budget actually bound the call ("run" or
    "key" — the tighter of ``run_budget_usd`` and ``budget_usd``, when both
    are configured).
    """
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    field = _limit_field_for(level)
    return Anomaly(
        detector=BUDGET_DETECTOR,
        severity="critical",
        message=(
            f"Reservation refused{whose}: a {cap_tokens}-token cap on model {model!r} "
            f"could cost up to ${worst_case:.4f}, and ${remaining:.4f} is left of "
            f"{field} ${limit:.4f}"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "reason": "reservation",
            "rule": "reservation",
            "model": model,
            "cap_tokens": cap_tokens,
            "worst_case_usd": worst_case,
            "reserved_usd": reserved,
            "remaining_usd": remaining,
            "budget_usd": limit,
            "level": level,
        },
    )


def _admission_anomaly(
    session: SessionState,
    model: str | None,
    estimate: float,
    remaining: float,
    reserved: float,
    limit: float,
    level: str = "key",
) -> Anomaly:
    """Describe the refusal :meth:`Engine._admit_budget` is about to raise.

    ``reserved_usd`` is what other in-flight calls on this worker are
    already holding; ``remaining_usd`` already has it subtracted. ``level``
    is "run" or "key" — see :func:`_reservation_anomaly`.
    """
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    field = _limit_field_for(level)
    return Anomaly(
        detector=BUDGET_DETECTOR,
        severity="critical",
        message=(
            f"Admission refused{whose}: estimated ${estimate:.4f} for model "
            f"{model!r} would exceed {field} (${remaining:.4f} left of "
            f"${limit:.4f})"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "reason": "admission",
            "rule": "admission",
            "model": model,
            "estimated_cost_usd": estimate,
            "reserved_usd": reserved,
            "remaining_usd": remaining,
            "budget_usd": limit,
            "level": level,
        },
    )


def _stamp_decision(
    anomaly: Anomaly,
    decision: Decision,
    *,
    session: "SessionState | None" = None,
    provider_called: bool = False,
) -> Anomaly:
    """``anomaly`` with ``decision`` stamped onto ``details["decision"]``.

    Also used directly by :meth:`Engine._admit_circuit`, the one door refusal
    that must not alert again — the circuit's own "just opened" transition
    already paged once, and every call refused while it stays open must not
    re-page — and by :meth:`Engine._react_at_door`'s warn/callback branch,
    which alerts but never raises. Every other refusal site reaches this
    through :meth:`Engine.refuse`.

    Every deny or restrict Decision carries, in ``evaluation``, whichever of
    ``limit``, ``used``, ``reserved``, ``estimate``, ``remaining`` the stage
    that built it already stated, plus ``provider_called`` — folded in here,
    once, so no refusal site has to remember to add it by hand. An ``allow``
    Decision is returned completely unchanged: there is nothing to explain
    about a call that went through. ``details["key_hash"]`` is a salted hash
    of the session's key (``None`` for the default, unkeyed session, or with
    no session at all) — together with ``decision.level`` this is what
    ``exc.scope`` reads.

    Keeps the anomaly's ``anomaly_id`` (``dataclasses.replace`` copies the
    existing value rather than re-running the default factory).
    """
    if decision.verdict != "allow":
        evaluation = dict(decision.evaluation) if isinstance(decision.evaluation, dict) else {}
        evaluation["provider_called"] = provider_called
        decision = dataclasses.replace(decision, evaluation=evaluation)
    key = getattr(session, "key", None) if session is not None else None
    key_hash = _key_hash_fn(key) if key else None
    details = anomaly.details if isinstance(anomaly.details, dict) else {}
    return dataclasses.replace(
        anomaly, details={**details, "decision": decision.as_dict(), "key_hash": key_hash}
    )


def _steps_door_anomaly(session: SessionState, decision: Decision, max_steps: int) -> Anomaly:
    """Describe the envelope's steps refusal — the wall's own words, one call early.

    ``turns`` in ``details`` is ``decision.evaluation["used"]``: the number
    :class:`~runbound.detectors.StepDetector` would have reported had this
    call been allowed through, not the session's current (pre-call) count.
    """
    turns = decision.evaluation.get("used", max_steps)
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=STEP_DETECTOR,
        severity="critical",
        message=(
            f"Step limit reached{whose}: the next call would be step {turns} "
            f"(model turns), limit {max_steps}"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "turns": turns,
            "max_steps": max_steps,
            "rule": "envelope",
        },
    )


def _stopped_door_anomaly(
    session: SessionState, decision: Decision, source: str, reason: str
) -> Anomaly:
    """Describe :meth:`Engine._admit_stopped`'s refusal: no provider touched.

    ``warn``-severity everywhere else in this module names the wall behind a
    door; this door has no wall behind it (a model call was never checked
    against the posture before this task), so there is nothing for a later
    detector to confirm — the anomaly itself is the whole story, same as
    every other envelope door stage.
    """
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=SAFE_MODE_DETECTOR,
        severity="critical",
        message=f"Run stopped{whose}: no model call may go out ({source}: {reason})",
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "posture": "stopped",
            "source": source,
            "reason": reason,
            "rule": "posture",
        },
    )


def _run_time_door_anomaly(session: SessionState, decision: Decision) -> Anomaly:
    """Describe the envelope's run-time refusal, in :class:`TimeoutDetector`'s own shape."""
    limit = decision.evaluation.get("limit")
    elapsed = decision.evaluation.get("used", 0.0)
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=TIMEOUT_DETECTOR,
        severity="critical",
        message=(
            f"Session timeout{whose} (run): running for {elapsed:.0f}s, "
            f"limit {limit:.0f}s"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "elapsed_s": elapsed,
            "limit": limit,
            "scope": "run",
            "rule": "envelope",
        },
    )


def _tokens_door_anomaly(session: SessionState, model: str | None, decision: Decision) -> Anomaly:
    """Describe the envelope's tokens refusal, in :class:`BudgetDetector`'s own shape.

    ``rule="envelope"`` (not ``"tokens"``) is what :meth:`Engine._alert`
    reads to keep this anomaly's dedup key apart from the post-call wall's
    own ``budget`` trip on the *same* session — see the ``"envelope"``
    branch there. ``reason`` keeps saying ``"tokens"``: that is the
    human-readable "which boundary" field, untouched.
    """
    limit = decision.evaluation.get("limit")
    used = decision.evaluation.get("used", 0)
    estimate = decision.evaluation.get("estimate", 0)
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=BUDGET_DETECTOR,
        severity="critical",
        message=(
            f"Token limit would be crossed{whose}: a {estimate}-token cap on model "
            f"{model!r} would bring the total to {used + estimate}, limit {limit}"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "reason": "tokens",
            "rule": "envelope",
            "model": model,
            "total_tokens": used,
            "cap_tokens": estimate,
            "max_total_tokens": limit,
        },
    )


def _actions_door_anomaly(session: SessionState, decision: Decision) -> Anomaly:
    """Describe the envelope's ``max_actions_per_run`` refusal, ``fanout``-shaped.

    ``evaluation["used"]`` is the "count already spent" convention every
    other envelope stage uses (``admission.steps``'s own ``used`` is
    ``turns + 1`` for the same reason): the count this *attempt* would bring
    the tally to, not a count of what has already run. The steps door's own
    message already says so correctly ("the next call would be step N"); this
    one used to read the number as if it were completed actions ("N taken"),
    which is off by one from what ``session.executed_actions`` (and
    ``session_status()["actions"]``) actually reports the moment this fires —
    the bug this phrasing exists to fix. Naming the attempt explicitly, the
    same way the steps door does, keeps the message honest without touching
    ``evaluation`` itself.
    """
    limit = decision.evaluation.get("limit")
    used = decision.evaluation.get("used")
    key = getattr(session, "key", None)
    return Anomaly(
        detector=FANOUT_DETECTOR,
        severity="critical",
        message=(
            f"Too many actions this run: the next action would be action "
            f"{used}, limit {limit}"
        ),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", None) or {}),
            "rule": "actions",
            "count": used,
            "limit": limit,
        },
    )


#: Output-token cap fields, checked in the order a request is likeliest to
#: carry one: `max_tokens` (Anthropic, and OpenAI's older chat completions),
#: `max_completion_tokens` (OpenAI's newer chat completions), then
#: `max_output_tokens` (OpenAI's Responses API). Mirrors
#: `wrappers.openai_wrapper.request_output_cap` and
#: `wrappers.anthropic_wrapper.request_output_cap`.
_ADMISSION_OUTPUT_CAP_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _admission_worst_case(
    price: tuple, stated_cap: int | None, admission_output_tokens: int, request: dict | None
) -> float:
    """The dollar worst case of one call: input estimate plus capped output.

    Pure arithmetic — no lock, no session — so :meth:`Engine._admit_budget`
    can compute it once, outside the ``session.lock`` section its
    compare-and-hold needs. ``stated_cap`` wins over ``admission_output_tokens``
    when the request named its own cap. The formula itself is
    :func:`runbound.pricing.admission_worst_case`, published so that anything
    else that estimates a call the same way composes the same thing; ``price``
    may be a 3-tuple when the model publishes a cached-input rate, and
    admission prices at the plain input rate regardless (it cannot know before
    the call how much a cache will serve).
    """
    output_tokens = stated_cap if stated_cap is not None else admission_output_tokens
    return admission_worst_case(price, output_tokens, _admission_request_chars(request))


def _admission_request_chars(request: dict | None) -> int:
    """Characters of input an admission estimate is based on.

    Everything the providers bill as input, whatever the API's shape: chat
    ``messages``, the Responses API's ``instructions`` and ``input``,
    Anthropic's ``system``, and the tool definitions. It is
    :func:`runbound.pricing.request_chars`, which lives in the leaf pricing
    module so that the engine (which must not import the wrapper package) and
    the wrappers read a request the same way.
    """
    return request_chars(request)


def _admission_output_cap(request: dict | None) -> int | None:
    """The output-token cap ``request`` stated, if any.

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
    for this session id, so the session is judged fresh on its very
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


def _refusal_shape(anomaly: Anomaly) -> tuple:
    """What a summary needs to remember of a refusal: its detector and the
    few details that say whose it was and what it refused (never a Decision,
    never an argument)."""
    details = {
        key: value
        for key, value in (anomaly.details or {}).items()
        if key in ("session_id", "key", "tags", "tool", "rule", "at_door", "refused_at_door")
    }
    return anomaly.detector, details


def _refusal_summary(shape: tuple, count: int) -> Anomaly:
    """The one anomaly that stands for ``count`` refusals past the record cap.

    Same detector, rule and tool as the refusals it summarises; carries no
    Decision of its own.
    """
    detector, base = shape
    details = dict(base)
    details["suppressed_count"] = int(count)
    tool = details.get("tool")
    return Anomaly(
        detector=detector,
        severity="warn",
        message=(
            f"{count} more refusals of the same kind ({detector}"
            f"{'' if tool is None else ', tool ' + repr(tool)}) were not recorded one by one"
        ),
        details=details,
    )


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
