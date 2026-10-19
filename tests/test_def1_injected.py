"""DEF-1: deferred approvals for real tools. A parameter the APP supplies (a DB session, a client, the tenant) is marked ``runbound.Injected`` and is never stored, sealed,
digested, shown or read by a rule; it is supplied again when the approved request runs. Structured model arguments (a pydantic model, a datetime, a Decimal, a UUID, a Path)
cross the JSON store through a codec and come back as the types the tool declares.

Every test runs the real SDK against a recording plane double (the same one ``test_approvals`` uses); the plane's own digest recomputation is restated here.
"""

import datetime
import decimal
import hashlib
import io
import json
import logging
import pathlib
import sys
import time
import types
import typing
import uuid
from typing import Annotated, Callable, Optional

import pytest

import runbound
from runbound import api, approvals
from runbound.approvals import verify_seal

SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Plane:
    fleet = True

    def __init__(self, status="pending"):
        self.status, self.calls, self.reports = status, [], []

    def approvals(self, method, path, body=None, timeout=5.0):
        if path.endswith("/report"):
            self.reports.append(body)
            return 200, {}
        self.calls.append((method, path, body))
        return 201, {"id": "a%d" % len(self.calls), "status": self.status, "quota": None}

    def created(self):
        return [c[2] for c in self.calls if c[1] == "/v1/approvals"]


class Session:
    """What an app injects: a DB session. Every instance is a different one."""

    count = 0

    def __init__(self):
        Session.count += 1
        self.n = Session.count


def start(plane, **config):
    Session.count = 0
    runbound.init(approval_secret=SECRET, **config)
    api._ENGINE.shared = plane
    results = []
    api._ENGINE.config.on_deferred_result = results.append
    return results


def approved(plane, out, index=-1):
    """The item a hello would carry once a person approved the request just filed."""
    create = plane.created()[index]
    return {"id": "a1", "tool": create["tool"], "args": create["args"], "request_id": out["request_id"], "seal": create["seal"], "service": create["service"],
            "expires_at": "2099-01-01T00:00:00Z"}


def run(plane, item):
    approvals._execute(item)
    deadline = time.monotonic() + 5
    while not plane.reports and time.monotonic() < deadline:
        time.sleep(0.01)
    return plane.reports[-1]["outcome"] if plane.reports else None


def plane_digest(create):
    """What the plane recomputes for a request (sha256 of canonical JSON ``{"tool", "args"}``)."""
    text = json.dumps({"tool": create["tool"], "args": create["args"]}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


DB = Annotated[Session, runbound.Injected]


# --- DEF-1-1 ----------------------------------------------------------------------------------------


def test_def_1_1_an_injected_parameter_is_never_stored_and_is_supplied_afresh_at_execution():
    plane = Plane()
    start(plane, provide={Session: Session})
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: DB):
        ran.append((query, db))
        return "found"

    filed = Session()  # the request's own session: it must not leave the process
    out = find("x", db=filed)
    assert out["status"] == "pending_approval"
    (create,) = plane.created()
    assert create["args"] == {"query": "x"} and '"db"' not in json.dumps(create) and "Session" not in json.dumps(create)
    assert create["args_digest"] == plane_digest(create)
    assert verify_seal(SECRET, create["seal"], create["service"], "find", create["request_id"], {"query": "x"}) is None  # sealed over the model's arguments only
    assert api._args_hash("find", ("x",), {"db": Session()}) == api._args_hash("find", (), {"query": "x", "db": Session()})  # the span's digest: the same call

    item = approved(plane, out)
    assert run(plane, item) == "executed"
    assert len(ran) == 1 and ran[0][0] == "x" and isinstance(ran[0][1], Session) and ran[0][1] is not filed
    out2 = find("x", db=Session())
    plane.reports.clear()
    assert run(plane, approved(plane, out2)) == "executed"
    assert ran[1][1] is not ran[0][1], "a second execution gets a second provider call: nothing is held across the wait"


