"""Tightening the plane's Controls against this worker's ``init()``
configuration, "refused and shown", the detector override (notify never
latches, shadow never stops), staleness and
fail-open.

Built the way ``tests/test_loop_policy.py`` builds its own engine-level
cases: a bare :class:`~runbound.engine.Engine`, a bare
:class:`~runbound.state.SessionState`, and events pushed through
``engine.process`` directly — no wrapper, no provider client, no plane
socket. The plane link itself is a tiny fake with a settable
``controls_directive()``, exactly the shape
:class:`~runbound.shared.RemoteState` answers with.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped
from spike_test_helpers import spike_controls_body
from runbound.state import SessionState


class FakeControlsPlane:
    """Just enough of :class:`~runbound.shared.SharedState` for the engine's
    own Controls merge: a settable directive, nothing else wired up."""

    fleet = True

    def __init__(self, body: dict | None = None) -> None:
        self.body = body

    def controls_directive(self):
        return self.body

    def posture_directive(self):
        return None

    def policy(self):
        return None

    @property
    def policy_version(self):
        return 0

    @property
    def policy_dry_run(self):
        return False


def engine(config: GuardrailConfig, plane: FakeControlsPlane | None = None) -> Engine:
    config.validate()
    return Engine(config, shared=plane)


def session() -> SessionState:
    return SessionState("s1")


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash)


def llm_event(step: int, cost: float = 0.0, tokens_out: int = 0) -> Event:
    return Event(kind="llm_call", ts=float(step), step=step, cost_usd=cost, tokens_out=tokens_out)


# --- the per-detector code baseline, table-driven --------------------------
#
# Engine._local_detector_action is the one thing that decides which
# direction invariant 3 protects for a given detector: read each knob's
# current semantics from config.py/engine.py, not from memory. Every name
# in DEFAULT_DETECTORS, plus circuit (door + wall) and the door-only names
# that reuse a wall's own detector string.


@pytest.mark.parametrize(
    "detector, config_kwargs, expected",
    [
        # circuit: on_provider_failure, not on_anomaly.
        ("circuit", {"on_provider_failure": "open", "on_anomaly": "warn"}, "stop"),
        ("circuit", {"on_provider_failure": "notify", "on_anomaly": "raise"}, "notify"),
        # loop: on_loop, when stated, overrides on_anomaly entirely.
        ("loop", {"on_loop": "throttle", "on_anomaly": "raise"}, "notify"),
        ("loop", {"on_loop": "break", "on_anomaly": "warn"}, "stop"),
        ("loop", {"on_loop": "escalate", "on_anomaly": "warn"}, "stop"),
        ("loop", {"on_loop": None, "on_anomaly": "raise"}, "stop"),
        ("loop", {"on_loop": None, "on_anomaly": "warn"}, "notify"),
        # velocity: always severity="warn", never stops, whatever on_anomaly is.
        ("velocity", {"on_anomaly": "raise"}, "notify"),
        ("velocity", {"on_anomaly": "callback", "callback": lambda a: None}, "notify"),
        # ordinary detectors: on_anomaly only, no knob of their own.
        ("budget", {"on_anomaly": "raise"}, "stop"),
        ("budget", {"on_anomaly": "warn"}, "notify"),
        ("steps", {"on_anomaly": "callback", "callback": lambda a: None}, "stop"),
        ("steps", {"on_anomaly": "warn"}, "notify"),
        ("events", {"on_anomaly": "raise"}, "stop"),
        ("events", {"on_anomaly": "warn"}, "notify"),
        ("error_storm", {"on_anomaly": "raise"}, "stop"),
        ("error_storm", {"on_anomaly": "warn"}, "notify"),
        ("timeout", {"on_anomaly": "raise"}, "stop"),
        ("timeout", {"on_anomaly": "warn"}, "notify"),
        # an unknown name (forward-compatible): the ordinary rule.
        ("something_future", {"on_anomaly": "raise"}, "stop"),
        ("something_future", {"on_anomaly": "warn"}, "notify"),
    ],
)
def test_local_detector_action_table(detector, config_kwargs, expected):
    eng = engine(GuardrailConfig(**config_kwargs))

    assert eng._local_detector_action(detector) == expected


@pytest.mark.parametrize(
    "on_spike, on_anomaly, expected",
    [
        ("notify", "raise", "notify"),
        ("trip", "warn", "stop"),
        ("limit", "warn", "stop"),
    ],
)
def test_local_detector_action_table_for_spike(on_spike, on_anomaly, expected):
    """Spike's own knob is ``on_spike``, not ``on_anomaly`` -- same rule
    as the table above. ``on_spike`` is a real, local ``GuardrailConfig``
    field too, but this test delivers it through the plane's Controls
    instead (``Engine._effective_config``), this suite's own coverage of
    that path; every other detector in the table reads straight off
    ``GuardrailConfig``."""
    plane = FakeControlsPlane(spike_controls_body(on_spike))
    eng = engine(GuardrailConfig(on_anomaly=on_anomaly), plane)

    assert eng._local_detector_action("spike") == expected


# --- the pure merge, read through the engine's own accessors --------------


def test_no_plane_leaves_the_codes_own_configuration_unchanged():
    eng = engine(GuardrailConfig(budget_usd=10.0, max_steps=5), FakeControlsPlane(None))

    assert eng._limit("budget_usd") == 10.0
    assert eng._limit("max_steps") == 5
    assert eng._effective_capabilities() == {}
    assert eng._effective_envelope() is True  # config.envelope's own default
    assert eng.controls_detector_override("loop") is None
    assert eng.controls_refusals() == []


def test_the_plane_tightens_a_limit():
    plane = FakeControlsPlane({"limits": {"org": {"budget_usd": 1.0}}})
    eng = engine(GuardrailConfig(budget_usd=10.0), plane)

    assert eng._limit("budget_usd") == 1.0
    assert eng.controls_refusals() == []


def test_the_plane_cannot_loosen_a_limit_and_it_is_refused_and_shown():
    plane = FakeControlsPlane({"limits": {"org": {"budget_usd": 100.0}}})
    eng = engine(GuardrailConfig(budget_usd=10.0), plane)

    assert eng._limit("budget_usd") == 10.0
    assert eng.controls_refusals() == [
        {"path": "limits.budget_usd", "base": 10.0, "candidate": 100.0}
    ]


def test_the_plane_tightens_capabilities():
    plane = FakeControlsPlane({"capabilities": {"financial": "deny"}})
    eng = engine(GuardrailConfig(), plane)

    assert eng._effective_capabilities() == {"financial": "deny"}


def test_the_plane_cannot_loosen_capabilities():
    plane = FakeControlsPlane({"capabilities": {"financial": "allow"}})
    # capabilities is a real, local init() field too; the engine's own
    # tighten-only merge (Engine._code_capabilities, read defensively
    # via getattr) is otherwise unchanged, so this sets it directly, the
    # same way a plane-delivered value from a previous merge round would
    # never do (config objects here are never shared between tests).
    config = GuardrailConfig()
    config.capabilities = {"financial": "deny"}
    eng = engine(config, plane)

    assert eng._effective_capabilities() == {"financial": "deny"}
    assert eng.controls_refusals() == [
        {"path": "capabilities.financial", "base": "deny", "candidate": "allow"}
    ]


def test_the_plane_can_turn_the_envelope_on():
    plane = FakeControlsPlane({"envelope": True})
    eng = engine(GuardrailConfig(envelope=False), plane)

    assert eng._effective_envelope() is True


def test_the_plane_cannot_turn_the_envelope_off():
    plane = FakeControlsPlane({"envelope": False})
    eng = engine(GuardrailConfig(envelope=True), plane)

    assert eng._effective_envelope() is True
    assert eng.controls_refusals() == [
        {"path": "envelope", "base": True, "candidate": False}
    ]


def test_the_plane_simply_saying_nothing_about_envelope_is_never_a_violation():
    """The common case: the SDK's own envelope default is already True, and
    a Controls body that never mentions it must not be reported as a
    refused loosening."""
    plane = FakeControlsPlane({"limits": {"org": {"max_steps": 3}}})
    eng = engine(GuardrailConfig(envelope=True), plane)

    assert eng._effective_envelope() is True
    assert eng.controls_refusals() == []


def test_agent_and_key_and_action_levels_are_carried_but_not_enforced():
    """No scope registry exists yet for agent/key/action levels — a strict limit stated
    only at ``agent``/``key``/``action`` must never leak into this
    process-wide number."""
    plane = FakeControlsPlane({"limits": {"agent": {"budget_usd": 0.01}, "key": {"max_steps": 1}}})
    eng = engine(GuardrailConfig(budget_usd=10.0, max_steps=5), plane)

    assert eng._limit("budget_usd") == 10.0
    assert eng._limit("max_steps") == 5


def test_a_malformed_controls_body_fails_open_never_loosening_and_never_crashing():
    plane = FakeControlsPlane("not a dict at all")
    eng = engine(GuardrailConfig(budget_usd=10.0), plane)

    assert eng._limit("budget_usd") == 10.0
    assert eng.controls_refusals() == []


def test_a_directive_that_raises_fails_open():
    class Explodes(FakeControlsPlane):
        def controls_directive(self):
            raise RuntimeError("plane says no")

    eng = engine(GuardrailConfig(budget_usd=10.0), Explodes(None))

    assert eng._limit("budget_usd") == 10.0


def test_the_merge_is_cached_by_body_identity_not_recomputed_every_call(monkeypatch):
    from runbound import controls_merge

    plane = FakeControlsPlane({"limits": {"org": {"budget_usd": 1.0}}})
    eng = engine(GuardrailConfig(budget_usd=10.0), plane)
    calls = []
    original = controls_merge.effective_limits

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(controls_merge, "effective_limits", counting)
    eng._controls_last_body = object()  # force one recompute to install the count hook cleanly
    eng._limit("budget_usd")
    eng._limit("budget_usd")
    eng._limit("budget_usd")

    assert len(calls) == 1


# --- detector override: the tighten-only rule -----------------------------
#
# "merged with the code's config so the plane can only tighten: lower
# budget, lower limit, notify -> stop but never stop -> notify" plus
# "on_anomaly is how your code handles being stopped ... and cannot be set
# from a dashboard". Two directions, both protected:
#
#   * a plane "notify"/"shadow" against a "stop" baseline (this worker's own
#     on_anomaly, or a detector's own knob, would already stop on it) is a
#     loosening -- refused, reported, and the worker keeps stopping.
#   * a plane "stop" against a "notify" baseline is a real tightening, but
#     is only ever *applied* when this worker can_stop at all
#     (on_anomaly in ("raise", "callback")) -- forcing a raise nothing
#     catches is the bug fixed for the local case, done remotely instead.


class _Recorder:
    def __init__(self):
        self.reacted = []

    def on_event(self, session, event):
        pass

    def on_anomaly(self, session, anomaly, reacted):
        self.reacted.append((anomaly.detector, reacted))


# --- probe A (steps, raise-mode): a plane "notify" cannot switch off a wall


def test_probe_a_a_notify_control_cannot_loosen_a_raise_mode_workers_steps_wall():
    plane = FakeControlsPlane({"detectors": {"steps": {"action": "notify", "mode": "enforce"}}})
    config = GuardrailConfig(max_steps=2, on_anomaly="raise")
    config.validate()
    observer = _Recorder()
    eng = Engine(config, observers=[observer], shared=plane)
    state = session()

    tripped = None
    for step in range(1, 6):
        try:
            eng.process(state, llm_event(step))
        except GuardrailTripped as exc:
            tripped = exc
            break

    assert tripped is not None and tripped.anomaly.detector == "steps"
    assert state.tripped_by is not None
    assert ("steps", "raise") in observer.reacted
    assert eng.controls_refusals() == [
        {"path": "detectors.steps.action", "base": "stop", "candidate": "notify"}
    ]


def test_probe_a_the_door_refuses_too_not_only_the_wall():
    """The steps door reuses the wall's own detector name; a plane
    "notify" that cannot loosen the wall must not unlock the door either."""
    plane = FakeControlsPlane({"detectors": {"steps": {"action": "notify", "mode": "enforce"}}})
    eng = engine(GuardrailConfig(max_steps=1, on_anomaly="raise"), plane)
    state = session()
    eng.process(state, llm_event(1))

    with pytest.raises(GuardrailTripped) as excinfo:
        eng._admit_steps(state)

    assert excinfo.value.anomaly.detector == "steps"
    assert state.tripped_by is not None


# --- probe C (budget, raise-mode): a plane "notify" cannot switch off money


def test_probe_c_a_notify_control_cannot_loosen_a_raise_mode_workers_budget_wall():
    plane = FakeControlsPlane({"detectors": {"budget": {"action": "notify", "mode": "enforce"}}})
    config = GuardrailConfig(budget_usd=0.0001, on_anomaly="raise")
    config.validate()
    observer = _Recorder()
    eng = Engine(config, observers=[observer], shared=plane)
    state = session()

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.process(state, llm_event(1, cost=5.0))

    assert excinfo.value.anomaly.detector == "budget"
    assert state.tripped_by is not None
    assert ("budget", "raise") in observer.reacted
    assert eng.controls_refusals() == [
        {"path": "detectors.budget.action", "base": "stop", "candidate": "notify"}
    ]


def test_a_shadow_control_cannot_loosen_a_real_stop_into_a_preview():
    """"shadow on a detector the code already stops on is loosening too" --
    a plane {"action": "stop", "mode": "shadow"} against a "stop" baseline
    is refused on the mode field, and the worker keeps stopping for real."""
    plane = FakeControlsPlane({"detectors": {"budget": {"action": "stop", "mode": "shadow"}}})
    config = GuardrailConfig(budget_usd=1.0, on_anomaly="raise")
    config.validate()
    observer = _Recorder()
    eng = Engine(config, observers=[observer], shared=plane)
    state = session()

    with pytest.raises(GuardrailTripped):
        eng.process(state, llm_event(1, cost=5.0))

    assert state.tripped_by is not None
    assert ("budget", "raise") in observer.reacted
    assert eng.controls_refusals() == [
        {"path": "detectors.budget.mode", "base": "enforce", "candidate": "shadow"}
    ]


def test_with_no_plane_the_same_overspend_still_stops_the_run():
    """The control opposite: no plane means the budget detector behaves
    exactly as it always has."""
    config = GuardrailConfig(budget_usd=1.0, on_anomaly="raise")
    config.validate()
    eng = Engine(config)
    state = session()

    with pytest.raises(GuardrailTripped) as excinfo:
        eng.process(state, llm_event(1, cost=5.0))
    assert excinfo.value.anomaly.detector == "budget"


# --- probe B (steps, warn-mode): a plane "stop" cannot be forced on a
# worker whose code never expects a GuardrailTripped


def test_probe_b_a_stop_control_is_not_applied_under_on_anomaly_warn():
    plane = FakeControlsPlane({"detectors": {"steps": {"action": "stop", "mode": "enforce"}}})
    config = GuardrailConfig(max_steps=2, on_anomaly="warn")
    config.validate()
    observer = _Recorder()
    eng = Engine(config, observers=[observer], shared=plane)
    state = session()

    for step in range(1, 6):
        eng.process(state, llm_event(step))  # never raises: on_anomaly="warn"

    assert state.tripped_by is None
    assert ("steps", "raise") not in observer.reacted
    assert eng.controls_refusals() == [
        {
            "path": "detectors.steps.action",
            "base": "notify",
            "candidate": "stop",
            "reason": "cannot_stop",
        }
    ]


def test_probe_b_the_door_does_not_refuse_either():
    plane = FakeControlsPlane({"detectors": {"steps": {"action": "stop", "mode": "enforce"}}})
    eng = engine(GuardrailConfig(max_steps=1, on_anomaly="warn"), plane)
    state = session()
    eng.process(state, llm_event(1))

    eng._admit_steps(state)  # must not raise: this worker cannot be made to stop

    assert state.tripped_by is None


# --- the positive case: a raise-mode worker's own notify-only spike can be
# escalated by the plane, because this worker's code can act on it


def test_a_dashboard_stop_escalates_a_raise_mode_workers_own_notify_spike():
    """Code: ``on_spike="notify"`` (spike watches but never stops on its
    own). Dashboard: spike -> stop. On a worker whose ``on_anomaly`` can
    raise at all, it now actually stops."""
    plane = FakeControlsPlane({"detectors": {"spike": {"action": "stop", "mode": "enforce"}}})
    config = GuardrailConfig(on_anomaly="raise")
    config.validate()
    eng = Engine(config, shared=plane)

    assert eng.controls_detector_override("spike") == "stop"
    assert eng.controls_refusals() == []  # a real tightening, nothing refused


def test_the_same_escalation_is_not_applied_under_warn_mode():
    """The same escalation, but this worker's own on_anomaly is "warn": not
    applied, and reported as "cannot_stop" rather than a loosening."""
    plane = FakeControlsPlane({"detectors": {"spike": {"action": "stop", "mode": "enforce"}}})
    config = GuardrailConfig(on_anomaly="warn")
    config.validate()
    eng = Engine(config, shared=plane)

    assert eng.controls_detector_override("spike") is None
    assert eng.controls_refusals() == [
        {
            "path": "detectors.spike.action",
            "base": "notify",
            "candidate": "stop",
            "reason": "cannot_stop",
        }
    ]


def test_shadow_mode_stops_nothing_regardless_of_can_stop():
    """mode: "shadow" never actually stops anything, whatever can_stop
    says -- it only ever previews. Tested under on_anomaly="warn", where a
    forced "stop" could never apply anyway, to isolate shadow's own rule."""
    plane = FakeControlsPlane({"detectors": {"spike": {"action": "stop", "mode": "shadow"}}})
    config = GuardrailConfig(on_anomaly="warn")
    config.validate()
    eng = Engine(config, shared=plane)

    assert eng.controls_detector_override("spike") == "dry_run"
    assert eng.controls_refusals() == []  # nothing was refused; shadow adopted freely


