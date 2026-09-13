"""The tool report: what tools this worker has, and when the plane hears it.

Two halves. The builder in :mod:`runbound._coverage` turns the
``@runbound.tool`` decorators a process imported into a list of plain dicts —
names, parameter names, annotations rendered as strings, one docstring line —
and nothing else, because everything else is a customer's data. The gate in
:class:`runbound.shared.RemoteState` puts a hash of that list on every
heartbeat and the list itself only when the hash moved, so a fleet of workers
costs one payload per deploy rather than one per five seconds.

No sockets: the plane is ``test_shared_state``'s in-memory fake and one helper
here replays exactly what :class:`runbound.plane.Poller` does with it.
"""

import json

import pytest

from test_shared_state import FakePlane, MovableClock, remote

import runbound
from runbound import _coverage, api
from runbound.plane_types import HelloReply, from_wire


@pytest.fixture(autouse=True)
def _empty_tool_store():
    """Every test here starts with a process that knows no tools at all."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def entry(report: list[dict], name: str) -> dict:
    """The one entry named ``name``, or a failed assertion saying it is missing."""
    found = [item for item in report if item["name"] == name]
    assert found, f"{name!r} is not in {[item['name'] for item in report]}"
    return found[0]


def heartbeat(shared, plane: FakePlane) -> dict:
    """One poll, exactly as :meth:`runbound.plane.Poller._tick` runs it."""
    payload = shared._hello_payload()
    reply = plane.hello(payload)
    if reply is not None:
        shared.apply_hello(reply)
    return payload


# --- what the report says ----------------------------------------------------


def test_the_three_demo_tools_arrive_with_their_parameters():
    """The founder's question — where are my tools? — answered from the code."""

    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    @runbound.tool
    def lookup_order(order_id: str): ...

    @runbound.tool
    def send_email(to: str): ...

    report = runbound.tools()

    assert [item["name"] for item in report] == [
        "issue_refund",
        "lookup_order",
        "send_email",
    ]
    assert entry(report, "issue_refund")["params"] == [
        {"name": "user", "annotation": "str", "required": True},
        {"name": "amount", "annotation": "float", "required": True},
    ]
    assert entry(report, "lookup_order")["params"] == [
        {"name": "order_id", "annotation": "str", "required": True}
    ]
    assert entry(report, "send_email")["params"] == [
        {"name": "to", "annotation": "str", "required": True}
    ]
    assert all(item["decorated"] for item in report)


def test_every_entry_has_exactly_the_five_agreed_keys_in_order():
    """The plane reads this shape; a sixth key or a reordering is a contract break."""

    @runbound.tool
    def search(query: str) -> list:
        """Look something up."""

    (item,) = runbound.tools()

    assert list(item) == ["name", "decorated", "params", "doc", "module"]
    assert item["doc"] == "Look something up."
    assert item["module"] == __name__
    assert list(item["params"][0]) == ["name", "annotation", "required"]


def test_a_default_makes_a_parameter_optional_and_the_value_never_travels():
    """Present or absent is a fact about the tool; the value is the customer's."""

    @runbound.tool
    def f(a, b=3): ...

    report = runbound.tools()

    assert entry(report, "f")["params"] == [
        {"name": "a", "annotation": None, "required": True},
        {"name": "b", "annotation": None, "required": False},
    ]
    assert "3" not in json.dumps(report)


def test_only_the_first_line_of_a_docstring_travels():
    """A privacy rule, not a formatting preference: the rest may hold anything."""

    @runbound.tool
    def refund(user: str):
        """Refund a customer.

        Internal: call the ledger at 10.0.0.4 with the shared secret first,
        then mail accounts@example.com.
        """

    doc = entry(runbound.tools(), "refund")["doc"]

    assert doc == "Refund a customer."
    assert "10.0.0.4" not in json.dumps(runbound.tools())


def test_a_tool_with_no_docstring_reports_none():
    @runbound.tool
    def bare(): ...

    assert entry(runbound.tools(), "bare")["doc"] is None


def test_star_args_are_named_as_written_and_never_required():
    @runbound.tool
    def variadic(first: int, *args, key: str = "k", **kwargs): ...

    assert entry(runbound.tools(), "variadic")["params"] == [
        {"name": "first", "annotation": "int", "required": True},
        {"name": "*args", "annotation": None, "required": False},
        {"name": "key", "annotation": "str", "required": False},
        {"name": "**kwargs", "annotation": None, "required": False},
    ]