def test_a_rule_does_not_read_an_injected_parameter_and_a_name_beats_a_type():
    plane = Plane()
    results = start(plane, provide={Session: lambda: "by type", "db": lambda: "by name"})

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: DB):
        return db

    assert approvals.bound_arguments("find", ("x",), {"db": Session()}) == {"query": "x"}
    out = find("x", db=Session())
    assert run(plane, approved(plane, out)) == "executed" and results[-1]["result"] == "by name"


# --- DEF-1-2 / 3 ------------------------------------------------------------------------------------


def test_def_1_2_a_missing_provider_runs_nothing_fails_the_request_and_warns_once(caplog):
    plane = Plane()
    results = start(plane)
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: DB):
        ran.append(query)

    caplog.set_level(logging.WARNING, logger="runbound")
    for _ in range(2):
        plane.reports.clear()
        out = find("x", db=Session())
        assert run(plane, approved(plane, out)) == "failed"
    assert ran == [] and results[-1]["outcome"] == "failed" and results[-1]["error"] == "injected_unavailable:db"
    assert len([r for r in caplog.records if "injected parameter" in r.getMessage()]) == 1


def test_def_1_3_a_provider_that_raises_is_a_failed_request_not_a_refusal():
    plane = Plane()

    def broken():
        raise RuntimeError("no database")

    results = start(plane, provide={Session: broken})
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: DB):
        ran.append(query)

    out = find("x", db=Session())
    assert run(plane, approved(plane, out)) == "failed"
    assert ran == [] and results[-1]["outcome"] == "failed" and results[-1]["error"] == "RuntimeError"


# --- DEF-1-4 ----------------------------------------------------------------------------------------


def test_def_1_4_a_callable_default_is_a_factory_and_a_plain_default_is_used_as_is():
    plane = Plane()
    start(plane)
    got = {}

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: Annotated[Session, runbound.Injected] = lambda: Session(), tenant: Annotated[str, runbound.Injected] = "acme"):
        got.update(db=db, tenant=tenant)

    out = find("x")
    assert plane.created()[0]["args"] == {"query": "x"}
    assert run(plane, approved(plane, out)) == "executed"
    assert isinstance(got["db"], Session) and got["tenant"] == "acme"


# --- DEF-1-5 / 9: a model argument ---------------------------------------------------------------------


class Order:
    """A stand-in with the two methods the codec uses, for an environment without pydantic."""

    def __init__(self, sku, customer):
        self.sku, self.customer = sku, customer

    def model_dump(self, mode="python"):
        return {"sku": self.sku, "customer": dict(self.customer)}

    @classmethod
    def model_validate(cls, value):
        return cls(value["sku"], value["customer"])

    def __eq__(self, other):
        return isinstance(other, Order) and (self.sku, self.customer) == (other.sku, other.customer)


def models():
    """The real pydantic model when it is installed, else the stand-in: the codec asks only for model_validate and model_dump."""
    try:
        import pydantic
    except ImportError:
        return Order

    class Customer(pydantic.BaseModel):
        id: int
        tier: str

    class Real(pydantic.BaseModel):
        sku: str
        customer: Customer
        placed: datetime.datetime

    return Real


def make_order(cls):
    if cls is Order:
        return Order("A-1", {"id": 7, "tier": "gold"})
    return cls(sku="A-1", customer={"id": 7, "tier": "gold"}, placed=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc))


def test_def_1_5_a_model_argument_is_stored_encoded_matches_the_plane_digest_and_comes_back_a_model():
    plane = Plane()
    start(plane)
    cls = models()
    seen = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def place(order: cls):
        seen.append(order)

    order = make_order(cls)
    out = place(order)
    (create,) = plane.created()
    assert isinstance(create["args"]["order"], dict) and create["args"]["order"]["customer"] == {"id": 7, "tier": "gold"}
    assert create["args_digest"] == plane_digest(create)  # the plane's recomputation over the encoded JSON agrees
    json.dumps(create)
    assert approvals.bound_arguments("place", (order,), {}) == {"order": create["args"]["order"]}  # a rule condition reads the encoded form, nested fields included
    assert run(plane, approved(plane, out)) == "executed"
    assert isinstance(seen[0], cls) and seen[0] == order


