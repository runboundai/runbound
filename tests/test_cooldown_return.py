"""A session the spike ladder stopped records its return when the cooldown is served.

The ladder closes a session (posture ``stopped``) and, on the key's next
entry, replaces it with a fresh one latched for ``spike_cooldown_seconds``.
Serving that cooldown lifts the latch: the key is answered again, on a session
that holds no posture. Before this, nothing said so, and whoever watched the
stopped posture (a control plane, a console) never heard it end.
"""

import logging

import pytest

import runbound
from runbound import api, ladder, local_events, state as state_module
from runbound.export import Exporter, change_to_wire
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import EntryDecision

import test_ladder_api as ladder_tests
from test_ladder_api import COOLDOWN, KEY, _plane, _uninitialized, clock  # noqa: F401

REASON = "cooldown served"


def _returns() -> list:
    """Every posture record that is a stopped session coming back."""
    return [r for r in runbound.events(500)
            if r["kind"] == "posture" and r.get("reason") == REASON]


def _postures() -> list:
    return [r for r in runbound.events(500) if r["kind"] == "posture"]


def _serve_the_cooldown(clock) -> None:
    ladder_tests.configure()
    ladder_tests.rolled_over()
    clock.advance(COOLDOWN + 1)


# --- C1: exactly once, only where a rollover cooldown is retired ---------------


def test_c1_serving_the_cooldown_records_one_return(clock):
    _serve_the_cooldown(clock)
    assert _returns() == []  # the door is shut: nothing has returned yet

    ladder_tests.turn(KEY)

    assert len(_returns()) == 1


def test_c1_the_return_is_not_recorded_again_on_later_entries(clock):
    _serve_the_cooldown(clock)

    for _ in range(4):
        ladder_tests.turn(KEY)

    assert len(_returns()) == 1


def test_c1_nothing_is_recorded_while_the_cooldown_is_still_running(clock):
    ladder_tests.configure()
    ladder_tests.rolled_over()
    clock.advance(COOLDOWN - 1)

    ladder_tests.refused()

    assert _returns() == []


def test_c1_an_ordinary_session_records_no_return(clock):
    ladder_tests.configure()

    for _ in range(3):
        ladder_tests.turn(KEY)

    assert _returns() == []


def test_c1_a_second_close_and_cooldown_records_a_second_return(clock):
    """One return per served cooldown, not one per key."""
    ladder_tests.configure(spike_max_strikes=3)
    ladder_tests.rolled_over()
    clock.advance(COOLDOWN + 1)
    ladder_tests.to_the_limit()
    ladder_tests.close_the_session()
    ladder_tests.refused()  # the second rollover
    clock.advance(COOLDOWN + 1)

    ladder_tests.turn(KEY)

    assert len(_returns()) == 2


# --- C2: what is recorded --------------------------------------------------------


def test_c2_the_return_says_from_stopped_to_the_real_posture_at_the_ladder_rung(clock):
    _serve_the_cooldown(clock)

    ladder_tests.turn(KEY)

    (record,) = _returns()
    assert record["from"] == "stopped"
    assert record["source"] == "ladder"
    assert record["scope"] == "session"
    assert record["key"] == KEY
    fresh = api._REGISTRY[KEY]
    assert record["level"] == ladder.LEVEL_NAMES[fresh.spike_level]
    assert record["session_id"] == fresh.session_id


# --- C3: the effective posture after the cooldown is full ---------------------------


def test_c3_the_session_holds_no_posture_after_the_cooldown_so_it_returns_to_full(clock):
    _serve_the_cooldown(clock)

    ladder_tests.turn(KEY)

    fresh = api._REGISTRY[KEY]
    assert fresh.posture is None
    (record,) = _returns()
    assert record["posture"] == "full"


def test_c3_the_recorded_posture_is_read_from_the_session_not_assumed(clock, monkeypatch):
    """`to` is what the session holds; were it ever narrower it would say so."""
    _serve_the_cooldown(clock)
    fresh = api._REGISTRY[KEY]
    fresh.posture = state_module.make_posture_state("restricted", "x", "ladder")

    ladder_tests.turn(KEY)

    (record,) = _returns()
    assert record["posture"] == "restricted"


# --- C4: the terminal block records nothing until clear() -------------------------


def test_c4_a_blocked_key_has_no_return_of_its_own(clock):
    """The first cooldown was served (one return); the block never is."""
    ladder_tests.configure()
    ladder_tests.rolled_over()
    clock.advance(COOLDOWN + 1)
    ladder_tests.turn(KEY)  # the first cooldown's return
    ladder_tests.to_the_limit()
    ladder_tests.close_the_session()
    ladder_tests.refused()  # blocked
    served = len(_postures())
    clock.advance(10_000.0)
    assert ladder_tests.refused().details["action"] == "blocked"

    assert len(_returns()) == 1
    assert len(_postures()) == served  # and nothing else moved while blocked


# --- C5: it travels like every other posture change ---------------------------------


