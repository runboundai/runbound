"""A budget that cannot be crossed — soft line, reservation, remaining.

Three things, each with the acceptance line it answers:

* ``budget_admission="capped"`` (the new default): a request that states its
  own output cap is refused before it goes out when that cap, priced at the
  model's output rate, plus its input estimate would cross what is left. A
  request with no stated cap takes 0.3.0's path unchanged.
* ``budget_soft`` / ``on_budget_soft``: a line under the budget that warns once
  and never latches — or puts the session in safe mode.
* ``runbound.budget()``: what is left, readable, agreeing with
  ``session_status()`` to the cent.
"""

import pytest

import runbound
from runbound import api, shared as shared_module
from runbound.config import GuardrailConfig
from runbound.detectors import BudgetDetector
from runbound.events import Event
from runbound.exceptions import GuardrailTripped, SafeModeViolation
from runbound.plane_types import HelloReply
from runbound.state import SessionState
from test_shared_state import FakePlane

KEY = "user:8842"
MODEL = "gpt-4o"  # $2.50 in / $10.00 out per 1M tokens
PLANE_URL = "https://plane.example"


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
    """The soft line is a real, local ``init()`` knob too (see
    ``test_reservation_on_a_stated_cap_is_the_new_default`` below); this
    fixture is this suite's own coverage of the plane-delivered path -- a
    real worker also sees one once the plane's ``/v1/controls`` states a
    ``budget_soft`` bundle, delivered on a heartbeat exactly like
    ``tests/test_controls_delivery.py``'s harness."""
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


def init_with_soft_line(plane: FakePlane, fraction, reaction="notify", **kwargs) -> None:
    """``runbound.init(**kwargs)`` connected to ``plane``, with a
    ``budget_soft`` bundle already delivered -- the plane-delivered
    alternative to ``runbound.init(budget_soft=..., on_budget_soft=..., ...)``."""
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
    plane.controls_body = {
        "version": 1,
        "controls": {"budget_soft": {"fraction": fraction, "reaction": reaction}},
    }
    api._SHARED.apply_hello(HelloReply(controls_version=1))


def spend(usd: float) -> None:
    """Record one gpt-4o call costing exactly ``usd``, all of it output."""
    api._record_llm_call(MODEL, 0, int(round(usd * 100_000)))


class Recorder:
    def __init__(self):
        self.events = []
        self.anomalies = []

    def on_event(self, session, event):
        self.events.append(event)

    def on_anomaly(self, session, anomaly, reacted):
        self.anomalies.append((anomaly, reacted))


def observe() -> Recorder:
    recorder = Recorder()
    api._ENGINE.observers.append(recorder)
    return recorder


# --- the fake client --------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 10


class _Response:
    def __init__(self, model):
        self.model = model
        self.usage = _Usage()


class _Completions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _Response(kwargs.get("model"))


class FakeOpenAI:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions()


SHORT = [{"role": "user", "content": "hi"}]


# --- defaults and validation ------------------------------------------------


def test_reservation_on_a_stated_cap_is_the_new_default():
    config = GuardrailConfig()
    assert config.budget_admission == "capped"
    # budget_soft/on_budget_soft are real, local fields -- off, and
    # "notify", by default.
    assert config.budget_soft is None
    assert config.on_budget_soft == "notify"


@pytest.mark.parametrize("value", [False, True, "capped"])
def test_budget_admission_accepts_its_three_modes(value):
    GuardrailConfig(budget_admission=value).validate()


@pytest.mark.parametrize("value", ["yes", "on", 1, 0, None])
def test_budget_admission_rejects_anything_else(value):
    with pytest.raises(ValueError):
        GuardrailConfig(budget_admission=value).validate()


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 1.5, -0.1, "0.8", True])
def test_budget_soft_must_be_a_fraction_strictly_between_zero_and_one(value):
    """budget_soft is validated loudly at init(): a bad fraction raises
    ValueError, not a silent parse fallback."""
    with pytest.raises(ValueError, match="budget_soft"):
        GuardrailConfig(budget_usd=10.0, budget_soft=value).validate()


def test_a_soft_line_with_no_budget_usd_is_rejected_at_init():
    with pytest.raises(ValueError, match="budget_usd"):
        GuardrailConfig(budget_soft=0.8).validate()


@pytest.mark.parametrize("value", ["stop", "raise", None, True])
def test_on_budget_soft_rejects_anything_unrecognized(value):
    with pytest.raises(ValueError, match="on_budget_soft"):
        GuardrailConfig(budget_usd=10.0, budget_soft=0.8, on_budget_soft=value).validate()


# --- the wire's own defensive parse, for whatever a plane still tightens ---


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 1.5, -0.1, "0.8", True])
def test_the_wires_own_parse_falls_back_to_none_for_a_bad_fraction(value):
    """A plane's raw payload is still untrusted wire data, parsed
    defensively by runbound.controls_merge._parse_budget_soft, which falls
    back to None (nothing stated) rather than raising -- unlike the local
    ``init()`` keyword above, a malformed plane value must never crash a
    worker."""
    from runbound.controls_merge import _parse_budget_soft

    assert _parse_budget_soft({"fraction": value}) is None