def test_def_1_9_a_tampered_model_argument_still_reports_tampered():
    plane = Plane()
    results = start(plane)
    cls = models()
    seen = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def place(order: cls):
        seen.append(order)

    out = place(make_order(cls))
    item = approved(plane, out)
    item["args"] = {"order": {**item["args"]["order"], "sku": "Z-9"}}
    assert run(plane, item) == "tampered"
    assert seen == [] and results[-1]["outcome"] == "tampered"


# --- DEF-1-6 ----------------------------------------------------------------------------------------


def test_def_1_6_datetime_decimal_uuid_and_path_round_trip_also_inside_optional_and_list():
    plane = Plane()
    start(plane)
    seen = {}

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def book(at: datetime.datetime, price: decimal.Decimal, ref: uuid.UUID, where: pathlib.Path, due: Optional[datetime.date], slots: list[datetime.time],
             maybe: Optional[decimal.Decimal] = None, by_id: Optional[dict[str, uuid.UUID]] = None):
        seen.update(at=at, price=price, ref=ref, where=where, due=due, slots=slots, maybe=maybe, by_id=by_id)

    values = dict(at=datetime.datetime(2026, 10, 18, 1, 2, 3, tzinfo=datetime.timezone.utc), price=decimal.Decimal("12.50"), ref=uuid.uuid4(),
                  where=pathlib.Path("/srv/data/x.csv"), due=datetime.date(2026, 11, 1), slots=[datetime.time(9, 30), datetime.time(14, 0)],
                  maybe=None, by_id={"a": uuid.uuid4()})
    out = book(**values)
    (create,) = plane.created()
    assert create["args"]["at"] == "2026-10-18T01:02:03+00:00" and create["args"]["price"] == "12.50" and create["args"]["where"] == "/srv/data/x.csv"
    assert create["args"]["slots"] == ["09:30:00", "14:00:00"] and create["args"]["due"] == "2026-11-01" and create["args"]["maybe"] is None
    assert create["args_digest"] == plane_digest(create)
    assert run(plane, approved(plane, out)) == "executed"
    assert seen == values and type(seen["price"]) is decimal.Decimal and isinstance(seen["where"], pathlib.Path)


def test_a_naive_datetime_stays_naive():
    plane = Plane()
    start(plane)
    seen = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def at(when: datetime.datetime):
        seen.append(when)

    out = at(datetime.datetime(2026, 1, 1, 12))
    assert run(plane, approved(plane, out)) == "executed" and seen == [datetime.datetime(2026, 1, 1, 12)]


# --- DEF-1-7 ----------------------------------------------------------------------------------------


class Custom:
    pass


def refused_with(func):
    with pytest.raises(ValueError) as caught:
        runbound.tool(effects={"write"}, approval="required", mode="deferred")(func)
    return str(caught.value)


@pytest.mark.parametrize("parameter, why", [("handle", "a file object"), ("callback", "a callable"), ("thing", "Custom")])
def test_def_1_7_registration_names_the_parameter_and_the_fix(parameter, why):
    def open_file(name: str, handle: io.TextIOWrapper): ...

    def with_callback(name: str, callback: Callable[[int], int]): ...

    def with_custom(name: str, thing: Custom): ...

    message = refused_with({"handle": open_file, "callback": with_callback, "thing": with_custom}[parameter])
    assert f"parameter '{parameter}'" in message and why in message and "Mark a parameter runbound.Injected if the app supplies it" in message
    assert "'name'" not in message  # only what is wrong is named


def test_def_1_7_variadic_positional_arguments_are_named_and_marking_the_others_does_not_help():
    def many(*items): ...

    message = refused_with(many)
    assert "'*items'" in message and "variable positional" in message


def test_the_fix_the_error_names_works():
    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def open_file(name: str, handle: Annotated[io.TextIOWrapper, runbound.Injected]): ...

    @runbound.tool(effects={"write"}, approval="required", mode="deferred")
    def with_custom(name: str, thing: Annotated[Custom, runbound.Injected]): ...


def test_a_dict_key_must_be_json_not_a_codec_type():
    assert "'by'" in refused_with(_keyed)


def _keyed(by: dict[uuid.UUID, int]): ...


