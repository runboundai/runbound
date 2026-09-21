"""Local controls are free; a connected plane only ever tightens them.

Six controls (circuit rate mode, loop shapes beyond "repeat", the budget
soft line, ``max_actions_per_run``, class-rule capabilities and spike
detection/the ladder) are each a real, free, local ``init()`` keyword, and
a connected plane can only *tighten* what the code already configured (or
state one from scratch when the code states nothing) — never loosen it,
and never require a plane at all. This file is the one-stop acceptance
test for that guarantee, proven as a **triple** per control:

1. **code only** — the control fires from local configuration alone, no
   plane connected at all.
2. **plane only** — the code states nothing; a connected plane's own
   Controls turn the same behavior on from scratch.
3. **code tightened by plane** — the code states a stricter value than the
   plane tries to loosen it to; the merge refuses the loosening and the
   code's own value stands, visibly (``Engine.controls_refusals()``).

Also covered: ``enter_safe_mode``/``exit_safe_mode`` are back on the public
surface; a posture set locally, by the plane, or automatically (the
ladder) all still compose through ``effective_posture``.

Other files own the deeper behavioral coverage of each control
(``test_circuit_config.py``, ``test_circuit_posture.py``, ``test_loop_shapes.py``,
``test_budget_soft_and_reservation.py``, ``test_envelope.py``,
``test_postures.py``); this one is the narrow, literal proof of the tighten-
only acceptance sentence — including the six deliberate merge breaks this
file's own re-runs exercise (see each ``..._a_looser_plane_value_is_refused``
test below: flip its own ``effective_*`` direction and watch it fail).
"""

import pytest

import runbound
from runbound import api, controls_merge, shared as shared_module
from runbound.exceptions import GuardrailTripped, SafeModeViolation
from runbound.plane_types import HelloReply
from spike_test_helpers import spike_controls_body
from test_shared_state import FakePlane

PLANE_URL = "https://plane.example"


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
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


def init_connected(**kwargs) -> None:
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


def deliver(plane: FakePlane, controls: dict) -> None:
    plane.controls_body = {"version": 1, "controls": controls}
    api._SHARED.apply_hello(HelloReply(controls_version=1))


# --- circuit rate mode: code only / plane only / code tightened by plane ---


def _open_rate_mode_input(client, calls: int = 3) -> None:
    """Slow-but-successful calls -- what a rate-mode circuit opens on with
    zero errors, and a count-mode-only breaker never even looks at."""
    for _ in range(calls):
        client.chat.completions.create(model="m", messages=[], duration_s=5.0)


class _SlowOpenAI:
    """An OpenAI-shaped client whose every call takes ``duration_s``
    seconds (read by the fake, never the real wrapper)."""

    class _Usage:
        prompt_tokens = 0
        completion_tokens = 0

    class _Response:
        def __init__(self, model):
            self.model = model
            self.usage = _SlowOpenAI._Usage()

    class _Completions:
        def __init__(self, clock):
            self.clock = clock
            self.calls = 0

        def create(self, *, model, messages, duration_s=0.0, **kwargs):
            self.calls += 1
            self.clock.advance(duration_s)
            return _SlowOpenAI._Response(model)

    def __init__(self, clock):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _SlowOpenAI._Completions(clock)


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_a_count_mode_circuit_never_opens_on_slow_successful_calls(monkeypatch):
    from runbound import wrappers

    clock = _FakeClock()
    monkeypatch.setattr(wrappers, "time", clock)
    runbound.init(on_provider_failure="open")  # no local rate mode
    client = runbound.wrap(_SlowOpenAI(clock))

    _open_rate_mode_input(client)

    assert runbound.circuit_state("openai") == "closed"


def test_a_local_rate_mode_circuit_opens_with_no_plane_at_all(monkeypatch):
    from runbound import wrappers

    clock = _FakeClock()
    monkeypatch.setattr(wrappers, "time", clock)
    runbound.init(
        on_provider_failure="open",
        circuit_mode="rate",
        circuit_min_calls=3,
        circuit_slow_call_seconds=1.0,
        circuit_slow_rate=0.5,
    )
    client = runbound.wrap(_SlowOpenAI(clock))

    _open_rate_mode_input(client)  # three slow, successful calls

    assert runbound.circuit_state("openai") == "open"  # code only, no plane


