"""The SDK's side of the wall: what a worker does when its plan says no.

The control plane meters what an org is using and answers each heartbeat with
``entitlements`` — the plan, its limits, the codes it is currently *denying*
and a notice to show the customer. This file is about how a worker behaves
under each denial, and the shape of the answer is always the same one: the
plan can take away what the plane adds, and never what the SDK already does on
its own. Detection, latches, caps and policy keep running; the exits and the
trips keep flowing, so a fleet's totals stay right while it is over its plan.

Also here: ``control_plane_cache_s``, the knob that says how long one key's
entry answer is reused — and therefore how long a latch set on one worker
takes to reach a worker that already has an answer cached for that key.

No sockets and no sleeping: the plane is an in-memory fake and the clock is
one the test moves by hand.
"""

import threading

import pytest

from runbound.config import GuardrailConfig
from runbound.events import Anomaly
from runbound.plane_types import EntryDecision, ExitDelta, HelloReply, PlaneStatus
from runbound.shared import (
    DEGRADE_AFTER,
    NOTICE_DEBOUNCE_S,
    NOTICE_INTERVAL_S,
    LocalState,
    RemoteState,
)
from runbound.state import SessionState


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Plane:
    """The handful of client methods this file exercises, in memory."""

    service = "checkout"
    worker_id = "host-1:42"
    key_state = "ok"
    consecutive_failures = 0

    def __init__(self, decision: EntryDecision | None = None) -> None:
        self.decision = decision if decision is not None else EntryDecision()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _record(self, name: str) -> None:
        with self._lock:
            self.calls.append(name)

    def count(self, name: str) -> int:
        with self._lock:
            return self.calls.count(name)

    def enter(self, payload: dict):
        self._record("enter")
        return self.decision

    def trip(self, report) -> bool:
        self._record("trip")
        return True

    def clear(self, digest: str):
        self._record("clear")
        return 1

    def hello(self, payload: dict):
        self._record("hello")
        return None

    def events(self, batch: dict) -> bool:
        self._record("events")
        return True

    def policy(self, service: str):
        self._record("policy")
        return None


class Exporter:
    """The exporter's surface, recording instead of posting."""

    def __init__(self, include_events: bool = True) -> None:
        self.include_events = include_events
        self.anomalies: list = []
        self.trips: list = []
        self.exits: list = []

    def on_anomaly(self, session, anomaly, reacted) -> None:
        self.anomalies.append((anomaly, reacted))

    def on_trip(self, session, anomaly, reacted) -> None:
        self.trips.append((anomaly, reacted))

    def on_exit(self, delta) -> None:
        self.exits.append(delta)

    def on_circuit(self, label, state, failures, cooldown_s) -> None:
        pass


def config(**kwargs) -> GuardrailConfig:
    fields = {
        "control_plane_url": "https://plane.test",
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    fields.update(kwargs)
    return GuardrailConfig(**fields)


def remote(plane: Plane, clock: Clock, exporter=None, **kwargs) -> RemoteState:
    return RemoteState(plane, exporter, config(**kwargs), now=clock)


def state(key: str = "user-9") -> SessionState:
    return SessionState("sess-1", key=key, tags={"tier": "free"})


def hello(denied=None, notice=None, **entitlements) -> HelloReply:
    payload = {"plan": "team", "limits": {"workers_synced": 3}}
    if denied is not None:
        payload["denied"] = denied
    if notice is not None:
        payload["notice"] = notice
    payload.update(entitlements)
    return HelloReply(org_id="org_1", plan="team", entitlements=payload)


# --- control_plane_cache_s ---------------------------------------------------


def test_the_cache_window_defaults_to_five_seconds():
    assert GuardrailConfig().control_plane_cache_s == 5.0


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_a_non_positive_cache_window_is_refused(value):
    with pytest.raises(ValueError, match="control_plane_cache_s must be positive"):
        config(control_plane_cache_s=value).validate()


def test_one_key_costs_one_entry_question_inside_the_cache_window():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock, control_plane_cache_s=30.0)

    shared.enter("user-9", state(), config())
    clock.advance(29.0)
    shared.enter("user-9", state(), config())

    assert plane.count("enter") == 1


