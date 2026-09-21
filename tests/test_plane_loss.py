"""Tests for T61: ``stale_halt`` and ``on_plane_loss`` in :mod:`runbound.shared`.

Two independent knobs, both about what a worker does when the control plane
goes quiet:

* ``stale_halt`` says how long a fleet-wide halt is still enforced once the
  link that raised it has gone degraded — ``"release"`` (today's 60 s
  behavior) or ``"hold"`` (until a heartbeat says otherwise, however long
  that takes).
* ``on_plane_loss`` says what :meth:`~runbound.shared.RemoteState.enter`
  answers when it could not ask the plane, or the plane did not answer —
  ``"guard_locally"`` (today's ``None``, decide on this worker's own numbers)
  or ``"refuse"`` (a refusal decision, so the caller stops the session at the
  door instead of guessing alone).

No sockets and no sleeping: the plane is an in-memory double and the clock is
one the test moves by hand, in the same style as ``test_shared_state.py`` and
``test_wall_sdk.py``.

``runbound.config.GuardrailConfig`` may not yet validate ``stale_halt`` /
``on_plane_loss`` (T59 adds that concurrently) — :func:`config` below sets
them by plain attribute assignment after construction, which works whether or
not those fields exist yet, and :mod:`runbound.shared` reads them with
``getattr(..., default)`` for the same reason.
"""

from runbound.config import GuardrailConfig
from runbound.plane_types import EntryDecision, HelloReply, PlaneStatus
from runbound.shared import DEGRADE_AFTER, STALE_HALT_S, RemoteState
from runbound.state import SessionState


class MovableClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Plane:
    """A minimal control-plane double covering the three ways it can fail.

    ``mode`` controls :meth:`enter`: ``"ok"`` answers ``decision``,
    ``"timeout"`` answers ``None`` — a client that gave up quietly, the shape
    a real timeout takes — and ``"error"`` raises, a client that could not
    even complete the attempt. ``consecutive_failures`` lets a test degrade
    the link the way a failing heartbeat poller would, without driving
    ``DEGRADE_AFTER`` failed entries through here first.
    """

    service = "checkout"
    worker_id = "host-1:42"
    key_state = "unknown"

    def __init__(self, decision: EntryDecision | None = None, mode: str = "ok") -> None:
        self.decision = decision if decision is not None else EntryDecision()
        self.mode = mode
        self.consecutive_failures = 0
        self.calls: list[str] = []

    def enter(self, payload: dict):
        self.calls.append("enter")
        if self.mode == "timeout":
            return None
        if self.mode == "error":
            raise RuntimeError("the plane errored")
        return self.decision

    def count(self, name: str) -> int:
        return self.calls.count(name)

    def trip(self, report) -> bool:  # pragma: no cover - unused here
        return True

    def clear(self, digest: str):  # pragma: no cover - unused here
        return 1

    def policy(self, service: str):  # pragma: no cover - unused here
        return None


def config(**kwargs) -> GuardrailConfig:
    """A real ``GuardrailConfig``, with the two new fields set defensively.

    Setting them by attribute after construction (rather than as constructor
    keywords) means this file does not care whether T59 has already added
    them as declared, validated fields or not.
    """
    stale_halt = kwargs.pop("stale_halt", None)
    on_plane_loss = kwargs.pop("on_plane_loss", None)
    fields = {
        "control_plane_url": "https://plane.example",
        "token": "k",
        "service": "checkout",
        "worker_id": "host-1:42",
    }
    fields.update(kwargs)
    cfg = GuardrailConfig(**fields)
    if stale_halt is not None:
        cfg.stale_halt = stale_halt
    if on_plane_loss is not None:
        cfg.on_plane_loss = on_plane_loss
    return cfg


def remote(plane: Plane, clock: MovableClock, **kwargs) -> RemoteState:
    return RemoteState(plane, None, config(**kwargs), now=clock)


def state(key: str = "user-9") -> SessionState:
    return SessionState("sess-1", key=key, tags={})


# --- stale_halt: "release" (default, today's 60 s behavior) ----------------


def test_stale_halt_release_is_the_default():
    clock = MovableClock()
    shared = remote(Plane(), clock)
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S + 1)

    assert shared.halted() is False


def test_stale_halt_release_drops_the_halt_after_the_window():
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="release")
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S - 1)
    assert shared.halted() is True

    clock.advance(2)
    assert shared.halted() is False


# --- stale_halt: "hold" ------------------------------------------------------


def test_stale_halt_hold_keeps_the_halt_well_past_the_window():
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="hold")
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S * 10)

    assert shared.halted() is True


def test_stale_halt_hold_is_lifted_only_by_a_heartbeat_saying_so():
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="hold")
    shared.apply_hello(HelloReply(halt=True))
    clock.advance(STALE_HALT_S * 10)
    assert shared.halted() is True

    shared.apply_hello(HelloReply(halt=False))

    assert shared.halted() is False