def test_the_same_input_opens_it_once_the_plane_enables_rate_mode(plane, monkeypatch):
    from runbound import wrappers

    clock = _FakeClock()
    monkeypatch.setattr(wrappers, "time", clock)
    init_connected(on_provider_failure="open")  # no local rate mode
    deliver(
        plane,
        {"circuit_rate": {"min_calls": 3, "slow_call_seconds": 1.0, "slow_rate": 0.5}},
    )
    client = runbound.wrap(_SlowOpenAI(clock))

    _open_rate_mode_input(client)  # the exact same three calls

    assert runbound.circuit_state("openai") == "open"  # plane only


def test_a_plane_cannot_loosen_a_locally_configured_rate_mode(plane, monkeypatch):
    """A mutation-test regression: break ``effective_circuit_rate``'s direction
    (make it take ``max`` instead of ``min`` for ``min_calls``) and this
    test fails -- the looser plane value would then win instead of being
    refused."""
    from runbound import wrappers

    clock = _FakeClock()
    monkeypatch.setattr(wrappers, "time", clock)
    init_connected(
        on_provider_failure="open",
        circuit_mode="rate",
        circuit_min_calls=3,
        circuit_slow_call_seconds=1.0,
        circuit_slow_rate=0.5,
    )
    deliver(plane, {"circuit_rate": {"min_calls": 10}})  # tries to loosen 3 -> 10

    client = runbound.wrap(_SlowOpenAI(clock))
    _open_rate_mode_input(client, calls=3)  # would only open under the code's own min_calls=3

    assert runbound.circuit_state("openai") == "open"  # code's stricter min_calls stood
    engine = api._ENGINE
    refusals = engine.controls_refusals()
    assert any(v["path"] == "circuit_rate.min_calls" for v in refusals)


# --- loop shapes: code only / plane only / code tightened by plane --------


def _run_period_3_cycle() -> None:
    @runbound.tool
    def edit(text):
        return "ok"

    @runbound.tool
    def test_(text):
        return "ok"

    @runbound.tool
    def review(text):
        return "ok"

    for i in range(3):
        edit(f"file-{i}")
        test_(f"run-{i}")
        review(f"note-{i}")