# --- DEF-1-8: sync -------------------------------------------------------------------------------------


def test_def_1_8_sync_passes_an_injected_parameter_through_and_shows_the_same_arguments_as_deferred():
    sync_plane, deferred_plane = Plane(status="approved"), Plane()
    start(sync_plane)
    seen = []

    @runbound.tool(effects={"write"}, approval="required", mode="sync", timeout_s=60)
    def sync_find(query: str, db: DB):
        seen.append(db)
        return "found"

    mine = Session()
    assert sync_find("x", db=mine) == "found" and seen == [mine]  # passed straight through
    (sync_create,) = sync_plane.created()
    assert sync_create["args"] == {"query": "x"} and '"db"' not in json.dumps(sync_create)
    assert approvals.bound_arguments("sync_find", ("x",), {"db": mine}) == {"query": "x"}

    api._teardown_for_tests()
    start(deferred_plane)

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def sync_find(query: str, db: DB):  # noqa: F811 - the same tool and name, deferred
        return "found"

    sync_find("x", db=mine)
    (deferred_create,) = deferred_plane.created()
    assert deferred_create["args"] == sync_create["args"] and deferred_create["args_digest"] == sync_create["args_digest"]


# --- DEF-1-10 ---------------------------------------------------------------------------------------


def test_def_1_10_langchains_injected_arg_is_recognised_only_when_it_is_already_imported(monkeypatch):
    class InjectedToolArg:
        pass

    class InjectedState(InjectedToolArg):
        pass

    module = types.ModuleType("langchain_core.tools")
    module.InjectedToolArg = InjectedToolArg
    monkeypatch.setitem(sys.modules, "langchain_core.tools", module)
    assert approvals.is_injected(Annotated[Session, InjectedToolArg]) and approvals.is_injected(Annotated[dict, InjectedState()]) and approvals.is_injected(Annotated[dict, InjectedState])
    plane = Plane()
    start(plane)

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, state: Annotated[dict, InjectedState()]):
        return "found"

    find("x", state={"messages": []})
    assert plane.created()[0]["args"] == {"query": "x"}


def test_without_langchain_nothing_is_imported_and_a_lookalike_is_not_injected(monkeypatch):
    for name in [m for m in sys.modules if m == "langchain_core" or m.startswith("langchain_core.")]:
        monkeypatch.delitem(sys.modules, name)

    class InjectedToolArg:  # a class of that name that is NOT langchain's
        pass

    assert not approvals.is_injected(Annotated[Session, InjectedToolArg])
    assert not approvals.is_injected(Annotated[Session, "something"]) and not approvals.is_injected(Session)
    assert approvals.is_injected(Annotated[Session, runbound.Injected]) and approvals.is_injected(Optional[Annotated[Session, runbound.Injected]])
    assert "langchain_core" not in sys.modules


# --- the edges --------------------------------------------------------------------------------------


def test_an_injected_value_on_the_wire_is_never_used():
    plane = Plane()
    start(plane, provide={Session: Session})
    got = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def find(query: str, db: DB):
        got.append(db)

    out = find("x", db=Session())
    item = approved(plane, out)
    item["args"] = {**item["args"], "db": "smuggled"}  # a plane that adds one: the seal no longer holds, so it is tampered
    assert run(plane, item) == "tampered" and got == []


