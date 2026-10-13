"""A complete Decision: every deny or restrict is understandable six hours
later without querying mutable state.

Every refusal site this SDK raises carries a ``Decision`` that names who spoke
(``detector``, the same string the anomaly carries), what boundary was hit
(``boundary``), which scope decided (``level``), a sentence of ``reason``, and
in ``evaluation`` whichever numbers apply (``limit`` and ``used`` for the
action cap, ``count`` and ``limit`` for ``max_calls``, ``denied_class`` for a
class rule, the ``rule`` for a policy refusal). A table test walks every
refusal site and checks each against what CAN apply for its own boundary; call
arguments never appear.
"""

import pytest

import runbound
from runbound import api
from runbound.exceptions import GuardrailTripped
from runbound.engine import Engine
from runbound.config import RunboundConfig
from runbound.policy import ToolCall
from runbound.shared import LocalState
from runbound.state import SessionState

RECORD_KEYS = {"reason_code", "policy_state", "policy_version", "matched_rule_ids", "service_default_revision", "contributors", "sdk_version"}
KNOWN_EVALUATION_KEYS = {"limit", "used", "count", "rule", "denied_class"} | RECORD_KEYS  # the decision record rides the evaluation (TR2-O2)
SECRET = "sk-do-not-log-me-42"


@pytest.fixture(autouse=True)
def _pristine():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def _assert_complete(exc, *, boundary, required=(), forbidden=()):
    decision = exc.decision
    assert decision is not None
    assert decision.verdict in ("deny", "restrict")
    assert decision.kind == "action"
    assert decision.boundary == boundary
    assert decision.detector == exc.anomaly.detector
    assert decision.reason, "a Decision must say why, in words"
    assert decision.level in ("session", "process", "fleet")
    for key in decision.evaluation:
        assert key in KNOWN_EVALUATION_KEYS, key
    for key in required:
        assert key in decision.evaluation, (key, decision.evaluation)
    for key in forbidden:
        assert key not in decision.evaluation, (key, decision.evaluation)
    assert SECRET not in repr(decision)


# --- the capability class rule -----------------------------------------------


def test_capability_deny_refusal_names_the_class_and_decides_for_the_process():
    runbound.init(capabilities={"financial": "deny"})

    @runbound.tool(effects={"financial"})
    def wire_money(amount):
        return "sent"

    with pytest.raises(GuardrailTripped) as excinfo:
        wire_money(SECRET)

    _assert_complete(
        excinfo.value, boundary="capability", required=("denied_class",),
        forbidden=("limit", "used", "count"),
    )
    assert excinfo.value.decision.verdict == "deny"
    assert excinfo.value.decision.level == "process"
    assert excinfo.value.decision.evaluation["denied_class"] == "financial"


def test_capability_approve_refusal_is_a_restrict():
    runbound.init(capabilities={"financial": "approve"})

    @runbound.tool(effects={"financial"})
    def wire_money(amount):
        return "sent"

    with pytest.raises(GuardrailTripped) as excinfo:
        wire_money(1)

    _assert_complete(excinfo.value, boundary="capability", required=("denied_class",))
    assert excinfo.value.decision.verdict == "restrict"


# --- tool policy -------------------------------------------------------------


def test_policy_max_calls_refusal_states_count_and_limit():
    runbound.init()

    @runbound.tool(max_calls=1)
    def search(query):
        return "ok"

    search("a")
    with pytest.raises(GuardrailTripped) as excinfo:
        search("b")

    _assert_complete(excinfo.value, boundary="policy", required=("count", "limit", "rule"))
    assert excinfo.value.decision.evaluation["rule"] == "max_calls"
    assert (excinfo.value.decision.evaluation["count"], excinfo.value.decision.evaluation["limit"]) == (2, 1)


@pytest.mark.parametrize(
    "decorator, rule",
    [
        ({"blocked": True}, "deny"),
        ({"constraint": lambda call: False}, "constraint"),
        ({"require_approval": lambda call: False}, "approval"),
    ],
)
def test_policy_rule_refusals_state_their_rule_and_never_the_arguments(decorator, rule):
    runbound.init()

    @runbound.tool(**decorator)
    def send_email(to):
        return "sent"

    with pytest.raises(GuardrailTripped) as excinfo:
        send_email(SECRET)

    _assert_complete(excinfo.value, boundary="policy", required=("rule",), forbidden=("denied_class",))
    assert excinfo.value.decision.evaluation["rule"] == rule
    assert SECRET not in repr(excinfo.value.anomaly)


# --- actions: the envelope's own cap -------------------------------------------


def test_action_cap_refusal_states_limit_and_used():
    runbound.init(max_actions_per_run=1)

    @runbound.tool
    def ping():
        return "pong"

    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    _assert_complete(excinfo.value, boundary="blast_radius", required=("limit", "used"))
    assert {k: v for k, v in excinfo.value.decision.evaluation.items() if k not in RECORD_KEYS} == {"limit": 1, "used": 2}


# --- the fleet-level site: a halt, decided elsewhere and only relayed here -----


class HaltedPlane(LocalState):
    fleet = True

    def halted(self):
        return True

    def halt_mode(self):
        return "stop"


def test_halt_refusal_carries_its_identity_and_no_numbers_of_its_own():
    engine = Engine(RunboundConfig(), shared=HaltedPlane())

    with pytest.raises(GuardrailTripped) as excinfo:
        engine.admit_call(SessionState("s1"), ToolCall("search", (), {}, None, {}))

    _assert_complete(excinfo.value, boundary="halt", forbidden=("limit", "used", "count"))
    assert excinfo.value.decision.level == "fleet"
    assert "halt" in excinfo.value.decision.reason.lower()
    assert "stop" in excinfo.value.decision.reason


# --- no field this module does not recognize --------------------------------


def test_every_evaluation_key_a_refusal_states_is_a_recognized_one():
    """A regression net for a typo introducing a stray key nobody reads."""
    runbound.init(max_actions_per_run=1)

    @runbound.tool(max_calls=5)
    def ping():
        return "pong"

    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    for key in excinfo.value.decision.evaluation:
        assert key in KNOWN_EVALUATION_KEYS, key