def test_a_cycle_shape_loop_fires_locally_with_no_plane_at_all():
    """``loop_shapes`` defaults to ``("repeat", "sequence", "retry")`` --
    "sequence" is free, on by default, no plane needed."""
    runbound.init(on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as excinfo:
        _run_period_3_cycle()

    assert excinfo.value.anomaly.details["shape"] == "sequence"


def test_the_stall_shape_fires_once_the_plane_adds_it(plane):
    """"stall" is opt-in even locally (not in the default tuple); a plane
    can still turn it on from nothing, exactly as it always could. Every
    call below is identical -- the same hash, over and over -- which
    "stall" (no *new* hash for stall_turns turns) reads as stalled, not a
    repeat: repeat is unreachable here at loop_threshold=100."""
    init_connected(on_anomaly="raise", loop_threshold=100, loop_window=200)
    deliver(plane, {"loop_shapes": {"shapes": ["repeat", "stall"], "stall_turns": 2}})

    @runbound.tool
    def search(query):
        return "ok"

    runbound.record_call("m", 1, 0, 0.0)  # turn 1
    search("only-call")  # new hash at turn 1
    runbound.record_call("m", 1, 0, 0.0)  # turn 2, stalled=1
    with pytest.raises(GuardrailTripped) as excinfo:
        runbound.record_call("m", 1, 0, 0.0)  # turn 3, stalled=2 -> fires

    assert excinfo.value.anomaly.details["shape"] == "stall"


def test_a_plane_cannot_loosen_a_locally_configured_stall_turns(plane):
    """A mutation-test regression: flip ``effective_loop_shapes``'s stall_turns
    direction (``>`` instead of ``<``) and this test fails."""
    init_connected(
        on_anomaly="raise",
        loop_threshold=100,
        loop_window=200,
        loop_shapes=("repeat", "stall"),
        loop_stall_turns=2,
    )
    deliver(plane, {"loop_shapes": {"shapes": ["repeat", "stall"], "stall_turns": 10}})

    engine = api._ENGINE
    assert engine._controls_snapshot().loop_shapes["stall_turns"] == 2
    refusals = engine.controls_refusals()
    assert any(v["path"] == "loop_shapes.stall_turns" for v in refusals)


# --- budget soft line: code only / plane only / code tightened by plane ---


def _spend(usd: float) -> None:
    api._record_llm_call("gpt-4o", 0, int(round(usd * 100_000)))


def test_the_budget_soft_line_reacts_locally_with_no_plane_at_all():
    runbound.init(budget_usd=1.0, budget_soft=0.8, on_budget_soft="safe_mode")

    with runbound.session("soft-local"):
        _spend(0.85)  # 85%: past the local 80% line

    status = runbound.session_status("soft-local")
    assert status["posture"]["name"] == "restricted"
    assert status["posture"]["source"] == "budget_soft"


def test_the_same_spend_reacts_once_the_plane_states_the_soft_line(plane):
    init_connected(budget_usd=1.0)  # no local soft line
    deliver(plane, {"budget_soft": {"fraction": 0.8, "reaction": "safe_mode"}})

    with runbound.session("soft-fake-plane"):
        _spend(0.85)

    status = runbound.session_status("soft-fake-plane")
    assert status["posture"]["name"] == "restricted"
    assert status["posture"]["source"] == "budget_soft"


def test_a_plane_cannot_loosen_a_locally_configured_soft_fraction(plane):
    """A mutation-test regression: flip ``effective_budget_soft``'s fraction
    comparison and this test fails -- a spend that only crosses the
    code's own strict 50% line would then no longer react."""
    init_connected(budget_usd=1.0, budget_soft=0.5, on_budget_soft="safe_mode")
    deliver(plane, {"budget_soft": {"fraction": 0.9}})  # tries to loosen 50% -> 90%

    with runbound.session("soft-tightened"):
        _spend(0.6)  # past the code's own 50% line, well under the plane's 90%

    status = runbound.session_status("soft-tightened")
    assert status["posture"]["name"] == "restricted"  # code's stricter line stood
    engine = api._ENGINE
    refusals = engine.controls_refusals()
    assert any(v["path"] == "budget_soft.fraction" for v in refusals)


# --- class-rule capabilities: code only / plane only / code tightened -----


def test_a_local_class_rule_denies_a_tool_with_no_plane_at_all():
    runbound.init(capabilities={"financial": "deny"})

    @runbound.tool(effects={"financial"}, max_calls=1)
    def issue_refund():
        return "refunded"

    with pytest.raises(SafeModeViolation) as caught:
        issue_refund()

    assert caught.value.anomaly.details["denied_class"] == "financial"


def test_the_same_tool_is_denied_once_the_plane_states_the_class_rule(plane):
    init_connected()  # no local class rule

    @runbound.tool(effects={"financial"}, max_calls=1)
    def issue_refund():
        return "refunded"

    deliver(plane, {"capabilities": {"financial": "deny"}})

    with pytest.raises(SafeModeViolation) as caught:
        issue_refund()

    assert caught.value.anomaly.details["denied_class"] == "financial"


def test_a_plane_cannot_loosen_a_locally_denied_class(plane):
    init_connected(capabilities={"financial": "deny"})

    @runbound.tool(effects={"financial"}, max_calls=1)
    def issue_refund():
        return "refunded"

    deliver(plane, {"capabilities": {"financial": "allow"}})  # tries to loosen deny -> allow

    with pytest.raises(SafeModeViolation) as caught:
        issue_refund()  # the code's own deny stood

    assert caught.value.anomaly.details["denied_class"] == "financial"
    engine = api._ENGINE
    refusals = engine.controls_refusals()
    assert any(v["path"] == "capabilities.financial" for v in refusals)


# --- max_actions_per_run: code only / plane only / code tightened ---------


def test_max_actions_per_run_caps_a_run_with_no_plane_at_all():
    runbound.init(on_anomaly="raise", loop_threshold=10, max_actions_per_run=2)

    @runbound.tool
    def ping():
        return "pong"

    ping()
    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    assert excinfo.value.anomaly.detector == "fanout"
    assert excinfo.value.anomaly.details["rule"] == "actions"


def test_the_same_cap_applies_once_the_plane_states_the_limit(plane):
    init_connected(on_anomaly="raise", loop_threshold=10)  # no local cap
    deliver(plane, {"max_actions_per_run": 2})

    @runbound.tool
    def ping():
        return "pong"

    ping()
    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    assert excinfo.value.anomaly.detector == "fanout"


def test_a_plane_cannot_loosen_a_locally_configured_action_cap(plane):
    init_connected(on_anomaly="raise", loop_threshold=10, max_actions_per_run=2)
    deliver(plane, {"max_actions_per_run": 10})  # tries to loosen 2 -> 10

    @runbound.tool
    def ping():
        return "pong"

    ping()
    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()  # the third action is still refused, not the eleventh

    assert excinfo.value.anomaly.detector == "fanout"
    engine = api._ENGINE
    refusals = engine.controls_refusals()
    assert any(v["path"] == "max_actions_per_run" for v in refusals)


# --- spike detection and the ladder: code only / plane only / tightened ---


def _spike_input(key: str) -> None:
    """Warm a keyed session at its own steady 2s normal, then two calls
    20x that -- the exact input the ladder confirms and climbs to level 2
    on once spike detection and the ladder run (spike_confirm's default is
    2 straight abnormal calls; spike_warmup_calls' default is 4)."""
    for _ in range(5):
        with runbound.session(key):
            api._record_llm_call("gpt-4o", 10, 100, duration_s=2.0)
    for _ in range(2):
        with runbound.session(key):
            api._record_llm_call("gpt-4o", 10, 100, duration_s=400.0)


def test_the_ladder_climbs_locally_with_no_plane_at_all():
    runbound.init(on_spike="limit")  # spike_detection=True by default

    _spike_input("plane-less-spiker")

    assert runbound.session_status("plane-less-spiker")["level"] == 2


def test_the_same_spike_input_climbs_the_ladder_once_the_plane_enables_limit(plane):
    init_connected()  # on_spike defaults "notify" locally -- never climbs on its own
    deliver(plane, spike_controls_body("limit"))

    _spike_input("fake-plane-spiker")

    assert runbound.session_status("fake-plane-spiker")["level"] == 2


def test_a_plane_cannot_loosen_a_locally_configured_spike_mode(plane):
    """A mutation-test regression: flip ``effective_spike``'s mode ranking (rank
    "notify" above "trip") and this test fails -- the plane's attempted
    downgrade would then win."""
    init_connected(on_spike="trip", on_anomaly="raise")
    deliver(plane, {"spike_enabled": True, "spike": {"mode": "notify"}})  # tries trip -> notify

    with pytest.raises(GuardrailTripped) as excinfo:
        _spike_input("tightened-spiker")  # still stops the run -- "trip" stood

    assert excinfo.value.anomaly.detector == "spike"
    engine = api._ENGINE
    refusals = engine.controls_refusals()
    assert any(v["path"] == "spike.mode" for v in refusals)


def test_a_control_the_plane_delivers_still_runs_when_entitlements_omit_it(plane):
    """Entitlements and Controls are separate objects, and the control
    engine reads no entitlement at
    all -- this delivers spike/the ladder through Controls while the
    hello's own entitlements say nothing about it at all (the plain
    ``HelloReply(controls_version=...)`` every other test in this file
    already sends)."""
    init_connected()
    deliver(plane, spike_controls_body("limit"))

    assert runbound.plane_status().entitlements == {}  # nothing named, nothing to omit
    _spike_input("entitlements-blind-spiker")

    assert runbound.session_status("entitlements-blind-spiker")["level"] == 2


# --- enter_safe_mode / exit_safe_mode are back on the public surface ------


def test_enter_safe_mode_and_exit_safe_mode_are_on_the_process():
    runbound.init()

    runbound.enter_safe_mode(reason="manual", posture="restricted")
    assert runbound.posture() == "restricted"

    runbound.exit_safe_mode()
    assert runbound.posture() == "full"


def test_enter_safe_mode_and_exit_safe_mode_are_on_the_session():
    runbound.init()

    with runbound.session("s1") as state:
        state.enter_safe_mode(reason="manual", posture="restricted")
        assert runbound.session_status("s1")["posture"]["name"] == "restricted"
        state.exit_safe_mode()
        assert runbound.session_status("s1")["posture"] is None


def test_a_plane_directive_still_moves_the_posture_too(plane):
    init_connected()
    assert runbound.posture() == "full"

    api._SHARED.apply_hello(HelloReply(posture="restricted"))
    assert runbound.posture() == "restricted"

    api._SHARED.apply_hello(HelloReply(posture=None))
    assert runbound.posture() == "full"