def test_stale_halt_hold_still_lifts_normally_when_the_plane_is_healthy():
    """Hold changes what happens while the link is degraded, nothing else."""
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="hold")
    shared.apply_hello(HelloReply(halt=True))

    shared.apply_hello(HelloReply(halt=False))

    assert shared.halted() is False


def test_an_unrecognized_stale_halt_value_behaves_like_release():
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="bogus")
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S + 1)

    assert shared.halted() is False


# --- halt_stale_s on PlaneStatus ---------------------------------------------


def test_halt_stale_s_is_none_with_no_halt():
    clock = MovableClock()
    shared = remote(Plane(), clock)

    assert shared.status().halt_stale_s is None


def test_halt_stale_s_counts_seconds_since_last_contact_while_enforced():
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="hold")
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(90.0)

    status = shared.status()
    assert status.halt_stale_s == 90.0
    assert status.halt_stale_s == status.last_contact_age_s


def test_halt_stale_s_goes_back_to_none_once_release_lets_the_halt_lapse():
    clock = MovableClock()
    shared = remote(Plane(), clock, stale_halt="release")
    shared.apply_hello(HelloReply(halt=True))

    clock.advance(STALE_HALT_S + 1)

    assert shared.status().halt_stale_s is None


def test_plane_status_default_construction_is_unchanged():
    assert PlaneStatus() == PlaneStatus(
        mode="local",
        last_contact_age_s=None,
        consecutive_failures=0,
        notice=None,
        entitlements={},
        halt_stale_s=None,
    )


# --- on_plane_loss: "guard_locally" (default) -------------------------------


def test_guard_locally_on_a_degraded_link_answers_none():
    clock = MovableClock()
    plane = Plane()
    plane.consecutive_failures = DEGRADE_AFTER
    shared = remote(plane, clock, on_plane_loss="guard_locally")

    decision = shared.enter("user-9", state(), shared._config)

    assert decision is None
    assert plane.count("enter") == 0


def test_guard_locally_on_a_timeout_answers_none():
    clock = MovableClock()
    plane = Plane(mode="timeout")
    shared = remote(plane, clock, on_plane_loss="guard_locally")

    decision = shared.enter("user-9", state(), shared._config)

    assert decision is None


def test_guard_locally_on_an_error_answers_none():
    clock = MovableClock()
    plane = Plane(mode="error")
    shared = remote(plane, clock, on_plane_loss="guard_locally")

    decision = shared.enter("user-9", state(), shared._config)

    assert decision is None


def test_guard_locally_is_the_default_when_unset():
    clock = MovableClock()
    plane = Plane(mode="error")
    shared = remote(plane, clock)  # no on_plane_loss kwarg at all

    assert shared.enter("user-9", state(), shared._config) is None


# --- on_plane_loss: "refuse" -------------------------------------------------


def _assert_plane_refusal(decision, mode: str) -> None:
    assert decision is not None
    assert decision.allow is False
    assert decision.refusal["detector"] == "plane"
    assert decision.refusal["severity"] == "critical"
    assert "on_plane_loss" in decision.refusal["message"]
    assert decision.refusal["details"] == {"reason": "plane_unreachable", "mode": mode}
    # A refusal is a one-time answer, never fleet state.
    assert decision.latch is None
    assert decision.strikes == 0
    assert decision.generation == 0
    assert decision.halt is False


def test_refuse_on_a_degraded_link():
    clock = MovableClock()
    plane = Plane()
    plane.consecutive_failures = DEGRADE_AFTER
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), shared._config)

    _assert_plane_refusal(decision, "degraded")
    assert plane.count("enter") == 0


def test_refuse_on_a_timeout():
    clock = MovableClock()
    plane = Plane(mode="timeout")
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), shared._config)

    _assert_plane_refusal(decision, "timeout")


def test_refuse_on_an_error():
    clock = MovableClock()
    plane = Plane(mode="error")
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), shared._config)

    _assert_plane_refusal(decision, "error")


def test_refuse_is_never_cached_the_next_entry_asks_again():
    clock = MovableClock()
    plane = Plane(mode="timeout")
    shared = remote(plane, clock, on_plane_loss="refuse")

    shared.enter("user-9", state(), shared._config)
    shared.enter("user-9", state(), shared._config)

    assert plane.count("enter") == 2


def test_a_fresh_cached_decision_is_still_served_under_refuse():
    clock = MovableClock()
    plane = Plane(EntryDecision(fleet_spend_usd=3.0), mode="ok")
    shared = remote(plane, clock, on_plane_loss="refuse")

    first = shared.enter("user-9", state(), shared._config)
    assert first is not None and first.allow is True

    plane.mode = "error"  # the plane goes away right after answering
    second = shared.enter("user-9", state(), shared._config)

    assert second is first
    assert plane.count("enter") == 1  # served from cache, no second call


# --- invalid API key: a configuration error, not plane loss ----------------