def test_the_plane_is_asked_again_once_the_cache_window_has_passed():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock, control_plane_cache_s=1.0)

    shared.enter("user-9", state(), config())
    clock.advance(1.5)
    shared.enter("user-9", state(), config())

    assert plane.count("enter") == 2


def test_the_fleet_status_ages_out_with_the_same_window():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock, control_plane_cache_s=1.0)
    shared.enter("user-9", state(), config())

    clock.advance(0.5)
    assert shared.fleet_status("user-9") is not None
    clock.advance(1.0)
    assert shared.fleet_status("user-9") is None


# --- entitlements: the telemetry lanes ---------------------------------------


@pytest.mark.parametrize("code", ["events_denied", "events_over_cap"])
def test_a_denied_events_code_closes_the_telemetry_lanes(code):
    clock, plane, exporter = Clock(), Plane(), Exporter()
    shared = remote(plane, clock, exporter)

    shared.apply_hello(hello(denied=[code]))

    assert exporter.include_events is False
    assert shared.observers() == []


def test_lifting_the_denial_opens_the_lanes_again():
    clock, plane, exporter = Clock(), Plane(), Exporter()
    shared = remote(plane, clock, exporter)

    shared.apply_hello(hello(denied=["events_over_cap"]))
    shared.apply_hello(hello(denied=[]))

    assert exporter.include_events is True
    assert shared.observers() == [exporter]


def test_telemetry_the_customer_switched_off_stays_off():
    """The plan may take export away; lifting a denial may not hand it back."""
    clock, plane, exporter = Clock(), Plane(), Exporter(include_events=False)
    shared = remote(plane, clock, exporter, export_events=False)

    shared.apply_hello(hello(denied=["events_denied"]))
    shared.apply_hello(hello(denied=[]))

    assert exporter.include_events is False


def test_an_unknown_denial_code_changes_nothing():
    clock, plane, exporter = Clock(), Plane(), Exporter()
    shared = remote(plane, clock, exporter)

    shared.apply_hello(hello(denied=["some_code_from_next_year"]))

    assert exporter.include_events is True
    assert shared.limited is False


# --- entitlements: limited mode ----------------------------------------------


def test_too_many_synced_workers_puts_this_one_in_limited_mode():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)

    shared.apply_hello(hello(denied=["workers_synced_exceeded"]))

    assert shared.limited is True
    assert shared.status().mode == "limited"


def test_limited_mode_answers_session_entry_locally():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    shared.apply_hello(hello(denied=["workers_synced_exceeded"]))

    for _ in range(5):
        assert shared.enter(f"user-{_}", state(), config()) is None

    assert plane.count("enter") == 0


def test_limited_mode_keeps_the_heartbeat_and_the_trips_and_the_exits():
    clock, plane, exporter = Clock(), Plane(), Exporter()
    shared = remote(plane, clock, exporter)
    shared.apply_hello(hello(denied=["workers_synced_exceeded"]))

    session = state()
    anomaly = Anomaly(detector="budget", severity="critical", message="over", details={})
    shared.trip("user-9", session, anomaly, None, door=False)
    shared.exit("user-9", session, ExitDelta(key_hash="h", seq=1, spend_delta_usd=0.5))
    shared.clear("user-9")

    assert plane.count("trip") == 1
    assert plane.count("clear") == 1
    assert exporter.exits[0].spend_delta_usd == 0.5
    assert exporter.include_events is True


def test_a_decision_already_cached_is_still_served_while_limited():
    """The plane raises the floor of what we know; a limit may not lower it."""
    clock, plane = Clock(), Plane(EntryDecision(allow=False, refusal={"reason": "latched"}))
    shared = remote(plane, clock)
    assert shared.enter("user-9", state(), config()) is not None

    shared.apply_hello(hello(denied=["workers_synced_exceeded"]))
    decision = shared.enter("user-9", state(), config())

    assert decision is not None and decision.allow is False
    assert plane.count("enter") == 1


