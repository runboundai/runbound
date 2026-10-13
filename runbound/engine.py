"""The engine: one attempted tool call in, an allow or a refusal out.

Record the event, tell the observers, run the pre-hook chain (:meth:`Engine.admit_call`).
Anything that refuses ends in :meth:`Engine.refuse`: stamp the Decision onto the anomaly,
tell the observers, raise. Observers hear *after* the fact (``on_event``, ``on_anomaly``)
and can never change it; one that raises is logged and skipped.
"""

import contextvars
import dataclasses
import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

from . import _coverage, admission, approvals as approvals_module, gates as gates_module, hooks as hooks_module, local_events
from . import policy_rows as rows_module, quota as quota_module
from .capabilities import _RANK as _CAPABILITY_RANK, class_rules
from .config import RunboundConfig
from .events import Anomaly, Decision, Event
from .exceptions import CapabilityRefused, GuardrailTripped, PolicyViolation
from .plane_types import key_hash as _key_hash_fn
from .refusal_log import REFUSAL_RECORD_CAP, RefusalLog  # noqa: F401  (re-exported)
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
from .shared import LocalState
from .state import SessionState

_LOG = logging.getLogger("runbound")

#: The detector names refusals carry. None is a detector: the customer (or the
#: plane) stated a rule and we enforced it, nothing is inferred.
POLICY_DETECTOR = "policy"
CAPABILITY_DETECTOR = "capability"
FANOUT_DETECTOR = "fanout"
HALT_DETECTOR = "halt"

#: An error string kept on an event is truncated to this many characters.
ERROR_MAX_CHARS = 500





