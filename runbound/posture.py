"""Postures: a capability contract, and the only thing that decides by class.

A tool declares a *set* of capability classes (:data:`runbound.policy.
CAPABILITIES`). A **posture** says what an agent may currently do with those
classes: ``Posture.allows(effects)`` is the one function in runbound that
turns a class set into a verdict, so "what may this agent do right now" has
exactly one answer and one place to read it (see INVARIANTS.md).

Five postures ship built in, from the widest to the narrowest:

======================  ====================================================
``full``                every class allowed — the default, nothing narrowed
``restricted``          read and write; no external, financial, destructive
                        or privileged action
``read_only``           read; nothing else
``no_side_effects``     no tool at all; model calls still go out
``stopped``             nothing runs
======================  ====================================================

Safe mode is ``posture != full``. A budget's soft line, the spike ladder's
limited rung, a half-open circuit, the dashboard or a manual call move the
posture; :func:`tighten` is how two of them combine, and it can only ever
narrow.

Pure: no clock, no state, no I/O. Plain data in, a verdict out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .policy import CAPABILITIES

#: The three verdicts a class can carry. ``approve`` is a class *rule*
#: (``init(capabilities=...)``); no built-in posture states one, and until
#: approvals have a queue the engine refuses an ``approve`` and says so.
VERDICTS = ("allow", "approve", "deny")

#: How strict each verdict is. The worst verdict across a tool's classes wins.
_RANK = {"allow": 0, "approve": 1, "deny": 2}

#: What a tool with no declared class counts as in a message.
UNCLASSIFIED = "unclassified"


def stricter(first: str, second: str) -> str:
    """The stricter of two verdicts — how a posture and a class rule combine."""
    return max(first, second, key=_RANK.__getitem__)


def _row(allowed: Iterable[str]) -> dict:
    """One posture's table: the named classes allowed, every other denied."""
    allow = set(allowed)
    return {name: ("allow" if name in allow else "deny") for name in CAPABILITIES}


@dataclass(frozen=True)
class Posture:
    """What an agent may do, class by class.

    ``verdicts`` carries every class in :data:`runbound.policy.CAPABILITIES`,
    so a lookup never has to decide what a missing key meant.
    """

    name: str
    verdicts: Mapping[str, str]
    #: How strict this posture is among the built-ins, widest first. Two
    #: postures can share a class table and still differ: ``stopped`` denies
    #: every class exactly as ``no_side_effects`` does, and also ends the run.
    #: :func:`tighten` breaks that tie with this. A posture from
    #: ``init(postures=...)`` carries the rank of the row it overrides, or 0.
    rank: int = 0

    def verdict_for(self, capability: str) -> str:
        """This posture's verdict for one class; ``"deny"`` for an unknown one."""
        return self.verdicts.get(capability, "deny")

    def allows(self, effects: frozenset) -> str:
        """The verdict for a tool carrying ``effects`` — the worst of its classes.

        A tool is only as permitted as its least permitted class: a tool that
        both writes and reaches outside is refused wherever either is. A tool
        with **no** declared class is allowed only by a posture that allows
        everything, because nothing said what it does and runbound will not
        assume it merely reads.

        Returns one of :data:`VERDICTS`. The five built-in postures only ever
        answer ``"allow"`` or ``"deny"``; ``"approve"`` can only come from a
        posture override that states it.
        """
        classes = frozenset(effects or ())
        if not classes:
            return "allow" if self.widest() else "deny"
        return max((self.verdict_for(name) for name in classes), key=_RANK.__getitem__)

    def widest(self) -> bool:
        """Does this posture allow every class? (``full``, and anything like it.)"""
        return all(verdict == "allow" for verdict in self.verdicts.values())

    def denied_class(self, effects: frozenset) -> str:
        """Which class of ``effects`` this posture refuses — for the message.

        The strictest class, and the first of those in
        :data:`runbound.policy.CAPABILITIES` order so the answer is stable.
        :data:`UNCLASSIFIED` for a tool that declared none.
        """
        classes = frozenset(effects or ())
        if not classes:
            return UNCLASSIFIED
        worst = self.allows(classes)
        for name in CAPABILITIES:
            if name in classes and self.verdict_for(name) == worst:
                return name
        return UNCLASSIFIED  # pragma: no cover - worst always comes from a class