def test_lifting_the_worker_denial_starts_the_entry_questions_again():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    shared.apply_hello(hello(denied=["workers_synced_exceeded"]))
    shared.enter("user-9", state(), config())

    shared.apply_hello(hello(denied=[]))
    shared.enter("user-9", state(), config())

    assert shared.limited is False
    assert plane.count("enter") == 1
    assert shared.status().mode == "connected"


def test_a_degraded_link_is_reported_ahead_of_a_limited_plan():
    """Both are true; the one that means "we stopped calling at all" wins."""
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    shared.apply_hello(hello(denied=["workers_synced_exceeded"]))
    plane.consecutive_failures = DEGRADE_AFTER

    assert shared.status().mode == "degraded"
    assert shared.limited is True


# --- the notice --------------------------------------------------------------


def test_the_notice_is_logged_once_an_hour(caplog):
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    reply = hello(denied=["events_over_cap"], notice="you are over your events cap")

    with caplog.at_level("WARNING", logger="runbound"):
        for _ in range(10):
            shared.apply_hello(reply)
            clock.advance(5.0)
        first = caplog.text.count("over your events cap")

        clock.advance(NOTICE_INTERVAL_S)
        shared.apply_hello(reply)
        second = caplog.text.count("over your events cap")

    assert first == 1
    assert second == 2


def test_nothing_is_logged_while_the_plan_is_denying_nothing(caplog):
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(hello(denied=[], notice="your trial ends on Friday"))

    assert "your trial ends" not in caplog.text
    assert shared.status().notice == "your trial ends on Friday"


def test_the_entitlements_notice_wins_over_the_replys_own():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    reply = HelloReply(
        entitlements={"denied": [], "notice": "plan notice"}, notice="generic notice"
    )

    shared.apply_hello(reply)

    assert shared.status().notice == "plan notice"


# --- the notice's debounce (T115: a rolling-deploy blip must not nag) -------
#
# The free plan syncs one worker. A rolling deploy overlaps two heartbeats
# for up to 15 seconds (the plane's HEARTBEAT_TTL_S), so an in-plan customer
# briefly shows as two live workers and gets denied `workers_synced_exceeded`
# on both. `limited` and the telemetry lanes still flip on that very first
# reply, silently — a customer genuinely over their limit must be limited
# right away. Only writing the notice to the log waits to see whether it
# outlives one deploy blip: `NOTICE_DEBOUNCE_S` (30s, twice the plane's TTL)
# of continuous presence before the existing `_log_notice` path is allowed to
# run at all.


def test_limited_flips_on_the_first_reply_but_the_notice_is_not_logged_yet(caplog):
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    reply = hello(denied=["workers_synced_exceeded"], notice="you're over your worker limit")

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(reply)

    assert shared.limited is True
    assert shared.status().entitlements["notice"] == "you're over your worker limit"
    assert "over your worker limit" not in caplog.text


def test_a_deploy_blip_under_the_debounce_window_never_logs(caplog):
    """The motivating case: two heartbeats overlap for ~15s, well under the

    30s debounce, and the notice clears on the very next poll once the
    deploy settles. It must never reach the log.
    """
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(
            hello(denied=["workers_synced_exceeded"], notice="over your worker limit")
        )
        clock.advance(15.0)
        shared.apply_hello(hello(denied=[]))

    assert "over your worker limit" not in caplog.text


def test_a_notice_sustained_across_the_debounce_window_logs_exactly_once_then_the_hourly_cadence_resumes(
    caplog,
):
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    reply = hello(denied=["workers_synced_exceeded"], notice="over your worker limit")

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(reply)
        clock.advance(NOTICE_DEBOUNCE_S - 1.0)
        shared.apply_hello(reply)
        assert "over your worker limit" not in caplog.text

        clock.advance(1.0)
        shared.apply_hello(reply)
        assert caplog.text.count("over your worker limit") == 1

        clock.advance(5.0)
        shared.apply_hello(reply)
        assert caplog.text.count("over your worker limit") == 1

        clock.advance(NOTICE_INTERVAL_S)
        shared.apply_hello(reply)
        assert caplog.text.count("over your worker limit") == 2


