"""Tests for the action policy at the tool hook.

The core (``runbound.policy``) decides whether one call breaks one rule;
these tests pin the thing customers actually rely on — that the decision
reaches the place where the tool would have run. A refused call must not run,
in a sync tool, an async tool, or a LangChain chain, and must be refused
whatever ``on_anomaly`` says, because the rule is the customer's own.
"""

import asyncio
import logging
import sys
import types

import pytest

import runbound
from runbound import api
from runbound.events import Anomaly
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.integrations import langchain as lc

SECRET = "sk-do-not-log-me-42"


class RecordingObserver:
    """Remembers every anomaly it was told about."""

    def __init__(self) -> None:
        self.sent: list[Anomaly] = []

    def on_event(self, session, event) -> None:
        pass

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.sent.append(anomaly)


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    lc._HANDLER_CLASS = None
    yield
    api._teardown_for_tests()
    lc._HANDLER_CLASS = None


@pytest.fixture
def handler(monkeypatch):
    """The LangChain handler, built against a minimal fake ``langchain_core``."""

    class BaseCallbackHandler:
        raise_error = False

    callbacks = types.ModuleType("langchain_core.callbacks")
    callbacks.BaseCallbackHandler = BaseCallbackHandler
    core = types.ModuleType("langchain_core")
    core.callbacks = callbacks
    monkeypatch.setitem(sys.modules, "langchain_core", core)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)
    return lc.GuardrailCallbackHandler()


def observer() -> RecordingObserver:
    """Attach a recording observer to the engine built by the last init()."""
    spy = RecordingObserver()
    api._ENGINE.observers.append(spy)
    return spy


class Counter:
    """A tool body that counts the times it actually ran."""

    def __init__(self) -> None:
        self.runs = 0

    def __call__(self, *args, **kwargs):
        self.runs += 1
        return "ran"


# --- deny / allow -----------------------------------------------------------


def test_denied_tool_is_refused_before_its_body_runs():
    runbound.init(tool_policy={"deny": ["delete_record"]})
    deleted, read = Counter(), Counter()

    @runbound.tool
    def delete_record(record_id):
        return deleted(record_id)

    @runbound.tool
    def read_record(record_id):
        return read(record_id)

    with pytest.raises(PolicyViolation) as caught:
        delete_record(7)

    assert deleted.runs == 0
    assert caught.value.violation.rule == "deny"
    assert caught.value.violation.tool == "delete_record"
    assert isinstance(caught.value, GuardrailTripped)
    assert read_record(7) == "ran"
    assert read.runs == 1


def test_only_allow_listed_tools_run():
    runbound.init(tool_policy={"allow": ["search"]})
    searched, wired = Counter(), Counter()

    @runbound.tool
    def search(q):
        return searched(q)

    @runbound.tool
    def wire_money(amount):
        return wired(amount)

    assert search("hats") == "ran"
    with pytest.raises(PolicyViolation) as caught:
        wire_money(100)

    assert wired.runs == 0
    assert caught.value.violation.rule == "allow"
    assert searched.runs == 1


# --- max_calls --------------------------------------------------------------


def test_max_calls_refuses_the_call_past_the_limit():
    runbound.init(tool_policy={"max_calls": {"send_email": 1}})
    sent = Counter()

    @runbound.tool
    def send_email(to):
        return sent(to)

    assert send_email("a@example.com") == "ran"
    with pytest.raises(PolicyViolation) as caught:
        send_email("b@example.com")

    assert sent.runs == 1
    assert caught.value.violation.rule == "max_calls"
    assert caught.value.violation.details["limit"] == 1
    # Both attempts are counted; only one of them ran.
    assert runbound.tool_calls() == {"send_email": 2}


def test_max_calls_is_counted_per_session():
    runbound.init(tool_policy={"max_calls": {"send_email": 1}})
    sent = Counter()

    @runbound.tool
    def send_email(to):
        return sent(to)

    with runbound.session("user-a"):
        send_email("first")
        with pytest.raises(PolicyViolation):
            send_email("second")
    with runbound.session("user-b"):
        assert send_email("first") == "ran"

    assert sent.runs == 2
    assert runbound.tool_calls("user-a") == {"send_email": 2}
    assert runbound.tool_calls("user-b") == {"send_email": 1}


# --- constraints ------------------------------------------------------------


