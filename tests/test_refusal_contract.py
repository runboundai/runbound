"""The refusal contract: a structured, retry-aware dependency failure.

Never a fake success, never a swallowed error, never a business decision
made for the customer, and never something a generic retry loop turns into
the storm runbound exists to stop. ``ExecutionRefused`` is the public name;
``GuardrailTripped`` is the identical class object under its old name.
"""

import pytest

import runbound
from runbound import api
from runbound.events import Anomaly, Decision
from runbound.exceptions import (
    REASONS,
    CircuitOpen,
    ExecutionRefused,
    GuardrailTripped,
    PolicyViolation,
    SafeModeViolation,
    is_retryable,
)
from runbound.policy import Violation


@pytest.fixture(autouse=True)
def _pristine():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


# --- the alias: one class, two names, permanently ---------------------------


def test_guardrail_tripped_is_execution_refused_the_same_object():
    assert runbound.GuardrailTripped is runbound.ExecutionRefused
    assert GuardrailTripped is ExecutionRefused


def test_every_built_in_refusal_is_still_an_execution_refused():
    assert issubclass(CircuitOpen, ExecutionRefused)
    assert issubclass(PolicyViolation, ExecutionRefused)
    assert issubclass(SafeModeViolation, ExecutionRefused)


def test_an_except_guardrail_tripped_still_catches_every_refusal():
    """Every handler already written as ``except GuardrailTripped:`` keeps
    working — the whole point of the alias being the same object, not a
    lookalike."""
    runbound.init(on_anomaly="raise")

    @runbound.tool(blocked=True)
    def wire_money():
        return "sent"

    caught = None
    try:
        wire_money()
    except GuardrailTripped as exc:
        caught = exc
    assert isinstance(caught, PolicyViolation)
    assert isinstance(caught, ExecutionRefused)


def test_no_refusal_ever_subclasses_a_provider_sdk_exception():
    openai = pytest.importorskip("openai")
    anthropic = pytest.importorskip("anthropic")
    for cls in (ExecutionRefused, GuardrailTripped, CircuitOpen, PolicyViolation, SafeModeViolation):
        assert not issubclass(cls, openai.APIError)
        assert not issubclass(cls, anthropic.APIError)


# --- the closed set of reasons ----------------------------------------------


def test_the_reason_set_is_exactly_the_stated_closed_set():
    assert set(REASONS) == {
        "budget", "tokens", "steps", "time", "posture", "policy", "approval",
        "circuit", "concurrency", "blast_radius", "halt", "plane", "loop",
        "error_storm", "spike",
    }
    assert len(REASONS) == len(set(REASONS))  # no duplicates


def _refused(boundary: str | None, detector: str, violation: Violation | None = None):
    """A bare ``ExecutionRefused`` carrying a Decision with this boundary."""
    decision = Decision(verdict="deny", kind="model_call", boundary=boundary, detector=detector)
    anomaly = Anomaly(
        detector=detector, severity="critical", message="refused",
        details={"decision": decision.as_dict()},
    )
    exc = ExecutionRefused(anomaly)
    if violation is not None:
        exc.violation = violation  # PolicyViolation/SafeModeViolation-shaped, without the subclass
    return exc


@pytest.mark.parametrize(
    "boundary, detector, expected_reason, expected_retryable",
    [
        ("money", "budget", "budget", False),
        ("tokens", "budget", "tokens", False),
        ("steps", "steps", "steps", False),
        ("time", "timeout", "time", False),
        ("posture", "safe_mode", "posture", False),
        ("capability", "safe_mode", "policy", False),
        ("blast_radius", "fanout", "blast_radius", False),
        ("circuit", "circuit", "circuit", True),
        ("concurrency", "inflight", "concurrency", True),
        ("policy", "policy", "policy", False),
        ("halt", "halt", "halt", False),
        ("plane", "plane", "plane", True),
    ],
)
def test_reason_and_retryable_from_the_decisions_boundary(
    boundary, detector, expected_reason, expected_retryable
):
    exc = _refused(boundary, detector)
    assert exc.reason == expected_reason
    assert exc.retryable is expected_retryable


@pytest.mark.parametrize(
    "detector, expected_reason",
    [
        ("loop", "loop"),
        ("error_storm", "error_storm"),
        ("spike", "spike"),
    ],
)
def test_reason_from_the_detector_when_theres_no_decision_at_all(detector, expected_reason):
    """A post-call wall trip other than the budget crossing carries no
    Decision (``exc.decision`` is ``None``); the reason still resolves, from
    the anomaly's own detector name."""
    anomaly = Anomaly(detector=detector, severity="critical", message="refused", details={})
    exc = ExecutionRefused(anomaly)
    assert exc.decision is None
    assert exc.reason == expected_reason
    assert exc.retryable is False  # none of these three are in the retryable set


def test_an_approval_violation_reasons_as_approval_not_policy():
    violation = Violation("wire_money", "approval", "no approver configured", {})
    exc = _refused("policy", "policy", violation=violation)
    assert exc.reason == "approval"
    assert exc.retryable is False


def test_retryable_is_false_for_every_reason_the_contract_names_non_retryable():
    for reason, boundary, detector in [
        ("budget", "money", "budget"),
        ("tokens", "tokens", "budget"),
        ("steps", "steps", "steps"),
        ("time", "time", "timeout"),
        ("posture", "posture", "safe_mode"),
        ("policy", "policy", "policy"),
        ("halt", "halt", "halt"),
        ("blast_radius", "blast_radius", "fanout"),
    ]:
        exc = _refused(boundary, detector)
        assert exc.reason == reason
        assert exc.retryable is False, reason
        assert is_retryable(exc) is False, reason