def test_self_and_cls_are_not_parameters_of_a_tool():
    """A bound method's receiver is plumbing, not something the model passes."""

    class Bot:
        @runbound.tool(name="method_tool")
        def act(self, order_id: str): ...

        @classmethod
        @runbound.tool(name="class_tool")
        def make(cls, order_id: str): ...

    report = runbound.tools()

    assert entry(report, "method_tool")["params"] == [
        {"name": "order_id", "annotation": "str", "required": True}
    ]
    assert entry(report, "class_tool")["params"] == [
        {"name": "order_id", "annotation": "str", "required": True}
    ]


def test_a_generic_annotation_is_rendered_as_its_source_text():
    @runbound.tool
    def batch(ids: list[int], opts: dict[str, float] | None = None): ...

    assert entry(runbound.tools(), "batch")["params"] == [
        {"name": "ids", "annotation": "list[int]", "required": True},
        {"name": "opts", "annotation": "dict[str, float] | None", "required": False},
    ]


def test_a_custom_class_annotation_carries_no_memory_address():
    """``str()`` of an object is only acceptable while it is stable run to run."""

    class Order: ...

    @runbound.tool
    def ship(order: Order): ...

    annotation = entry(runbound.tools(), "ship")["params"][0]["annotation"]

    assert annotation is not None
    assert "0x" not in annotation
    assert "Order" in annotation


def test_the_name_keyword_is_the_name_the_report_carries():
    @runbound.tool(name="web_search")
    def search(query: str): ...

    assert [item["name"] for item in runbound.tools()] == ["web_search"]


def test_an_async_tool_reports_its_parameters_like_any_other():
    @runbound.tool
    async def fetch(url: str): ...

    assert entry(runbound.tools(), "fetch")["params"] == [
        {"name": "url", "annotation": "str", "required": True}
    ]


# --- tools the model asks for, that nothing guards ---------------------------


def test_a_requested_tool_nobody_decorated_is_reported_undecorated():
    """The console shows this one in red: the model can ask, nothing guards it."""
    _coverage.tool_requested("mystery")

    assert runbound.tools() == [
        {
            "name": "mystery",
            "decorated": False,
            "params": [],
            "doc": None,
            "module": None,
        }
    ]


def test_a_request_never_downgrades_a_declared_tool_whichever_order_it_comes_in():
    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    _coverage.tool_requested("issue_refund")
    after = entry(runbound.tools(), "issue_refund")

    _coverage.reset_for_tests()
    _coverage.tool_requested("issue_refund")
    _coverage.tool_declared("issue_refund", issue_refund)
    before = entry(runbound.tools(), "issue_refund")

    assert after == before
    assert after["decorated"] is True
    assert after["params"] == [
        {"name": "user", "annotation": "str", "required": True},
        {"name": "amount", "annotation": "float", "required": True},
    ]


def test_the_wrappers_tool_request_hook_records_the_name():
    """A model asking for a tool the developer dispatches by hand still counts."""
    runbound.init()
    api._HOOKS.tool_request("wire_money", "req:abc")

    assert entry(runbound.tools(), "wire_money")["decorated"] is False


# --- limits and failures -----------------------------------------------------


def test_more_tools_than_the_cap_are_truncated_not_dropped_and_not_all_sent():
    for index in range(_coverage.TOOL_REPORT_MAX + 100):
        _coverage.tool_declared(f"tool_{index:04d}")

    report = runbound.tools()

    assert len(report) == _coverage.TOOL_REPORT_MAX
    assert report[0]["name"] == "tool_0000"
    assert report[-1]["name"] == f"tool_{_coverage.TOOL_REPORT_MAX - 1:04d}"


def test_a_callable_with_no_readable_signature_still_gets_an_entry():
    """Builtins and exotic wrappers are reported as known, with no parameters."""

    class Exotic:
        @property
        def __signature__(self):
            raise ValueError("this callable does not describe itself")

        def __call__(self, order_id): ...

    _coverage.tool_declared("exotic", Exotic())

    item = entry(runbound.tools(), "exotic")

    assert item["decorated"] is True
    assert item["params"] == []


def test_a_builder_that_explodes_costs_a_report_and_nothing_else():
    """Fail-open: a broken report is an empty report, never a raised exception."""

    class Hostile:
        @property
        def __doc__(self):
            raise RuntimeError("no docstring for you")

        def __call__(self): ...

    _coverage.tool_declared("hostile", Hostile())

    assert isinstance(runbound.tools(), list)


def test_the_report_a_caller_mutates_is_not_the_stored_one():
    @runbound.tool
    def search(query: str): ...

    report = runbound.tools()
    report[0]["name"] = "clobbered"
    report[0]["params"].clear()

    assert entry(runbound.tools(), "search")["params"] != []


def test_reset_for_tests_empties_the_store():
    @runbound.tool
    def search(query: str): ...

    _coverage.reset_for_tests()

    assert runbound.tools() == []


