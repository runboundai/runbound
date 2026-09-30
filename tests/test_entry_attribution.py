"""The entry window records *why* each local decision was made, and
``plane_status().reason`` names the majority cause instead of always saying
"entry timeouts".

From a real state-outage run: for up to a minute after the outage, the entry
window held "local" outcomes with no cause attached, so the third rule in
``RemoteState.status()`` always printed ``"entry timeouts"`` — including for
entries that were really 503s from lost fleet state.

Same two traps ``test_fleet_state_unavailable.py`` documents, and the same
fix: a real background :class:`~runbound.plane.Poller` thread, talking to a
real ``http.server.ThreadingHTTPServer`` over a real socket, with every
reply built by ``plane_types.to_wire`` — never a direct ``apply_hello()`` or
``heartbeat()`` call, and never a hand-written dict standing in for the real
wire encoding. ``/v1/enter`` is driven directly from the test thread (the
production call a ``session()`` block makes), which is a different thing
from bypassing the heartbeat: this is the call under test.

The entry window itself uses a movable fake clock (``RemoteState``'s own
``now`` constructor parameter, as every other test in this area does) so its
minute-long window can be pruned or left alone precisely without a real
minute of sleep — independent of the real poller thread, which still runs on
wall-clock ``poll_s`` ticks underneath it.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from typing import Any

import pytest

from runbound.plane import PlaneClient
from runbound.plane_types import EntryDecision, HelloReply, to_wire
from runbound.shared import ENTRY_WINDOW_MIN_ENTRIES, RemoteState

POLL_S = 0.02
WAIT_TIMEOUT_S = 5.0

#: Longer than the client's own ``timeout_s`` below, so a "slow" ``/v1/enter``
#: genuinely trips the client's socket timeout rather than merely being a
#: slow-but-successful reply.
SLOW_S = 0.3
CLIENT_TIMEOUT_S = 0.05


class _FakePlaneState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hello = HelloReply()
        #: ``"ok"`` (answers ``enter_decision``), ``"plane_loss"`` (503 with
        #: the store-down body — ``"cause": "fleet_state_unavailable"``,
        #: a genuine state outage's real shape), ``"plane_unavailable"`` (503 with the
        #: *plain* body ``app.FailOpenMiddleware`` answers any other
        #: unhandled SDK-path failure with — no ``cause`` at all: a
        #: saturated pool, a handler bug — the exact shape that used to be
        #: indistinguishable from ``"plane_loss"`` and is the mislabel this
        #: task closes), ``"slow"`` (sleeps past the client's own timeout
        #: before answering ``"ok"`` — a genuine timeout, not a canned
        #: failure), or ``"http_error"`` (a plain 500).
        self.enter_mode = "ok"
        self.enter_decision = EntryDecision()


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
                reply = state.hello
            self._send_json(200, to_wire(reply))
            return
        if self.path == "/v1/enter":
            with state.lock:
                mode = state.enter_mode
                decision = state.enter_decision
            if mode == "plane_loss":
                self._send_json(
                    503, {"error": "plane_unavailable", "cause": "fleet_state_unavailable"}
                )
            elif mode == "plane_unavailable":
                self._send_json(503, {"error": "plane_unavailable"})
            elif mode == "http_error":
                self._send_json(500, {"error": "internal"})
            elif mode == "slow":
                time.sleep(SLOW_S)
                self._send_json(200, to_wire(decision))
            else:
                self._send_json(200, to_wire(decision))
            return
        self._send_json(404, {"error": "not_found"})


class FakePlane:
    """A real HTTP server, on its own thread — see the module docstring."""

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

    def set_enter_mode(self, mode: str, decision: EntryDecision | None = None) -> None:
        with self.state.lock:
            self.state.enter_mode = mode
            if decision is not None:
                self.state.enter_decision = decision

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class _MovableClock:
    """A monotonic clock this test advances by hand — the entry window's
    own minute is proven without a real minute of sleep."""

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
    def __init__(self) -> None:
        self.service = "checkout"
        self.budget_usd = None
        self.stale_halt = "release"
        self.on_plane_loss = "guard_locally"
        self.control_plane_poll_s = POLL_S
        self.control_plane_cache_s = 0.0  # every entry is a fresh call, never a cache hit
        self.send_session_keys = False
        self.circuit_fleet = False
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
    client = PlaneClient(
        fake_plane.url, None, config.service, "w1", timeout_s=CLIENT_TIMEOUT_S, now=clock
    )
    remote = RemoteState(client, exporter=None, config=config, now=clock)
    remote.start(engine=None)
    return remote


class _State:
    """A minimal session-state double: only ``enter``'s own reads matter."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.total_cost_usd = 0.0
        self.total_tokens = 0
        self.tags: dict = {}