def test_retryable_is_true_with_a_retry_after_for_circuit_and_concurrency():
    circuit_exc = _refused("circuit", "circuit")
    assert circuit_exc.retryable is True
    assert is_retryable(circuit_exc) is True

    concurrency_exc = _refused("concurrency", "inflight")
    assert concurrency_exc.retryable is True
    assert concurrency_exc.retry_after is not None
    assert concurrency_exc.retry_after > 0


def test_is_retryable_is_false_for_an_exception_runbound_never_raised():
    assert is_retryable(ValueError("nope")) is False
    assert is_retryable(RuntimeError()) is False


# --- provider_called and scope ----------------------------------------------


def test_provider_called_is_false_for_every_admission_refusal():
    runbound.init(budget_usd=0.001, budget_admission="capped", on_anomaly="raise",
                   custom_prices={"m": (0.0, 1_000_000.0)})

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):  # pragma: no cover - must never run
                    raise AssertionError("the provider must never be reached")

    client = runbound.wrap(Client())
    with pytest.raises(ExecutionRefused) as excinfo:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}], max_tokens=1000)

    assert excinfo.value.provider_called is False
    assert excinfo.value.reason == "budget"


def test_provider_called_is_true_for_a_post_call_budget_crossing():
    runbound.init(budget_usd=1.0, on_anomaly="raise")

    class FakeUsage:
        prompt_tokens = 10
        completion_tokens = 1_000_000  # deliberately huge: crosses after the fact

    class FakeResponse:
        model = "gpt-4o"
        usage = FakeUsage()

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return FakeResponse()

    client = runbound.wrap(Client())
    with pytest.raises(ExecutionRefused) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "x"}])

    assert excinfo.value.reason == "budget"
    assert excinfo.value.provider_called is True
    assert "already happened" in (excinfo.value.decision.reason or "")


def test_scope_carries_the_level_and_a_key_hash_never_the_key():
    runbound.init(on_anomaly="raise")

    @runbound.tool(blocked=True)
    def wire_money():
        return "sent"

    with runbound.session("user:8842"):
        with pytest.raises(ExecutionRefused) as excinfo:
            wire_money()

    scope = excinfo.value.scope
    assert scope["level"] == "session"
    assert scope["key_hash"] is not None
    assert scope["key_hash"] != "user:8842"
    assert scope["key_hash"] == runbound.key_hash("user:8842")


def test_scope_key_hash_is_none_for_the_default_session():
    runbound.init(on_anomaly="raise")

    @runbound.tool(blocked=True)
    def wire_money():
        return "sent"

    with pytest.raises(ExecutionRefused) as excinfo:
        wire_money()

    assert excinfo.value.scope["key_hash"] is None


# --- the test that matters: a retry loop cannot turn into a storm ----------


def test_a_predicate_driven_retry_loop_makes_exactly_one_attempt_on_a_budget_refusal():
    """``runbound.is_retryable`` as the predicate: a well-behaved retry loop
    that retries on ``Exception`` but checks the predicate first stops
    immediately on a budget refusal, because ``reason == "budget"`` is never
    retryable — no amount of waiting and asking again puts more money in the
    budget."""
    runbound.init(
        budget_usd=0.001, budget_admission="capped", on_anomaly="raise",
        custom_prices={"m": (0.0, 1_000_000.0)},
    )

    class Client:
        class chat:
            class completions:
                calls = 0

                @classmethod
                def create(cls, **kwargs):
                    cls.calls += 1
                    raise AssertionError("the provider must never be reached")

    client = runbound.wrap(Client())

    attempts = 0
    last_exc = None
    for _ in range(5):
        attempts += 1
        try:
            client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "x"}], max_tokens=1000
            )
            break
        except Exception as exc:
            last_exc = exc
            if not runbound.is_retryable(exc):
                break

    assert attempts == 1
    assert last_exc.reason == "budget"
    assert Client.chat.completions.calls == 0


def test_a_retry_loop_that_ignores_the_predicate_is_stopped_by_the_loop_detector():
    """A careless ``except Exception: retry`` loop, with no predicate at all,
    calling a tool that is refused every time: the retry storm this would
    otherwise become is bounded not by luck but by runbound's own loop
    detector, which — content-blind, watching only the repeated identical
    call — latches the session within ``loop_threshold`` attempts, turning
    every attempt after that into an instant, free refusal rather than an
    unbounded stream of real ones."""
    threshold = 3
    runbound.init(on_anomaly="raise", on_loop="break", loop_threshold=threshold, tool_policy={"deny": ["wire_money"]})

    @runbound.tool
    def wire_money():
        return "sent"  # pragma: no cover - never admitted to run

    attempts = 0
    detectors_seen = []
    for _ in range(threshold + 5):
        attempts += 1
        try:
            wire_money()
        except Exception as exc:  # deliberately blind: no is_retryable check
            detectors_seen.append(getattr(exc, "anomaly", None) and exc.anomaly.detector)
            if runbound.is_tripped() is not None:
                break
            continue

    assert attempts <= threshold + 1
    assert "loop" in detectors_seen
    assert runbound.is_tripped() is not None  # the loop detector latched it