#: The five built-in postures, widest first.
POSTURES: dict = {
    "full": Posture("full", _row(CAPABILITIES), 0),
    "restricted": Posture("restricted", _row(("read", "write")), 1),
    "read_only": Posture("read_only", _row(("read",)), 2),
    "no_side_effects": Posture("no_side_effects", _row(()), 3),
    "stopped": Posture("stopped", _row(()), 4),
}

#: The posture a process runs under until something narrows it.
FULL = POSTURES["full"]

#: Built-in names, widest first — the order a customer sees them in.
POSTURE_NAMES = tuple(POSTURES)


def tighten(first: Posture, second: Posture) -> Posture:
    """The stricter of two postures: per class, the worse verdict wins.

    Whichever input already states that combination is returned as itself, so
    the common case keeps its name (``tighten(full, restricted)`` is
    ``restricted``). Two postures where neither dominates — only reachable
    with an override — produce a combined one named ``"a+b"``, which is the
    honest answer: it is neither of them.
    """
    merged = {
        name: max(first.verdict_for(name), second.verdict_for(name), key=_RANK.__getitem__)
        for name in CAPABILITIES
    }
    keeps_first = all(merged[name] == first.verdict_for(name) for name in CAPABILITIES)
    keeps_second = all(merged[name] == second.verdict_for(name) for name in CAPABILITIES)
    if keeps_first and keeps_second:
        # The same class table, two names: the higher rank is the stricter.
        return first if first.rank >= second.rank else second
    if keeps_first:
        return first
    if keeps_second:
        return second
    return Posture(f"{first.name}+{second.name}", merged, max(first.rank, second.rank))


def resolve(name: str, overrides: Mapping | None = None) -> Posture:
    """The posture called ``name``, with ``init(postures=...)`` applied.

    Raises ``ValueError`` for a name no built-in and no override defines —
    a posture nobody stated is a configuration mistake, not a default.
    """
    table = dict(POSTURES)
    for posture_name, row in (overrides or {}).items():
        existing = table.get(posture_name)
        base = existing.verdicts if existing is not None else FULL.verdicts
        table[posture_name] = Posture(
            posture_name, {**base, **dict(row)}, existing.rank if existing is not None else 0
        )
    posture = table.get(name)
    if posture is None:
        raise ValueError(
            f"unknown posture {name!r}; the built-in ones are {', '.join(POSTURE_NAMES)}"
            " (add your own with init(postures={...}))"
        )
    return posture


def class_rules(rules: Mapping | None) -> Posture:
    """``init(capabilities=...)`` as a posture, so one function still decides.

    A class rule is not a posture — it holds whatever the posture is — but it
    answers the same question about the same classes, so it is expressed the
    same way and combined with :func:`tighten`.
    """
    verdicts = {name: "allow" for name in CAPABILITIES}
    verdicts.update(dict(rules or {}))
    return Posture("class_rule", verdicts, 0)


def validate_class_rules(rules: object) -> None:
    """``init(capabilities=...)`` must be a mapping of known class to verdict."""
    if rules is None:
        return
    if not isinstance(rules, Mapping):
        raise ValueError(f"capabilities must be a dict of class -> verdict, got {rules!r}")
    for name, verdict in rules.items():
        if name not in CAPABILITIES:
            raise ValueError(
                f"capabilities: {name!r} is not a capability class; "
                f"the classes are {', '.join(CAPABILITIES)}"
            )
        if verdict not in VERDICTS:
            raise ValueError(
                f"capabilities[{name!r}] must be one of {', '.join(VERDICTS)}, got {verdict!r}"
            )


def validate_overrides(overrides: object) -> None:
    """``init(postures=...)`` must be a mapping of name to class-verdict rows."""
    if overrides is None:
        return
    if not isinstance(overrides, Mapping):
        raise ValueError(
            f"postures must be a dict of posture name -> {{class: verdict}}, got {overrides!r}"
        )
    for name, row in overrides.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"postures: {name!r} is not a posture name")
        if not isinstance(row, Mapping):
            raise ValueError(f"postures[{name!r}] must be a dict of class -> verdict, got {row!r}")
        validate_class_rules(row)
