"""End-to-end tests for fleet mode, through the public API.

``runbound.init(control_plane_url=...)`` and then ordinary SDK usage: a
:class:`FakePlane` stands in for the control plane (injected where the api
builds its client), and everything else — the engine, the exporter, the
poller, the session registry — is the real thing.

The promises under test are the ones a customer would notice: the plane is
contacted at the door and at the exit and nowhere else, a fleet-wide budget
stops the same turn a single worker would, a plane that is slow or broken
costs a request a bounded delay and never an answer, and none of it leaks a
thread.
"""

import threading
import time

import pytest

import runbound
from runbound import api, shared as shared_module
from runbound.exceptions import CircuitOpen, GuardrailTripped, PolicyViolation
from runbound.plane_types import EntryDecision, HelloReply, key_hash
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
        "auto_wrap": False,
    }
    fields.update(kwargs)
    runbound.init(**fields)


def main_thread_calls(plane: FakePlane) -> list[str]:
    """Every plane call made on the thread the agent runs on."""
    name = threading.current_thread().name
    return [called for called, _payload, thread in plane.calls if thread == name]


def exported(plane: FakePlane, lane: str) -> list:
    """Everything the exporter has posted on one lane, flushed first."""
    api._SHARED._exporter.flush(1.0)
    return [record for batch in plane.payloads("events") for record in batch.get(lane, [])]


def latch_payload(**fields) -> dict:
    payload = {
        "detector": "budget",
        "severity": "critical",
        "message": "Budget exceeded on another worker",
        "details": {"limit_hit": "budget_usd", "total_cost_usd": 5.4},
        "ttl_remaining_s": 30.0,
    }
    payload.update(fields)
    return payload


# --- the plane is contacted at the door and nowhere else --------------------


def test_the_observation_path_never_touches_the_network(plane):
    plane.decision = EntryDecision(fleet_spend_usd=0.1)
    start(budget_usd=100.0, custom_prices={"test": (0.0, 300.0)})

    @runbound.tool
    def search(query: str) -> str:
        return "ok"

    with runbound.session("user-9"):
        for index in range(5):
            search(f"q{index}")
            runbound.record_call("test", tokens_in=10, tokens_out=10)

    assert main_thread_calls(plane) == ["enter"]


def test_entry_and_exit_are_the_only_calls_a_quiet_block_makes(plane):
    plane.decision = EntryDecision()
    start(export_events=True)

    with runbound.session("user-9"):
        pass

    assert main_thread_calls(plane) == ["enter"]
    assert [delta["key_hash"] for delta in exported(plane, "exits")] == [key_hash("user-9")]


def test_a_process_without_a_plane_url_stays_local():
    runbound.init(budget_usd=5.0)

    assert runbound.plane_status().mode == "local"
    assert runbound.fleet_status("user-9") is None


# --- what the plane says at the door ----------------------------------------


def test_a_remote_latch_refuses_the_block_before_it_runs(plane):
    plane.decision = EntryDecision(latch=latch_payload())
    start(on_anomaly="raise")
    ran = False

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            ran = True

    assert ran is False
    assert caught.value.anomaly.message == "Budget exceeded on another worker"
    assert caught.value.anomaly.detector == "budget"
    assert runbound.is_tripped("user-9") is not None


def test_a_remote_latch_expires_on_its_own_ttl(plane):
    plane.decision = EntryDecision(latch=latch_payload(ttl_remaining_s=0.0))
    start(on_anomaly="raise")

    with runbound.session("user-9"):
        pass  # a latch with nothing left on it stops nothing


def test_the_fleet_spend_offset_trips_mid_block(plane):
    plane.decision = EntryDecision(fleet_spend_usd=4.80)
    start(budget_usd=5.0, on_anomaly="raise", custom_prices={"test": (0.0, 300.0)})

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            # $0.30 locally, $4.80 already spent by the rest of the fleet.
            runbound.record_call("test", tokens_in=0, tokens_out=1000)

    assert caught.value.anomaly.detector == "budget"
    assert caught.value.anomaly.details["fleet_spend_offset_usd"] == pytest.approx(4.80)


