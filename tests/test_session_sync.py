"""End-to-end tests for fleet mode, through the public API.

``runbound.init(control_plane_url=...)`` and then ordinary SDK usage: a
:class:`FakePlane` stands in for the control plane (injected where the api
builds its client), and everything else — the engine, the exporter, the
poller — is the real thing.

The promises under test are the ones a customer would notice: tool calls never
touch the network, a fleet halt stops (or only warns about) every
tool call, a plane that is slow or broken never reaches the host, the heartbeat
brings policy and its poll interval, and none of it leaks a thread.
"""

import threading
import time

import pytest

import runbound
from runbound import api, shared as shared_module
from runbound.exceptions import GuardrailTripped, PolicyViolation, CapabilityRefused
from runbound.plane_types import HelloReply
from test_shared_state import FakePlane

PLANE_URL = "https://plane.example"


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
    """A fake control plane, wired in wherever the api builds a client."""
    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url = url
        fake.token = token
        fake.service = service
        fake.worker_id = worker_id
        fake.timeout_s = timeout_s
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    return fake


def start(**kwargs) -> None:
    """init() in fleet mode, with the poller effectively switched off."""
    fields = {
        "control_plane_url": PLANE_URL,
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
        "control_plane_poll_s": 3600.0,
        "export_events": False,
    }
    fields.update(kwargs)
    runbound.init(**fields)


def main_thread_calls(plane: FakePlane) -> list[str]:
    """Every plane call made on the thread the agent runs on."""
    name = threading.current_thread().name
    return [called for called, _payload, thread in plane.calls if thread == name]


def _tool(effects, name="issue_refund"):
    ran = []

    @runbound.tool(name=name, effects=effects, max_calls=99)
    def fn():
        ran.append(True)
        return "done"

    return fn, ran


def _read_tool():
    ran = []

    @runbound.tool(name="lookup", effects={"read"})
    def lookup():
        ran.append(True)
        return "found"

    return lookup, ran


# --- the plane is not on the call path --------------------------------------


def test_tool_calls_never_touch_the_network_on_the_calling_thread(plane):
    start()

    @runbound.tool(effects={"read"})
    def search(query: str) -> str:
        return "ok"

    with runbound.session("user-9"):
        for index in range(5):
            search(f"q{index}")

    assert main_thread_calls(plane) == []


def test_a_process_without_a_plane_url_stays_local():
    runbound.init()

    assert runbound.plane_status().mode == "local"


# --- halts, enforced per tool call ------------------------------------------


def test_a_fleet_halt_refuses_a_tool_call_under_raise(plane):
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True))
    tool, ran = _tool({"write"})

    with runbound.session("user-9"):
        with pytest.raises(GuardrailTripped) as caught:
            tool()

    assert ran == []
    assert caught.value.anomaly.detector == "halt"
    assert caught.value.anomaly.message == "Fleet halted by the control plane"
    assert caught.value.anomaly.details["service"] == "checkout"
    assert caught.value.reason == "halt"
    decision = caught.value.decision
    assert decision is not None
    assert decision.boundary == "halt"
    assert decision.level == "fleet"
    assert caught.value.retryable is False


def test_a_fleet_halt_refuses_even_a_read_tool_under_stop(plane):
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True, halt_mode="stop"))
    tool, ran = _read_tool()

    with runbound.session("user-9"):
        with pytest.raises(GuardrailTripped):
            tool()

    assert ran == []


def test_the_session_door_stays_open_under_a_halt(plane):
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True))
    ran = False

    with runbound.session("user-9"):
        ran = True

    assert ran is True


def test_a_fleet_halt_only_warns_under_warn(plane, caplog):
    start(on_halt="warn")
    api._SHARED.apply_hello(HelloReply(halt=True))
    tool, ran = _tool({"write"})

    with caplog.at_level("WARNING", logger="runbound"):
        with runbound.session("user-9"):
            assert tool() == "done"

    assert ran == [True]
    assert "halt" in caplog.text.lower()


def test_the_halt_warning_is_not_repeated_for_every_call(plane, caplog):
    start(on_halt="warn")
    api._SHARED.apply_hello(HelloReply(halt=True))
    tool, _ran = _tool({"write"})

    with caplog.at_level("WARNING", logger="runbound"):
        with runbound.session("user-9"):
            for _ in range(3):
                tool()

    assert caplog.text.lower().count("halted by the control plane") == 1


def test_a_lifted_halt_lets_the_tool_run_again(plane):
    start(on_halt="raise")
    tool, ran = _tool({"write"})
    api._SHARED.apply_hello(HelloReply(halt=True))
    with runbound.session("user-9"):
        with pytest.raises(GuardrailTripped):
            tool()

    api._SHARED.apply_hello(HelloReply(halt=False))
    with runbound.session("user-9"):
        assert tool() == "done"

    assert ran == [True]


def test_a_stale_halt_stops_being_enforced(plane):
    start(on_halt="raise")
    tool, ran = _tool({"write"})
    api._SHARED.apply_hello(HelloReply(halt=True))
    real = api._SHARED._now
    api._SHARED._now = lambda: real() + shared_module.STALE_HALT_S + 5

    with runbound.session("user-9"):
        assert tool() == "done"

    assert ran == [True]


# --- Narrow no longer exists: a narrow halt is a full halt -------------------


