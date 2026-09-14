"""Action policy — the deterministic rules a customer states about tools.

Detectors learn what a session normally does; a policy states what an agent is
*allowed* to do, and neither the model nor a judge gets a vote. Rules are the
customer's ("this agent may never call ``wire_money``", "at most one refund per
session", "a send over $500 needs a human"); this module only decides whether
one tool call breaks one of them, in a fixed order, with no I/O and no state.

``evaluate`` takes plain data in and returns plain data out — the engine owns
the consequence, the wrappers own the hook.

A customer states those rules on the tool itself, as keyword arguments to
``@runbound.tool``; :class:`ToolRules` is one such statement and
:func:`from_decorators` folds a registry of them into the very
:class:`ToolPolicy` the customer could have written by hand. Nothing about
:func:`evaluate` or :func:`merge` knows the difference, which is the point.

**The one deliberate exception to fail-open.** Everywhere else in runbound a
bug in guardrail code is logged and swallowed so the host's call proceeds. A
customer's constraint predicate or approval callback is different: it is a
permission gate the customer opted into, and a gate that cannot answer must not
wave the call through. A predicate that raises therefore counts as a violation
(fail-CLOSED) — the tool is refused, exactly as if the predicate had returned
``False``. Only the exception's *type* is reported; its message may quote the
arguments it choked on, and raw arguments never leave this module.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace

_LOG = logging.getLogger("runbound")

#: What a violation does. "block": refuse this call. "block_and_latch": refuse
#: it and stop the session (honoring ``on_trip``). "dry_run": allow the call,
#: log and alert what would have been refused — how a policy is rolled out.
ON_VIOLATION_MODES = ("block", "block_and_latch", "dry_run")

#: How strict each mode is; merging two policies keeps the stricter one.
_STRICTNESS = {"dry_run": 0, "block": 1, "block_and_latch": 2}

#: The rules, in the order :func:`evaluate` applies them.
RULES = ("deny", "allow", "max_calls", "constraint", "approval")

#: ``details["origin"]`` on a violation of a rule that came from the org
#: (fleet) policy rather than from the local one.
ORIGIN_ORG = "org"


@dataclass(frozen=True)
class ToolCall:
    """One attempted tool call, as the policy sees it.

    Handed to the customer's own predicates so they can decide on the real
    arguments. It is never stored, logged, or put in an anomaly: ``args`` and
    ``kwargs`` live for the duration of the predicate call only. (The loop
    detector separately keeps a salted digest of a tool call's arguments —
    ``api._args_hash`` — never the arguments themselves.)
    ``session_key`` and ``tags`` are the calling session's identity, so a rule
    can say "free-tier users may not do this".
    """

    name: str
    args: tuple
    kwargs: dict
    session_key: str | None
    tags: dict


@dataclass(frozen=True)
class Violation:
    """The rule one tool call broke, and why.

    ``reason`` is human-readable and carries the numbers; ``details`` carries
    the same machine-readably. Neither ever contains call arguments.
    """

    tool: str
    rule: str  # one of RULES
    reason: str
    details: dict


@dataclass
class ToolPolicy:
    """What an agent's tools may and may not do, stated by the customer.

    Every field is optional and an empty policy permits everything, so a
    policy only ever *removes* freedom. ``allow`` is the exception that
    inverts: set it and the listed tools are the only ones permitted.
    """

    deny: list[str] = field(default_factory=list)  # never allowed
    allow: list[str] | None = None  # if set: ONLY these
    max_calls: dict[str, int] = field(default_factory=dict)  # per session, per tool
    constraints: dict[str, Callable[[ToolCall], bool]] = field(default_factory=dict)
    # a constraint returns True to allow the call
    require_approval: list[str] = field(default_factory=list)
    approval_callback: Callable[[ToolCall], bool] | None = None
    on_violation: str = "block"  # "block" | "block_and_latch" | "dry_run"

    # Bookkeeping written by :func:`merge` and read by :func:`evaluate`: which
    # rules came from the org policy, and whether the org policy is being
    # rolled out dry. Not part of what the customer wrote, so out of repr and
    # equality — two policies with the same rules are the same policy.
    _origins: dict[str, set[str]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _remote_dry_run: bool = field(default=False, repr=False, compare=False)

    def validate(self) -> None:
        """Raise ``ValueError`` if this policy cannot be enforced as written.

        Rejects an unknown ``on_violation`` mode, tools needing approval with
        no callback to ask, a per-session limit that is not a positive int,
        a tool listed in both ``deny`` and ``allow`` (the two disagree about
        the same tool), and tool names that are not strings.
        """
        if self.on_violation not in ON_VIOLATION_MODES:
            raise ValueError(
                f"on_violation must be one of {ON_VIOLATION_MODES}, "
                f"got {self.on_violation!r}"
            )
        for name in ("deny", "allow", "require_approval"):
            self._validate_names(name, getattr(self, name))
        if self.require_approval and self.approval_callback is None:
            raise ValueError(
                "require_approval needs an approval_callback to ask; "
                f"got require_approval={list(self.require_approval)!r}"
            )
        for tool, limit in self.max_calls.items():
            if not isinstance(limit, int) or limit < 1:
                raise ValueError(
                    f"max_calls[{tool!r}] must be an int >= 1, got {limit!r}"
                )
        both = sorted(set(self.deny) & set(self.allow or ()))
        if both:
            raise ValueError(f"tools listed in both deny and allow: {both}")

    @staticmethod
    def _validate_names(field_name: str, names) -> None:
        """Every tool name in a list field must be a string."""
        for name in names or ():
            if not isinstance(name, str):
                raise ValueError(
                    f"{field_name} entries must be tool names (str), got {name!r}"
                )


def coerce(policy: "ToolPolicy | dict | None") -> "ToolPolicy | None":
    """Turn a policy written as a plain dict into a :class:`ToolPolicy`.

    ``None`` and an existing policy pass through untouched. A dict with an
    unknown key fails here — at configuration time, loudly — rather than
    silently dropping a rule the customer believed was being enforced.
    """
    if policy is None or isinstance(policy, ToolPolicy):
        return policy
    if not isinstance(policy, dict):
        raise ValueError(
            "tool_policy must be a ToolPolicy, a dict of its fields, or None; "
            f"got {type(policy).__name__}"
        )
    try:
        return ToolPolicy(**policy)
    except TypeError as exc:
        public = [f.name for f in fields(ToolPolicy) if not f.name.startswith("_")]
        known = ", ".join(public)
        raise ValueError(f"tool_policy dict is not a ToolPolicy ({exc}); fields: {known}") from exc


# --- rules stated on the decorator ------------------------------------------


@dataclass(frozen=True)
class ToolRules:
    """The rule one ``@runbound.tool`` states about its own tool.

    The keyword arguments of the decorator, kept together so the registry the
    engine folds is a mapping of tool name to *this*, not five parallel dicts.
    Every field is optional and an empty ``ToolRules`` states nothing at all —
    a known tool with no rule, which is a perfectly good thing to be unless
    ``require_rules`` is on.

    ``reviewed`` is **not** :attr:`ToolPolicy.allow`. It means "this tool was
    reviewed and is deliberately unrestricted" and exists only so a tool can
    satisfy ``require_rules`` without being given a rule it does not need.
    :func:`from_decorators` therefore never folds it anywhere — see that
    function's docstring for what folding it would do.
    """

    blocked: bool = False
    max_calls: int | None = None
    constraint: Callable[[ToolCall], bool] | None = None
    require_approval: Callable[[ToolCall], bool] | None = None
    reviewed: bool = False

    def stated(self) -> bool:
        """Does this state a rule at all?

        ``repeatable`` and ``name`` are not rules and are not here: one marks
        polling and the other renames the tool, and neither removes any
        freedom from the agent.
        """
        return bool(
            self.blocked
            or self.max_calls is not None
            or self.constraint is not None
            or self.require_approval is not None
            or self.reviewed
        )

    def as_report(self) -> dict:
        """The rules as the tool report carries them: JSON, and only what was said.

        A predicate becomes ``"module:qualname"`` — enough for a console to
        show which function governs a tool, and never the function itself: the
        report goes over the wire, and a customer's code is never shipped or
        called off-process. A rule that was not stated is absent rather than
        null, so an unruled tool reports ``{}`` and its entry's hash does not
        move when this key was added.
        """
        report: dict = {}
        if self.blocked:
            report["blocked"] = True
        if self.max_calls is not None:
            report["max_calls"] = self.max_calls
        if self.constraint is not None:
            report["constraint"] = _dotted(self.constraint)
        if self.require_approval is not None:
            report["require_approval"] = _dotted(self.require_approval)
        if self.reviewed:
            report["reviewed"] = True
        return report


def _dotted(func: Callable) -> str:
    """``"module:qualname"`` for a callable, with never an address in it.

    An entry whose text changed on every restart would resend the whole tool
    report on every heartbeat of every worker and tell the console nothing, so
    anything that will not name itself falls back to its *type's* name — the
    same trade :func:`runbound._coverage._annotation` makes.
    """
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        kind = type(func)
        module, qualname = kind.__module__, kind.__qualname__
    return f"{module}:{qualname}"


def from_decorators(
    rules: Mapping[str, ToolRules], base: "ToolPolicy | dict | None" = None
) -> "ToolPolicy | None":
    """Fold the rules stated on the decorators onto the configured policy.

    The result is an ordinary :class:`ToolPolicy` — ``blocked`` becomes a
    ``deny`` entry (so the rule name in a refusal and in the ledger is the one
    it always was), ``max_calls`` and ``constraint`` become that tool's entry
    in those dicts, and ``require_approval`` puts the tool's name on the list
    with one dispatching :attr:`~ToolPolicy.approval_callback` that routes each
    tool's question to that tool's own callable.

    **Where both sides name a tool, the decorator wins**, because the rule that
    ships in the same diff as the function is the one that was reviewed with
    it; a tool the decorator blocks is dropped from ``base``'s ``allow`` list,
    exactly as :func:`merge` resolves the same disagreement. The caller is the
    one that warns about it (see :func:`conflicts`) — this function is pure.

    ``ToolRules.reviewed`` is folded **nowhere**. :attr:`ToolPolicy.allow` is an
    inverting allow-list: putting one reviewed tool on it would make that tool
    the only permitted one in the whole process and deny every other. The
    decorator's ``reviewed=True`` says only "looked at, deliberately unrestricted",
    and the fleet-wide allow-list stays a thing ``init(tool_policy=...)`` says.

    Pure, and cheap to call again: ``base`` is neither read after this returns
    nor mutated, and when no rule is stated ``base`` itself comes straight back
    — the same object, so a caller caching on its identity does not thrash.
    """
    base = coerce(base)
    stated = {
        name: rule
        for name, rule in (rules or {}).items()
        if rule is not None and rule.stated()
    }
    if not stated:
        return base

    policy = ToolPolicy() if base is None else replace(base, **_copied(base))
    approvals: dict[str, Callable] = {}
    for name, rule in stated.items():
        if rule.blocked and name not in policy.deny:
            policy.deny.append(name)
        if rule.max_calls is not None:
            policy.max_calls[name] = rule.max_calls
        if rule.constraint is not None:
            policy.constraints[name] = rule.constraint
        if rule.require_approval is not None:
            approvals[name] = rule.require_approval
            if name not in policy.require_approval:
                policy.require_approval.append(name)
    if policy.allow is not None:
        policy.allow = [name for name in policy.allow if name not in policy.deny]
    if approvals:
        policy.approval_callback = _ApprovalDispatch(approvals, policy.approval_callback)
    policy.validate()
    return policy


def conflicts(
    rules: Mapping[str, ToolRules], base: "ToolPolicy | dict | None"
) -> list[str]:
    """Tools whose rule a decorator *replaces* something ``base`` said about, sorted.

    :func:`from_decorators` resolves every one of these in the decorator's
    favor and says nothing; this is what a caller warns with, so the customer
    learns that the entry they wrote on ``init()`` is not the one enforced.

    Deliberately narrow — only where the decorator actually takes something
    away. A tool that both sides ``deny`` agree with each other, and a tool on
    ``base``'s fleet-wide ``allow`` list that merely carries a decorator rule
    is still on that list: neither is worth a warning, and a warning nobody
    needs is how people learn to ignore them.
    """
    if base is None:
        return []
    try:
        base = coerce(base)
    except ValueError:
        return []
    return sorted(
        name
        for name, rule in (rules or {}).items()
        if rule is not None and _replaces(name, rule, base)
    )


def _replaces(name: str, rule: ToolRules, base: ToolPolicy) -> bool:
    """Does ``rule`` overrule what ``base`` states about this one tool?"""
    return bool(
        (rule.blocked and name in (base.allow or ()))
        or (rule.max_calls is not None and name in base.max_calls)
        or (rule.constraint is not None and name in base.constraints)
        or (rule.require_approval is not None and name in base.require_approval)
    )


class _ApprovalDispatch:
    """One :attr:`ToolPolicy.approval_callback` over many per-tool callables.

    ``ToolPolicy`` asks one question — "may this call happen?" — of one
    callback, while a decorator gives a different callable to each tool. This
    routes on ``call.name``, falls through to whatever callback the customer
    configured on ``init()`` for a tool no decorator claimed, and **refuses**
    when there is neither: an approval gate nobody can answer must not wave the
    call through (fail-CLOSED, see the module docstring). A callable that
    raises is left to raise — :func:`evaluate` turns that into the same
    refusal, reporting only the exception's type.
    """

    def __init__(
        self, callbacks: Mapping[str, Callable], fallback: Callable | None
    ) -> None:
        self._callbacks = dict(callbacks)
        self._fallback = fallback
        self._warned: set[str] = set()

    def __call__(self, call: ToolCall) -> bool:
        callback = self._callbacks.get(call.name, self._fallback)
        if callback is None:
            self._no_approver(call.name)
            return False
        return bool(callback(call))

    def _no_approver(self, tool: str) -> None:
        """Say once per tool that its approval could not be asked of anyone."""
        if tool in self._warned:
            return
        self._warned.add(tool)
        _LOG.warning(
            "runbound: tool %r requires approval and no approval callback is "
            "registered for it; the call was refused",
            tool,
        )


def merge(
    local: "ToolPolicy | dict | None",
    remote: "ToolPolicy | dict | None",
    *,
    remote_dry_run: bool = False,
) -> "ToolPolicy | None":
    """Combine the local policy with an org policy into one that enforces both.

    The merge can only ever *remove* freedom: bans are unioned (``deny``,
    ``require_approval``), permissions are intersected (``allow``, when both
    sides state one), and a per-session limit becomes the lower of the two.
    Callables are never taken from the org side — a remote policy arrives over
    the wire and cannot carry code — so ``constraints`` and
    ``approval_callback`` stay exactly as the customer wrote them locally.
    ``on_violation`` stays local unless the org states one and is being
    enforced, in which case the stricter of the two wins
    (``dry_run`` < ``block`` < ``block_and_latch``).

    A tool the org denies is dropped from a local ``allow`` list rather than
    reported as a conflict: deny always wins. Anything the merged policy
    genuinely cannot enforce raises ``ValueError`` here — notably an org rule
    requiring approval when no local ``approval_callback`` exists to ask.

    Which rules came from the org is remembered, so a violation of one is
    reported with ``details["origin"] == "org"``. With ``remote_dry_run`` those
    violations also carry ``details["dry_run"] is True``, leaving the decision
    to log rather than block to the engine; local rules are untouched by it.

    Pure: neither input is read after this returns, and neither is mutated.
    ``None`` on both sides is ``None``; one side alone is a copy of the other.
    """
    remote_states_mode = _states_mode(remote)
    local = coerce(local)
    remote = coerce(remote)
    if remote is None:
        return None if local is None else replace(local, **_copied(local))
    if local is None:
        local = ToolPolicy()

    origins: dict[str, set[str]] = {}
    deny = _union(local.deny, remote.deny, origins, "deny")
    merged = ToolPolicy(
        deny=deny,
        allow=_merge_allow(local, remote, origins, deny),
        max_calls=_merge_max_calls(local, remote, origins),
        constraints=dict(local.constraints),
        require_approval=_union(
            local.require_approval, remote.require_approval, origins, "approval"
        ),
        approval_callback=local.approval_callback,
        on_violation=_merge_mode(local, remote, remote_states_mode, remote_dry_run),
        _origins=origins,
        _remote_dry_run=remote_dry_run,
    )
    merged.validate()
    return merged


def _copied(policy: ToolPolicy) -> dict:
    """The mutable fields of ``policy``, copied, so no container is shared."""
    return {
        "deny": list(policy.deny),
        "allow": None if policy.allow is None else list(policy.allow),
        "max_calls": dict(policy.max_calls),
        "constraints": dict(policy.constraints),
        "require_approval": list(policy.require_approval),
        "_origins": {key: set(value) for key, value in policy._origins.items()},
    }


def _states_mode(remote: "ToolPolicy | dict | None") -> bool:
    """Did the org actually state an ``on_violation``, or just take the default?

    A dict is the wire form, so a missing key means "no opinion"; a
    :class:`ToolPolicy` always carries a mode.
    """
    return not isinstance(remote, dict) or "on_violation" in remote


def _mark(origins: dict[str, set[str]], rule: str, tool: str | None = None) -> None:
    """Record that one rule (optionally for one tool) came from the org."""
    origins["%s:%s" % (rule, tool) if tool is not None else rule] = {ORIGIN_ORG}


def _union(
    local: list[str], remote: list[str], origins: dict[str, set[str]], rule: str
) -> list[str]:
    """``local`` then the tools only the org named, which are marked as its own."""
    merged = list(local)
    for tool in remote:
        if tool not in merged:
            merged.append(tool)
            _mark(origins, rule, tool)
    return merged


def _merge_allow(
    local: ToolPolicy, remote: ToolPolicy, origins: dict[str, set[str]], deny: list[str]
) -> list[str] | None:
    """The intersection when both sides restrict, else whichever side does.

    Denied tools are removed here so deny and allow never disagree. When only
    the org restricts, every unlisted tool is its doing (the bare ``"allow"``
    origin); when both do, only the tools the org alone removed are.
    """
    if local.allow is None and remote.allow is None:
        return None
    if local.allow is None:
        _mark(origins, "allow")
        allowed = list(remote.allow)
    elif remote.allow is None:
        allowed = list(local.allow)
    else:
        allowed = [tool for tool in local.allow if tool in remote.allow]
        for tool in local.allow:
            if tool not in allowed:
                _mark(origins, "allow", tool)
    return [tool for tool in allowed if tool not in deny]


def _merge_max_calls(
    local: ToolPolicy, remote: ToolPolicy, origins: dict[str, set[str]]
) -> dict[str, int]:
    """The lower limit per tool, over every tool either side limits."""
    merged = dict(local.max_calls)
    for tool, limit in remote.max_calls.items():
        theirs = merged.get(tool)
        if theirs is None or _lower(limit, theirs):
            merged[tool] = limit
            _mark(origins, "max_calls", tool)
    return merged


def _lower(limit, other) -> bool:
    """Is ``limit`` the stricter of two limits? Non-ints sort last, so
    ``validate`` is the one that reports them."""
    try:
        return limit < other
    except TypeError:
        return False


def _merge_mode(
    local: ToolPolicy,
    remote: ToolPolicy,
    remote_states_mode: bool,
    remote_dry_run: bool,
) -> str:
    """The stricter of the two modes, unless the org has no say."""
    if remote_dry_run or not remote_states_mode:
        return local.on_violation
    ranked = [local.on_violation, remote.on_violation]
    if any(mode not in _STRICTNESS for mode in ranked):
        return remote.on_violation  # unenforceable; validate() reports it
    return max(ranked, key=_STRICTNESS.__getitem__)


def evaluate(policy: ToolPolicy, call: ToolCall, calls_so_far: int) -> Violation | None:
    """The rule ``call`` breaks under ``policy``, or ``None`` if it is allowed.

    Rules are applied in a fixed order — deny, allow, max_calls, constraint,
    approval — and the first one broken is reported, so an explicitly denied
    tool reads as "denied" however many other rules also cover it.

    ``calls_so_far`` counts this attempt: with a limit of 1, the first call
    passes and the second is a violation.

    A constraint or approval callback that raises is a violation (fail-closed,
    see the module docstring); only the exception type is reported.

    On a policy that came out of :func:`merge`, a violation of a rule the org
    contributed carries ``details["origin"] == "org"``, and additionally
    ``details["dry_run"] is True`` while the org policy is being rolled out
    dry — the rule itself is reported unchanged, and what to do about it is
    the engine's decision.
    """
    violation = (
        _check_deny(policy, call)
        or _check_allow(policy, call)
        or _check_max_calls(policy, call, calls_so_far)
        or _check_constraint(policy, call)
        or _check_approval(policy, call)
    )
    if violation is None:
        return None
    return _with_origin(policy, violation)


def _with_origin(policy: ToolPolicy, violation: Violation) -> Violation:
    """Tag ``violation`` when the rule it names came from the org policy.

    Looks for the rule keyed by tool first (``"deny:wire_money"``), then for
    the rule as a whole (``"allow"``, which no tool can be keyed by).
    """
    origins = getattr(policy, "_origins", None)
    if not origins:
        return violation
    rule = violation.rule
    keys = ("%s:%s" % (rule, violation.tool), rule)
    if not any(ORIGIN_ORG in origins.get(key, ()) for key in keys):
        return violation
    details = {**violation.details, "origin": ORIGIN_ORG}
    if getattr(policy, "_remote_dry_run", False):
        details["dry_run"] = True
    return replace(violation, details=details)


def _violation(call: ToolCall, rule: str, reason: str, **extra) -> Violation:
    """Build a violation whose details always name the tool and the rule."""
    return Violation(
        tool=call.name,
        rule=rule,
        reason=reason,
        details={"tool": call.name, "rule": rule, **extra},
    )


def _check_deny(policy: ToolPolicy, call: ToolCall) -> Violation | None:
    if call.name in policy.deny:
        return _violation(call, "deny", f"tool {call.name!r} is on the deny list")
    return None


def _check_allow(policy: ToolPolicy, call: ToolCall) -> Violation | None:
    if policy.allow is not None and call.name not in policy.allow:
        return _violation(
            call, "allow", f"tool {call.name!r} is not on the allow list"
        )
    return None


def _check_max_calls(
    policy: ToolPolicy, call: ToolCall, calls_so_far: int
) -> Violation | None:
    limit = policy.max_calls.get(call.name)
    if limit is None or calls_so_far <= limit:
        return None
    return _violation(
        call,
        "max_calls",
        f"tool {call.name!r} called {calls_so_far}x this session, "
        f"limit is {limit}",
        count=calls_so_far,
        limit=limit,
    )


def _check_constraint(policy: ToolPolicy, call: ToolCall) -> Violation | None:
    predicate = policy.constraints.get(call.name)
    if predicate is None:
        return None
    try:
        allowed = predicate(call)
    except Exception as exc:  # fail-CLOSED: a gate that errors must refuse
        return _gate_error(call, "constraint", exc)
    if allowed:
        return None
    return _violation(
        call, "constraint", f"tool {call.name!r} failed its constraint"
    )


def _check_approval(policy: ToolPolicy, call: ToolCall) -> Violation | None:
    if call.name not in policy.require_approval:
        return None
    callback = policy.approval_callback
    if callback is None:
        # validate() forbids this; an unvalidated policy still refuses rather
        # than quietly performing the action nobody could approve.
        return _violation(
            call,
            "approval",
            f"tool {call.name!r} requires approval but no approval_callback "
            "is configured",
        )
    try:
        approved = callback(call)
    except Exception as exc:  # fail-CLOSED
        return _gate_error(call, "approval", exc)
    if approved:
        return None
    return _violation(call, "approval", f"tool {call.name!r} was not approved")


def _gate_error(call: ToolCall, rule: str, exc: Exception) -> Violation:
    """A permission gate that raised: refuse, naming the error type only.

    The exception's message is deliberately dropped — it commonly quotes the
    argument the predicate choked on, and raw arguments never leave here.
    """
    error = type(exc).__name__
    return _violation(
        call,
        rule,
        f"{rule} for tool {call.name!r} raised {error}; refusing the call",
        error=error,
    )