def test_the_offset_never_double_counts_this_workers_own_spend(plane):
    plane.decision = EntryDecision(fleet_spend_usd=4.80)
    start(budget_usd=5.0, custom_prices={"test": (0.0, 300.0)})

    with runbound.session("user-9") as state:
        runbound.record_call("test", tokens_in=0, tokens_out=500)
    # $4.95 fleet-wide now, of which $0.15 is this worker's and already
    # counted locally: the offset is still the other workers' $4.80.
    api._SHARED._cache.clear()
    plane.decision = EntryDecision(fleet_spend_usd=4.80 + 0.15)
    with runbound.session("user-9") as state:
        pass

    assert state.spend_offset_usd == pytest.approx(4.80)


def test_a_cached_decision_never_hides_this_workers_own_spending(plane):
    plane.decision = EntryDecision(fleet_spend_usd=4.80)
    start(budget_usd=5.0, custom_prices={"test": (0.0, 300.0)})

    with runbound.session("user-9") as state:
        runbound.record_call("test", tokens_in=0, tokens_out=500)
    with runbound.session("user-9") as state:  # inside the 5 s cache window
        pass

    assert state.spend_offset_usd == pytest.approx(4.80)
    assert state.total_cost_usd == pytest.approx(0.15)


def test_the_fleet_token_offset_is_applied_too(plane):
    plane.decision = EntryDecision(fleet_tokens=900)
    start(max_total_tokens=1000, on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            runbound.record_call("test", tokens_in=60, tokens_out=60)

    assert caught.value.anomaly.details["limit_hit"] == "max_total_tokens"


def test_the_fleets_strikes_and_generation_are_carried_over(plane):
    plane.decision = EntryDecision(strikes=2, generation=7)
    start(on_spike="limit")

    with runbound.session("user-9") as state:
        assert state.fleet_generation == 7

    assert runbound.session_status("user-9")["strikes"] == 2


def test_a_local_strike_is_never_lowered_by_the_plane(plane):
    plane.decision = EntryDecision(strikes=0)
    start()
    api._STRIKES["user-9"] = 3

    with runbound.session("user-9"):
        pass

    assert api._STRIKES["user-9"] == 3


# --- halts ------------------------------------------------------------------


def test_a_fleet_halt_refuses_the_block_under_raise(plane):
    plane.decision = EntryDecision(halt=True)
    start(on_halt="raise")
    ran = False

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            ran = True

    assert ran is False
    assert caught.value.anomaly.detector == "halt"
    assert caught.value.anomaly.message == "Fleet halted by the control plane"
    assert caught.value.anomaly.details["service"] == "checkout"
    assert caught.value.anomaly.details["key_hash"] == key_hash("user-9")


def test_a_fleet_halt_only_warns_under_warn(plane, caplog):
    plane.decision = EntryDecision(halt=True)
    start(on_halt="warn")
    ran = False

    with caplog.at_level("WARNING", logger="runbound"):
        with runbound.session("user-9"):
            ran = True

    assert ran is True
    assert "halt" in caplog.text.lower()


def test_the_halt_warning_is_not_repeated_for_every_block(plane, caplog):
    plane.decision = EntryDecision(halt=True)
    start(on_halt="warn")

    with caplog.at_level("WARNING", logger="runbound"):
        for index in range(3):
            with runbound.session(f"user-{index}"):
                pass

    assert caplog.text.lower().count("halted by the control plane") == 1


def test_a_halt_the_heartbeat_reported_refuses_too(plane):
    plane.decision = EntryDecision()
    start(on_halt="raise")
    api._SHARED.apply_hello(HelloReply(halt=True))

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            pass

    assert caught.value.anomaly.detector == "halt"


# --- a plane that is slow, broken, or rejecting us --------------------------


def test_a_slow_plane_costs_the_entry_its_timeout_and_no_more(plane):
    plane.delay_s = 1.0
    plane.decision = EntryDecision(fleet_spend_usd=99.0)
    start(budget_usd=5.0, control_plane_timeout_s=0.15)

    started = time.monotonic()
    with runbound.session("user-9") as state:
        elapsed = time.monotonic() - started

    # Bounded by the timeout, not by the second the plane took to answer.
    assert elapsed <= 0.20
    # Served locally: the plane's answer never arrived, so no offset applied.
    assert state.spend_offset_usd == 0.0


def test_a_rejected_key_degrades_the_link_and_stops_calling(plane):
    def reject(payload):
        plane.key_state = "invalid"
        return None

    plane.enter = reject  # a 401 as PlaneClient reports it
    start(budget_usd=5.0, on_anomaly="raise", custom_prices={"test": (0.0, 300.0)})

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            runbound.record_call("test", tokens_in=0, tokens_out=20000)

    assert caught.value.anomaly.detector == "budget"
    assert runbound.plane_status().mode == "degraded"


def test_every_detector_still_fires_while_the_plane_is_down(plane):
    plane.ok = False
    start(max_steps=2, on_anomaly="raise", loop_threshold=2)

    with pytest.raises(GuardrailTripped) as caught:
        with runbound.session("user-9"):
            for _ in range(4):
                runbound.record_call("test", tokens_in=1, tokens_out=1)

    assert caught.value.anomaly.detector == "steps"


def test_a_plane_that_explodes_never_reaches_the_host(plane):
    plane.explodes = True
    start()

    with runbound.session("user-9") as state:
        assert state is not None


# --- exits ------------------------------------------------------------------


def test_exits_are_deltas_with_a_rising_sequence(plane):
    plane.decision = EntryDecision()
    start(export_events=True, custom_prices={"test": (0.0, 300.0)})

    with runbound.session("user-9"):
        runbound.record_call("test", tokens_in=0, tokens_out=1000)
    with runbound.session("user-9"):
        runbound.record_call("test", tokens_in=0, tokens_out=2000)

    exits = exported(plane, "exits")
    assert [delta["seq"] for delta in exits] == [1, 2]
    assert exits[0]["spend_delta_usd"] == pytest.approx(0.30)
    assert exits[1]["spend_delta_usd"] == pytest.approx(0.60)
    assert exits[1]["tokens_delta"] == 2000


def test_an_exit_reports_the_tools_that_ran_inside_the_block(plane):
    plane.decision = EntryDecision()
    start(export_events=True)

    @runbound.tool
    def search(query: str) -> str:
        return "ok"

    with runbound.session("user-9"):
        search("a")
        search("b")

    assert exported(plane, "exits")[0]["tool_calls"] == {"search": 2}


def test_a_block_that_did_nothing_still_reports_its_exit(plane):
    plane.decision = EntryDecision()
    start(export_events=True)

    with runbound.session("user-9"):
        pass

    exits = exported(plane, "exits")
    assert len(exits) == 1
    assert exits[0]["spend_delta_usd"] == 0.0


def test_an_exception_inside_the_block_still_reports_the_exit(plane):
    plane.decision = EntryDecision()
    start(export_events=True)

    with pytest.raises(ValueError):
        with runbound.session("user-9"):
            raise ValueError("the app blew up")

    assert len(exported(plane, "exits")) == 1


# --- trips reach the plane synchronously ------------------------------------


def test_a_trip_is_reported_to_the_plane_at_once(plane):
    plane.decision = EntryDecision()
    start(budget_usd=0.10, on_anomaly="raise", custom_prices={"test": (0.0, 300.0)})

    with pytest.raises(GuardrailTripped):
        with runbound.session("user-9"):
            runbound.record_call("test", tokens_in=0, tokens_out=1000)

    reports = plane.payloads("trip")
    assert len(reports) == 1
    assert reports[0].key_hash == key_hash("user-9")
    assert reports[0].anomaly["detector"] == "budget"
    assert reports[0].anomaly["reacted"] == "raise"
    assert reports[0].refused_at_door is False


# --- clear ------------------------------------------------------------------


def test_clear_forwards_to_the_plane(plane):
    plane.decision = EntryDecision()
    start()

    with runbound.session("user-9"):
        pass
    runbound.clear("user-9")

    assert plane.payloads("clear") == [key_hash("user-9")]


def test_clear_before_init_touches_nothing(plane):
    runbound.clear("user-9")

    assert plane.calls == []


# --- what the heartbeat changes while the process runs ----------------------


def test_an_org_policy_arrives_without_an_init(plane):
    plane.decision = EntryDecision()
    plane.policy_body = {"version": 1, "policy": {"deny": ["wire_transfer"]}}
    start()

    @runbound.tool
    def wire_transfer(amount: int) -> str:
        return "sent"

    with runbound.session("user-9"):
        assert wire_transfer(10) == "sent"  # nothing to enforce yet

    api._SHARED.apply_hello(HelloReply(policy_version=1))

    with pytest.raises(PolicyViolation):
        with runbound.session("user-9"):
            wire_transfer(10)


def test_an_org_dry_run_policy_lets_the_tool_run(plane):
    plane.decision = EntryDecision()
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


def test_a_fleet_circuit_refuses_calls_under_open(plane):
    plane.decision = EntryDecision()
    start(on_provider_failure="open")

    api._SHARED.apply_hello(
        HelloReply(circuits={"openai@api": {"state": "open", "until_s": 30}})
    )

    assert runbound.circuit_state("openai@api") == "open"
    with pytest.raises(CircuitOpen):
        api._HOOKS.before("openai@api")


def test_a_fleet_circuit_only_notifies_under_notify(plane):
    plane.decision = EntryDecision()
    start(on_provider_failure="notify")

    api._SHARED.apply_hello(HelloReply(circuits={"openai@api": "open"}))

    assert runbound.circuit_state("openai@api") == "open"
    api._HOOKS.before("openai@api")  # counted and reported; never refused


def test_the_plane_can_close_a_circuit_again(plane):
    plane.decision = EntryDecision()
    start(on_provider_failure="open")
    api._SHARED.apply_hello(HelloReply(circuits={"openai@api": "open"}))

    api._SHARED.apply_hello(HelloReply(circuits={"openai@api": "closed"}))

    assert runbound.circuit_state("openai@api") == "closed"
    api._HOOKS.before("openai@api")
    api._HOOKS.release("openai@api")


# --- the public reporting surface -------------------------------------------


def test_plane_status_reports_a_connected_link(plane):
    plane.decision = EntryDecision()
    start()

    with runbound.session("user-9"):
        pass
    status = runbound.plane_status()

    assert status.mode == "connected"
    assert status.consecutive_failures == 0
    assert status.last_contact_age_s is not None


def test_plane_status_before_init_is_local():
    assert runbound.plane_status().mode == "local"


def test_fleet_status_reports_what_the_plane_said(plane):
    plane.decision = EntryDecision(
        fleet_spend_usd=4.5, fleet_tokens=900, strikes=1, generation=3
    )
    start()

    with runbound.session("user-9"):
        pass
    status = runbound.fleet_status("user-9")

    assert status["fleet_spend_usd"] == 4.5
    assert status["fleet_tokens"] == 900
    assert status["strikes"] == 1
    assert status["generation"] == 3
    assert status["halt"] is False
    assert status["latched"] is False
    assert status["age_s"] >= 0.0


def test_fleet_status_is_none_for_a_key_the_plane_has_not_seen(plane):
    plane.decision = EntryDecision()
    start()

    assert runbound.fleet_status("nobody") is None


def test_key_hash_is_the_public_digest():
    assert runbound.key_hash("user-9") == key_hash("user-9")
    assert "user-9" not in runbound.key_hash("user-9")


# --- threads ----------------------------------------------------------------


def runbound_threads() -> list[str]:
    """The fleet-mode threads running right now, by name."""
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name in ("runbound-exporter", "runbound-plane-poller")
    )


def test_three_inits_leak_no_threads(plane):
    plane.decision = EntryDecision()

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
