"""The kill switch's two modes, at the engine level.

``Stop`` (``halt=True``, ``halt_mode="stop"``) refuses every guarded session
at the door — not retested here (see
``tests/test_shared_state.py``'s halt tests and ``tests/test_session_sync.py``
for that path). ``Narrow`` states posture ``restricted`` fleet-wide: model
calls keep serving, and a decorated tool's posture/capability check is the
*only* thing that refuses it.

Built the way ``tests/test_controls_engine.py`` builds its own cases: a bare
:class:`~runbound.engine.Engine`, a bare :class:`~runbound.state.SessionState`,
and a tiny fake plane link with a settable ``halt_mode()``/
``halt_posture_directive()`` — exactly the shape
:class:`~runbound.shared.RemoteState` answers with.
"""

import pytest

from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import SafeModeViolation
from runbound.state import SessionState, make_posture_state


class FakeHaltPlane:
    """Just enough of :class:`~runbound.shared.SharedState` for the kill
    switch's own engine-level merge: a settable halt mode/posture,
    nothing else wired."""

    fleet = True

    def __init__(self, mode: "str | None" = None, controls_posture=None) -> None:
        self._mode = mode
        self._controls_posture = controls_posture

    def halt_mode(self):
        return self._mode

    def halt_posture_directive(self):
        if self._mode != "narrow":
            return None
        return make_posture_state("restricted", "a fleet-wide Narrow halt", "halt")

    def controls_directive(self):
        return None

    def posture_directive(self):
        return self._controls_posture

    def policy(self):
        return None

    @property
    def policy_version(self):
        return 0

    @property
    def policy_dry_run(self):
        return False


def engine(config: GuardrailConfig, plane=None) -> Engine:
    config.validate()
    return Engine(config, shared=plane)


def session() -> SessionState:
    return SessionState("s1")


READ = frozenset({"read"})
WRITE_EXTERNAL = frozenset({"write", "external"})


# --- Narrow: model calls keep serving, non-read tools refuse ---------------


def test_narrow_refuses_a_non_read_tool():
    eng = engine(GuardrailConfig(on_anomaly="raise"), FakeHaltPlane(mode="narrow"))
    state = session()

    with pytest.raises(SafeModeViolation):
        eng.admit(state, kind="action", tool="send_email", effects=WRITE_EXTERNAL, decorated=True)


def test_narrow_allows_a_read_only_tool():
    eng = engine(GuardrailConfig(on_anomaly="raise"), FakeHaltPlane(mode="narrow"))
    state = session()

    eng.admit(state, kind="action", tool="lookup", effects=READ, decorated=True)  # does not raise


def test_narrow_leaves_model_calls_serving():
    """A Narrow halt is a posture, and every built-in posture but
    ``stopped`` admits model calls (``no_side_effects``'s own footnote:
    "still admits model calls") — ``restricted`` is looser than
    that. ``admit`` with the default ``kind="model_call"`` must not raise."""
    eng = engine(GuardrailConfig(on_anomaly="raise"), FakeHaltPlane(mode="narrow"))
    state = session()

    eng.admit(state)  # kind="model_call" -- does not raise


def test_narrow_refusal_names_the_halt_as_its_source():
    eng = engine(GuardrailConfig(on_anomaly="raise"), FakeHaltPlane(mode="narrow"))
    state = session()

    with pytest.raises(SafeModeViolation) as excinfo:
        eng.admit(state, kind="action", tool="send_email", effects=WRITE_EXTERNAL, decorated=True)

    assert "halt" in str(excinfo.value)


def test_effective_posture_is_restricted_under_narrow():
    eng = engine(GuardrailConfig(), FakeHaltPlane(mode="narrow"))
    assert eng.effective_posture(session()).name == "restricted"


def test_no_halt_leaves_the_posture_full():
    eng = engine(GuardrailConfig(), FakeHaltPlane(mode=None))
    assert eng.effective_posture(session()).name == "full"


def test_stop_mode_states_no_posture_at_all():
    """``halt_mode() == "stop"`` never sets ``halt_posture_directive`` on the
    real :class:`~runbound.shared.RemoteState` (tested there); this asserts
    the engine does not itself treat "stop" as if it narrowed anything."""
    eng = engine(GuardrailConfig(), FakeHaltPlane(mode="stop"))
    assert eng.effective_posture(session()).name == "full"


# --- Narrow and Controls are two independent plane sources (trap #2) -------


def test_lifting_narrow_leaves_a_controls_posture_standing():
    plane = FakeHaltPlane(mode="narrow", controls_posture=make_posture_state(
        "read_only", "stated by the control plane", "plane"
    ))
    eng = engine(GuardrailConfig(), plane)
    assert eng.effective_posture(session()).name == "read_only"  # tighten(narrow=restricted, plane=read_only)

    plane._mode = None  # the halt lifts; the Controls-stated posture is untouched
    assert eng.effective_posture(session()).name == "read_only"


def test_lifting_a_controls_posture_leaves_narrow_standing():
    plane = FakeHaltPlane(mode="narrow", controls_posture=make_posture_state(
        "read_only", "stated by the control plane", "plane"
    ))
    eng = engine(GuardrailConfig(), plane)

    plane._controls_posture = None  # the operator clears the Controls row
    assert eng.effective_posture(session()).name == "restricted"  # the halt's own narrow still stands


# --- on_halt="warn": never force an exception into code that asked not to --


def test_narrow_is_not_applied_at_all_under_on_halt_warn():
    eng = engine(GuardrailConfig(on_anomaly="raise", on_halt="warn"), FakeHaltPlane(mode="narrow"))
    state = session()

    eng.admit(state, kind="action", tool="send_email", effects=WRITE_EXTERNAL, decorated=True)  # no raise
    assert eng.effective_posture(state).name == "full"


def test_narrow_is_applied_under_the_default_on_halt_raise():
    eng = engine(GuardrailConfig(on_anomaly="raise", on_halt="raise"), FakeHaltPlane(mode="narrow"))
    state = session()

    with pytest.raises(SafeModeViolation):
        eng.admit(state, kind="action", tool="send_email", effects=WRITE_EXTERNAL, decorated=True)
