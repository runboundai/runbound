"""A complete Decision: every deny or restrict is understandable six hours
later without querying mutable state.

Every refusal site carries a ``Decision`` that states, in ``evaluation``,
whichever of ``limit``, ``used``, ``reserved``, ``estimate``, ``remaining``
apply to what it is judging, plus ``provider_called`` — always, whether or
not the boundary has any of the other five. That includes the fleet-level
sites (an org budget or an unreachable plane the plane itself decided, a
halt, a latch relayed from another worker) whose ``evaluation`` states only
``provider_called`` — they carry no numbers of their own to report, only the
fact and the source of the refusal, and inventing limit/used figures that
were never actually compared would be less honest than an evaluation that
says only what is known. A table test walks every refusal site this SDK
raises and checks each against what CAN apply for its own boundary; only a
post-call wall trip other than the budget crossing (`loop`, `spike`,
`error_storm`, the post-call `steps`/`timeout` walls) carries no Decision at
all — covered separately in ``tests/test_refusal_contract.py``.
"""

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped
from runbound.plane_types import EntryDecision

from test_session_sync import plane, start  # noqa: F401  (fixture + helper)

KNOWN_EVALUATION_KEYS = {"limit", "used", "reserved", "estimate", "remaining", "provider_called"}


@pytest.fixture(autouse=True)
def _pristine():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class FakeUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=10):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeResponse:
    def __init__(self, model=None, usage=None):
        self.model = model
        self.usage = usage or FakeUsage()


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(model=kwargs.get("model"))


class FakeOpenAI:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions()


def _assert_complete(decision, *, required=(), forbidden=()):
    """Every key in ``required`` is present; ``provider_called`` is always
    present and a bool; nothing in ``evaluation`` is a key this module does
    not recognize (a typo would silently ship as an unread field forever)."""
    evaluation = decision.evaluation
    assert "provider_called" in evaluation
    assert isinstance(evaluation["provider_called"], bool)
    for key in required:
        assert key in evaluation, (key, evaluation)
    for key in forbidden:
        assert key not in evaluation, (key, evaluation)


# --- circuit -----------------------------------------------------------------


def test_circuit_refusal_states_provider_called_false():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    client = runbound.wrap(FakeOpenAI())
    runbound.record_call("gpt-4o", 0, 0, provider="openai@default", error=TimeoutError("x"))

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.provider_called is False


# --- stopped -------------------------------------------------------------