def test_a_constraint_decides_on_the_real_arguments():
    runbound.init(
        tool_policy={
            "constraints": {"issue_refund": lambda c: c.kwargs.get("amount", 0) <= 500}
        }
    )
    refunded = Counter()

    @runbound.tool
    def issue_refund(*, amount):
        return refunded(amount=amount)

    assert issue_refund(amount=100) == "ran"
    with pytest.raises(PolicyViolation) as caught:
        issue_refund(amount=900)

    assert refunded.runs == 1
    assert caught.value.violation.rule == "constraint"


def test_a_constraint_sees_the_calling_session_and_its_tags():
    seen = []

    def only_paid(call):
        seen.append(call)
        return call.tags.get("plan") == "paid"

    runbound.init(tool_policy={"constraints": {"export": only_paid}})

    @runbound.tool
    def export(what):
        return "ran"

    with runbound.session("user-a", tags={"plan": "free"}):
        with pytest.raises(PolicyViolation):
            export("everything")
    with runbound.session("user-b", tags={"plan": "paid"}):
        assert export("everything") == "ran"

    assert [(c.session_key, c.tags["plan"]) for c in seen] == [
        ("user-a", "free"),
        ("user-b", "paid"),
    ]
    assert seen[0].name == "export"
    assert seen[0].args == ("everything",)


# --- approval ---------------------------------------------------------------


def test_approval_gate_allows_refuses_and_fails_closed():
    answers = {"ok": True}

    def ask(call):
        answer = answers["ok"]
        if answer == "boom":
            raise RuntimeError(f"cannot reach the approver for {call.name}")
        return answer

    runbound.init(
        tool_policy={"require_approval": ["wire_money"], "approval_callback": ask}
    )
    wired = Counter()

    @runbound.tool
    def wire_money(amount):
        return wired(amount)

    assert wire_money(10) == "ran"

    answers["ok"] = False
    with pytest.raises(PolicyViolation) as refused:
        wire_money(20)
    assert refused.value.violation.rule == "approval"

    answers["ok"] = "boom"
    with pytest.raises(PolicyViolation) as errored:
        wire_money(30)
    assert errored.value.violation.rule == "approval"
    assert errored.value.violation.details["error"] == "RuntimeError"

    assert wired.runs == 1


# --- on_violation modes -----------------------------------------------------