def test_an_undecodable_stored_value_fails_the_request():
    plane = Plane()
    results = start(plane)
    seen = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def at(when: datetime.datetime):
        seen.append(when)

    out = at(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    create = plane.created()[0]
    from runbound.approvals import make_seal

    bad = {"when": "not a date"}
    item = {**approved(plane, out), "args": bad, "seal": make_seal(SECRET, create["service"], "at", create["request_id"], bad, 60)}
    assert run(plane, item) == "failed" and seen == [] and results[-1]["outcome"] == "failed"


def test_a_tool_with_none_of_it_is_untouched():
    plane = Plane()
    start(plane)

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def plain(n: int, names: list[str]):
        return n

    plain(1, ["a"])
    assert plane.created()[0]["args"] == {"n": 1, "names": ["a"]}
    assert "plain" not in approvals._SHAPES


def test_provide_is_validated():
    for bad in ({"db": 5}, {3: Session}, [Session], "x"):
        with pytest.raises(ValueError, match="provide"):
            runbound.init(provide=bad)
    api._teardown_for_tests()
    runbound.init(provide={Session: Session, "tenant": lambda: "acme"})


# --- a codec must round-trip: the approved tool runs with exactly what the caller passed ----------------------------------


class Lossy:
    """A stand-in whose JSON form is not the value: it dumps masked, drops a field, or cannot read its own dump."""

    def __init__(self, secret="hunter2", note="n"):
        self.secret, self.note = secret, note

    def __eq__(self, other):
        return isinstance(other, Lossy) and (self.secret, self.note) == (other.secret, other.note)


class Masked(Lossy):
    def model_dump(self, mode="python"):
        return {"secret": "**********", "note": self.note}

    @classmethod
    def model_validate(cls, value):
        return cls(value["secret"], value["note"])


class Dropping(Lossy):
    def model_dump(self, mode="python"):
        return {"secret": self.secret}  # `note` is excluded from the dump

    @classmethod
    def model_validate(cls, value):
        return cls(value["secret"], value["note"])  # ...and required on the way back


class Aliased(Lossy):
    def model_dump(self, mode="python"):
        return {"secret": self.secret, "note": self.note}

    @classmethod
    def model_validate(cls, value):
        return cls(value["Secret"], value["note"])  # reads the alias only


def refused_before_asking(model, value, parameter="payload"):
    plane = Plane()
    start(plane)
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def send(payload: model):
        ran.append(payload)

    with pytest.raises(runbound.ExecutionRefused) as caught:
        send(value)
    assert plane.calls == [] and ran == [], "no approval is opened and the tool never runs"
    assert caught.value.reason == "invalid_arguments" and not caught.value.retryable
    message = str(caught.value)
    assert f"parameter '{parameter}' cannot round-trip through JSON" in message and 'use mode="sync" or a JSON-friendly type' in message
    return message


@pytest.mark.parametrize("model", [Masked, Dropping, Aliased])
def test_a_model_that_does_not_come_back_equal_is_refused_before_an_approval_is_opened(model):
    refused_before_asking(model, model())


def test_the_refusal_says_why():
    assert "not equal" in refused_before_asking(Masked, Masked())
    assert "KeyError" in refused_before_asking(Dropping, Dropping())


def test_the_same_model_in_sync_mode_is_not_checked_because_nothing_is_replayed():
    plane = Plane(status="approved")
    start(plane)

    @runbound.tool(effects={"write"}, approval="required", mode="sync", timeout_s=60)
    def send(payload: Masked):
        return "sent"

    assert send(Masked()) == "sent"


def test_a_codec_inside_a_list_is_checked_too():
    plane = Plane()
    start(plane)

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def send_all(items: list[Masked]):
        ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        send_all([Masked()])
    assert "parameter 'items'" in str(caught.value) and plane.calls == []


def test_real_pydantic_models_that_do_not_round_trip_are_refused_and_ones_that_do_are_asked():
    pydantic = pytest.importorskip("pydantic")

    class WithSecret(pydantic.BaseModel):
        user: str
        token: pydantic.SecretStr

    class WithExcluded(pydantic.BaseModel):
        user: str
        token: str = pydantic.Field(default="d", exclude=True)

    class WithAlias(pydantic.BaseModel):
        user: str = pydantic.Field(alias="userName")

    class Plain(pydantic.BaseModel):
        user: str
        n: int = 1
        when: datetime.datetime

    assert "round-trip" in refused_before_asking(WithSecret, WithSecret(user="a", token="s3cr3t"))
    assert "round-trip" in refused_before_asking(WithExcluded, WithExcluded(user="a", token="not default"))
    assert "round-trip" in refused_before_asking(WithAlias, WithAlias(userName="a"))
    plane = Plane()
    start(plane)
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def send(payload: Plain):
        ran.append(payload)

    value = Plain(user="a", when=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    out = send(value)
    assert out["status"] == "pending_approval" and run(plane, approved(plane, out)) == "executed" and ran == [value]