def test_stopped_refusal_states_provider_called_false():
    runbound.init(on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    runbound.enter_safe_mode("manual", "stopped")

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.provider_called is False
    assert client.chat.completions.calls == 0


# --- steps / run time / tokens (the envelope's own door stages) ------------


def test_steps_door_refusal_states_limit_used_provider_called_false():
    runbound.init(max_steps=1, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    _assert_complete(excinfo.value.decision, required=("limit", "used"))
    assert excinfo.value.provider_called is False


def test_tokens_door_refusal_states_limit_used_estimate_provider_called_false():
    runbound.init(max_total_tokens=10, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[], max_tokens=1000)

    _assert_complete(excinfo.value.decision, required=("limit", "used", "estimate"))
    assert excinfo.value.provider_called is False


# --- money: the reservation door --------------------------------------------


def test_reservation_refusal_states_limit_reserved_estimate_remaining():
    runbound.init(budget_usd=1.0, custom_prices={"m": (0.0, 1_000_000.0)})
    client = runbound.wrap(FakeOpenAI())

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="m", messages=[], max_tokens=2)

    _assert_complete(
        excinfo.value.decision, required=("limit", "reserved", "estimate", "remaining")
    )
    assert excinfo.value.provider_called is False


def test_post_call_budget_crossing_states_limit_used_provider_called_true():
    runbound.init(budget_usd=1.0, on_anomaly="raise")

    class HugeUsage:
        prompt_tokens = 10
        completion_tokens = 1_000_000

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return FakeResponse(model="gpt-4o", usage=HugeUsage())

    client = runbound.wrap(Client())
    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    _assert_complete(excinfo.value.decision, required=("limit", "used"))
    assert excinfo.value.provider_called is True


# --- actions: the envelope's own cap ----------------------------------------


def test_action_cap_refusal_states_limit_used_provider_called_false(monkeypatch):
    from runbound import shared as shared_module
    from runbound.plane_types import HelloReply
    from test_shared_state import FakePlane

    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url, fake.token, fake.service, fake.worker_id = url, token, service, worker_id
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    runbound.init(
        control_plane_url="https://plane.example", token="k", service="checkout",
        worker_id="host-1:1", control_plane_poll_s=3600.0, export_events=False,
        auto_wrap=False, on_anomaly="raise",
    )
    fake.controls_body = {"version": 1, "controls": {"max_actions_per_run": 1}}
    api._SHARED.apply_hello(HelloReply(controls_version=1))

    @runbound.tool
    def ping():
        return "pong"

    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    _assert_complete(excinfo.value.decision, required=("limit", "used"))
    assert excinfo.value.provider_called is False


# --- posture / capability ----------------------------------------------------


def test_posture_refusal_states_provider_called_false_no_numeric_fields():
    runbound.init(on_anomaly="raise")

    @runbound.tool(effects={"financial"})
    def wire_money():
        return "sent"

    runbound.enter_safe_mode("manual", "restricted")
    with pytest.raises(GuardrailTripped) as excinfo:
        wire_money()

    _assert_complete(excinfo.value.decision, forbidden=("limit", "used", "reserved", "estimate", "remaining"))
    assert excinfo.value.provider_called is False


def test_capability_class_rule_refusal_states_provider_called_false():
    runbound.init(on_anomaly="raise", capabilities={"financial": "deny"})

    @runbound.tool(effects={"financial"})
    def wire_money():
        return "sent"

    with pytest.raises(GuardrailTripped) as excinfo:
        wire_money()

    assert excinfo.value.decision.boundary == "capability"
    _assert_complete(excinfo.value.decision, forbidden=("limit", "used", "reserved", "estimate", "remaining"))
    assert excinfo.value.provider_called is False


# --- tool policy -------------------------------------------------------------


def test_policy_max_calls_refusal_states_provider_called_false():
    runbound.init(on_anomaly="raise")

    @runbound.tool(max_calls=1)
    def search():
        return "ok"

    search()
    with pytest.raises(GuardrailTripped) as excinfo:
        search()

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.provider_called is False


# --- concurrency and blast radius (api-level entry doors) -------------------


def test_inflight_refusal_states_limit_used_provider_called_false():
    runbound.init(max_inflight_calls=1)
    client = runbound.wrap(FakeOpenAI())
    api._INFLIGHT["openai@default"] = 1

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    _assert_complete(excinfo.value.decision, required=("limit", "used"))
    assert excinfo.value.provider_called is False


def test_fanout_refusal_states_limit_used_provider_called_false():
    runbound.init(max_active_sessions=1)
    api._ACTIVE = 5

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("a"):
            pass

    _assert_complete(excinfo.value.decision, required=("limit", "used"))
    assert excinfo.value.provider_called is False


# --- the fleet-level sites: a Decision with nothing but its own identity ---
#
# Decided elsewhere (the plane, another worker) and only relayed here as
# data, so none of these states limit/used/reserved/estimate/remaining --
# only provider_called, which is always known (none of these ever touched a
# provider), and a boundary/level that says what kind of refusal it is and
# which scope decided it.


def test_org_budget_refusal_from_the_plane_states_provider_called_false(plane):
    plane.decision = EntryDecision(allow=False, refusal={
        "detector": "budget",
        "severity": "critical",
        "message": "Org daily budget spent: $12.40 of $10.00",
        "details": {"reason": "org_budget"},
    })
    start(on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("org-budget-table-test"):
            pass

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.decision.boundary == "money"
    assert excinfo.value.decision.level == "fleet"
    assert excinfo.value.provider_called is False


def test_plane_unreachable_refusal_states_provider_called_false(plane):
    plane.ok = False
    start(on_anomaly="raise", on_plane_loss="refuse")

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("plane-loss-table-test"):
            pass

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.decision.boundary == "plane"
    assert excinfo.value.decision.level == "fleet"
    assert excinfo.value.provider_called is False


def test_halt_refusal_states_provider_called_false(plane):
    plane.decision = EntryDecision(halt=True)
    start(on_halt="raise")

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("halt-table-test"):
            pass

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.decision.boundary == "halt"
    assert excinfo.value.decision.level == "fleet"
    assert excinfo.value.provider_called is False


def test_remote_latch_refusal_states_provider_called_false(plane):
    plane.decision = EntryDecision(latch={
        "detector": "budget",
        "severity": "critical",
        "message": "Budget exceeded on another worker",
        "ttl_remaining_s": 60.0,
    })
    start(on_anomaly="raise")

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("remote-latch-table-test"):
            pass

    _assert_complete(excinfo.value.decision)
    assert excinfo.value.decision.boundary == "money"
    assert excinfo.value.decision.level == "fleet"
    assert excinfo.value.provider_called is False


# --- no field this module does not recognize --------------------------------


def test_every_evaluation_key_this_test_file_sees_is_a_recognized_one():
    """A regression net for a typo introducing a stray key nobody reads:
    every site above is re-collected here and checked against the closed
    ``KNOWN_EVALUATION_KEYS`` set, plus each stage's own ``rule``/other
    documented extras."""
    extras = {"rule"}  # admission.actions, the tool-policy Decisions
    runbound.init(max_steps=1, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI())
    client.chat.completions.create(model="gpt-4o", messages=[])
    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    for key in excinfo.value.decision.evaluation:
        assert key in KNOWN_EVALUATION_KEYS | extras, key


def test_a_relayed_refusal_never_has_an_empty_reason(plane):
    """A Decision must read on its own six hours later. A halt arrives as a
    flag and a mode, with no numbers and no directive id, so its Decision
    says exactly that: a sentence, and the mode the plane stated."""
    plane.decision = EntryDecision(halt=True)
    start(on_halt="raise")

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("halt-reason-test"):
            pass

    decision = excinfo.value.decision
    assert decision.reason, "a relayed refusal must still say why, in words"
    assert "halt" in decision.reason.lower()
    assert decision.evaluation.get("halt_mode") == "stop"
    assert decision.evaluation.get("provider_called") is False
