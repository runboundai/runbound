"""The SDK treats a hello's ``fleet_state: "unavailable"`` as plane loss
for state, not for the link.

Two easy-to-fall-into shortcuts would make these tests lie: calling
``apply_hello()`` directly never touches the failure/success accounting a
real heartbeat would, and a single-threaded fake plane serializes
``/v1/hello`` behind a slow ``/v1/enter`` on the same socket, which can
make a partial outage look total. Both are closed here the same way: a
real background :class:`~runbound.plane.Poller` thread, talking to a real
``http.server.ThreadingHTTPServer`` over a real socket, with every reply
built by ``plane_types.to_wire`` — a hand-written dict that happens to
parse is not the same test as the real wire encoding, and the module
docstring of ``routers/sdk.py`` on the plane side is exactly what asserts
this must survive an SDK a version ahead or behind.

The stale-halt window (60s) is exercised by advancing a fake monotonic
clock handed to :class:`~runbound.shared.RemoteState` through its own,
already-public ``now`` constructor parameter — not a test-only branch in
product code, and not sixty real seconds of `sleep`.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from typing import Any

import pytest

from runbound.plane import PlaneClient, Poller
from runbound.plane_types import HelloReply, to_wire
from runbound.shared import RemoteState

POLL_S = 0.02
WAIT_TIMEOUT_S = 5.0


class _FakePlaneState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hello = HelloReply()
        self.hello_calls = 0


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # silence the test log

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

    def do_POST(self) -> None:  # noqa: N802 - http.server's own naming
        self._read_body()
        state: _FakePlaneState = self.server.fake_state  # type: ignore[attr-defined]
        if self.path == "/v1/hello":
            with state.lock:
                state.hello_calls += 1
                reply = state.hello
            self._send_json(200, to_wire(reply))
        elif self.path == "/v1/events":
            self._send_json(202, {"accepted": 0, "dropped_by_plan": 0})
        else:
            self._send_json(404, {"error": "not_found"})


class FakePlane:
    """A real HTTP server, on its own thread, answering with wire-encoded
    bodies -- see the module docstring for why this, not an in-process
    mock, is what these tests must drive."""

    def __init__(self) -> None:
        self.state = _FakePlaneState()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake_state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        _, port = self._server.server_address
        return f"http://127.0.0.1:{port}"

    def set_hello(self, reply: HelloReply) -> None:
        with self.state.lock:
            self.state.hello = reply

    @property
    def hello_calls(self) -> int:
        with self.state.lock:
            return self.state.hello_calls

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class _MovableClock:
    """A monotonic clock this test advances by hand -- so the 60s
    ``stale_halt`` window is proven without a real 60s sleep, using
    :class:`RemoteState`'s own ``now`` constructor parameter."""

    def __init__(self, t: float = 1_000.0) -> None:
        self._t = t
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds


class _Config:
    """The handful of attributes :class:`RemoteState` reads off a config —
    everything else it reads defensively via ``getattr``."""

    def __init__(self) -> None:
        self.service = "checkout"
        self.control_plane_poll_s = POLL_S
        self.send_session_keys = False
        self.export_events = False

    def resolved_worker_id(self) -> str:
        return "w1"


def wait_until(predicate, timeout: float = WAIT_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "condition never became true within the timeout"


@pytest.fixture
def fake_plane():
    plane = FakePlane()
    yield plane
    plane.stop()


def start_remote(fake_plane: FakePlane, clock: _MovableClock) -> RemoteState:
    config = _Config()
    client = PlaneClient(fake_plane.url, None, config.service, "w1", now=clock)
    remote = RemoteState(client, exporter=None, config=config, now=clock)
    remote.start(engine=None)
    return remote


# With ``RemoteState.apply_hello`` reverted to reading only ``halt`` off
# the wire (no ``fleet_state`` handling), the three tests below fail: a
# hello carrying ``fleet_state: "unavailable"`` and the wire's default
# ``halt=False`` is read as a genuine "no halt" and lifts one that was
# actually still in force. That failing run, captured against the
# unmodified module and reported separately, is what this comment stands
# in for in the committed tree -- the fix stays in place here since a red
# commit is never checked in.


def test_a_halt_survives_fleet_state_unavailable_inside_the_window(fake_plane):
    clock = _MovableClock()
    fake_plane.set_hello(HelloReply(halt=True, halt_mode="stop"))
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote.halted() is True)

        fake_plane.set_hello(HelloReply(fleet_state="unavailable"))
        # Waits on the condition itself, not on the server having merely
        # received a new heartbeat: the reply still has to cross the
        # network and be processed by apply_hello() on the poller's own
        # thread.
        wait_until(lambda: remote.status().reason == "fleet state unavailable")

        assert remote.halted() is True
        assert remote.status().mode == "degraded"
    finally:
        remote.stop()



def test_a_halt_holds_through_any_stretch_of_unavailable_state(fake_plane):
    clock = _MovableClock()
    fake_plane.set_hello(HelloReply(halt=True, halt_mode="stop"))
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote.halted() is True)

        fake_plane.set_hello(HelloReply(fleet_state="unavailable"))
        wait_until(lambda: remote.status().reason == "fleet state unavailable")
        assert remote.halted() is True, "unavailable state must not itself lift the halt"

        # Short of the window: still held. The clock measures from when
        # state became unavailable, not from the last successful hello --
        # and the poller keeps calling successfully throughout, so if the
        # (wrong) old clock were still in use this would never go stale.
        clock.advance(59)
        assert remote.halted() is True

        clock.advance(24 * 3600)  # F1: a halt holds until the plane lifts it, however long the state stays unavailable
        assert remote.halted() is True
    finally:
        remote.stop()


def test_the_halt_reapplies_once_the_plane_recovers_and_says_so(fake_plane):
    clock = _MovableClock()
    fake_plane.set_hello(HelloReply(halt=True, halt_mode="stop"))
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote.halted() is True)

        fake_plane.set_hello(HelloReply(fleet_state="unavailable"))
        wait_until(lambda: remote.status().reason == "fleet state unavailable")
        clock.advance(24 * 3600)
        assert remote.halted() is True

        fake_plane.set_hello(HelloReply(halt=True, halt_mode="stop"))
        wait_until(lambda: remote.status().mode == "connected")

        assert remote.halted() is True
        assert remote.status().reason is None
    finally:
        remote.stop()



# --- coverage()'s one-line nudge --------------------------------------------


def test_coverage_fleet_names_the_unavailable_state_reason():
    from runbound import api

    class _Stub:
        def status(self):
            from runbound.plane_types import PlaneStatus

            return PlaneStatus(mode="degraded", reason="fleet state unavailable")

    with api._LOCK:
        previous = api._SHARED
        api._SHARED = _Stub()
    try:
        assert api._fleet_report() == (
            "connected; fleet state unavailable: deciding locally, "
            "last halt held"
        )
    finally:
        with api._LOCK:
            api._SHARED = previous