def test_the_wires_own_parse_has_no_notion_of_budget_usd():
    """_parse_budget_soft has no notion of budget_usd at all (that
    invariant lives in GuardrailConfig.validate() for the local keyword);
    BudgetDetector._soft_line's own `config.budget_usd is None` guard is
    what keeps a stray soft line from ever firing."""
    from runbound.controls_merge import _parse_budget_soft

    assert _parse_budget_soft({"fraction": 0.8}) == {"fraction": 0.8, "reaction": "notify"}


@pytest.mark.parametrize("value", ["stop", "raise", None, True])
def test_the_wires_own_parse_falls_back_to_notify_for_anything_unrecognized(value):
    from runbound.controls_merge import _parse_budget_soft

    result = _parse_budget_soft({"fraction": 0.8, "reaction": value})
    assert result == {"fraction": 0.8, "reaction": "notify"}


# --- reservation on a stated cap --------------------------------------------


def test_a_stated_cap_that_would_cross_what_is_left_is_refused_before_any_socket_opens():
    runbound.init(budget_usd=1.0, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    spend(0.90)  # $0.10 left
    before = runbound.budget().spent

    with pytest.raises(GuardrailTripped) as caught:
        # 15,000 output tokens at $10/1M is $0.15 worst case.
        client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=15_000)

    assert client.chat.completions.calls == 0
    assert runbound.budget().spent == pytest.approx(before)
    details = caught.value.anomaly.details
    assert details["reason"] == "reservation"
    assert details["cap_tokens"] == 15_000
    assert details["worst_case_usd"] == pytest.approx(0.15, abs=1e-4)
    assert details["remaining_usd"] == pytest.approx(0.10)
    assert runbound.is_tripped() is None  # a reservation refusal never latches


def test_a_stated_cap_that_fits_goes_out():
    runbound.init(budget_usd=1.0, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    spend(0.90)
    client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=5_000)  # $0.05
    assert client.chat.completions.calls == 1