def test_invalid_key_guards_locally_under_guard_locally(caplog):
    clock = MovableClock()
    plane = Plane()
    plane.key_state = "invalid"
    shared = remote(plane, clock, on_plane_loss="guard_locally")

    with caplog.at_level("WARNING", logger="runbound"):
        decision = shared.enter("user-9", state(), shared._config)

    assert decision is None
    assert plane.count("enter") == 0
    assert any(
        "rejected this api key" in record.message.lower() for record in caplog.records
    )


def test_invalid_key_guards_locally_under_refuse_instead_of_refusing_forever(caplog):
    """SHOULD 7: an invalid key is a config error, not plane loss — it must
    not turn on_plane_loss="refuse" into refusing every session forever."""
    clock = MovableClock()
    plane = Plane()
    plane.key_state = "invalid"
    shared = remote(plane, clock, on_plane_loss="refuse")

    with caplog.at_level("WARNING", logger="runbound"):
        decision = shared.enter("user-9", state(), shared._config)

    assert decision is None
    assert plane.count("enter") == 0
    assert any(
        "rejected this api key" in record.message.lower() for record in caplog.records
    )


def test_invalid_key_warning_is_rate_limited_to_once_a_minute():
    clock = MovableClock()
    plane = Plane()
    plane.key_state = "invalid"
    shared = remote(plane, clock, on_plane_loss="refuse")

    import logging

    logger = logging.getLogger("runbound")
    seen = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record)
    logger.addHandler(handler)
    try:
        shared.enter("user-9", state(), shared._config)
        shared.enter("user-9", state(), shared._config)
        shared.enter("user-9", state(), shared._config)
        warnings_so_far = sum(
            1 for r in seen if "rejected this api key" in r.getMessage().lower()
        )
        assert warnings_so_far == 1

        clock.advance(61.0)
        shared.enter("user-9", state(), shared._config)
        warnings_after = sum(
            1 for r in seen if "rejected this api key" in r.getMessage().lower()
        )
        assert warnings_after == 2
    finally:
        logger.removeHandler(handler)


def test_invalid_key_still_serves_a_fresh_cached_decision():
    """A decision cached before the key went bad is still the floor."""
    clock = MovableClock()
    plane = Plane(EntryDecision(fleet_spend_usd=3.0), mode="ok")
    shared = remote(plane, clock, on_plane_loss="refuse")

    first = shared.enter("user-9", state(), shared._config)
    assert first is not None and first.allow is True

    plane.key_state = "invalid"
    second = shared.enter("user-9", state(), shared._config)

    assert second is first
    assert plane.count("enter") == 1


def test_degraded_still_refuses_under_refuse_when_the_key_is_fine():
    """The invalid-key carve-out must not swallow the ordinary degraded case."""
    clock = MovableClock()
    plane = Plane()
    plane.consecutive_failures = DEGRADE_AFTER
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), shared._config)

    _assert_plane_refusal(decision, "degraded")


def test_timeout_still_refuses_under_refuse_when_the_key_is_fine():
    clock = MovableClock()
    plane = Plane(mode="timeout")
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), shared._config)

    _assert_plane_refusal(decision, "timeout")


def test_error_still_refuses_under_refuse_when_the_key_is_fine():
    clock = MovableClock()
    plane = Plane(mode="error")
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), shared._config)

    _assert_plane_refusal(decision, "error")


# --- limited mode: not plane loss, in either on_plane_loss setting ---------


def test_limited_mode_answers_locally_under_guard_locally():
    clock = MovableClock()
    plane = Plane()
    shared = remote(plane, clock, on_plane_loss="guard_locally")
    shared.apply_hello(HelloReply(entitlements={"denied": ["workers_synced_exceeded"]}))

    decision = shared.enter("user-9", state(), shared._config)

    assert decision is None
    assert plane.count("enter") == 0


def test_limited_mode_answers_locally_under_refuse_too():
    """A plan limit is not plane loss: the plane is answering fine."""
    clock = MovableClock()
    plane = Plane()
    shared = remote(plane, clock, on_plane_loss="refuse")
    shared.apply_hello(HelloReply(entitlements={"denied": ["workers_synced_exceeded"]}))

    decision = shared.enter("user-9", state(), shared._config)

    assert decision is None
    assert plane.count("enter") == 0


# --- fail-open: our own bug is not plane loss --------------------------------


class _ConfigMissingService:
    """A config double missing what ``_entry_payload`` needs: raises, not loses."""

    send_session_keys = False


def test_a_payload_construction_failure_stays_none_even_under_refuse():
    """The ``except Exception`` guard in ``enter()`` is our bug, not plane loss.

    It must never be turned into a manufactured refusal — that would refuse
    real sessions over an SDK defect instead of failing open.
    """
    clock = MovableClock()
    plane = Plane()
    shared = remote(plane, clock, on_plane_loss="refuse")

    decision = shared.enter("user-9", state(), _ConfigMissingService())

    assert decision is None
    assert plane.count("enter") == 0