def test_c5_the_return_is_an_ordinary_posture_record_on_the_wire(clock):
    _serve_the_cooldown(clock)
    ladder_tests.turn(KEY)
    (record,) = _returns()

    wire = change_to_wire(record)

    assert wire["kind"] == "posture_change"
    assert (wire["from"], wire["to"], wire["source"], wire["reason"]) == (
        "stopped", "full", "ladder", REASON)
    assert wire["level"] == record["level"]
    assert "key" not in wire and KEY not in repr(wire)  # only the hash travels


def test_c5_the_return_is_sent_even_with_events_export_off(clock):
    _serve_the_cooldown(clock)
    ladder_tests.turn(KEY)
    (record,) = _returns()

    class Spy:
        service, worker_id, key_state = "checkout", "host-1:1", "ok"

        def __init__(self) -> None:
            self.batches: list = []

        def events(self, batch: dict) -> bool:
            self.batches.append(batch)
            return True

    spy = Spy()
    exporter = Exporter(spy, include_events=False)
    exporter.on_change(record)
    exporter.flush()

    assert [c["reason"] for c in spy.batches[0]["changes"]] == [REASON]


# --- fail-open ---------------------------------------------------------------------


def test_a_recorder_that_raises_never_reaches_the_host_call(clock, monkeypatch, caplog):
    _serve_the_cooldown(clock)

    def boom(*args, **kwargs):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(local_events, "record_posture", boom)
    caplog.set_level(logging.WARNING, logger="runbound")

    ladder_tests.turn(KEY)  # served, no exception

    assert runbound.session_status(KEY)["tripped_by"] is None
    assert "could not record a posture change" in caplog.text


# --- only the ladder's own rollover cooldown is a return ---------------------------


def _latch(detector: str, ttl: float | None = 30.0, **details) -> EntryDecision:
    """A trip another worker made, as the plane relays it on entry, with the
    strikes the plane holds for the key alongside (they travel together)."""
    return EntryDecision(strikes=int(details.get("strikes", 0)), latch={
        "detector": detector, "severity": "critical", "message": "m",
        "details": details, "ttl_remaining_s": ttl,
    })


def _plane_says(decision: EntryDecision) -> None:
    """Change what the plane answers on entry; the decision cache is per key
    on the real clock, so a test that swaps the answer drops it."""
    ladder_tests._PLANE.decision = decision
    api._SHARED._cache.clear()


def test_d1_an_expired_remote_latch_is_not_a_return(clock):
    """A latch the fleet handed this worker (a budget wall here) is not a
    session this worker's ladder stopped: serving it out records nothing."""
    ladder_tests.configure()
    _plane_says(_latch("budget", limit_hit="budget_usd"))
    assert ladder_tests.refused().detector == "budget"

    _plane_says(EntryDecision())
    clock.advance(31)
    ladder_tests.turn(KEY)

    assert _returns() == []


ROLLOVER = dict(action="rollover", strikes=1, level=3)


def _relayed_rollover_served(clock, **overrides) -> None:
    """Another worker rolled the key over; this one is told, refuses it, and
    serves it once the cooldown has run out."""
    ladder_tests.configure()
    _plane_says(_latch("spike", **{**ROLLOVER, **overrides}))
    assert ladder_tests.refused().detector == "spike"
    _plane_says(EntryDecision())
    clock.advance(31)


def test_c1_a_relayed_rollover_records_exactly_one_return_once_admitted(clock):
    """The key's next request can land on any worker, so the worker that only
    heard of the rollover records the stop's return when it serves the key."""
    _relayed_rollover_served(clock)
    assert _returns() == []  # refused so far: nothing has returned

    ladder_tests.turn(KEY)

    (record,) = _returns()
    assert (record["from"], record["posture"], record["source"]) == ("stopped", "full", "ladder")
    assert record["key"] == KEY and record["scope"] == "session"


@pytest.mark.parametrize("detector, details", [
    ("budget", {"limit_hit": "budget_usd"}),
    ("loop", {"action": "trip"}),
    ("admin", {"by": "operator"}),
])
def test_c1_other_relayed_latches_are_silent(clock, detector, details):
    ladder_tests.configure()
    _plane_says(_latch(detector, **details))
    ladder_tests.refused()
    _plane_says(EntryDecision())
    clock.advance(31)

    ladder_tests.turn(KEY)

    assert _returns() == []


def test_c1_a_relayed_terminal_block_never_returns(clock):
    """The last strike's block has no expiry: it is refused for good, and
    nothing ever returns from it."""
    ladder_tests.configure()
    _plane_says(_latch("spike", ttl=None, action="blocked", strikes=2, level=4))
    assert ladder_tests.refused().details["action"] == "blocked"
    clock.advance(10_000)

    assert ladder_tests.refused().details["action"] == "blocked"
    assert _returns() == []


def test_c1_a_relayed_block_that_did_expire_is_still_not_a_return(clock):
    """Defensive: were a block ever relayed with an expiry, serving it out is
    not a stopped session's cooldown."""
    ladder_tests.configure()
    _plane_says(_latch("spike", ttl=30.0, action="blocked", strikes=2, level=4))
    ladder_tests.refused()
    _plane_says(EntryDecision())
    clock.advance(31)

    ladder_tests.turn(KEY)

    assert _returns() == []