def test_the_old_coverage_counters_keep_their_meaning():
    """The report is a second, richer store — it does not replace the counters."""

    @runbound.tool
    def search(query: str): ...

    snapshot = _coverage.snapshot()

    assert snapshot["decorated_tools"] == 1
    assert snapshot["decorated_tool_names"] == ["search"]
    assert _coverage.DECORATED_TOOL_NAMES_MAX == 32
    assert _coverage.TOOL_REPORT_MAX == 500


# --- the hash ----------------------------------------------------------------


def test_the_hash_is_sixteen_hex_characters_and_stable_for_one_report():
    @runbound.tool
    def search(query: str): ...

    digest = _coverage.tool_report_hash()

    assert len(digest) == 16
    assert all(char in "0123456789abcdef" for char in digest)
    assert digest == _coverage.tool_report_hash(runbound.tools())


def test_the_hash_moves_when_a_tool_does():
    before = _coverage.tool_report_hash()

    @runbound.tool
    def search(query: str): ...

    after = _coverage.tool_report_hash()
    _coverage.tool_declared("search", lambda query, page=1: None)

    assert before != after
    assert _coverage.tool_report_hash() != after


# --- the heartbeat gate ------------------------------------------------------


def test_the_hash_rides_every_heartbeat_and_the_report_only_the_first():
    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    plane = FakePlane(reply=HelloReply())
    shared = remote(plane, MovableClock())

    first = heartbeat(shared, plane)
    second = heartbeat(shared, plane)

    assert first["tools_hash"] == second["tools_hash"] == _coverage.tool_report_hash()
    assert [item["name"] for item in first["tools"]] == ["issue_refund"]
    assert "tools" not in second


def test_a_new_tool_puts_the_report_back_on_the_next_heartbeat():
    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    plane = FakePlane(reply=HelloReply())
    shared = remote(plane, MovableClock())
    first = heartbeat(shared, plane)
    assert "tools" not in heartbeat(shared, plane)

    @runbound.tool
    def send_email(to: str): ...

    third = heartbeat(shared, plane)

    assert third["tools_hash"] != first["tools_hash"]
    assert [item["name"] for item in third["tools"]] == ["issue_refund", "send_email"]
    assert "tools" not in heartbeat(shared, plane)


def test_a_plane_that_says_it_has_no_tools_gets_the_whole_report_again():
    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    plane = FakePlane(reply=HelloReply())
    shared = remote(plane, MovableClock())
    heartbeat(shared, plane)
    assert "tools" not in heartbeat(shared, plane)

    plane.reply = HelloReply(tools_known=False)
    heartbeat(shared, plane)
    plane.reply = HelloReply()
    resent = heartbeat(shared, plane)

    assert [item["name"] for item in resent["tools"]] == ["issue_refund"]
    assert "tools" not in heartbeat(shared, plane)


def test_an_old_plane_that_never_mentions_tools_never_causes_a_resend():
    """``tools_known`` defaults to ``True`` precisely so this cannot loop forever."""

    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    plane = FakePlane(reply=from_wire(HelloReply, {"org_id": "acme", "plan": "pro"}))
    shared = remote(plane, MovableClock())

    first = heartbeat(shared, plane)

    assert plane.reply.tools_known is True
    assert "tools" in first
    for _ in range(3):
        assert "tools" not in heartbeat(shared, plane)


def test_a_hello_that_is_never_answered_does_not_count_as_acknowledged():
    @runbound.tool
    def issue_refund(user: str, amount: float): ...

    plane = FakePlane(reply=None)
    shared = remote(plane, MovableClock())

    first = heartbeat(shared, plane)
    second = heartbeat(shared, plane)

    assert "tools" in first
    assert [item["name"] for item in second["tools"]] == ["issue_refund"]


def test_a_broken_tool_report_costs_the_heartbeat_neither_key_nor_the_host(monkeypatch):
    def _boom():
        raise RuntimeError("no report today")

    monkeypatch.setattr(_coverage, "tool_report", _boom)
    plane = FakePlane(reply=HelloReply())
    shared = remote(plane, MovableClock())

    payload = heartbeat(shared, plane)

    assert "tools" not in payload
    assert "tools_hash" not in payload
    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert isinstance(payload["coverage"], dict)


def test_the_report_on_the_wire_is_what_runbound_tools_shows():
    """``runbound.tools()`` is a promise about the payload, not a pretty print."""

    @runbound.tool
    def issue_refund(user: str, amount: float):
        """Refund a customer."""

    plane = FakePlane(reply=HelloReply())
    shared = remote(plane, MovableClock())

    assert heartbeat(shared, plane)["tools"] == runbound.tools()
