"""Slot 1 of the pre-hook chain: argument validation from signatures and type hints.

The case table is RS-3's (docs/design/hook-chain.md §1.5), the minimum for this
slot: validate, never coerce; ``bool`` is refused where ``int`` or ``float`` is
declared; unknown TypedDict keys are refused; an unsupported hint is skipped
with one warning per (tool, parameter); dataclasses are validated as dicts;
``Annotated`` metadata is ignored.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import functools
import logging
from typing import Annotated, Any, Literal, NewType, Optional, TypedDict

import pytest

import runbound
from runbound import api, hooks
from runbound.hooks import Validator


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Addr(TypedDict):
    street: str
    zip: str


class AddrLoose(TypedDict, total=False):
    street: str


@dataclasses.dataclass
class Money:
    amount: float
    currency: str = "USD"


UserId = NewType("UserId", int)


def t_int(n: int): ...
def t_float(x: float): ...
def t_bool(flag: bool): ...
def t_str(s: str): ...
def t_opt(s: Optional[str]): ...
def t_union(s: "str | None"): ...
def t_lit(mode: Literal["fast", "slow"]): ...
def t_litint(level: Literal[1, 2]): ...
def t_list(ids: list[int]): ...
def t_dict(m: dict[str, int]): ...
def t_td(a: Addr): ...
def t_tdloose(a: AddrLoose): ...
def t_dc(m: Money): ...
def t_enum(c: Color): ...
def t_ann(n: Annotated[int, "gt=0"]): ...
def t_fwd(x: "NotDefinedAnywhere"): ...  # noqa: F821
async def t_async(n: int): ...
def t_dt(when: datetime.datetime): ...
def t_any(x: Any): ...
def t_kw(a: int, **extra): ...
def t_default(a: int, b: int = 2): ...
def t_nohint(a, b): ...
def t_newtype(uid: UserId): ...


def deco(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


@deco
def t_wrapped(n: int): ...


class Svc:
    def refund(self, order_id: str, amount: float): ...


CASES = [
    ("1 int <- '5'", t_int, ("5",), {}, "refuse"),
    ("2 int <- True", t_int, (True,), {}, "refuse"),
    ("3 float <- 3", t_float, (3,), {}, "allow"),
    ("4 bool <- 1", t_bool, (1,), {}, "refuse"),
    ("5 str <- None", t_str, (None,), {}, "refuse"),
    ("6 Optional[str] <- None", t_opt, (None,), {}, "allow"),
    ("7 'str | None' <- 5", t_union, (5,), {}, "refuse"),
    ("8a Literal <- 'fast'", t_lit, ("fast",), {}, "allow"),
    ("8b Literal <- 'medium'", t_lit, ("medium",), {}, "refuse"),
    ("9 Literal[1, 2] <- True", t_litint, (True,), {}, "refuse"),
    ("10 list[int] <- [1, 2, '3']", t_list, ([1, 2, "3"],), {}, "refuse"),
    ("11a dict[str, int] ok", t_dict, ({"a": 1},), {}, "allow"),
    ("11b dict[str, int] <- {'a': '1'}", t_dict, ({"a": "1"},), {}, "refuse"),
    ("12a TypedDict missing key", t_td, ({"street": "x"},), {}, "refuse"),
    ("12b TypedDict total=False absent", t_tdloose, ({},), {}, "allow"),
    ("13 TypedDict unknown key", t_td, ({"street": "x", "zip": "1", "extra": 1},), {}, "refuse"),
    ("14a dataclass as dict ok", t_dc, ({"amount": 5.0},), {}, "allow"),
    ("14b dataclass as dict missing field", t_dc, ({"currency": "EUR"},), {}, "refuse"),
    ("15a Enum by value", t_enum, ("red",), {}, "allow"),
    ("15b Enum unknown value", t_enum, ("pink",), {}, "refuse"),
    ("16 Annotated metadata ignored", t_ann, (-1,), {}, "allow"),
    ("17 string hints resolved, int <- '5'", t_int, ("5",), {}, "refuse"),
    ("18 unresolvable forward ref skips", t_fwd, (object(),), {}, "allow"),
    ("20 functools.wraps chain", t_wrapped, ("x",), {}, "refuse"),
    ("21 bound method, self dropped", Svc().refund, ("o-1", "ten"), {}, "refuse"),
    ("22a missing required argument", t_default, (), {}, "refuse"),
    ("22b unexpected keyword", t_default, (1,), {"c": 3}, "refuse"),
    ("23 datetime <- str skips", t_dt, ("2026-10-07",), {}, "allow"),
    ("23b datetime <- a real datetime", t_dt, (datetime.datetime(2026, 10, 7),), {}, "allow"),
    ("24 Any", t_any, (object(),), {}, "allow"),
    ("25 **kwargs never checked", t_kw, (1,), {"zzz": "q"}, "allow"),
    ("26 default used when omitted", t_default, (1,), {}, "allow"),
    ("27 no hints", t_nohint, (1, "x"), {}, "allow"),
    ("28 NewType(int) <- '5'", t_newtype, ("5",), {}, "refuse"),
    ("29 float <- True", t_float, (True,), {}, "refuse"),
]


@pytest.mark.parametrize("label,func,args,kwargs,expected", CASES, ids=[c[0] for c in CASES])
def test_the_case_table(label, func, args, kwargs, expected):
    reason = Validator(func).check(args, kwargs)
    assert ("refuse" if reason else "allow") == expected, reason


def test_a_reason_names_the_parameter_and_the_types_never_the_value():
    assert Validator(t_list).check(([1, 2, "3"],), {}) == "ids[2]: expected int, got str"
    secret = "s3cr3t-value"
    for func, value in ((t_lit, secret), (t_enum, secret), (t_int, secret)):
        assert secret not in Validator(func).check((value,), {})


def test_an_unsupported_hint_warns_once_per_tool_and_parameter(caplog):
    validator = Validator(t_dt)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        for _ in range(3):
            assert validator.check(("2026-10-07",), {}) is None
    assert caplog.text.count("cannot be checked from JSON") == 1


def test_an_unresolvable_forward_reference_skips_all_hints_with_one_warning(caplog):
    def tool(n: int, ghost: "Nowhere"): ...  # noqa: F821

    validator = Validator(tool)
    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert validator.check(("not-an-int", object()), {}) is None
        assert validator.check(("still-not-an-int", object()), {}) is None
    assert caplog.text.count("could not be resolved") == 1


# --- through the decorator: refused before the body runs, sync and async ---------


def test_a_sync_tool_is_refused_before_its_body_runs():
    runbound.init()
    ran = []

    @runbound.tool
    def book(slots: int):
        ran.append(slots)

    with pytest.raises(runbound.ExecutionRefused) as caught:
        book("two")

    assert ran == []
    assert caught.value.reason == "invalid_arguments"
    assert caught.value.decision.boundary == "arguments"
    assert caught.value.anomaly.detector == "validation"
    assert runbound.current_session().refused_actions == 1
    book(2)
    assert ran == [2]


def test_an_async_tool_is_refused_before_its_body_runs():
    runbound.init()
    ran = []

    @runbound.tool
    async def book(slots: int):
        ran.append(slots)

    async def go():
        with pytest.raises(runbound.ExecutionRefused) as caught:
            await book(True)  # bool is not int
        assert caught.value.reason == "invalid_arguments"
        await book(3)

    asyncio.run(go())
    assert ran == [3]


def test_validation_runs_without_a_policy_and_never_coerces():
    runbound.init()

    @runbound.tool
    def add(a: int, b: int):
        return a + b

    assert add(1, 2) == 3
    with pytest.raises(runbound.ExecutionRefused):
        add("1", 2)
    with pytest.raises(runbound.ExecutionRefused):
        add(1)  # a missing argument is the signature's own refusal