def enter_many(remote: RemoteState, n: int, prefix: str) -> None:
    """Drive ``n`` real ``enter()`` calls, spaced out past ``POLL_S``.

    Back-to-back calls with no gap can outrun the background poller: with a
    frozen fake clock, three real entry failures in a row (the link's own
    ``DEGRADE_AFTER``) would make ``_may_call()`` start skipping the network
    entirely, reusing the previous call's cause forever rather than letting
    each call hit the fake plane's *current* canned response. A real
    heartbeat succeeding between calls is exactly what keeps that gate open
    in production (the consecutive-failure count keeps getting reset by a
    heartbeat that has nothing to do with the entry path — the whole reason
    the entry window exists in the first place) and is what this small
    sleep gives the real poller thread room to do.
    """
    for i in range(n):
        remote.enter(f"{prefix}-{i}", _State(), remote._config)
        time.sleep(POLL_S * 2)


# --- (a0) the mislabel's own mechanism, proven without real threads ---------


class _RacyClient:
    """A minimal duck-typed stand-in for
    :class:`~runbound.plane.PlaneClient`, used only to pin down the race in
    isolation -- deterministically, with no real threads or sockets. The
    fix itself is proven the way every other test in this file is: against
    the real threaded fake plane, below.

    Mimics the shape a state outage's own run exposed: ``last_failure_kind``
    is whole-client state (see ``test_plane_client.py``), so a concurrent
    success elsewhere on the same client -- the heartbeat, always, in
    production -- can clear it between this call's own failure and whatever
    reads it back afterward. Here that concurrent clobber is simulated
    directly: ``enter_with_kind`` reports this call's real, correct answer
    and then immediately clears ``last_failure_kind``, exactly as a
    heartbeat success interleaving right after would.
    """

    def __init__(self, kind: str) -> None:
        self.last_failure_kind: str | None = None
        self.consecutive_failures = 0
        self.key_state = "valid"
        self._kind = kind

    def enter_with_kind(self, payload: dict):
        self.last_failure_kind = None  # the concurrent clobber
        return None, self._kind

    def enter(self, payload: dict):
        decision, _ = self.enter_with_kind(payload)
        return decision


def test_the_entry_windows_cause_is_not_clobbered_by_a_concurrent_success():
    """Deterministic proof of the mechanism behind the flaky failure this
    fix closes: reading ``last_failure_kind`` back off the client after the
    call is exactly what a concurrent heartbeat success can already have
    cleared, mislabeling a plane-loss 503 as a timeout. The entry window
    must record this call's own answer, not whatever the shared attribute
    reads afterward.
    """
    clock = _MovableClock()
    config = _Config()
    remote = RemoteState(_RacyClient("plane_loss"), exporter=None, config=config, now=clock)

    remote.enter("k", _State(), config)

    counts = remote._entry_window_counts()
    assert counts["local_causes"]["plane_loss"] == 1
    assert counts["local_causes"]["timeout"] == 0


# --- (a) a state outage, entry-door only, then recovery ---------------------