def test_the_notices_text_changing_does_not_restart_the_debounce_timer(caplog):
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(hello(denied=["workers_synced_exceeded"], notice="first wording"))
        clock.advance(20.0)
        shared.apply_hello(hello(denied=["workers_synced_exceeded"], notice="second wording"))
        assert caplog.text == ""

        clock.advance(15.0)  # 35s continuously present, even though the text changed
        shared.apply_hello(hello(denied=["workers_synced_exceeded"], notice="second wording"))

    assert "second wording" in caplog.text


def test_a_reply_with_no_notice_resets_the_debounce_timer(caplog):
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(hello(denied=["workers_synced_exceeded"], notice="over limit"))
        clock.advance(25.0)
        shared.apply_hello(hello(denied=[]))  # notice gone: the clock resets
        clock.advance(10.0)  # only 10s since the notice reappeared
        shared.apply_hello(hello(denied=["workers_synced_exceeded"], notice="over limit"))

    assert "over limit" not in caplog.text


def test_a_clock_running_backwards_never_manufactures_elapsed_notice_time(caplog):
    """A naive ``abs(now - since)`` would treat a large backward jump as a

    long presence and log immediately; the spec requires elapsed to floor at
    zero instead.
    """
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    reply = hello(denied=["workers_synced_exceeded"], notice="over limit")

    with caplog.at_level("WARNING", logger="runbound"):
        shared.apply_hello(reply)
        clock.advance(40.0)  # would ordinarily be well past the debounce
        clock.value = 1.0  # the clock itself goes backwards
        shared.apply_hello(reply)

    assert "over limit" not in caplog.text


# --- what plane_status() reports ---------------------------------------------


def test_the_status_carries_the_whole_entitlements_dict():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    shared.apply_hello(hello(denied=["events_over_cap"], notice="upgrade"))

    status = shared.status()

    assert status.entitlements == {
        "plan": "team",
        "limits": {"workers_synced": 3},
        "denied": ["events_over_cap"],
        "notice": "upgrade",
    }
    assert status.notice == "upgrade"
    assert status.mode == "connected"


def test_the_status_of_a_worker_with_no_plane_says_nothing_about_a_plan():
    assert LocalState().status() == PlaneStatus(mode="local")
    assert LocalState().status().entitlements == {}


def test_entitlements_start_empty_and_are_a_copy():
    clock, plane = Clock(), Plane()
    shared = remote(plane, clock)
    assert shared.status().entitlements == {}

    shared.apply_hello(hello(denied=[]))
    shared.status().entitlements["denied"] = ["tampered"]

    assert shared.status().entitlements["denied"] == []


# --- fail-open ---------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        HelloReply(entitlements={"denied": "not-a-list"}),
        HelloReply(entitlements={"denied": [None, 7]}),
        HelloReply(),
        "not a reply at all",
    ],
)
def test_a_reply_the_sdk_cannot_read_leaves_the_worker_alone(reply, caplog):
    clock, plane, exporter = Clock(), Plane(), Exporter()
    shared = remote(plane, clock, exporter)

    with caplog.at_level("WARNING", logger="runbound"):
        assert shared.apply_hello(reply) is None

    assert shared.limited is False
    assert exporter.include_events is True


def test_an_exporter_that_refuses_the_flag_does_not_break_the_heartbeat(caplog):
    class Stubborn:
        """An observer whose ``include_events`` cannot be assigned to."""

        @property
        def include_events(self) -> bool:
            return True

    clock, plane = Clock(), Plane()
    shared = remote(plane, clock, Stubborn())

    with caplog.at_level("WARNING", logger="runbound"):
        assert shared.apply_hello(hello(denied=["events_denied"])) is None

    assert shared.status().mode == "connected"