class Engine:
    """Applies one configuration's rules to a session's tool calls."""

    def __init__(
        self,
        config: RunboundConfig,
        observers: Sequence | None = None,
        shared=None,
    ) -> None:
        self.config = config
        self.observers = list(observers) if observers is not None else []
        self.shared = shared if shared is not None else LocalState()
        self._alerted: set[tuple] = set()
        self._refusals = RefusalLog()
        self._merged_key: tuple | None = None
        self._merged: ToolPolicy | None = None
        # The configured policy with the @runbound.tool rules folded onto it, cached on the
        # decorator registry's version and config.tool_policy's identity (decorators run after init()).
        self._local_key: tuple | None = None
        self._local: ToolPolicy | None = None
        # A tool whose rule both a decorator and init(tool_policy=...) state is warned about once.
        self._policy_conflicts_warned: set[str] = set()

    # --- the path of one call -----------------------------------------------------

    def process(self, session: SessionState, event: Event, *, latch: bool = True) -> None:
        """Record ``event``, tell the observers, and refuse a latched session.

        Recorded *before* admission, so an attempt counts towards ``tool_calls``
        whether or not it is then refused. A session an earlier
        ``block_and_latch`` violation stopped stays stopped: the stored anomaly is
        raised again here, without alerting.
        """
        session.record(event)
        self._notify_event(session, event)
        latched = session.tripped_by if latch else None
        if latched is not None:
            session.mark_action_refused()
            raise GuardrailTripped(latched)

    def admit_call(self, session: SessionState, call: ToolCall, **chain: Any) -> ToolCall:
        """The pre-hook chain for one call (see :meth:`_run_chain`), holding the call's ``max_calls`` unit.

        The unit is held from the start so concurrent calls see each other; a call that is refused
        (or deferred to a person) gives it back, so only calls that cleared the chain are counted.
        """
        session.enter_chain(call.name)
        cleared = False
        _REPEATS.set([])
        _KEYS.set([])
        try:
            final = self._run_chain(session, call, **chain)
            cleared = True
            return final
        finally:
            session.leave_chain(call.name, cleared)
            held = _REPEATS.get() or []
            if not cleared:  # a refused or deferred call gives its repetition count back
                for tool, digest in held:
                    session.release_repeat(tool, digest)
            for tool, key in _KEYS.get() or []:
                session.leave_key(tool, key, cleared)
            _KEYS.set(None)
            _REPEATS.set(None)
            _RECORD.set(None)

    def _run_chain(
        self,
        session: SessionState,
        call: ToolCall,
        *,
        validator: "hooks_module.Validator | None" = None,
        hooks: Sequence = (),
        gate_hooks: Sequence = (),
        effects: "frozenset | None" = None,
        decorated: bool = False,
        allow_rewrite: bool = True,
        tags: "list | None" = None,
        approval: Any = None,
        info: "dict | None" = None,
    ) -> ToolCall:
        """The pre-hook chain for one call; returns the call whose arguments the body gets.

        A fleet halt first (the door), then :mod:`runbound.hooks`' fixed order: user
        rewrite hooks, validation of the final arguments, policy rules, capability
        class and action cap, the approval rule, and last the user gate hooks.
        """
        row = getattr(self.shared, "row_for", lambda _tool: None)(call.name)  # the dashboard's rule for this tool
        declared = effects  # the code's own effects: what the service defaults are resolved from
        row = rows_module.apply_defaults(row, call.name, effects, self.shared)  # the service defaults are the fallback of a tool no line decides
        record = self._record(call, row, approval)
        _RECORD.set(record)  # every refusal and the span of this call carry it (TPD §12 item 6)
        if info is not None:
            info["record"] = record
        gates_module.halt(self, session, call.name)
        unclassified = not effects  # the code declared nothing: a row's class may raise it, never relax how it is judged
        effects = rows_module.effective_effects(row, effects)  # a row's risk_class can only raise the tool's
        gates_module.plane(self, session, call, effects)
        timeout = self.config.hook_timeout_s
        try:
            call = hooks_module.run_hooks(
                hooks, call, effects, kind="pre", allow_rewrite=allow_rewrite, timeout=timeout, tags=tags
            )
            hooks_module.validate(validator, call)
        except hooks_module.StageRefused as refused:
            self._refuse_stage(session, call, refused)
        self.enforce_policy(session, call, stage="rules")
        row = rows_module.resolve_call(row, approvals_module.bound_arguments(call.name, call.args, call.kwargs), self._rule_context())
        if row is not None and row.conditional and row.inherit:
            row = rows_module.apply_defaults(row, call.name, declared, self.shared)  # no line matched: the service defaults decide
        if row is not None and (row.conditional or row.candidate is not None):
            record.update(self._record(call, row, approval))  # the lines that matched on THIS call's arguments
        if row is not None and row.conditional and not row.unsupported:
            key = ",".join(row.matched)  # a conditional line's cap counts the calls that matched the same lines
            session.hold_key(call.name, key)
            held_keys = _KEYS.get()
            if held_keys is not None:
                held_keys.append((call.name, key))
        rows_module.enforce(self, session, call, row, tags)  # blocked, then the session cap
        self.admit_action(session, call.name, effects, decorated, unclassified=unclassified)
        names, json_ok = approvals_module.tool_meta(call.name)
        approval = rows_module.approval_spec(row, approval, names, json_ok, approvals_module.ApprovalSpec)
        asks = approval is not None and approvals_module.current_request() is None
        if approvals_module.current_request() is None:  # an approved deferred call already holds its unit
            quota_module.admit(self, session, call, row, asks, tags)
        self.enforce_policy(session, call, stage="approval")
        try:
            if approval is not None:
                held = {} if info is None else info
                approvals_module.admit(self, session, call, approval, held)
                if held.get("granted"):  # a grant admitted it and the plane took no quota unit: take our own, as for a call nobody is asked about
                    quota_module.admit(self, session, call, row, False, tags)
            if gate_hooks:
                hooks_module.run_hooks(gate_hooks, call, effects, kind="gate", timeout=timeout, tags=tags)
        except hooks_module.StageRefused as refused:
            self._refuse_stage(session, call, refused)
        return call

    def _record(self, call: ToolCall, row: Any, approval: Any) -> dict:
        """The decision record of one call (TPD §4c.6, §12 item 6): why, under which policy, in which state. Never raises."""
        from . import __version__

        try:
            state = getattr(self.shared, "policy_state", None)
            policy_state = state() if callable(state) else None
            version = int(getattr(self.shared, "policy_version", 0) or 0) or None
            usable = row is not None and not row.unsupported
            matched = list(row.matched) if usable else []
            contributors = []
            policy = self._policy()
            if policy is not None and call.name in policy.deny:
                contributors.append("code:blocked")
            if approval is not None or (policy is not None and call.name in policy.require_approval):
                contributors.append("code:approval")
            contributors.extend(row.contributors if usable else ())
            if not contributors:
                contributors.append("default:service")
            return {
                "policy_state": policy_state, "policy_version": version, "matched_rule_ids": matched,
                "service_default_revision": self._default_revision(), "contributors": contributors, "sdk_version": __version__,
            }
        except Exception:
            _LOG.debug("runbound could not build a decision record", exc_info=True)
            return {}

    def _default_revision(self) -> "int | None":
        """The revision of the enforced service defaults this worker holds, for the decision record; ``None`` without any."""
        held = getattr(self.shared, "defaults", lambda: None)()
        return held.revision if held is not None and held.state == "enforced" else None

    def _rule_context(self) -> dict:
        """The trusted context a console rule's ``ctx`` conditions read: the service, and the environment and version the deployment states."""
        import os

        context = {"service": getattr(self.config, "service", None), "environment": os.environ.get("RUNBOUND_ENVIRONMENT"),
                   "version": os.environ.get("RUNBOUND_SERVICE_VERSION")}
        return {key: value for key, value in context.items() if isinstance(value, str) and value}

    def call_digest(self, call: ToolCall) -> str:
        """The call's argument digest (the span's ``args_digest``): an equality token for the same tool with the same arguments in this process."""
        from . import api as api_module

        return api_module._args_hash(call.name, call.args, call.kwargs) or ""

    def hold_repeat(self, session: SessionState, call: ToolCall, digest: str, limit: int) -> bool:
        """Count this call against a repetition control; ``False`` when the session already made ``limit`` of them. Released if the call is later refused."""
        if not session.reserve_repeat(call.name, digest, limit):
            return False
        held = _REPEATS.get()
        if held is not None:
            held.append((call.name, digest))
        return True

    def _refuse_stage(self, session: SessionState, call: ToolCall, refused: "hooks_module.StageRefused") -> None:
        """Turn a refusal of a hook, validator, approval or failed built-in into the exception raised."""
        stage = refused.stage
        decision = (
            admission.arguments(refused.reason) if stage == "arguments"
            else admission.approval(refused.hook, refused.reason, refused.retry_after_s) if stage == "approval"
            else admission.hook(stage, refused.reason, refused.hook)
        )
        why = f"invalid arguments ({refused.reason})" if stage == "arguments" else refused.reason
        lead = f"Refused tool {call.name!r}{{whose}}: {why}"
        anomaly = _anomaly(
            session, decision.detector, "warn", lead,
            {"tool": call.name, "rule": refused.stage, "hook": refused.hook},
        )
        self.refuse(session, anomaly, decision, reacted="blocked")

    def admit_action(
        self,
        session: SessionState,
        tool: str,
        effects: "frozenset | None" = None,
        decorated: bool = False,
        unclassified: bool = False,
    ) -> None:
        """The capability class rule (decorated tools only) and the action cap.

        ``unclassified``: the code declared no effects, so ``effects`` may be only what a row added; the tool is judged
        as the stricter of "unclassified" and those classes (register C15).
        """
        effects = effects or frozenset()
        if decorated:
            try:
                judgment = self.judge_action(effects, unclassified)
            except Exception:
                _LOG.warning("runbound could not read the capability rules for %r", tool, exc_info=True)
                failed = hooks_module.StageRefused("hook_error", "capability check failed", "capability")
                self._refuse_stage(session, ToolCall(tool, (), {}, None, {}), failed)
                judgment = None
            if judgment is not None and judgment["verdict"] != "allow":
                self.refuse_by_capability(session, tool, effects, judgment)
        self._admit_actions(session)

    def judge_action(self, effects: frozenset, unclassified: bool = False) -> dict:
        """The ``init(capabilities=...)`` verdict for a tool carrying ``effects``.

        A tool the code left unclassified is refused whenever anything is narrowed; a row that names a class for it must
        not hand it a more lenient verdict, so it is judged as the stricter of "unclassified" and the row's class.
        """
        rules = class_rules(self.config.capabilities)
        verdict, denied = rules.allows(effects), rules.denied_class(effects)
        if unclassified and effects:
            base = rules.allows(frozenset())
            if _CAPABILITY_RANK[base] > _CAPABILITY_RANK[verdict]:
                verdict, denied = base, rules.denied_class(frozenset())
        return {
            "verdict": verdict,
            "denied_class": denied,
            "source": "class_rule",
            "reason": "a capability class rule on init()",
            "level": "process",
        }

    def refuse_by_capability(
        self, session: SessionState, tool: str, effects: frozenset, judgment: dict
    ) -> None:
        """Refuse a tool a class rule denies; raises :class:`CapabilityRefused`.

        Latches nothing: the run goes on with less autonomy. The anomaly is
        ``warn`` and never carries arguments.
        """
        approval = judgment["verdict"] == "approve"
        denied = judgment["denied_class"]
        detail = (
            f"it needs approval for its {denied} capability, and this process has "
            "no approval queue"
            if approval
            else f"its {denied} capability is denied"
        )
        violation = Violation(tool, "capability", f"{detail} ({judgment['source']}: {judgment['reason']})", {
            "denied_class": denied, "effects": sorted(effects or ()), "verdict": judgment["verdict"],
            "reason": judgment["reason"], "source": judgment["source"],
        })
        anomaly = _anomaly(
            session, CAPABILITY_DETECTOR, "warn",
            f"Refused tool {tool!r}{{whose}}: {violation.reason}",
            {"tool": tool, "rule": violation.rule, **violation.details},
        )
        decision = admission.capability(judgment["verdict"], denied)
        self.refuse(session, anomaly, decision, exc=CapabilityRefused, reacted="blocked", violation=violation)

    def _admit_actions(self, session: SessionState) -> None:
        """Refuse the ``(max_actions_per_run + 1)``\\ th executed action."""
        limit = self.config.max_actions_per_run
        if limit is None:
            return
        with session.lock:
            executed = session.executed_actions
        decision = admission.actions(executed, limit)
        if decision.verdict == "allow":
            return
        anomaly = _anomaly(
            session, FANOUT_DETECTOR, "critical",
            f"Action limit reached{{whose}}: {decision.reason}",
            {"rule": "max_actions_per_run", "limit": limit, "executed": executed},
        )
        self.refuse(session, anomaly, decision, reacted="door")

    # --- the tool policy -------------------------------------------------------------

    def enforce_policy(self, session: SessionState, call: ToolCall, stage: str = "all") -> None:
        """Apply the action policy to one attempted tool call (``tool_calls`` already counts it).

        The reaction is the policy's own ``on_violation``: ``"dry_run"`` alerts what would have been
        refused; ``"block"`` refuses; ``"block_and_latch"`` also stops the session. A policy we cannot
        evaluate refuses (a built-in failing); a predicate that raises is a refusal too.
        """
        policy = self._policy()
        if policy is None:
            return
        with session.lock:
            calls_so_far = session.counted_calls(call.name)
        try:
            violation = evaluate(policy, call, calls_so_far, stage)
        except Exception:
            _LOG.warning("runbound could not evaluate the tool policy for %r", call.name, exc_info=True)
            self._refuse_stage(
                session, call, hooks_module.StageRefused("hook_error", "policy evaluation failed", "policy")
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
        self.refuse(
            session, anomaly, decision, exc=PolicyViolation, reacted="blocked",
            latch=policy.on_violation == "block_and_latch", violation=violation,
        )

    def _policy(self) -> ToolPolicy | None:
        """The policy to enforce: the customer's, plus the org's if there is one.

        Combined by :func:`runbound.policy.merge` (which can only remove freedom)
        and cached until the plane states a new version. A merge that cannot be
        honored is logged and the local policy alone applies.
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
                "runbound: the org tool policy could not be merged; enforcing the local one",
                exc_info=True,
            )
            merged = local
        self._merged_key = key
        self._merged = merged
        return merged

    def _local_policy(self) -> ToolPolicy | None:
        """What the decorators say, folded over what ``init()`` said.

        Folded on demand, because decorators run *after* ``init()``; cached on the
        registry's version and the configured policy's identity, and the *same
        object* comes back while neither moves (:meth:`_policy` keys on its ``id()``).
        """
        configured = self._configured_policy()
        try:
            key = (_coverage.tool_rules_version(), id(configured))
            if self._local_key == key:
                return self._local
            policy = self._fold(configured)
        except Exception:
            _LOG.warning(
                "runbound: the decorator rules could not be folded into the tool "
                "policy; enforcing the configured one",
                exc_info=True,
            )
            return configured
        # The value before the key: a racing reader sees the old key (refolds) or a matching pair.
        self._local = policy
        self._local_key = key
        return policy

    def _fold(self, configured: ToolPolicy | None) -> ToolPolicy | None:
        """``configured`` with every ``@runbound.tool`` rule folded onto it."""
        rules = _coverage.tool_rules()
        for tool in conflicts(rules, configured):
            if tool in self._policy_conflicts_warned:
                continue
            self._policy_conflicts_warned.add(tool)
            _LOG.warning(
                "runbound: tool %r has a rule on both @runbound.tool and "
                "init(tool_policy=...); the decorator's rule is the one enforced",
                tool,
            )
        return from_decorators(rules, configured)

    def _configured_policy(self) -> ToolPolicy | None:
        """The ``init()`` policy, coerced from a dict if it still is one."""
        policy = self.config.tool_policy
        if policy is None or isinstance(policy, ToolPolicy):
            return policy
        try:
            policy = coerce(policy)
        except ValueError:
            _LOG.warning("runbound: tool_policy is not usable; no policy is enforced", exc_info=True)
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

    # --- refusing, alerting, observing -----------------------------------------------

    def refuse(
        self,
        session: "SessionState | None",
        anomaly: Anomaly,
        decision: Decision,
        *,
        exc: type = GuardrailTripped,
        reacted: str | None = None,
        latch: bool = False,
        **exc_kwargs: Any,
    ) -> None:
        """The single exit for a refusal site: stamp, latch, alert, raise.

        ``decision`` is stamped onto ``anomaly.details["decision"]`` (what
        ``exc.decision`` reads); a broken observer never loses the refusal; then
        ``exc`` is raised with the stamped anomaly. Never returns.
        """
        stamped = _stamp_decision(anomaly, decision, session=session)
        if latch and session is not None:
            self._latch(session, stamped)
        try:
            self._alert(stamped, session, reacted, refusal=True)
            _LOG.warning("[runbound] %s", stamped.message)
        except Exception:
            _LOG.warning("runbound could not alert on a refusal; the refusal still stands", exc_info=True)
        raise exc(stamped, **exc_kwargs)

    def _latch(self, session: SessionState, anomaly: Anomaly) -> None:
        """Stop this session on ``anomaly``; the first latch wins and keeps its reason."""
        with session.lock:
            if session.tripped_by is not None:
                return
            session.tripped_by = anomaly
            session.tripped_at = time.monotonic()

    def _alert(
        self,
        anomaly: Anomaly,
        session: SessionState,
        reacted: str | None = None,
        *,
        refusal: bool = False,
    ) -> None:
        """Notify the observers.

        **A refusal is not a notification.** Every refusal is evidence, recorded
        and exported as its own anomaly and Decision up to
        :data:`REFUSAL_RECORD_CAP` per ``(session, detector, rule, tool)``, past
        which they are counted and summarised (:meth:`flush_refusal_summaries`).
        Anything else (a dry run's notice) is told once per key.
        """
        key = (
            getattr(session, "session_id", ""),
            anomaly.detector,
            anomaly.severity,
            _detail(anomaly, "rule", None),
            _detail(anomaly, "tool", None),
        )
        if refusal:
            self._alerted.add(key)
            if not self._refusals.admit(getattr(session, "session_id", ""), anomaly, reacted):
                return
        elif key in self._alerted:
            return
        else:
            self._alerted.add(key)
        self._notify_anomaly(session, anomaly, reacted or "warn")

    def flush_refusal_summaries(self, session: SessionState) -> None:
        """Report what this session's refusals suppressed since the last call."""
        try:
            for summary, reacted in self._refusals.summaries(getattr(session, "session_id", "")):
                self._notify_anomaly(session, summary, reacted)
        except Exception:
            _LOG.warning("runbound could not summarise suppressed refusals", exc_info=True)

    def _notify_event(self, session: SessionState, event: Event) -> None:
        """Hand one recorded event to every observer."""
        for observer in self.observers:
            try:
                observer.on_event(session, event)
            except Exception:
                _LOG.warning(
                    "runbound observer %s failed on an event", type(observer).__name__, exc_info=True
                )

    def _notify_anomaly(self, session: SessionState, anomaly: Anomaly, reacted: str) -> None:
        """Hand one verdict, and what it cost, to every observer. Never raises."""
        try:
            local_events.record_anomaly(
                getattr(session, "session_id", None), anomaly, reacted, getattr(session, "key", None)
            )
        except Exception:
            _LOG.warning("runbound could not record a local event", exc_info=True)
        for observer in self.observers:
            try:
                observer.on_anomaly(session, anomaly, reacted)
            except Exception:
                _LOG.warning(
                    "runbound observer %s failed on an anomaly", type(observer).__name__, exc_info=True
                )


def _anomaly(session: SessionState, detector: str, severity: str, lead: str, details: dict) -> Anomaly:
    """An anomaly naming the session (key, id, tags) and the tool; never the arguments."""
    key = getattr(session, "key", None)
    whose = f" for session {key!r}" if key else ""
    return Anomaly(
        detector=detector,
        severity=severity,
        message=lead.replace("{whose}", whose),
        details={
            "session_id": getattr(session, "session_id", ""),
            "key": key,
            "tags": dict(getattr(session, "tags", {}) or {}),
            **details,
        },
    )


def _policy_anomaly(session: SessionState, violation: Violation, dry_run: bool) -> Anomaly:
    """Describe a refused (or would-be-refused) tool call.

    Names the session, the tool and the rule; never the arguments. A dry run is
    a "warn" — nothing was actually stopped — and everything else is critical.
    """
    lead = "Policy dry-run: would block tool" if dry_run else "Policy blocked tool"
    return _anomaly(
        session, POLICY_DETECTOR, "warn" if dry_run else "critical",
        f"{lead} {violation.tool!r}{{whose}}: {violation.reason}",
        {"tool": violation.tool, "rule": violation.rule, **violation.details},
    )


def _policy_decision(violation: Violation) -> Decision:
    """The :class:`Decision` behind one policy violation.

    ``evaluation`` carries whatever numbers the violation stated (``count`` and
    ``limit`` for ``max_calls``) — never call arguments.
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


def _stamp_decision(
    anomaly: Anomaly, decision: Decision, *, session: "SessionState | None" = None
) -> Anomaly:
    """``anomaly`` with ``decision`` stamped onto ``details["decision"]``.

    ``details["key_hash"]`` is an unsalted sha256 hash of the session's key
    (``None`` for the default, unkeyed session) — together with
    ``decision.level`` this is what ``exc.scope`` reads. Keeps the anomaly's
    own ``anomaly_id``.
    """
    key = getattr(session, "key", None) if session is not None else None
    key_hash = _key_hash_fn(key) if key else None
    details = anomaly.details if isinstance(anomaly.details, dict) else {}
    record = _RECORD.get()
    if record:  # the decision record rides the Decision's evaluation and the anomaly (WIRE-TR2 §7, §10f)
        record = {**record, "reason_code": _reason_code(anomaly, decision)}
        decision = dataclasses.replace(decision, evaluation={**decision.evaluation, **record})
        details = {**details, **record}
    return dataclasses.replace(
        anomaly, details={**details, "decision": decision.as_dict(), "key_hash": key_hash}
    )


#: The record of the call being admitted (set per call in the engine; read by ``_stamp_decision``).
_RECORD: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar("runbound_record", default=None)
#: The repetition counts the call being admitted holds, given back if it does not clear the chain.
_KEYS: "contextvars.ContextVar[list | None]" = contextvars.ContextVar("runbound_keys", default=None)
_REPEATS: "contextvars.ContextVar[list | None]" = contextvars.ContextVar("runbound_repeats", default=None)


def _reason_code(anomaly: Anomaly, decision: Decision) -> str:
    """The refusal's primary reason code: one of the SDK's existing ``REASONS``, verbatim (WIRE-TR2 §7)."""
    from .exceptions import _VIOLATION_RULE_REASON, _infer_reason

    evaluation = decision.evaluation if isinstance(decision.evaluation, dict) else {}
    if decision.boundary == "policy" and evaluation.get("rule") in _VIOLATION_RULE_REASON:
        return _VIOLATION_RULE_REASON[evaluation["rule"]]
    return _infer_reason(anomaly, decision, None)


def _is_dry_run(violation: Violation) -> bool:
    """Is this violation one the org is still rolling out? (reported, never enforced)"""
    details = violation.details
    return isinstance(details, dict) and details.get("dry_run") is True


def _detail(anomaly: Anomaly, key: str, default):
    """One value out of an anomaly's details, tolerating a malformed one."""
    details = anomaly.details
    if not isinstance(details, dict):
        return default
    value = details.get(key, default)
    return default if value is None else value
