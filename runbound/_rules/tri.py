"""Three-valued logic: ``True``, ``False`` and ``None`` (unknown).

Kleene's strong tables, which are the TPD's §4b tables: ``ALL OF`` is false if any input is false, else
unknown if any is unknown, else true; ``ANY OF`` is true if any input is true, else unknown if any is
unknown, else false. Both are associative and commutative, so a group of three or more equals the pairwise
fold of the two-input table in any order (register K2). An empty ``ALL OF`` is true and an empty ``ANY OF``
is false (the identities of the folds); neither is a rule anyone can write.
"""

from __future__ import annotations

from collections.abc import Iterable

Tri = "bool | None"


def and3(a: "bool | None", b: "bool | None") -> "bool | None":
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def or3(a: "bool | None", b: "bool | None") -> "bool | None":
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def not3(a: "bool | None") -> "bool | None":
    """Three-valued negation (``UNLESS``): true -> false, false -> true, unknown -> unknown."""
    return None if a is None else (not a)


def all_of(values: "Iterable[bool | None]") -> "bool | None":
    out: "bool | None" = True
    for value in values:
        out = and3(out, value)
    return out


def any_of(values: "Iterable[bool | None]") -> "bool | None":
    out: "bool | None" = False
    for value in values:
        out = or3(out, value)
    return out