# --- a warn-severity anomaly is never forced to stop, whatever a control
# says (velocity is always "warn"; an escalating loop's early stage too)


def test_a_stop_control_on_velocity_never_stops_it_is_always_warn_severity():
    plane = FakeControlsPlane({"detectors": {"velocity": {"action": "stop", "mode": "enforce"}}})
    config = GuardrailConfig(tokens_per_minute_limit=1, on_anomaly="raise")
    config.validate()
    observer = _Recorder()
    eng = Engine(config, observers=[observer], shared=plane)
    state = session()

    eng.process(state, llm_event(1, tokens_out=2))  # over tokens_per_minute_limit=1, severity "warn"

    assert state.tripped_by is None
    assert not any(reacted == "raise" for _detector, reacted in observer.reacted)


def test_the_door_does_not_refuse_when_a_notify_control_states_nothing_it_neednt():
    """Baseline sanity: a plane control on a *different* detector name must
    never affect the steps door."""
    plane = FakeControlsPlane({"detectors": {"budget": {"action": "notify", "mode": "enforce"}}})
    eng = engine(GuardrailConfig(max_steps=1, on_anomaly="raise"), plane)
    state = session()
    eng.process(state, llm_event(1))

    with pytest.raises(GuardrailTripped):
        eng._admit_steps(state)  # the steps wall itself, unaffected by a budget control
