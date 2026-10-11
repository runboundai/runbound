"""SDK-E1b: the plane says it is answering but not whole (``HelloReply.plane_degraded``), and the SDK shows it.

Informational only: no decision changes on it. ``PlaneStatus.plane_degraded`` is the last thing said (``None`` when it said
nothing or said it is fine); each CHANGE of it is one ``plane_degraded`` event, never one per hello."""

from __future__ import annotations

import pytest

import runbound
from runbound import api, cache, local_events
from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply, PlaneStatus, from_wire
from runbound.shared import RemoteState

URL = "http://plane.example"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Plane:
    service = "svc"
    worker_id = "w-1"
    key_state = "unknown"
    consecutive_failures = 0

    def policy(self, service):  # pragma: no cover - never asked here
        return None


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNBOUND_CACHE_DIR", str(tmp_path))
    api._teardown_for_tests()
    cache.reset_for_tests()
    yield
    api._teardown_for_tests()


def shared() -> RemoteState:
    config = RunboundConfig(control_plane_url=URL, token="k", service="svc", worker_id="w-1")
    config.validate()
    return RemoteState(Plane(), None, config, now=Clock())


def degraded_events() -> list:
    return [e for e in local_events.events(1000) if e["kind"] == "plane_degraded"]


def test_a_hello_carrying_plane_degraded_shows_in_the_status_and_one_without_clears_it():
    state = shared()
    assert state.status().plane_degraded is None
    state.apply_hello(HelloReply(plane_degraded="ledger_unreachable"))
    assert state.status().plane_degraded == "ledger_unreachable"
    assert state.status().mode == "connected"  # the link is fine: the plane says it is not whole, which is another fact
    state.apply_hello(HelloReply())
    assert state.status().plane_degraded is None


def test_the_public_plane_status_reads_it(monkeypatch):
    runbound.init(control_plane_url=URL, token="k", service="svc")
    api._SHARED.apply_hello(HelloReply(plane_degraded="ledger_unreachable"))
    assert runbound.plane_status().plane_degraded == "ledger_unreachable"
    assert PlaneStatus().plane_degraded is None
    assert "plane_degraded" in (runbound.plane_status.__doc__ or "")


def test_exactly_one_event_per_transition_over_twenty_hellos():
    state = shared()
    before = len(degraded_events())
    script = ["ledger_unreachable"] * 6 + [None] * 4 + ["ledger_unreachable"] * 4 + [None] * 6  # set, clear, set, clear
    assert len(script) == 20
    for said in script:
        state.apply_hello(HelloReply(plane_degraded=said))
    events = degraded_events()[before:]
    assert [(e["from"], e["to"], e["cleared"]) for e in events] == [
        (None, "ledger_unreachable", False), ("ledger_unreachable", None, True),
        (None, "ledger_unreachable", False), ("ledger_unreachable", None, True),
    ]


def test_a_change_of_the_reason_is_one_event_and_a_steady_nothing_is_none():
    state = shared()
    before = len(degraded_events())
    for said in (None, None, None):
        state.apply_hello(HelloReply(plane_degraded=said))
    assert len(degraded_events()) == before  # nothing said, nothing recorded
    state.apply_hello(HelloReply(plane_degraded="ledger_unreachable"))
    state.apply_hello(HelloReply(plane_degraded="ledger_slow"))
    assert [(e["from"], e["to"]) for e in degraded_events()[before:]] == [(None, "ledger_unreachable"), ("ledger_unreachable", "ledger_slow")]


def test_on_event_hears_the_transition_once():
    heard = []
    runbound.init(control_plane_url=URL, token="k", service="svc", on_event=heard.append)
    for _ in range(5):
        api._SHARED.apply_hello(HelloReply(plane_degraded="ledger_unreachable"))
    assert [e["kind"] for e in heard if e["kind"] == "plane_degraded"] == ["plane_degraded"]


def test_unknown_keys_are_ignored_and_plane_degraded_is_read_beside_them():
    reply = from_wire(HelloReply, {"policy_version": 4, "plane_degraded": "ledger_unreachable", "some_future_key": {"x": 1}})
    assert reply.plane_degraded == "ledger_unreachable" and reply.policy_version == 4
    assert not hasattr(reply, "some_future_key")
    assert from_wire(HelloReply, {"policy_version": 4}).plane_degraded is None  # a plane that predates it
    assert from_wire(HelloReply, {"plane_degraded": 7}).plane_degraded is None  # not text: ignored, never raised
    state = shared()
    state.apply_hello(from_wire(HelloReply, {"plane_degraded": "", "other_future_key": 1}))
    assert state.status().plane_degraded is None  # an empty string says nothing


def test_it_changes_no_decision_a_tool_runs_exactly_as_before():
    runbound.init(control_plane_url=URL, token="k", service="svc")
    api._SHARED.apply_hello(HelloReply(plane_degraded="ledger_unreachable"))

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ran"

    assert look() == "ran"