def test_plane_loss_503s_read_fleet_state_unavailable_and_clear_on_recovery(fake_plane):
    """The heartbeat never fails here — only ``/v1/enter`` does, with the
    503 plane-loss body — so this isolates the entry-window path from the
    hello-based ``fleet_state`` signal, which already has its own tests in
    ``test_fleet_state_unavailable.py``.
    """
    clock = _MovableClock()
    fake_plane.set_enter_mode("plane_loss")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)  # a hello has landed

        # From the first window that qualifies (ENTRY_WINDOW_MIN_ENTRIES)
        # to well past it, the reason is the real cause, never the old
        # hardcoded "entry timeouts".
        for round_ in range(3):
            enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES, f"outage-{round_}")
            status = remote.status()
            assert status.mode == "degraded"
            assert status.reason == "fleet state unavailable"
            assert status.reason != "entry timeouts"
            assert status.entries_window["local_causes"]["plane_loss"] > 0

        # Recovery: /v1/enter starts answering normally. A handful of good
        # answers dilutes the window well past the degrade threshold, inside
        # one poll interval's worth of wall-clock time.
        fake_plane.set_enter_mode("ok", EntryDecision())
        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES * 3, "recovered")

        recovered = remote.status()
        assert recovered.mode == "connected"
        assert recovered.reason is None
    finally:
        remote.stop()


# --- (a2) recovery from a state outage is immediate, not a minute of history


def test_one_answer_after_a_state_outage_clears_the_reason(fake_plane):
    """The realistic recovery shape: a worker that spent the outage deciding
    thirty entries locally gets ONE fresh answer from the plane, not a flood
    of them. A fresh answer to ``/v1/enter`` proves the plane's state is back
    at that moment, so the outage's own local decisions stop being evidence
    that anything is wrong now. Without this, the reason kept reading "fleet
    state unavailable" for most of a minute after the state had come back,
    until enough good answers outnumbered the outage's history.
    """
    clock = _MovableClock()
    fake_plane.set_enter_mode("plane_loss")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)
        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES * 3, "outage")
        assert remote.status().reason == "fleet state unavailable"

        fake_plane.set_enter_mode("ok", EntryDecision())
        enter_many(remote, 1, "first-answer")

        recovered = remote.status()
        assert recovered.mode == "connected"
        assert recovered.reason is None
        # The window still reports what happened -- it is history, not health.
        assert recovered.entries_window["local"] == ENTRY_WINDOW_MIN_ENTRIES * 3
        assert recovered.entries_window["local_causes"]["plane_loss"] == ENTRY_WINDOW_MIN_ENTRIES * 3
        assert recovered.entries_local_share > 0.5
    finally:
        remote.stop()


def test_a_hello_saying_state_is_back_clears_the_reason_without_new_entries(fake_plane):
    """No entries need to flow for recovery to show: the heartbeat that says
    the fleet state is available again supersedes the outage's local
    decisions just as a fresh entry answer does."""
    clock = _MovableClock()
    fake_plane.set_enter_mode("plane_loss")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)
        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES * 2, "outage")

        with fake_plane.state.lock:
            fake_plane.state.hello = HelloReply(fleet_state="unavailable")
        wait_until(lambda: remote.status().reason == "fleet state unavailable"
                   and remote._fleet_state_unavailable)
        with fake_plane.state.lock:
            fake_plane.state.hello = HelloReply()
        wait_until(lambda: not remote._fleet_state_unavailable)

        recovered = remote.status()
        assert recovered.mode == "connected"
        assert recovered.reason is None
    finally:
        remote.stop()


def test_one_answer_does_not_clear_genuine_timeouts(fake_plane):
    """Only a state outage is superseded by the plane answering again.
    Timeouts keep the window's one-minute smoothing: a plane that answers
    one entry in eleven is still failing the hot path."""
    clock = _MovableClock()
    fake_plane.set_enter_mode("slow")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)
        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES, "slow")

        fake_plane.set_enter_mode("ok", EntryDecision())
        enter_many(remote, 1, "one-answer")

        status = remote.status()
        assert status.mode == "degraded"
        assert status.reason == "entry timeouts"
    finally:
        remote.stop()


# --- (b) genuine timeouts still read "entry timeouts" -----------------------


