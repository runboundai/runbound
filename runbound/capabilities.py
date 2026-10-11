"""Capability classes: the one function that turns a class set into a verdict.

A tool declares a *set* of capability classes (:data:`runbound.policy.
CAPABILITIES`). ``init(capabilities={...})`` states what the process may do
with each class; :meth:`ClassRules.allows` is the single place that answers
"may a tool carrying these classes run", so there is one answer and one place
to read it. Pure: no clock, no state, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .policy import CAPABILITIES

#: The three verdicts a class can carry. ``approve`` stands until approvals
#: have a queue (S5/A5): it refuses, and says it is the approval that is missing.
VERDICTS = ("allow", "approve", "deny")

_RANK = {"allow": 0, "approve": 1, "deny": 2}

#: What a tool with no declared class counts as in a message.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ClassRules:
    """What the process may do, class by class (every class has a verdict)."""

    verdicts: Mapping[str, str]

    def verdict_for(self, capability: str) -> str:
        """The verdict for one class; ``"deny"`` for an unknown one."""
        return self.verdicts.get(capability, "deny")

    def widest(self) -> bool:
        """Does every class allow? Then nothing is narrowed."""
        return all(verdict == "allow" for verdict in self.verdicts.values())

    def allows(self, effects: frozenset) -> str:
        """The verdict for a tool carrying ``effects`` — the worst of its classes.

        A tool with **no** declared class is allowed only while nothing is
        narrowed, because nothing said what it does and runbound will not
        assume it merely reads.
        """
        classes = frozenset(effects or ())
        if not classes:
            return "allow" if self.widest() else "deny"
        return max((self.verdict_for(name) for name in classes), key=_RANK.__getitem__)

    def denied_class(self, effects: frozenset) -> str:
        """Which class of ``effects`` is refused — the strictest, in :data:`CAPABILITIES` order."""
        classes = frozenset(effects or ())
        if not classes:
            return UNCLASSIFIED
        worst = self.allows(classes)
        for name in CAPABILITIES:
            if name in classes and self.verdict_for(name) == worst:
                return name
        return UNCLASSIFIED  # pragma: no cover - worst always comes from a class


def class_rules(rules: Mapping | None) -> ClassRules:
    """``init(capabilities=...)`` as a :class:`ClassRules`; unstated classes allow."""
    verdicts = {name: "allow" for name in CAPABILITIES}
    verdicts.update(dict(rules or {}))
    return ClassRules(verdicts)


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
