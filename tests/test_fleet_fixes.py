"""``export_events=False`` closes the telemetry lanes and nothing else.

The exporter drops events and verdicts where they are handed over (not counted
as lost: the customer asked for it), is not installed as an engine observer
when there is nothing for it to observe, and no plane thread outlives a
teardown.
"""

import threading
import time

import runbound
from runbound import api
from runbound.config import RunboundConfig
from runbound.events import Anomaly, Event
from runbound.export import Exporter
from runbound.shared import RemoteState
from runbound.state import SessionState

KEY = "user-9"


class Client:
    """The client half of the plane: records batches, answers True."""

    def __init__(self) -> None:
        self.service = "checkout"
        self.worker_id = "host-1:42"
        self.key_state = "unknown"
        self.batches: list[dict] = []

    def events(self, batch: dict) -> bool:
        self.batches.append(batch)
        return True


def _event() -> Event:
    return Event(ts=time.monotonic(), step=1, kind="tool_call", tool_name="t")


def _anomaly() -> Anomaly:
    return Anomaly("max_actions", "critical", "over", {})


def test_the_exporter_drops_events_and_anomalies_when_told_to():
    client = Client()
    sink = Exporter(client, flush_every_s=30.0, include_events=False)
    session = SessionState("sess-1", key=KEY)

    sink.on_event(session, _event())
    sink.on_anomaly(session, _anomaly(), "raise")

    assert sink.pending == 0
    assert sink.dropped == 0  # configured off, not lost


def test_the_exporter_drops_runtime_changes_when_events_are_off():
    client = Client()
    sink = Exporter(client, flush_every_s=30.0, include_events=False)

    sink.on_change(
        {"kind": "runtime_change", "at": time.time(), "what": "policy_version", "from": 1, "to": 2}
    )

    assert sink.pending == 0
    assert sink.dropped == 0


def test_the_exporter_keeps_runtime_changes_when_events_are_on():
    client = Client()
    sink = Exporter(client, flush_every_s=30.0, include_events=True)

    sink.on_change(
        {"kind": "runtime_change", "at": time.time(), "what": "policy_version", "from": 1, "to": 2}
    )
    sink.flush(1.0)

    assert [change["what"] for change in client.batches[0]["changes"]] == ["policy_version"]


def test_an_exporter_that_drops_events_is_not_an_engine_observer():
    config = RunboundConfig(
        control_plane_url="https://plane.example", token="k", service="checkout", export_events=False
    )
    exporter = Exporter(Client(), include_events=False)
    shared = RemoteState(Client(), exporter, config)

    assert shared.observers() == []


def test_an_exporter_that_keeps_events_is_an_engine_observer():
    config = RunboundConfig(
        control_plane_url="https://plane.example", token="k", service="checkout"
    )
    exporter = Exporter(Client(), include_events=True)
    shared = RemoteState(Client(), exporter, config)

    assert shared.observers() == [exporter]


def test_no_plane_thread_outlives_a_teardown_with_events_off():
    api._teardown_for_tests()
    runbound.init(
        token="k",
        control_plane_url="http://127.0.0.1:9",
        service="checkout",
        export_events=False,
    )
    assert api._SHARED._exporter is not None
    assert api._SHARED.observers() == []
    api._teardown_for_tests()

    deadline = time.monotonic() + 2.0
    names = ("runbound-exporter", "runbound-plane-poller")
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name in names]:
            break
        time.sleep(0.01)

    assert [t.name for t in threading.enumerate() if t.name in names] == []
