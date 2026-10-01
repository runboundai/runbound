"""Pure functions over a Controls body, and the SDK's own use of
them: tightening the plane's Controls against what this worker's code
already configured.

Two functions here, :func:`merge` and :func:`combine`, are a deliberate,
case-for-case port of the control plane's own merge module, of the same
shape. The SDK cannot import the plane (the public SDK tree must never
name a private monorepo path; ``scripts/lint_public_tree.py`` enforces it),
so "one definition of stricter" is kept honest a different way: both sides
are tested against the same table, ``tests/fixtures/controls_cases.json``
— read directly here by ``tests/test_controls_merge.py``, and by the
plane's and the dashboard's own suites, which point at this copy rather
than keep one of their own. A case that passes in one suite and fails in
the other is exactly the drift this setup exists to catch.

Five field families, one "stricter" direction each — see the plane's own
``merge.py`` docstring for the full reasoning; repeated here only where the
SDK's own use of these primitives needs it:

* ``limits`` — a per-level dict of numeric caps. Lower is stricter; ``None``
  ("no limit") is the loosest value a field can hold.
* ``capabilities`` — one of the six classes to a verdict, ranked ``allow <
  approve < deny``. Missing on either side reads as ``allow``.
* ``posture`` — ranked by :attr:`~runbound.posture.Posture.rank`. Not used
  by this worker's own merge (:attr:`Controls.posture` rides
  ``HelloReply.posture`` instead); kept here only so
  :func:`merge`/:func:`combine` agree with the plane case for case.
* ``detectors`` — per-detector ``{"action": "stop"|"notify", "mode":
  "shadow"|"enforce"}``, each ranked independently (``notify < stop``,
  ``shadow < enforce``). A name only one side states is taken from that
  side.
* ``envelope`` — ``bool | None``. ``None`` and ``False`` rank the same (the
  loosest); only ``True`` tightens.

Pure: no clock, no I/O, no lock. Plain dicts in, plain data out — safe to
call from inside a session lock, exactly like :mod:`runbound.admission`.

One function below, :func:`effective_detector`, is SDK-only and not
fixture-tested: the plane's own ``merge``/``combine`` have no notion of a
per-detector *code* baseline to protect at all (they only ever compare two
Controls bodies against each other — org vs service, draft vs served), so
there is nothing there to keep in step with the rule that a plane
``"stop"`` can only be *applied* when this worker's code can actually act
on one (``can_stop``) — see that function's docstring and
:meth:`runbound.engine.Engine._merge_detectors`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .policy import CAPABILITIES
from .posture import POSTURES, VERDICTS

#: The six levels a limit may be stated at (see INVARIANTS.md's chain bound).
LEVELS = ("org", "service", "agent", "key", "run", "action")

#: The Controls payload's numeric limit fields.
LIMIT_FIELDS = (
    "budget_usd",
    "window_s",
    "max_steps",
    "max_events",
    "loop_threshold",
    "max_cost_per_call_usd",
    "max_call_seconds",
    "max_tokens_out_per_call",
    #: A budget in tokens. Merged like ``budget_usd`` (the lower cap wins) and
    #: carried with the rest of a body's limits; this worker has no token
    #: budget of its own to hold it against, so nothing here enforces it.
    "budget_tokens",
)

#: The levels this worker can actually collapse into one effective number —
#: everything else (``agent``, ``key``, ``action``) is carried on the wire
#: (:class:`~runbound.plane_types.Controls`) but has no scope on this side
#: to enforce it against yet. ``run`` is where this worker's own
#: ``init()`` configuration lives — a session is a run.
ENFORCEABLE_LEVELS = ("org", "service", "run")

_VERDICT_RANK = {verdict: rank for rank, verdict in enumerate(VERDICTS)}
_ACTION_RANK = {"notify": 0, "stop": 1}
_MODE_RANK = {"shadow": 0, "enforce": 1}
_ENVELOPE_RANK = {None: 0, False: 0, True: 1}


@dataclass(frozen=True)
class Violation:
    """One field ``candidate`` loosens relative to ``base``, and what it was."""

    path: str
    base: Any
    candidate: Any

    def as_dict(self) -> dict:
        return {"path": self.path, "base": self.base, "candidate": self.candidate}


@dataclass(frozen=True)
class MergeResult:
    """Whether ``candidate`` tightens-or-matches ``base``, field by field."""

    accepted: bool
    merged: dict
    violations: tuple[Violation, ...] = ()


def merge(base: Mapping | None, candidate: Mapping) -> MergeResult:
    """``candidate`` against ``base`` — every field that would loosen it.

    ``base=None`` always reports accepted. This is a case-for-case port of
    the plane's own ``merge()``; kept for the shared fixture and for
    :func:`effective`, which reuses its per-field violations to know which
    of the plane's own values this worker refused.
    """
    if base is None:
        return MergeResult(True, dict(candidate), ())

    violations: list[Violation] = []
    violations += _limit_violations(base, candidate)
    violations += _capability_violations(base, candidate)
    violations += _posture_violation(base, candidate)
    violations += _detector_violations(base, candidate)
    violations += _envelope_violation(base, candidate)

    if violations:
        return MergeResult(False, dict(base), tuple(violations))
    return MergeResult(True, dict(candidate), ())


def combine(a: Mapping | None, b: Mapping | None) -> dict:
    """The field-wise strictest of two Controls bodies. A case-for-case port
    of the plane's own ``combine()`` — see that module for the full
    reasoning. Used here by :func:`effective` to fold this worker's own
    configuration together with what the plane states."""
    if a is None and b is None:
        return {}
    if a is None:
        return dict(b)
    if b is None:
        return dict(a)

    combined: dict[str, Any] = {}

    limits: dict[str, dict[str, float]] = {}
    a_limits = a.get("limits") or {}
    b_limits = b.get("limits") or {}
    for level in LEVELS:
        a_row = a_limits.get(level) or {}
        b_row = b_limits.get(level) or {}
        row = {
            field: value
            for field in LIMIT_FIELDS
            if (value := _stricter_limit(a_row.get(field), b_row.get(field))) is not None
        }
        if row:
            limits[level] = row
    if limits:
        combined["limits"] = limits

    a_caps = a.get("capabilities") or {}
    b_caps = b.get("capabilities") or {}
    caps = {
        name: value
        for name in CAPABILITIES
        if (value := _stricter_verdict(a_caps.get(name), b_caps.get(name))) is not None
    }
    if caps:
        combined["capabilities"] = caps

    posture = _stricter_posture(a.get("posture"), b.get("posture"))
    if posture is not None:
        combined["posture"] = posture

    a_detectors = a.get("detectors") or {}
    b_detectors = b.get("detectors") or {}
    detectors = {}
    for name in sorted(set(a_detectors) | set(b_detectors)):
        value = _stricter_detector(a_detectors.get(name), b_detectors.get(name))
        if value is not None:
            detectors[name] = value
    if detectors:
        combined["detectors"] = detectors

    envelope = _stricter_envelope(a.get("envelope"), b.get("envelope"))
    if envelope is not None:
        combined["envelope"] = envelope

    return combined


# --- limits -----------------------------------------------------------------


def _limit_violations(base: Mapping, candidate: Mapping) -> list[Violation]:
    base_limits = base.get("limits") or {}
    candidate_limits = candidate.get("limits") or {}
    violations = []
    for level in LEVELS:
        base_row = base_limits.get(level) or {}
        candidate_row = candidate_limits.get(level) or {}
        for field in LIMIT_FIELDS:
            old = base_row.get(field)
            new = candidate_row.get(field)
            if _stricter_limit(old, new) != new:
                violations.append(Violation(f"limits.{level}.{field}", old, new))
    return violations


def _stricter_limit(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


# --- capabilities -------------------------------------------------------------


def _capability_violations(base: Mapping, candidate: Mapping) -> list[Violation]:
    base_caps = base.get("capabilities") or {}
    candidate_caps = candidate.get("capabilities") or {}
    violations = []
    for name in CAPABILITIES:
        old = base_caps.get(name)
        new = candidate_caps.get(name)
        if _verdict_rank(old) > _verdict_rank(new):
            violations.append(Violation(f"capabilities.{name}", old, new))
    return violations


def _verdict_rank(verdict: str | None) -> int:
    return _VERDICT_RANK.get(verdict, _VERDICT_RANK["allow"])


def _stricter_verdict(a: str | None, b: str | None) -> str | None:
    return a if _verdict_rank(a) >= _verdict_rank(b) else b


# --- posture ------------------------------------------------------------------


def _posture_violation(base: Mapping, candidate: Mapping) -> list[Violation]:
    old = base.get("posture")
    new = candidate.get("posture")
    if _stricter_posture(old, new) != new:
        return [Violation("posture", old, new)]
    return []


def _posture_rank(name: str | None) -> int:
    if name is None:
        return -1
    posture = POSTURES.get(name)
    return posture.rank if posture is not None else -1


def _stricter_posture(a: str | None, b: str | None) -> str | None:
    return a if _posture_rank(a) >= _posture_rank(b) else b


# --- detectors ----------------------------------------------------------------


def _detector_violations(base: Mapping, candidate: Mapping) -> list[Violation]:
    base_detectors = base.get("detectors") or {}
    candidate_detectors = candidate.get("detectors") or {}
    violations = []
    for name in sorted(set(base_detectors) | set(candidate_detectors)):
        old = base_detectors.get(name)
        new = candidate_detectors.get(name)
        if old is None:
            continue
        if new is None:
            violations.append(Violation(f"detectors.{name}", old, new))
            continue
        old_action, new_action = old.get("action"), new.get("action")
        if _ACTION_RANK.get(old_action, 0) > _ACTION_RANK.get(new_action, 0):
            violations.append(Violation(f"detectors.{name}.action", old_action, new_action))
        old_mode, new_mode = old.get("mode"), new.get("mode")
        if _MODE_RANK.get(old_mode, 0) > _MODE_RANK.get(new_mode, 0):
            violations.append(Violation(f"detectors.{name}.mode", old_mode, new_mode))
    return violations


def _stricter_action(a: str | None, b: str | None) -> str | None:
    return a if _ACTION_RANK.get(a, 0) >= _ACTION_RANK.get(b, 0) else b


def _stricter_mode(a: str | None, b: str | None) -> str | None:
    return a if _MODE_RANK.get(a, 0) >= _MODE_RANK.get(b, 0) else b


def _stricter_detector(a: dict | None, b: dict | None) -> dict | None:
    if a is None:
        return b
    if b is None:
        return a
    return {
        "action": _stricter_action(a.get("action"), b.get("action")),
        "mode": _stricter_mode(a.get("mode"), b.get("mode")),
    }


# --- envelope -----------------------------------------------------------------


def _envelope_violation(base: Mapping, candidate: Mapping) -> list[Violation]:
    old = base.get("envelope")
    new = candidate.get("envelope")
    if _ENVELOPE_RANK.get(old, 0) > _ENVELOPE_RANK.get(new, 0):
        return [Violation("envelope", old, new)]
    return []


def _stricter_envelope(a: bool | None, b: bool | None) -> bool | None:
    return a if _ENVELOPE_RANK.get(a, 0) >= _ENVELOPE_RANK.get(b, 0) else b


# --- the SDK's own use: tighten the plane against this worker's own config --


def fold_levels(limits_by_level: Mapping | None) -> dict:
    """``{field: value}`` — the strictest value stated at any *enforceable*
    level (:data:`ENFORCEABLE_LEVELS`: org, service, run).

    ``agent``/``key``/``action`` are deliberately not folded in: this worker
    has no scope registry to enforce them against yet, and folding
    them in here would silently enforce a limit nothing said this process
    could check.
    """
    by_level = limits_by_level or {}
    folded: dict[str, float] = {}
    for level in ENFORCEABLE_LEVELS:
        row = by_level.get(level) if isinstance(by_level, Mapping) else None
        if not isinstance(row, Mapping):
            continue
        for field in LIMIT_FIELDS:
            # A value that is not a number is set aside for this field alone (see
            # :func:`malformed_limits`); it must not take the rest of the body with it.
            if field in row and _is_limit_value(row[field]):
                folded[field] = _stricter_limit(folded.get(field), row[field])
    return folded


def _is_limit_value(value: Any) -> bool:
    """A limit is a number or ``None`` ("no limit"). A bool, a string or NaN is neither."""
    if value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def malformed_limits(limits_by_level: Mapping | None) -> tuple:
    """``("limits.org.budget_usd", ...)`` — each limit an enforceable level states as
    something other than a number or ``None``. :func:`fold_levels` ignores these, so
    one bad value costs only its own limit; the caller says so once, in a log."""
    by_level = limits_by_level if isinstance(limits_by_level, Mapping) else {}
    found = []
    for level in ENFORCEABLE_LEVELS:
        row = by_level.get(level)
        if not isinstance(row, Mapping):
            continue
        found += [f"limits.{level}.{field}" for field in LIMIT_FIELDS if field in row and not _is_limit_value(row[field])]
    return tuple(found)


def effective_limits(code: Mapping | None, plane_limits_by_level: Mapping | None) -> tuple:
    """``(effective, violations)`` for every :data:`LIMIT_FIELDS` entry.

    ``code`` is ``{field: value}`` — this worker's own ``init()``
    configuration, flat (it has no notion of levels). ``plane_limits_by_
    level`` is the wire's own ``{level: {field: value}}``. The plane's
    levels are folded together first (:func:`fold_levels` — org, service
    and run compete freely among themselves, no protection needed there);
    the fold is then tightened against ``code`` last, which is where
    invariant 3's real protection lives — a folded plane value that is
    *looser* than what the code configured is refused, and ``code``'s own
    value is kept, named in ``violations``.

    Deliberately not a call through :func:`merge`/:func:`combine` (unlike
    :func:`effective_envelope`): those two treat an *absent* field as
    equivalent to "no limit" for ranking, which is right for comparing one
    whole Controls body against another (a draft-vs-served preview) but
    wrong here — the plane's payload routinely says nothing
    at all about a field the code cares about, and that silence must never
    be reported as the plane trying to loosen it. ``field in plane_folded``
    (true dict presence, which :func:`fold_levels` preserves) is what tells
    the two cases apart.
    """
    plane_folded = fold_levels(plane_limits_by_level)
    code = dict(code or {})
    effective: dict[str, float | None] = {}
    violations: list[Violation] = []
    for field in LIMIT_FIELDS:
        code_value = code.get(field)
        stated = field in plane_folded
        if not stated:
            effective[field] = code_value
            continue
        plane_value = plane_folded[field]
        result = _stricter_limit(code_value, plane_value)
        effective[field] = result
        if result != plane_value:
            violations.append(Violation(f"limits.{field}", code_value, plane_value))
    return effective, violations


def effective_capabilities(code: Mapping | None, plane: Mapping | None) -> tuple:
    """``(effective, violations)`` for the six capability classes.

    Unlike limits, capabilities are not leveled on the wire — a single
    ``{class: verdict}`` dict, already the plane's own org/service combine.
    Same reasoning as :func:`effective_limits` for not routing through
    :func:`merge`: a class the plane's payload never mentions must never be
    reported as a refused loosening of a class rule the code set.
    """
    code = dict(code or {})
    plane = dict(plane or {})
    effective: dict[str, str] = {}
    violations: list[Violation] = []
    for name in CAPABILITIES:
        code_value = code.get(name)
        stated = name in plane
        if not stated:
            if code_value is not None:
                effective[name] = code_value
            continue
        plane_value = plane[name]
        result = _stricter_verdict(code_value, plane_value)
        if result is not None:
            effective[name] = result
        if result != plane_value:
            violations.append(Violation(f"capabilities.{name}", code_value, plane_value))
    return effective, violations


def effective_envelope(code: bool | None, plane: bool | None, *, stated: bool) -> tuple:
    """``(effective, violation)`` for ``envelope``.

    ``stated`` is whether the plane's Controls body names ``envelope`` at
    all (``"envelope" in body`` at the call site). Unlike ``limits``/
    ``capabilities`` — a per-field presence test inside their own dict —
    this field has nothing finer to check, so the caller says. Not routed
    through :func:`merge` for the same reason :func:`effective_limits`
    isn't: ``merge``'s own ``_envelope_violation`` (correctly, for its own
    draft-vs-served use) reads *unstated* the same as *stated false* —
    both the loosest value — which would flag every worker whose plane
    payload simply never mentions ``envelope`` (the common case, since the
    SDK's own default is already ``True``) as a refused loosening.
    ``stated=False`` is therefore never a violation here, whatever ``code``
    was; only an explicit ``false`` that would truly loosen a ``True``
    ``code`` is.
    """
    if not stated:
        return code, None
    effective = _stricter_envelope(code, plane)
    violation = None
    if _ENVELOPE_RANK.get(code, 0) > _ENVELOPE_RANK.get(plane, 0):
        violation = Violation("envelope", code, plane)
    return effective, violation


def effective_detector(name: str, local_action: str, plane_spec: Any) -> tuple:
    """``(effective_spec, violations)`` for one detector name.

    ``local_action`` is what this worker's own configuration already does
    for ``name`` absent any plane Controls — ``"stop"`` or ``"notify"`` (see
    :meth:`runbound.engine.Engine._local_detector_action`, which reads each
    detector's own knob, not a blanket default). A plane's stated
    ``action`` can never rank looser than ``local_action`` in the result —
    invariant 3 — and, when it tried to, that is named in ``violations``
    (``"detectors.<name>.action"``).

    ``mode`` has no local concept of its own when ``local_action`` is
    ``"notify"`` (shadow mode does not exist without a plane, and nothing
    was being enforced to protect), so the plane's stated ``mode`` — or
    ``"enforce"`` when it states none — is simply adopted. When
    ``local_action`` is ``"stop"`` this worker is, by definition, already
    enforcing it for real: a plane ``mode: "shadow"`` here would turn a
    real stop into a preview, which is a loosening too, and is refused the
    same way (``"detectors.<name>.mode"``) — the merged ``mode`` stays
    ``"enforce"``.

    Neither this function nor its caller decides whether a ``"stop"`` this
    merge produces from a ``"notify"`` baseline can actually be *applied* —
    that depends on ``can_stop``, a runtime fact about the worker's
    ``on_anomaly`` this pure function has no way to see. See
    :meth:`runbound.engine.Engine._merge_detectors` for that gate and the
    distinct report it produces when it fails.

    ``(None, [])`` when the plane states nothing for this detector at all.
    """
    if not isinstance(plane_spec, Mapping):
        return None, []
    violations: list[Violation] = []

    plane_action = plane_spec.get("action")
    action = _stricter_action(local_action, plane_action)
    if plane_action is not None and _ACTION_RANK.get(action, 0) != _ACTION_RANK.get(plane_action, 0):
        violations.append(Violation(f"detectors.{name}.action", local_action, plane_action))

    plane_mode = plane_spec.get("mode")
    plane_mode = plane_mode if plane_mode in ("shadow", "enforce") else None
    if local_action == "stop":
        local_mode = "enforce"
        mode = _stricter_mode(local_mode, plane_mode) if plane_mode is not None else local_mode
        if plane_mode is not None and _MODE_RANK.get(mode, 0) != _MODE_RANK.get(plane_mode, 0):
            violations.append(Violation(f"detectors.{name}.mode", local_mode, plane_mode))
    else:
        mode = plane_mode or "enforce"

    return {"action": action, "mode": mode}, violations


# --- Local controls, tightened by an optional plane -----------------------
#
# Circuit rate mode, the loop shapes beyond "repeat", the budget soft
# line, max_actions_per_run and spike detection/the ladder are all real
# `init()` keywords -- every one of them a real, local `code` value, not
# a fiction. Each function below
# is the code-tightened-by-plane merge for one of them, the same shape as
# `effective_limits`/`effective_capabilities`/`effective_envelope` above:
# `code` is this worker's own configuration; the plane can only narrow it,
# or -- when `code` states nothing at all (count-mode circuit, no soft
# line, no action cap) -- state one from scratch. A plane value that would
# loosen an explicit `code` value is refused and named in `violations`,
# the same `controls_refusals()` mechanism `effective_limits` already
# feeds. Not part of the shared, fixture-tested `merge`/`combine` family
# (see `effective_detector`'s own docstring for why a function can live
# here without being in `controls_cases.json`): these six fields are new
# to the wire, SDK-and-plane-only, with no counterpart in the plane's own
# org/service Controls rows.
#
# Direction of "stricter" for each field is documented at its own
# function; the two already-settled rules ("the strictest spike reaction
# wins... limit > trip > notify... the longer cooldown counts as
# stricter") are carried forward unchanged.

#: Duplicated from `runbound.config.LOOP_SHAPES` on purpose, the same way
#: this module already duplicates the plane's own merge.py case for case
#: (see the module docstring): a plane payload is untrusted wire data, and
#: this module stays free of any dependency on `config.py`.
_LOOP_SHAPE_NAMES = ("repeat", "sequence", "retry", "stall")


def _parse_circuit_rate(value: Any) -> "dict | None":
    """A well-shaped rate-mode bundle from a plane's raw ``circuit_rate``
    value, every field defaulted the same way
    :class:`~runbound.config.GuardrailConfig` defaults them locally, or
    ``None`` for anything absent or malformed (fail-open to "state
    nothing")."""
    if not isinstance(value, Mapping):
        return None
    raw_min_calls = value.get("min_calls", 5)
    raw_half_open_calls = value.get("half_open_calls", 1)
    if isinstance(raw_min_calls, bool) or isinstance(raw_half_open_calls, bool):
        return None
    try:
        min_calls = int(raw_min_calls)
        failure_rate = float(value.get("failure_rate", 0.5))
        slow_rate = float(value.get("slow_rate", 0.5))
        half_open_calls = int(raw_half_open_calls)
        slow_call_seconds = value.get("slow_call_seconds")
        slow_call_seconds = None if slow_call_seconds is None else float(slow_call_seconds)
    except (TypeError, ValueError):
        return None
    if min_calls <= 0 or half_open_calls <= 0:
        return None
    if not (0 < failure_rate <= 1) or not (0 < slow_rate <= 1):
        return None
    if slow_call_seconds is not None and slow_call_seconds <= 0:
        return None
    return {
        "mode": "rate",
        "min_calls": min_calls,
        "failure_rate": failure_rate,
        "slow_call_seconds": slow_call_seconds,
        "slow_rate": slow_rate,
        "half_open_calls": half_open_calls,
    }


#: circuit_rate's own numeric fields, every one "lower is stricter" (a
#: rate-mode breaker that judges on fewer calls, a smaller failure/slow
#: fraction, or fewer half-open probes opens sooner and stays more
#: cautious); ``slow_call_seconds`` alone also treats ``None`` ("nothing is
#: slow") as its loosest value, the same rule :func:`_stricter_limit` uses.
_CIRCUIT_RATE_FIELDS = ("min_calls", "failure_rate", "slow_rate", "half_open_calls")


def effective_circuit_rate(code: "Mapping | None", plane_raw: Any) -> tuple:
    """``(effective, violations)`` for ``circuit_rate``.

    ``code`` is ``None`` when this worker's own ``circuit_mode`` is
    ``"count"`` (rate mode never explicitly turned on locally) or the full
    bundle :meth:`runbound.engine.Engine.__init__` builds from
    ``circuit_mode="rate"`` and its own knobs. With no local rate mode, a
    plane turning it on is pure tightening — adopted whole, nothing to
    violate. With a local rate mode already configured, only the fields
    the plane's *own raw payload actually states* (``field in plane_raw``,
    never a value :func:`_parse_circuit_rate` merely defaulted) are
    compared field by field; an absent field leaves ``code``'s own value
    standing.
    """
    plane_full = _parse_circuit_rate(plane_raw)
    if code is None:
        return (dict(plane_full) if plane_full is not None else None), []
    if plane_full is None or not isinstance(plane_raw, Mapping):
        return dict(code), []
    violations: list[Violation] = []
    effective = dict(code)
    effective["mode"] = "rate"
    for field in _CIRCUIT_RATE_FIELDS:
        if field not in plane_raw:
            continue
        code_value = code.get(field)
        plane_value = plane_full[field]
        result = min(code_value, plane_value)
        effective[field] = result
        if result != plane_value:
            violations.append(Violation(f"circuit_rate.{field}", code_value, plane_value))
    if "slow_call_seconds" in plane_raw:
        code_value = code.get("slow_call_seconds")
        plane_value = plane_full["slow_call_seconds"]
        result = _stricter_limit(code_value, plane_value)
        effective["slow_call_seconds"] = result
        if result != plane_value:
            violations.append(
                Violation("circuit_rate.slow_call_seconds", code_value, plane_value)
            )
    return effective, violations


def effective_circuit_posture(code: bool, plane_raw: Any, *, stated: bool) -> tuple:
    """``(effective, violation)`` for ``circuit_posture`` — a plain bool,
    independent of :func:`effective_circuit_rate` (a count-mode breaker
    narrows the posture on half-open exactly as a rate-mode one does).
    True can only ever be tightened *in*, so this reuses
    :func:`_stricter_envelope`'s own True-beats-False ranking; ``stated``
    (``"circuit_posture" in body``) is the same "absent is never a
    violation" rule :func:`effective_envelope` uses.
    """
    plane = plane_raw is True
    effective = _stricter_envelope(bool(code), plane)
    violation = None
    if stated and _ENVELOPE_RANK.get(bool(code), 0) > _ENVELOPE_RANK.get(plane, 0):
        violation = Violation("circuit_posture", bool(code), plane)
    return effective, violation


def _parse_loop_shapes(value: Any) -> "dict | None":
    """A well-shaped loop-shapes bundle from a plane's raw value, or
    ``None`` for anything absent or malformed. An unknown shape name is
    dropped rather than rejecting the whole bundle; a bundle left with no
    shapes at all is ``None``."""
    if not isinstance(value, Mapping):
        return None
    raw_shapes = value.get("shapes")
    if not isinstance(raw_shapes, (list, tuple)):
        return None
    shapes = tuple(
        name for name in raw_shapes if isinstance(name, str) and name in _LOOP_SHAPE_NAMES
    )
    if not shapes:
        return None
    try:
        max_period = int(value.get("max_period", 6))
        stall_turns = int(value.get("stall_turns", 5))
    except (TypeError, ValueError):
        return None
    if max_period < 2 or stall_turns < 1:
        return None
    return {"shapes": shapes, "max_period": max_period, "stall_turns": stall_turns}


def effective_loop_shapes(code: Mapping, plane_raw: Any) -> tuple:
    """``(effective, violations)`` for ``loop_shapes``.

    ``code`` is always a real bundle now (``loop_shapes`` defaults to
    ``("repeat", "sequence", "retry")`` locally) — there is no "off"
    state to adopt a plane bundle into from scratch. ``shapes`` can only
    grow: the effective set is ``code``'s shapes unioned with the plane's
    (a plane can add "stall"; it can never make this worker stop checking
    a shape the code already opted into), in :data:`_LOOP_SHAPE_NAMES`'s
    own canonical order, so there is nothing to violate there.
    ``max_period`` is how far "sequence" searches for a repeating cycle —
    larger is *more* thorough, so a plane raising it tightens; lowering it
    is refused. ``stall_turns`` is the opposite shape (a count *down* to a
    trip): a plane lowering it tightens (trips sooner); raising it is
    refused. Both only compared when the plane's own raw payload states
    the field.
    """
    plane_full = _parse_loop_shapes(plane_raw)
    code_shapes = tuple(code.get("shapes") or ())
    code_max_period = code.get("max_period")
    code_stall_turns = code.get("stall_turns")
    if plane_full is None or not isinstance(plane_raw, Mapping):
        return {
            "shapes": code_shapes,
            "max_period": code_max_period,
            "stall_turns": code_stall_turns,
        }, []
    violations: list[Violation] = []
    effective_shapes = tuple(
        name for name in _LOOP_SHAPE_NAMES if name in code_shapes or name in plane_full["shapes"]
    )
    max_period = code_max_period
    if "max_period" in plane_raw:
        plane_value = plane_full["max_period"]
        if plane_value > code_max_period:
            max_period = plane_value
        elif plane_value < code_max_period:
            violations.append(Violation("loop_shapes.max_period", code_max_period, plane_value))
    stall_turns = code_stall_turns
    if "stall_turns" in plane_raw:
        plane_value = plane_full["stall_turns"]
        if plane_value < code_stall_turns:
            stall_turns = plane_value
        elif plane_value > code_stall_turns:
            violations.append(Violation("loop_shapes.stall_turns", code_stall_turns, plane_value))
    return {"shapes": effective_shapes, "max_period": max_period, "stall_turns": stall_turns}, violations


def _parse_budget_soft(value: Any) -> "dict | None":
    """A well-shaped soft-line bundle from a plane's raw value, or ``None``
    for anything absent or malformed.

    ``{"fraction": 0.8, "reaction": "notify" | "safe_mode"}``.
    """
    if not isinstance(value, Mapping):
        return None
    fraction = value.get("fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return None
    if not 0 < fraction < 1:
        return None
    reaction = value.get("reaction")
    if reaction not in ("notify", "safe_mode"):
        reaction = "notify"
    return {"fraction": float(fraction), "reaction": reaction}


#: notify < safe_mode: a soft-line reaction that also enters safe mode is
#: stricter than one that only notifies.
_BUDGET_SOFT_REACTION_RANK = {"notify": 0, "safe_mode": 1}


def effective_budget_soft(code: "Mapping | None", plane_raw: Any) -> tuple:
    """``(effective, violations)`` for ``budget_soft``.

    ``code`` is ``None`` when this worker's own ``budget_soft`` is unset —
    a plane stating one then is pure tightening, adopted whole. With a
    local soft line already configured, only the fields the plane's own
    raw payload states are compared: a lower ``fraction`` fires sooner
    (stricter); a stronger ``reaction`` (``safe_mode`` over ``notify``) is
    stricter. Either direction that would loosen the local value is
    refused and named.
    """
    plane_full = _parse_budget_soft(plane_raw)
    if code is None:
        return (dict(plane_full) if plane_full is not None else None), []
    if plane_full is None or not isinstance(plane_raw, Mapping):
        return dict(code), []
    violations: list[Violation] = []
    effective = dict(code)
    if "fraction" in plane_raw:
        code_value = code["fraction"]
        plane_value = plane_full["fraction"]
        if plane_value < code_value:
            effective["fraction"] = plane_value
        elif plane_value > code_value:
            violations.append(Violation("budget_soft.fraction", code_value, plane_value))
    if "reaction" in plane_raw:
        code_value = code["reaction"]
        plane_value = plane_full["reaction"]
        if _BUDGET_SOFT_REACTION_RANK[plane_value] > _BUDGET_SOFT_REACTION_RANK[code_value]:
            effective["reaction"] = plane_value
        elif _BUDGET_SOFT_REACTION_RANK[plane_value] < _BUDGET_SOFT_REACTION_RANK[code_value]:
            violations.append(Violation("budget_soft.reaction", code_value, plane_value))
    return effective, violations


def _parse_max_actions_per_run(value: Any) -> "int | None":
    """A positive int from a plane's raw ``max_actions_per_run`` value, or
    ``None`` for anything absent or malformed."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def effective_max_actions_per_run(code: "int | None", plane_raw: Any) -> tuple:
    """``(effective, violations)`` for ``max_actions_per_run`` — exactly a
    :data:`LIMIT_FIELDS`-style field (lower is stricter, ``None`` is
    loosest), just not leveled on the wire, so it is folded by hand rather
    than through :func:`effective_limits`."""
    plane_value = _parse_max_actions_per_run(plane_raw)
    if plane_value is None:
        return code, []
    result = _stricter_limit(code, plane_value)
    violations = [] if result == plane_value else [Violation("max_actions_per_run", code, plane_value)]
    return result, violations


#: The three verdicts a plane-delivered ``spike.mode`` may hold — the same
#: vocabulary ``runbound.config.ON_SPIKE_MODES`` names locally now.
_SPIKE_MODES = ("notify", "trip", "limit")

#: ``notify < trip < limit`` — carried forward unchanged: "the
#: strictest spike reaction wins...
#: limit > trip > notify".
_SPIKE_MODE_RANK = {"notify": 0, "trip": 1, "limit": 2}

#: What the eleven spike_*/on_spike keywords default to locally
#: (``runbound.config.GuardrailConfig``) — kept here, duplicated on
#: purpose (see the module docstring), as the fallback :func:`_parse_spike`
#: fills an unstated field with when adopting a plane bundle from scratch.
_SPIKE_DEFAULTS: "dict[str, Any]" = {
    "mode": "notify",
    "limit_calls": 5,
    "cooldown_seconds": 300.0,
    "max_strikes": 3,
    "warmup_calls": 4,
    "min_duration_s": 2.0,
    "min_output_tokens": 500,
    "window": 50,
    "factor": 10.0,
    "confirm": 2,
}

#: Kept in step with ``runbound.detectors.SPIKE_CONFIRM_MAX`` by hand (this
#: module stays free of a dependency on either config.py or detectors.py —
#: see the module docstring's reasoning for the other duplicated literals).
SPIKE_CONFIRM_MAX = 5

#: Every spike/ladder numeric field where a *smaller* value is stricter
#: (fires sooner, trusts a baseline sooner, needs a smaller rise to count,
#: confirms off fewer calls, blocks after fewer strikes). ``mode`` and
#: ``cooldown_seconds`` are ranked the other way around — see
#: :func:`effective_spike`.
_SPIKE_LOWER_IS_STRICTER_FIELDS = (
    "limit_calls",
    "max_strikes",
    "warmup_calls",
    "min_duration_s",
    "min_output_tokens",
    "window",
    "factor",
    "confirm",
)


def _parse_spike(value: Any) -> "dict | None":
    """A fully-defaulted spike/ladder bundle from a plane's raw value
    (every field filled from :data:`_SPIKE_DEFAULTS` when the plane's
    payload leaves it out), or ``None`` for anything absent or
    malformed."""
    if not isinstance(value, Mapping):
        return None
    result = dict(_SPIKE_DEFAULTS)
    mode = value.get("mode", result["mode"])
    if mode not in _SPIKE_MODES:
        return None
    result["mode"] = mode
    try:
        for name in ("limit_calls", "max_strikes", "warmup_calls", "window", "confirm"):
            if name in value:
                raw = value[name]
                if isinstance(raw, bool) or not isinstance(raw, int):
                    return None
                result[name] = raw
        for name in ("cooldown_seconds", "min_duration_s", "factor"):
            if name in value:
                raw = value[name]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    return None
                result[name] = float(raw)
        if "min_output_tokens" in value:
            raw = value["min_output_tokens"]
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            result["min_output_tokens"] = raw
    except (TypeError, ValueError):
        return None
    if result["limit_calls"] < 1 or result["max_strikes"] < 1:
        return None
    if result["cooldown_seconds"] <= 0:
        return None
    if result["warmup_calls"] < 2 or result["window"] <= result["warmup_calls"]:
        return None
    if result["factor"] <= 1:
        return None
    if not 1 <= result["confirm"] <= SPIKE_CONFIRM_MAX:
        return None
    if result["min_duration_s"] <= 0 or result["min_output_tokens"] <= 0:
        return None
    return result


def effective_spike(code: Mapping, plane_raw: Any) -> tuple:
    """``(effective, violations)`` for the eleven spike/ladder tuning
    fields (``spike_enabled`` is its own, separate merge — see
    :func:`effective_spike_enabled`).

    ``code`` is always a real bundle (every ``spike_*`` keyword has a
    local default), so unlike ``circuit_rate``/``budget_soft``
    there is no "adopt the plane's bundle whole" branch: every field is
    compared, one at a time, but **only the fields the plane's own raw
    payload actually states** — never a value :func:`_parse_spike` merely
    filled in from :data:`_SPIKE_DEFAULTS`, which would otherwise be
    compared against a customer's own, differently-tuned default and
    flagged as a bogus "loosening" for every field the plane's bundle
    simply did not mention. ``mode`` ranks ``limit > trip > notify``;
    ``cooldown_seconds`` ranks longer as stricter; every other field ranks
    smaller as stricter (see :data:`_SPIKE_LOWER_IS_STRICTER_FIELDS`) —
    both directions already-settled rules.
    """
    code = dict(code)
    plane_full = _parse_spike(plane_raw)
    if plane_full is None or not isinstance(plane_raw, Mapping):
        return code, []
    violations: list[Violation] = []
    effective = dict(code)
    if "mode" in plane_raw:
        code_value = code["mode"]
        plane_value = plane_full["mode"]
        if _SPIKE_MODE_RANK[plane_value] > _SPIKE_MODE_RANK[code_value]:
            effective["mode"] = plane_value
        elif _SPIKE_MODE_RANK[plane_value] < _SPIKE_MODE_RANK[code_value]:
            violations.append(Violation("spike.mode", code_value, plane_value))
    if "cooldown_seconds" in plane_raw:
        code_value = code["cooldown_seconds"]
        plane_value = plane_full["cooldown_seconds"]
        if plane_value > code_value:
            effective["cooldown_seconds"] = plane_value
        elif plane_value < code_value:
            violations.append(Violation("spike.cooldown_seconds", code_value, plane_value))
    for field in _SPIKE_LOWER_IS_STRICTER_FIELDS:
        if field not in plane_raw:
            continue
        code_value = code[field]
        plane_value = plane_full[field]
        if plane_value < code_value:
            effective[field] = plane_value
        elif plane_value > code_value:
            violations.append(Violation(f"spike.{field}", code_value, plane_value))
    return effective, violations


def effective_spike_enabled(code: bool, plane_raw: Any, *, stated: bool) -> tuple:
    """``(effective, violation)`` for ``spike_enabled`` — whether the spike
    detector's ratio-based judgement and the abuse ladder run at all.
    ``code`` is ``spike_detection`` (default ``True``). Ranked
    and reported exactly like :func:`effective_circuit_posture` (True can
    only be tightened in); the per-call hard ceilings
    (``max_call_seconds``, ``max_tokens_out_per_call``,
    ``max_cost_per_call_usd``) are unaffected either way — they are their
    own, free run limits.
    """
    plane = plane_raw is True
    effective = _stricter_envelope(bool(code), plane)
    violation = None
    if stated and _ENVELOPE_RANK.get(bool(code), 0) > _ENVELOPE_RANK.get(plane, 0):
        violation = Violation("spike_enabled", bool(code), plane)
    return effective, violation
