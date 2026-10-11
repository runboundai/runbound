"""Admission — pure stage functions, numbers in and a :class:`Decision` out.

Every function here is plain arithmetic and comparison: no lock, no I/O, no
session object, no clock read, so each is table-driven to test without mocks
and safe to call from inside a section that already holds ``session.lock``.

Each stage answers one question and returns :data:`~runbound.events.ALLOW` —
the same shared instance, so the allow path allocates nothing — or a fresh,
denying :class:`~runbound.events.Decision`. Nothing here raises, alerts or
latches: that is :mod:`runbound.engine`'s job, once it has a Decision in hand.

The boundary condition is the same everywhere: "exactly at the limit still
allows; one over denies".
"""

from .events import ALLOW, Decision


def actions(executed: int, max_actions_per_run: int | None) -> Decision:
    """Would the action about to run be the ``(max_actions_per_run + 1)``\\ th *executed* one?

    ``executed`` is this session's ``executed_actions`` count *before* the
    attempt being judged — an admission refusal must not itself count as having
    executed. ``executed >= max_actions_per_run`` means this attempt, if
    admitted, would push the count over the limit.
    """
    if max_actions_per_run is None or executed < max_actions_per_run:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="action",
        boundary="blast_radius",
        detector="fanout",
        reason=(
            f"action limit reached: {executed} actions already executed this run, "
            f"limit {max_actions_per_run}"
        ),
        evaluation={"limit": max_actions_per_run, "used": executed + 1},
    )


def capability(verdict: str, denied_class: str) -> Decision:
    """What an ``init(capabilities=...)`` class rule says about a tool's class."""
    if verdict == "allow":
        return ALLOW
    return Decision(
        verdict="restrict" if verdict == "approve" else "deny",
        kind="action",
        boundary="capability",
        level="process",  # init(capabilities=...) is this process's own rule
        detector="capability",
        reason=f"a capability class rule on init() denies {denied_class}",
        evaluation={"denied_class": denied_class},
    )


def halted(is_halted: bool, reason: str) -> Decision:
    """Has the control plane halted the fleet? Every tool is refused while it has."""
    if not is_halted:
        return ALLOW
    return Decision(
        verdict="deny",
        kind="action",
        boundary="halt",
        level="fleet",
        detector="halt",
        reason=reason,
        evaluation={},
    )


def row(code: str, reason: str, retry_after_s: float | None = None) -> Decision:
    """A dashboard policy row's rule (``tool_blocked``, ``session_max_calls``, ``quota_exceeded``,
    ``quota_plane_unavailable``): ``code`` is the refusal reason."""
    evaluation: dict = {"code": code}
    if retry_after_s is not None:
        evaluation["retry_after_s"] = retry_after_s
    return Decision(verdict="deny", kind="action", boundary="row", detector="policy", reason=reason, evaluation=evaluation)


def plane_unavailable(reason: str) -> Decision:
    """The plane is gone and this tool's class does not run on a cache alone (S7)."""
    return Decision(
        verdict="deny",
        kind="action",
        boundary="plane",
        level="fleet",
        detector="plane",
        reason=reason,
        evaluation={"code": "plane_unavailable"},
    )


def arguments(reason: str) -> Decision:
    """The arguments did not fit the tool's signature or type hints (slot 1)."""
    return Decision(
        verdict="deny",
        kind="action",
        boundary="arguments",
        detector="validation",
        reason=reason,
        evaluation={},
    )


def approval(code: str, reason: str, retry_after_s: float | None = None) -> Decision:
    """A person's answer (or the lack of one) to ``approval=\"required\"``: ``code`` is the refusal reason."""
    return Decision(
        verdict="deny",
        kind="action",
        boundary="approval",
        detector="approval",
        reason=reason,
        evaluation={"code": code} if retry_after_s is None else {"code": code, "retry_after_s": retry_after_s},
    )


def hook(stage: str, reason: str, name: str | None = None) -> Decision:
    """A user pre-hook refused (``stage="hook"``) or failed (``stage="hook_error"``)."""
    return Decision(
        verdict="deny",
        kind="action",
        boundary=stage,
        detector="hook",
        reason=reason,
        evaluation={} if name is None else {"hook": name},
    )