def test_dry_run_lets_the_call_through_but_logs_and_alerts_it(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init(
        tool_policy={"deny": ["delete_record"], "on_violation": "dry_run"}
    )
    spy = observer()
    deleted = Counter()

    @runbound.tool
    def delete_record(record_id):
        return deleted(record_id)

    assert delete_record(1) == "ran"
    assert delete_record(2) == "ran"

    assert deleted.runs == 2
    assert "dry-run" in caplog.text
    assert len(spy.sent) == 1  # the same refusal, retried, pages nobody twice
    assert spy.sent[0].severity == "warn"
    assert spy.sent[0].detector == "policy"


def test_block_and_latch_stops_the_whole_session():
    runbound.init(
        tool_policy={"deny": ["delete_record"], "on_violation": "block_and_latch"}
    )

    @runbound.tool
    def delete_record(record_id):
        return "ran"

    with runbound.session("user-a"):
        with pytest.raises(PolicyViolation):
            delete_record(1)

    latched = runbound.is_tripped("user-a")
    assert latched is not None
    assert latched.detector == "policy"


def test_block_and_latch_under_on_trip_once_stops_only_that_call():
    runbound.init(
        on_trip="once",
        tool_policy={"deny": ["delete_record"], "on_violation": "block_and_latch"},
    )

    @runbound.tool
    def delete_record(record_id):
        return "ran"

    with runbound.session("user-a"):
        with pytest.raises(PolicyViolation):
            delete_record(1)

    assert runbound.is_tripped("user-a") is None


def test_block_leaves_the_session_running():
    runbound.init(tool_policy={"deny": ["delete_record"]})

    @runbound.tool
    def delete_record(record_id):
        return "ran"

    @runbound.tool
    def read_record(record_id):
        return "ran"

    with runbound.session("user-a"):
        with pytest.raises(PolicyViolation):
            delete_record(1)
        assert read_record(1) == "ran"

    assert runbound.is_tripped("user-a") is None


def test_policy_is_enforced_whatever_on_anomaly_says():
    runbound.init(on_anomaly="warn", tool_policy={"deny": ["delete_record"]})
    deleted = Counter()

    @runbound.tool
    def delete_record(record_id):
        return deleted(record_id)

    with pytest.raises(PolicyViolation):
        delete_record(1)
    assert deleted.runs == 0


# --- async tools ------------------------------------------------------------


def test_async_denied_tool_is_refused_before_the_body_is_awaited():
    runbound.init(tool_policy={"deny": ["delete_record"]})
    deleted, fetched = Counter(), Counter()

    @runbound.tool
    async def delete_record(record_id):
        return deleted(record_id)

    @runbound.tool
    async def fetch(url):
        return fetched(url)

    async def run():
        with pytest.raises(PolicyViolation) as caught:
            await delete_record(1)
        assert caught.value.violation.rule == "deny"
        assert await fetch("https://example.com") == "ran"

    asyncio.run(run())

    assert deleted.runs == 0
    assert fetched.runs == 1


def test_async_max_calls_is_enforced():
    runbound.init(tool_policy={"max_calls": {"send_email": 1}})
    sent = Counter()

    @runbound.tool
    async def send_email(to):
        return sent(to)

    async def run():
        await send_email("first")
        with pytest.raises(PolicyViolation):
            await send_email("second")

    asyncio.run(run())
    assert sent.runs == 1


# --- the LangChain handler --------------------------------------------------


def test_langchain_tool_start_refuses_a_denied_tool(handler):
    runbound.init(tool_policy={"deny": ["delete_record"]})

    with pytest.raises(PolicyViolation) as caught:
        handler.on_tool_start({"name": "delete_record"}, "id=7")

    assert caught.value.violation.rule == "deny"
    handler.on_tool_start({"name": "search"}, "hats")  # allowed: no exception


def test_langchain_max_calls_is_counted_per_session(handler):
    runbound.init(tool_policy={"max_calls": {"send_email": 1}})

    with runbound.session("user-a"):
        handler.on_tool_start({"name": "send_email"}, "to=a")
        with pytest.raises(PolicyViolation):
            handler.on_tool_start({"name": "send_email"}, "to=b")
    with runbound.session("user-b"):
        handler.on_tool_start({"name": "send_email"}, "to=c")

    assert runbound.tool_calls("user-a") == {"send_email": 2}
    assert runbound.tool_calls("user-b") == {"send_email": 1}


# --- inert, private, and exported -------------------------------------------


def test_no_policy_is_enforced_before_init():
    ran = Counter()

    @runbound.tool
    def delete_record(record_id):
        return ran(record_id)

    assert delete_record(1) == "ran"
    assert ran.runs == 1
    assert runbound.tool_calls() == {}


def test_tool_calls_reports_copies_and_never_creates_a_session():
    runbound.init()

    @runbound.tool
    def search(q):
        return "ran"

    with runbound.session("user-a"):
        search("hats")

    counts = runbound.tool_calls("user-a")
    counts["search"] = 99
    assert runbound.tool_calls("user-a") == {"search": 1}
    assert runbound.tool_calls("nobody") == {}
    assert runbound.session_status("nobody") is None


def test_raw_arguments_never_reach_the_exception_or_the_anomaly():
    runbound.init(tool_policy={"deny": ["send_email"]})
    spy = observer()

    @runbound.tool
    def send_email(to, *, api_key):
        return "ran"

    with pytest.raises(PolicyViolation) as caught:
        send_email("a@example.com", api_key=SECRET)

    anomaly = spy.sent[0]
    assert SECRET not in str(caught.value)
    assert SECRET not in anomaly.message
    assert SECRET not in repr(anomaly.details)
    assert SECRET not in repr(caught.value.violation)


def test_the_policy_names_are_exported():
    assert runbound.ToolPolicy is not None
    assert runbound.ToolCall is not None
    assert runbound.Violation is not None
    assert issubclass(runbound.PolicyViolation, runbound.GuardrailTripped)
    assert callable(runbound.tool_calls)
    for name in ("ToolPolicy", "ToolCall", "Violation", "PolicyViolation", "tool_calls"):
        assert name in runbound.__all__


def test_a_broken_session_lookup_never_blocks_the_host(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    runbound.init(tool_policy={"deny": ["delete_record"]})
    ran = Counter()

    @runbound.tool
    def delete_record(record_id):
        return ran(record_id)

    def broken():
        raise RuntimeError("the registry is on fire")

    monkeypatch.setattr(api, "_active", broken)

    assert delete_record(1) == "ran"  # fail-open: our bug never stops the host
    assert ran.runs == 1
    assert "tool policy" in caplog.text
