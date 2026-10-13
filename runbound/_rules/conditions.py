"""Typed conditions, evaluated to true / false / unknown (TPD §4b step 3, §5.2).

The closed operator list of TPD §5.2 (R3): number ``>``, ``>=``, ``<``, ``<=`` and ``between``; string ``equals``, ``starts_with``,
``ends_with`` (case-sensitive) and ``is_one_of``; boolean ``is_true`` and ``is_false``; list ``contains``, ``length_lt`` and
``length_gt``. No regex, no wildcard, no trimming. A condition is **unknown** when its input is missing, ``None``, of the wrong type for
the operator (a bool is not a number, a string is not a list), the operator is not on the list, or evaluating it raises: it is never read
as false, because false could let a call through.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .tri import all_of, any_of, and3, not3

NUMBER = (int, float, Decimal)


@dataclass(frozen=True)
class Lit:
    """A condition already resolved to ``True``, ``False`` or ``None``: for tables and properties."""

    value: "bool | None"


@dataclass(frozen=True)
class Cond:
    """``<name> <op> <value>`` on a call argument (``source="arg"``) or a trusted context field (``"context"``)."""

    name: str
    op: str
    value: Any
    source: str = "arg"


@dataclass(frozen=True)
class Group:
    """``ALL OF`` (``kind="all"``) or ``ANY OF`` (``kind="any"``) over conditions and nested groups."""

    kind: str
    items: tuple

    def __post_init__(self) -> None:
        if self.kind not in ("all", "any"):
            raise ValueError(f"a group is 'all' or 'any', not {self.kind!r}")
        object.__setattr__(self, "items", tuple(self.items))


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, NUMBER):
        return False
    return not (isinstance(value, float) and math.isnan(value)) and not (isinstance(value, Decimal) and value.is_nan())


def _lookup(source: str, name: str, args: Any, context: Any) -> Any:
    mapping = {"arg": args, "context": context}.get(source)
    if mapping is None or name not in mapping:
        return _MISSING
    return mapping[name]


_MISSING = object()


def _is_list(value: Any) -> bool:
    return isinstance(value, (list, tuple, set, frozenset))


def _same_kind(left: Any, right: Any) -> bool:
    """Two scalars of one kind (bool, number or string): ``True`` is never the number 1 and ``"1"`` is never 1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if _is_number(left) or _is_number(right):
        return _is_number(left) and _is_number(right)
    return isinstance(left, str) and isinstance(right, str)


def _operator(op: str, actual: Any, value: Any) -> "bool | None":
    """One operator applied to an input that is present: true, false, or ``None`` (unknown) when the input or the value does not fit it."""
    if op in (">", ">=", "<", "<="):
        if not (_is_number(actual) and _is_number(value)):
            return None
        return bool({">": actual > value, ">=": actual >= value, "<": actual < value, "<=": actual <= value}[op])
    if op == "between":
        if not (_is_number(actual) and _is_list(value) and len(value) == 2 and all(_is_number(v) for v in value)):
            return None
        low, high = value
        return None if low > high else bool(low <= actual <= high)
    if op in ("equals", "starts_with", "ends_with"):
        if not (isinstance(actual, str) and isinstance(value, str)):
            return None
        return actual == value if op == "equals" else actual.startswith(value) if op == "starts_with" else actual.endswith(value)
    if op == "is_one_of":
        if not (isinstance(actual, str) and _is_list(value) and value and all(isinstance(v, str) for v in value)):
            return None
        return actual in value
    if op in ("is_true", "is_false"):
        if not isinstance(actual, bool):
            return None
        return actual is (op == "is_true")
    if op == "contains":
        if not _is_list(actual) or isinstance(value, (list, tuple, set, dict)) or value is None:
            return None
        return any(_same_kind(item, value) and item == value for item in actual)
    if op in ("length_lt", "length_gt"):
        if not _is_list(actual) or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return len(actual) < value if op == "length_lt" else len(actual) > value
    return None  # an operator outside the closed list is unknown, not false


def condition(node: Any, args: Any = None, context: Any = None) -> "bool | None":
    """One node (``Lit``, ``Cond`` or ``Group``) to true / false / unknown. Never raises."""
    try:
        if isinstance(node, Lit):
            return node.value
        if isinstance(node, Group):
            values = [condition(item, args, context) for item in node.items]
            return all_of(values) if node.kind == "all" else any_of(values)
        if isinstance(node, Cond):
            actual = _lookup(node.source, node.name, args, context)
            if actual is _MISSING or actual is None:
                return None
            return _operator(node.op, actual, node.value)
        return None
    except Exception:
        return None


def block(when: Any, unless: Any, args: Any = None, context: Any = None) -> "bool | None":
    """A rule's block: ``when`` ALL OF ``NOT unless`` (no ``when`` means unconditional)."""
    head = True if when is None else condition(when, args, context)
    if unless is None:
        return head
    return and3(head, not3(condition(unless, args, context)))