@pytest.mark.parametrize("effect", ["read", "write", "financial", "destructive", "external", "privileged"])
def test_a_narrow_halt_is_treated_as_a_full_halt(effect):
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True, halt_mode="narrow"))
    tool, ran = _tool({effect})

    with runbound.session("user-9"):
        with pytest.raises(GuardrailTripped) as caught:
            tool()

    assert ran == []
    assert caught.value.reason == "halt"
    assert caught.value.decision.level == "fleet"


def test_a_narrow_halt_says_once_that_it_is_treated_as_full(caplog):
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True, halt_mode="narrow"))
    tool, _ran = _tool({"read"})

    with caplog.at_level("WARNING", logger="runbound"):
        for _ in range(3):
            with pytest.raises(GuardrailTripped):
                tool()

    assert caplog.text.count("no longer supported") == 1


def test_a_narrow_halt_does_not_refuse_the_door():
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True, halt_mode="narrow"))
    ran = False

    with runbound.session("user-9"):
        ran = True

    assert ran is True


def test_a_narrow_halt_is_not_applied_under_on_halt_warn(caplog):
    start(on_halt="warn")
    api._SHARED.apply_hello(HelloReply(halt=True, halt_mode="narrow"))
    tool, ran = _tool({"financial"})

    with caplog.at_level("WARNING", logger="runbound"):
        with runbound.session("user-9"):
            tool()  # does not raise: on_halt="warn" applies no halt at all

    assert ran == [True]
    assert "halted" in caplog.text.lower()


# --- a plane that is down, slow or broken -----------------------------------


def test_a_plane_that_is_down_never_stops_a_tool(plane):
    plane.ok = False
    start(on_halt="raise")
    tool, ran = _tool({"write"})

    with runbound.session("user-9"):
        assert tool() == "done"

    assert ran == [True]


def test_a_plane_that_explodes_never_reaches_the_host(plane):
    plane.explodes = True
    start()
    tool, _ran = _tool({"write"})

    with runbound.session("user-9"):
        assert tool() == "done"


# --- what the heartbeat changes while the process runs ----------------------


def test_an_org_policy_arrives_without_an_init(plane):
    plane.policy_body = {"version": 1, "policy": {"deny": ["wire_transfer"]}}
    start()

    @runbound.tool(effects={"write"})
    def wire_transfer(amount: int) -> str:
        return "sent"

    with runbound.session("user-9"):
        assert wire_transfer(10) == "sent"  # nothing to enforce yet

    api._SHARED.apply_hello(HelloReply(policy_version=1))

    with pytest.raises(PolicyViolation):
        with runbound.session("user-9"):
            wire_transfer(10)


def test_an_org_dry_run_policy_lets_the_tool_run(plane):
    plane.policy_body = {
        "version": 1,
        "dry_run": True,
        "policy": {"deny": ["wire_transfer"]},
    }
    start()

    @runbound.tool
    def wire_transfer(amount: int) -> str:
        return "sent"

    api._SHARED.apply_hello(HelloReply(policy_version=1))

    with runbound.session("user-9"):
        assert wire_transfer(10) == "sent"


# --- the public reporting surface -------------------------------------------


def test_plane_status_reports_a_connected_link(plane):
    start()

    api._SHARED.apply_hello(HelloReply())
    status = runbound.plane_status()

    assert status.mode == "connected"
    assert status.consecutive_failures == 0
    assert status.last_contact_age_s is not None


def test_plane_status_before_init_is_local():
    assert runbound.plane_status().mode == "local"


def test_plane_status_reports_the_applied_policy_version(plane):
    plane.policy_body = {"version": 3, "policy": {"deny": ["x"]}}
    start()

    api._SHARED.apply_hello(HelloReply(policy_version=3))

    assert runbound.plane_status().applied_policy_version == 3


# --- the heartbeat's interval -------------------------------------------------


def test_the_planes_poll_interval_reaches_the_live_poller(plane):
    plane.reply = HelloReply(poll_s=42.0)
    start(control_plane_poll_s=0.01)

    deadline = time.monotonic() + 3.0
    while api._SHARED._poller._poll_s != 42.0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert api._SHARED._poller._poll_s == 42.0


# --- threads ----------------------------------------------------------------


def runbound_threads() -> list[str]:
    """The fleet-mode threads running right now, by name."""
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name in ("runbound-exporter", "runbound-plane-poller")
    )


def test_three_inits_leak_no_threads(plane):
    for _ in range(3):
        start(export_events=True)
        with runbound.session("user-9"):
            pass
        assert runbound_threads() == ["runbound-exporter", "runbound-plane-poller"]

    api._teardown_for_tests()
    deadline = time.monotonic() + 2.0
    while runbound_threads() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert runbound_threads() == []


def test_the_heartbeat_says_hello_on_its_own_thread(plane):
    plane.reply = HelloReply(poll_s=5.0, notice="all good")
    start(control_plane_poll_s=0.01)

    deadline = time.monotonic() + 2.0
    while "hello" not in plane.names() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert "hello" in plane.names()
    assert all(
        thread != threading.current_thread().name
        for called, _payload, thread in plane.calls
        if called == "hello"
    )
    payload = plane.payloads("hello")[0]
    assert payload["service"] == "checkout"
    assert payload["worker_id"] == "host-1:42"
    assert payload["policy_version_seen"] == 0