# --- C2: at most once per rollover a worker held ------------------------------------------


def test_c2_many_admitted_calls_after_the_expiry_record_one_return(clock):
    _relayed_rollover_served(clock)

    for _ in range(6):
        ladder_tests.turn(KEY)

    assert len(_returns()) == 1


def test_c2_the_same_rollover_repeated_before_the_expiry_does_not_stack(clock):
    ladder_tests.configure()
    for _ in range(4):  # the plane repeats it on every entry while it runs
        _plane_says(_latch("spike", **ROLLOVER))
        ladder_tests.refused()
    _plane_says(EntryDecision())
    clock.advance(31)

    ladder_tests.turn(KEY)
    ladder_tests.turn(KEY)

    assert len(_returns()) == 1


def test_c2_a_stale_repeat_of_a_spent_rollover_does_not_re_arm_the_mark(clock):
    """The plane's decision cache can replay the old latch after the return
    was recorded; the key is refused again, but the same rollover is not
    recorded a second time."""
    _relayed_rollover_served(clock)
    ladder_tests.turn(KEY)
    assert len(_returns()) == 1

    _plane_says(_latch("spike", **ROLLOVER))
    ladder_tests.refused()
    _plane_says(EntryDecision())
    clock.advance(31)
    ladder_tests.turn(KEY)

    assert len(_returns()) == 1


def test_c2_a_later_rollover_of_the_same_key_is_its_own_return(clock):
    _relayed_rollover_served(clock)
    ladder_tests.turn(KEY)

    _plane_says(_latch("spike", action="rollover", strikes=2, level=3))
    ladder_tests.refused()
    _plane_says(EntryDecision())
    clock.advance(31)
    ladder_tests.turn(KEY)

    assert len(_returns()) == 2


# --- C3: clear() is its own path ----------------------------------------------------------


def test_c3_clearing_a_local_rollover_cooldown_leaves_no_ladder_return(clock):
    ladder_tests.configure()
    ladder_tests.rolled_over()

    runbound.clear(KEY)
    clock.advance(COOLDOWN + 1)
    ladder_tests.turn(KEY)

    assert _returns() == []


def test_c3_clearing_a_relayed_rollover_leaves_no_ladder_return(clock):
    ladder_tests.configure()
    _plane_says(_latch("spike", **ROLLOVER))
    ladder_tests.refused()

    runbound.clear(KEY)
    _plane_says(EntryDecision())
    clock.advance(31)
    ladder_tests.turn(KEY)

    assert _returns() == []


def test_c3_clear_spends_the_mark_even_on_a_session_still_held(clock):
    """A block already inside the session keeps a reference to the state clear
    discards; the mark must be gone from it too."""
    ladder_tests.configure()
    ladder_tests.rolled_over()
    held = api._REGISTRY[KEY]
    assert held.returns_from == "stopped"

    runbound.clear(KEY)

    assert held.returns_from is None


# --- C5: two workers ------------------------------------------------------------------------


def test_c5_a_key_closed_on_one_worker_and_admitted_on_another_returns_once(clock):
    """Two workers are two initialisations of the SDK in turn, each with its own
    plane double, sharing only what the wire carries. Worker A closes the key
    and reports its trip; the plane relays that trip to worker B as a latch
    carrying the ttl A reported; the key's next request lands on B, which
    records the return. What each worker recorded is turned into the wire
    shape the plane reads."""
    ladder_tests.configure()
    reported = []
    api._SHARED.trip = lambda key, state, anomaly, ttl, door: reported.append(
        (anomaly, ttl, door))
    ladder_tests.rolled_over()  # A: the rollover the ladder ordered
    (trip_anomaly, trip_ttl, _door), *_ = [t for t in reported if not t[2]]
    assert trip_anomaly.details["action"] == "rollover"
    assert trip_ttl == COOLDOWN  # what A tells the plane: the real cooldown, not forever
    stopped = [r for r in runbound.events(500)
               if r["kind"] == "posture" and r["posture"] == "stopped"]
    assert len(stopped) == 1
    assert _returns() == []  # A never sees the key again

    relayed = _latch(trip_anomaly.detector, ttl=trip_ttl, **trip_anomaly.details)
    api._teardown_for_tests()  # worker B starts with nothing
    ladder_tests.configure(worker_id="host-2:2")
    _plane_says(relayed)
    ladder_tests.refused()
    _plane_says(EntryDecision())
    clock.advance(trip_ttl + 1)
    ladder_tests.turn(KEY)
    ladder_tests.turn(KEY)

    returned = _returns()
    assert len(returned) == 1
    wire = [change_to_wire(r) for r in stopped + returned]
    assert [(w["from"], w["to"], w["reason"]) for w in wire[1:]] == [("stopped", "full", REASON)]
    assert wire[0]["key_hash"] == wire[1]["key_hash"]  # one key, two workers