def test_genuine_timeouts_still_read_entry_timeouts(fake_plane):
    """``/v1/enter`` is answered, just too slowly — a real client-side
    socket timeout (``TimeoutError``), not a canned failure — and the
    heartbeat keeps succeeding throughout."""
    clock = _MovableClock()
    fake_plane.set_enter_mode("slow")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)

        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES, "slow")

        status = remote.status()
        assert status.mode == "degraded"
        assert status.reason == "entry timeouts"
        assert status.entries_window["local_causes"]["timeout"] >= ENTRY_WINDOW_MIN_ENTRIES
    finally:
        remote.stop()


# --- (c) a mix: the majority cause wins, ties break deterministically -------


def test_a_mixed_window_reads_the_majority_cause(fake_plane):
    clock = _MovableClock()
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)

        # 6 timeouts, 3 plane-loss 503s, 1 plain error: timeout is the
        # strict majority.
        fake_plane.set_enter_mode("slow")
        enter_many(remote, 6, "slow")
        fake_plane.set_enter_mode("plane_loss")
        enter_many(remote, 3, "loss")
        fake_plane.set_enter_mode("http_error")
        enter_many(remote, 1, "err")

        status = remote.status()
        assert status.entries_window["local_causes"] == {
            "timeout": 6, "plane_loss": 3, "plane_unavailable": 0, "error": 1,
        }
        assert status.mode == "degraded"
        assert status.reason == "entry timeouts"
    finally:
        remote.stop()


def test_a_tied_window_breaks_toward_plane_loss(fake_plane):
    """Documented tie-break (``LOCAL_CAUSE_TIE_BREAK``): ``plane_loss`` beats
    an equally-sized group of ``timeout``."""
    clock = _MovableClock()
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)

        fake_plane.set_enter_mode("slow")
        enter_many(remote, 4, "slow")
        fake_plane.set_enter_mode("plane_loss")
        enter_many(remote, 4, "loss")
        fake_plane.set_enter_mode("http_error")
        enter_many(remote, 2, "err")

        status = remote.status()
        assert status.entries_window["local_causes"] == {
            "timeout": 4, "plane_loss": 4, "plane_unavailable": 0, "error": 2,
        }
        assert status.mode == "degraded"
        assert status.reason == "fleet state unavailable"
    finally:
        remote.stop()


# --- (e) the mislabel this task exists to remove: plane_loss is not any
# --- 503 shaped like it ------------------------------------------------


def test_a_plain_middleware_body_never_reads_fleet_state_unavailable(fake_plane):
    """The exact regression a reviewer's probe caught: ``app.
    FailOpenMiddleware`` answers *any* unhandled SDK-path failure (a
    saturated connection pool, a handler bug, a failed batch write) with
    the same ``{"error": "plane_unavailable"}`` shape a genuine state
    outage uses — no ``cause`` field. A plane in that state (Redis
    perfectly healthy) must never tell the operator its fleet state is
    gone."""
    clock = _MovableClock()
    fake_plane.set_enter_mode("plane_unavailable")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)

        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES, "unavailable")

        status = remote.status()
        assert status.mode == "degraded"
        assert status.reason == "plane unavailable"
        assert status.reason != "fleet state unavailable"
        assert status.entries_window["local_causes"]["plane_unavailable"] > 0
        assert status.entries_window["local_causes"]["plane_loss"] == 0
    finally:
        remote.stop()


def test_a_store_down_body_still_reads_fleet_state_unavailable(fake_plane):
    """The other half of the same proof: the *specific* store-down body
    (a genuine outage's real shape, ``cause: fleet_state_unavailable``) still reads
    the specific reason."""
    clock = _MovableClock()
    fake_plane.set_enter_mode("plane_loss")
    remote = start_remote(fake_plane, clock)
    try:
        wait_until(lambda: remote._client.consecutive_failures == 0)

        enter_many(remote, ENTRY_WINDOW_MIN_ENTRIES, "loss")

        status = remote.status()
        assert status.mode == "degraded"
        assert status.reason == "fleet state unavailable"
    finally:
        remote.stop()