@pytest.mark.parametrize(
    "cap_field", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
def test_every_cap_field_a_provider_uses_is_a_stated_cap(cap_field):
    runbound.init(budget_usd=1.0, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    spend(0.90)
    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model=MODEL, messages=SHORT, **{cap_field: 15_000})
    assert client.chat.completions.calls == 0


def _script(admission) -> tuple[list, list]:
    """The same no-cap traffic, walked past the budget, under one admission mode."""
    runbound.init(budget_usd=0.05, budget_admission=admission, on_anomaly="warn")
    recorder = observe()
    client = runbound.wrap(FakeOpenAI())
    huge = [{"role": "user", "content": "x" * 400_000}]  # ~100k estimated tokens
    for _ in range(3):
        client.chat.completions.create(model=MODEL, messages=huge)  # no cap stated
    spend(0.10)  # crosses the budget after the fact
    events = [(e.kind, e.model, e.tokens_in, e.tokens_out, e.cost_usd) for e in recorder.events]
    anomalies = [
        (a.detector, a.severity, a.message, {k: v for k, v in a.details.items() if k != "session_id"}, r)
        for a, r in recorder.anomalies
    ]
    calls = client.chat.completions.calls
    api._teardown_for_tests()
    return events + [("calls", calls)], anomalies


def test_with_no_stated_cap_capped_is_byte_identical_to_admission_off():
    assert _script("capped") == _script(False)


def test_admission_off_admits_a_stated_cap_that_capped_would_refuse():
    runbound.init(budget_usd=1.0, budget_admission=False, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    spend(0.90)
    client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=15_000)
    assert client.chat.completions.calls == 1


def test_true_still_estimates_an_assumed_cap_when_none_is_stated():
    runbound.init(
        budget_usd=1.0, budget_admission=True, admission_output_tokens=15_000, on_anomaly="raise"
    )
    client = runbound.wrap(FakeOpenAI())
    spend(0.90)
    with pytest.raises(GuardrailTripped) as caught:
        client.chat.completions.create(model=MODEL, messages=SHORT)  # no cap: 15k assumed
    assert caught.value.anomaly.details["rule"] == "admission"
    assert client.chat.completions.calls == 0


def test_capped_lets_the_same_uncapped_call_through():
    runbound.init(budget_usd=1.0, admission_output_tokens=15_000, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    spend(0.90)
    client.chat.completions.create(model=MODEL, messages=SHORT)
    assert client.chat.completions.calls == 1


# --- the soft line ----------------------------------------------------------


def test_the_soft_line_warns_once_at_the_first_call_that_crosses_it_and_never_latches(plane):
    init_with_soft_line(plane, 0.8, budget_usd=1.0, on_anomaly="raise")
    recorder = observe()
    spend(0.50)
    assert recorder.anomalies == []
    spend(0.35)  # $0.85: over the $0.80 line
    soft = [a for a, _ in recorder.anomalies if a.details.get("limit_hit") == "budget_soft"]
    assert len(soft) == 1
    assert soft[0].detector == "budget"
    assert soft[0].severity == "warn"
    assert soft[0].details["soft_at_usd"] == pytest.approx(0.80)
    spend(0.05)  # still over the line: silent
    assert len([a for a, _ in recorder.anomalies if a.details.get("limit_hit") == "budget_soft"]) == 1
    assert runbound.is_tripped() is None


def test_exactly_at_the_soft_line_does_not_fire(plane):
    init_with_soft_line(plane, 0.8, budget_usd=1.0)
    recorder = observe()
    spend(0.80)
    assert recorder.anomalies == []


def test_a_call_that_crosses_both_lines_trips_the_wall_and_the_soft_line_stays_quiet(plane):
    init_with_soft_line(plane, 0.8, budget_usd=1.0, on_anomaly="raise")
    recorder = observe()
    with pytest.raises(GuardrailTripped) as caught:
        spend(1.20)
    assert caught.value.anomaly.details["limit_hit"] == "budget_usd"
    assert not [a for a, _ in recorder.anomalies if a.details.get("limit_hit") == "budget_soft"]


def test_on_budget_soft_safe_mode_narrows_the_session_and_clear_lifts_it(plane):
    init_with_soft_line(plane, 0.8, reaction="safe_mode", budget_usd=1.0, on_anomaly="raise")
    ran = []

    @runbound.tool(effects={"financial"}, max_calls=9)
    def issue_refund():
        ran.append("write")

    @runbound.tool(effects={"read"}, reviewed=True)
    def lookup_order():
        ran.append("read")

    with runbound.session(KEY):
        spend(0.85)
        lookup_order()
        with pytest.raises(SafeModeViolation) as caught:
            issue_refund()
    assert caught.value.anomaly.details["source"] == "budget_soft"
    assert runbound.session_status(KEY)["posture"]["source"] == "budget_soft"
    assert runbound.session_status(KEY)["posture"]["name"] == "restricted"
    assert ran == ["read"]

    runbound.clear(KEY)
    with runbound.session(KEY):
        issue_refund()
    assert ran == ["read", "write"]


def test_the_soft_line_lifts_its_safe_mode_when_spend_is_back_under_it():
    config = GuardrailConfig(budget_usd=1.0)
    config.validate()
    # Set directly here, exactly what Engine._config_for_detection's shim
    # does once the plane states a budget_soft bundle.
    config.budget_soft = 0.8
    config.on_budget_soft = "safe_mode"
    detector = BudgetDetector()
    state = SessionState("s1")
    event = Event(kind="llm_call", ts=0.0, step=1)

    state.total_cost_usd = 0.85
    detector.check(state, event, config)
    assert state.posture is not None and state.posture.source == "budget_soft"

    state.total_cost_usd = 0.50  # the entering counter drops under its line
    detector.check(state, event, config)
    assert state.posture is None


def test_the_soft_line_never_lifts_a_manual_safe_mode():
    config = GuardrailConfig(budget_usd=1.0)
    config.validate()
    config.budget_soft = 0.8
    config.on_budget_soft = "safe_mode"
    detector = BudgetDetector()
    state = SessionState("s1")
    state.enter_safe_mode("operator")
    detector.check(state, Event(kind="llm_call", ts=0.0, step=1), config)
    assert state.posture.source == "manual"


def test_notify_never_touches_safe_mode(plane):
    init_with_soft_line(plane, 0.8, budget_usd=1.0)
    with runbound.session(KEY):
        spend(0.85)
    assert runbound.session_status(KEY)["posture"] is None


# --- budget(): what is left -------------------------------------------------


def test_budget_is_none_before_init_and_without_a_budget():
    assert runbound.budget() is None
    runbound.init()
    assert runbound.budget() is None


def test_budget_reads_what_is_left_and_agrees_with_session_status_to_the_cent(plane):
    init_with_soft_line(plane, 0.5, budget_usd=2.0)
    with runbound.session(KEY):
        spend(0.30)
        spend(0.12)
        live = runbound.current_session().total_cost_usd
        inside = runbound.budget()
    view = runbound.budget(KEY)
    assert inside == view
    assert view.limit == 2.0
    assert round(view.spent, 2) == round(live, 2) == 0.42
    assert round(view.remaining, 2) == 1.58
    assert view.soft_at == pytest.approx(1.0)
    assert (view.window, view.resets_at, view.scope) == (None, None, "session")
    status = runbound.session_status(KEY)["budget"]
    assert round(status["spent"], 2) == round(view.spent, 2)
    assert round(status["remaining"], 2) == round(view.remaining, 2)


def test_budget_counts_what_the_fleet_already_spent():
    runbound.init(budget_usd=2.0)
    with runbound.session(KEY):
        runbound.current_session().spend_offset_usd = 0.5
        spend(0.25)
    assert runbound.budget(KEY).spent == pytest.approx(0.75)


def test_remaining_never_goes_below_zero_after_the_crossing_call():
    runbound.init(budget_usd=0.10, on_anomaly="warn")
    spend(0.25)
    view = runbound.budget()
    assert view.spent == pytest.approx(0.25)
    assert view.remaining == 0.0


def test_budget_for_a_key_with_no_session_is_none():
    runbound.init(budget_usd=1.0)
    assert runbound.budget("nobody") is None
