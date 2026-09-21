"""The execution envelope's engine/api integration.

``tests/test_admission_stages.py`` covers the pure stage functions in
isolation; this file covers what :class:`~runbound.engine.Engine` and
:mod:`runbound.api` do with them — the door refusing a call before it goes
out, latching, the Decision on every refusal, ``max_actions_per_run``,
``runbound.envelope()``, refusal profiles, and fail-open.
"""

import asyncio
import logging
import time

import pytest

import runbound
from runbound import admission, api
from runbound import engine as engine_module
from runbound import shared as shared_module
from runbound import state as state_module
from runbound.events import Decision
from runbound.exceptions import CircuitOpen, GuardrailTripped, PolicyViolation
from runbound.plane_types import HelloReply
from test_shared_state import FakePlane

PLANE_URL = "https://plane.example"


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def plane(monkeypatch) -> FakePlane:
    """Postures and ``max_actions_per_run`` are real, local ``init()``
    knobs too; this fixture is this suite's own coverage of the
    plane-delivered path, exactly ``tests/test_controls_delivery.py``'s
    own harness."""
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


def narrow(posture_name: str) -> None:
    api._SHARED.apply_hello(HelloReply(posture=posture_name))


def enable_max_actions_per_run(plane: FakePlane, limit: int) -> None:
    plane.controls_body = {"version": 1, "controls": {"max_actions_per_run": limit}}
    api._SHARED.apply_hello(HelloReply(controls_version=1))


# --- fakes -------------------------------------------------------------


class FakeUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeResponse:
    def __init__(self, model, usage):
        self.model = model
        self.usage = usage


class FakeCompletions:
    def __init__(self, model="gpt-4o", tokens=(0, 1)):
        self.model = model
        self.tokens = tokens
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(kwargs.get("model", self.model), FakeUsage(*self.tokens))


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(**kwargs)

    @property
    def completions(self) -> FakeCompletions:
        return self.chat.completions


class FakeClock:
    """Stands in for the ``time`` module the api, engine and state read."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(api, "time", fake)
    monkeypatch.setattr(state_module, "time", fake)
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


def assert_reserved_zero() -> None:
    states = list(api._REGISTRY.values())
    if api._SESSION is not None:
        states.append(api._SESSION)
    for session in states:
        assert not any(session.reserved.values()), session.reserved


# --- stage order ---------------------------------------------------------


def test_a_call_that_breaks_both_money_and_steps_reports_steps_and_holds_nothing():
    """Steps is the cheap, deterministic check; it runs before the money
    hold (invariant 5's order) and reports first when both would refuse."""
    runbound.init(
        max_steps=1,
        budget_usd=1.5,
        budget_admission=True,
        admission_output_tokens=1,  # $1 estimate per call, matching actual usage
        custom_prices={"m": (0.0, 1_000_000.0)},  # $1/output token
        on_anomaly="raise",
    )
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 1)))
    client.chat.completions.create(model="m", messages=[])  # turns: 0 -> 1, spends $1

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="m", messages=[])

    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 1  # the offending call never reached the fake
    assert_reserved_zero()  # no money hold was ever taken


def test_a_tool_that_breaks_both_posture_and_policy_reports_posture(plane):
    init_connected(on_anomaly="raise")

    @runbound.tool(effects={"financial"}, max_calls=1)
    def both_tool():
        return "done"

    both_tool()  # 1st call: allowed, uses up max_calls=1
    narrow("restricted")

    with pytest.raises(GuardrailTripped) as excinfo:
        both_tool()  # 2nd call: over max_calls AND denied by posture

    assert excinfo.value.anomaly.detector == "safe_mode"
    assert excinfo.value.decision.boundary == "posture"


# --- Decision on every refusal site --------------------------------------


def test_circuit_open_carries_a_decision():
    runbound.init(on_provider_failure="open", circuit_failure_threshold=1)
    client = runbound.wrap(FakeOpenAI())

    # A client with no base_url labels itself "openai@default" (see
    # wrappers.provider_label) — record_call must mark the same label's
    # circuit for the wrapped client's next call to see it open.
    runbound.record_call(
        "gpt-4o", 0, 0, provider="openai@default", error=TimeoutError("read timed out")
    )

    with pytest.raises(CircuitOpen) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.decision.boundary == "circuit"
    assert excinfo.value.decision.verdict == "deny"
    assert excinfo.value.provider == "openai@default"  # CircuitOpen.provider unchanged


def test_policy_violation_carries_a_decision_and_violation_unchanged():
    runbound.init(tool_policy={"deny": ["wire_money"]})

    @runbound.tool
    def wire_money():
        return "sent"

    with pytest.raises(PolicyViolation) as excinfo:
        wire_money()

    assert excinfo.value.decision.boundary == "policy"
    assert excinfo.value.decision.verdict == "deny"
    assert excinfo.value.violation.rule == "deny"  # PolicyViolation.violation unchanged
    assert excinfo.value.violation.tool == "wire_money"


def test_reservation_refusal_carries_a_decision():
    runbound.init(budget_usd=1.0, custom_prices={"m": (0.0, 1_000_000.0)})
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 1)))

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="m", messages=[], max_tokens=2)

    assert excinfo.value.decision.boundary == "money"
    assert excinfo.value.decision.detector == "budget"


def test_inflight_refusal_carries_a_decision():
    runbound.init(max_inflight_calls=1)
    client = runbound.wrap(FakeOpenAI())
    api._INFLIGHT["openai@default"] = 1  # simulate one call already running

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.decision.boundary == "concurrency"
    assert excinfo.value.decision.kind == "entry"


def test_fanout_refusal_carries_a_decision():
    runbound.init(max_active_sessions=1)
    api._ACTIVE = 5  # simulate several blocks already open

    with pytest.raises(GuardrailTripped) as excinfo:
        with runbound.session("a"):
            pass

    assert excinfo.value.anomaly.detector == "fanout"
    assert excinfo.value.decision.boundary == "blast_radius"


# --- Decision round-trips through as_dict/from_dict -----------------------


def test_a_refusals_decision_round_trips():
    runbound.init(max_steps=1, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    decision = excinfo.value.decision
    restored = Decision.from_dict(decision.as_dict())
    assert restored == decision


# --- max_steps under the envelope ----------------------------------------


def test_max_steps_refuses_before_the_fake_client_is_touched_and_latches():
    runbound.init(max_steps=1, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 1
    assert excinfo.value.anomaly.details["turns"] == 2  # the number the wall would report
    tripped = runbound.is_tripped()
    assert tripped is not None
    assert tripped.detector == "steps"


def test_max_steps_allows_exactly_at_the_limit():
    runbound.init(max_steps=2, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert client.completions.calls == 2
    assert runbound.is_tripped() is None


def test_envelope_off_lets_the_offending_call_reach_the_fake():
    runbound.init(max_steps=1, on_anomaly="raise", envelope=False)
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 2  # the offending call DID reach the fake


# --- max_session_seconds under the envelope -------------------------------


def test_max_session_seconds_refuses_at_the_door_with_a_patched_clock(clock):
    runbound.init(max_session_seconds=60.0, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    clock.advance(60.001)

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "timeout"
    assert excinfo.value.anomaly.details["scope"] == "run"
    assert client.completions.calls == 1
    assert runbound.is_tripped() is not None


def test_max_session_seconds_allows_exactly_at_the_limit(clock):
    runbound.init(max_session_seconds=60.0, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    clock.advance(60.0)
    client.chat.completions.create(model="gpt-4o", messages=[])
    assert client.completions.calls == 2


# --- max_total_tokens under the envelope, with a stated cap ---------------


def test_max_total_tokens_refuses_a_stated_cap_before_it_goes_out():
    runbound.init(max_total_tokens=10, on_anomaly="raise", custom_prices={"m": (0.0, 0.0)})
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 8)))
    client.chat.completions.create(model="m", messages=[])  # 8 tokens, uncapped

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="m", messages=[], max_tokens=5)  # 8+5 > 10

    assert excinfo.value.anomaly.detector == "budget"
    # rule="envelope" (not "tokens"): this is what keeps the door's anomaly
    # from deduping against a later post-call budget trip on the same
    # session (see test_both_the_door_and_a_later_wall_trip_reach_the_observer).
    assert excinfo.value.anomaly.details["rule"] == "envelope"
    assert excinfo.value.anomaly.details["reason"] == "tokens"
    assert client.completions.calls == 1
    # money/tokens denies never latch (CONTROLS §2.5)
    assert runbound.is_tripped() is None


def test_max_total_tokens_uncapped_calls_are_not_checked_at_the_door():
    """No stated cap: the door is silent, exactly like the money reservation."""
    runbound.init(max_total_tokens=10, on_anomaly="warn", custom_prices={"m": (0.0, 0.0)})
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 8)))
    client.chat.completions.create(model="m", messages=[])
    client.chat.completions.create(model="m", messages=[])  # crosses 10, uncapped -> wall only
    assert client.completions.calls == 2


# --- on_anomaly="warn" must not become enforcing (review finding #1) -----


@pytest.mark.parametrize(
    "envelope, want_results, want_calls",
    [
        (True, ["ok", "ok", "ok", "ok", "ok"], 5),
        (False, ["ok", "ok", "ok", "ok", "ok"], 5),
    ],
)
def test_on_anomaly_warn_never_becomes_enforcing_under_the_envelope(
    envelope, want_results, want_calls
):
    """The exact probe from review: ``max_steps=2, on_anomaly="warn"`` must
    behave identically whether the envelope is on or off — a customer who
    asked only to be told must never start seeing exceptions just because
    ``envelope`` defaults on. Before the fix, ``envelope=True`` here produced
    ``['ok', 'ok', 'GuardrailTripped', 'GuardrailTripped', 'GuardrailTripped']``
    with only 2 provider calls."""
    runbound.init(max_steps=2, on_anomaly="warn", envelope=envelope)
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))

    results = []
    for _ in range(5):
        try:
            client.chat.completions.create(model="gpt-4o", messages=[])
            results.append("ok")
        except GuardrailTripped:
            results.append("GuardrailTripped")

    assert results == want_results
    assert client.completions.calls == want_calls


def test_on_anomaly_warn_still_lets_the_call_through_and_logs():
    runbound.init(max_steps=1, on_anomaly="warn")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    # The would-be-refused call still goes through, and is never latched:
    # "warn" never latches anywhere in this codebase, at the door or the wall.
    client.chat.completions.create(model="gpt-4o", messages=[])

    assert client.completions.calls == 2
    assert runbound.is_tripped() is None


def test_on_anomaly_callback_invokes_the_handler_and_lets_the_call_through():
    """The wall's own callback path never stops the call it fired on, only a
    later one (the session is latched for that). The door, checked one call
    earlier, must behave the same way."""
    seen = []
    runbound.init(max_steps=1, on_anomaly="callback", callback=seen.append)
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    client.chat.completions.create(model="gpt-4o", messages=[])  # let through, callback invoked

    assert client.completions.calls == 2
    assert len(seen) == 1
    assert seen[0].detector == "steps"
    # steps latches even on the callback path, exactly as the wall's own
    # callback branch does (_react latches under "callback" too) — so a
    # *later* call sees the session stopped.
    assert runbound.is_tripped() is not None


def test_on_anomaly_raise_still_refuses_at_the_door():
    """The other half of the fix: "raise" must keep refusing before the
    fake is touched — only "warn"/"callback" changed."""
    runbound.init(max_steps=1, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 1


def test_max_actions_per_run_and_money_reservation_still_refuse_under_warn(plane):
    """The two doors review said to leave alone: a brand-new control
    (``max_actions_per_run``) and the money-reservation door both keep refusing
    unconditionally, ignoring ``on_anomaly``, exactly as before."""
    init_connected(on_anomaly="warn", loop_threshold=10)
    enable_max_actions_per_run(plane, 1)

    @runbound.tool
    def ping():
        return "pong"

    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()
    assert excinfo.value.anomaly.detector == "fanout"

    api._teardown_for_tests()
    runbound.init(budget_usd=1.0, on_anomaly="warn", custom_prices={"m": (0.0, 1_000_000.0)})
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 1)))
    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="m", messages=[], max_tokens=2)
    assert excinfo.value.anomaly.detector == "budget"


# --- rule="envelope" keeps a door refusal from swallowing the wall's own
# anomaly (review finding #2) ----------------------------------------------


def test_both_the_door_and_a_later_wall_trip_reach_the_observer():
    """The bug review found: without ``rule="envelope"`` distinguishing a
    door refusal from the wall's own post-call trip, both are detector
    ``budget`` with no other distinguishing field, so ``Engine._alert``'s
    dedup key collapses them into one and the second is silently dropped —
    even though the session genuinely crossed ``max_total_tokens`` and that
    is real news. Under ``on_anomaly="warn"`` neither call raises, so both
    anomalies must reach the observer for this to be visible at all.
    """
    runbound.init(max_total_tokens=500, on_anomaly="warn", custom_prices={"m": (0.0, 0.0)})
    seen: list[tuple] = []

    class Recorder:
        def on_event(self, session, event):
            pass

        def on_anomaly(self, session, anomaly, reacted):
            seen.append((anomaly.detector, anomaly.severity, anomaly.details.get("rule"), reacted))

    api._ENGINE.observers.append(Recorder())
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 400)))

    client.chat.completions.create(model="m", messages=[])  # 400 tokens, quiet
    # Capped at 400: the door projects 400 + 400 = 800 > 500 and, under
    # "warn", logs/alerts and lets the call through; the call then actually
    # uses 400 tokens (the fake's own number) and the post-call wall fires
    # for real on the same completed call.
    client.chat.completions.create(model="m", messages=[], max_tokens=400)

    assert client.completions.calls == 2
    detectors_and_rules = [(d, rule) for d, sev, rule, r in seen]
    assert ("budget", "envelope") in detectors_and_rules  # the door's own projection
    assert ("budget", None) in detectors_and_rules  # the wall's real crossing
    assert len(seen) == 2  # neither swallowed the other


# --- reacted == "door" for a refusal at the door (review finding #3) ------


def test_a_door_refusal_reports_reacted_door_not_raise():
    runbound.init(max_steps=1, on_anomaly="raise")
    seen: list[str] = []

    class Recorder:
        def on_event(self, session, event):
            pass

        def on_anomaly(self, session, anomaly, reacted):
            seen.append(reacted)

    api._ENGINE.observers.append(Recorder())
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert seen == ["door"]


def test_max_actions_per_run_also_reports_reacted_door(plane):
    init_connected(on_anomaly="raise", loop_threshold=10)
    enable_max_actions_per_run(plane, 1)
    seen: list[str] = []

    class Recorder:
        def on_event(self, session, event):
            pass

        def on_anomaly(self, session, anomaly, reacted):
            seen.append(reacted)

    api._ENGINE.observers.append(Recorder())

    @runbound.tool
    def ping():
        return "pong"

    ping()
    with pytest.raises(GuardrailTripped):
        ping()

    assert seen == ["door"]


# --- max_actions_per_run --------------------------------------------------


def test_max_actions_per_run_refuses_the_nplus1th_before_the_body(plane):
    # A high loop_threshold: three identical calls to the same tool would
    # otherwise trip the loop detector first, which is not what this test is
    # about.
    init_connected(on_anomaly="raise", loop_threshold=10)
    enable_max_actions_per_run(plane, 2)
    calls: list[int] = []

    @runbound.tool
    def ping():
        calls.append(1)
        return "pong"

    ping()
    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    assert excinfo.value.anomaly.detector == "fanout"
    assert excinfo.value.anomaly.details["rule"] == "actions"
    assert calls == [1, 1]  # the third call's body never ran
    # A refused attempt no longer counts as executed -- only the two
    # that actually ran do. (This used to read 3: the tool_call event's
    # own recording bumped executed_actions before admission ever ran, so
    # the refused third attempt still counted as "executed".)
    state = runbound.current_session()
    assert state.executed_actions == 2
    assert state.admitted_actions == 2
    assert state.refused_actions == 1
    assert runbound.is_tripped() is not None  # blast radius latches


def test_the_action_cap_message_agrees_with_the_counters_it_describes():
    """The customer-facing sentence must say what actually ran, not
    ``evaluation["used"]`` read as if it were a count of completed actions --
    ``used`` is "the count this attempt would bring it to" (the same
    "count already spent" convention ``admission.steps`` uses, and its own
    door message already gets this right: "the next call would be step N",
    never "N steps taken"). A cap of 3 with 3 actions actually executed and
    a 4th refused must never claim "4 taken"."""
    runbound.init(on_anomaly="raise", max_actions_per_run=3, loop_threshold=20)

    @runbound.tool
    def ping():
        return "pong"

    with runbound.session("capmsg"):
        ping()
        ping()
        ping()
        with pytest.raises(GuardrailTripped) as fourth:
            ping()
        with pytest.raises(GuardrailTripped) as fifth:
            ping()

    for excinfo in (fourth, fifth):
        message = excinfo.value.anomaly.message
        assert "4 taken" not in message, message
        assert "taken" not in message, message  # no claim of a completed count at all
        assert "action 4" in message  # names the attempt explicitly, like the steps door does
        assert "limit 3" in message

    # The cap latches (one of the deterministic envelope denies that does):
    # the 4th attempt is refused through admission and counted; the
    # 5th never reaches admission at all -- the session is already latched,
    # so it is re-served the stored anomaly by the latch path instead, which
    # is not a fresh "refused" attempt.
    status = runbound.session_status("capmsg")["actions"]
    assert status == {"requested": 0, "admitted": 3, "executed": 3, "refused": 1}


def test_five_attempts_two_refused_report_all_four_action_counters():
    """Five tool attempts with two refused reads
    requested 5, admitted 3, executed 3, refused 2 -- never counting a
    refused attempt itself as admitted or executed. ``max_calls`` (an
    ordinary tool-policy rule, ``on_violation="block"``, which latches
    nothing) is what lets all five attempts run through admission
    independently; ``max_actions_per_run``'s own cap *does* latch on refusal
    (see ``test_max_actions_per_run_refuses_the_nplus1th_before_the_body``
    above), so a chain of repeated attempts against it stops being judged by
    admission at all after the first refusal -- the session's existing latch
    answers every attempt after that without this call ever reaching
    ``_admit_action`` again. Both are still "the action cap that measures
    executed", just two different rules: this test's own point is the
    counters, not which rule enforces the ceiling.
    """
    runbound.init(on_anomaly="raise", loop_threshold=10)

    @runbound.tool(max_calls=3)
    def ping():
        return "pong"

    refused = 0
    for i in range(5):
        api._HOOKS.tool_request("ping", f"req:{i}")  # the model asking, each time
        try:
            ping()
        except GuardrailTripped:
            refused += 1

    assert refused == 2
    state = runbound.current_session()
    assert state.requested_actions == 5
    assert state.admitted_actions == 3
    assert state.executed_actions == 3
    assert state.refused_actions == 2
    assert state.tool_calls["ping"] == 5  # tool_calls() still counts every attempt

    envelope = runbound.envelope()
    assert envelope["execution"]["actions_requested"] == 5
    assert envelope["execution"]["actions_admitted"] == 3
    assert envelope["execution"]["actions_executed"] == 3
    assert envelope["execution"]["actions_refused"] == 2

    # session_status() only answers for a *keyed* session -- check the same
    # four counters there too.
    with runbound.session("acct:1"):
        for i in range(5):
            api._HOOKS.tool_request("ping", f"req:keyed:{i}")
            try:
                ping()
            except GuardrailTripped:
                pass
    keyed_status = runbound.session_status("acct:1")
    assert keyed_status["actions"] == {
        "requested": 5, "admitted": 3, "executed": 3, "refused": 2,
    }


def test_max_actions_per_run_cap_refuses_the_fourth_executed_action(plane):
    """The other half of the same acceptance line, via the envelope's own
    cap rather than a tool-policy rule: a cap of 3 admits and executes the
    first three attempts and refuses the fourth."""
    init_connected(on_anomaly="raise", loop_threshold=10)
    enable_max_actions_per_run(plane, 3)

    @runbound.tool
    def ping():
        return "pong"

    ping()
    ping()
    ping()
    with pytest.raises(GuardrailTripped) as excinfo:
        ping()

    assert excinfo.value.anomaly.details["rule"] == "actions"
    state = runbound.current_session()
    assert state.admitted_actions == 3
    assert state.executed_actions == 3
    assert state.refused_actions == 1


def test_max_actions_per_run_allows_exactly_at_the_limit(plane):
    init_connected(on_anomaly="raise")
    enable_max_actions_per_run(plane, 2)

    @runbound.tool
    def ping():
        return "pong"

    ping()
    ping()
    assert runbound.current_session().executed_actions == 2
    assert runbound.is_tripped() is None


def test_requested_actions_is_counted_alongside_executed_actions():
    runbound.init()
    api._HOOKS.tool_request("search", "req:abc")
    api._HOOKS.tool_request("search", "req:def")

    @runbound.tool
    def search():
        return "done"

    search()

    state = runbound.current_session()
    assert state.requested_actions == 2
    assert state.executed_actions == 1


def test_max_actions_per_run_refuses_the_async_wrapper_too(plane):
    init_connected(on_anomaly="raise", loop_threshold=10)
    enable_max_actions_per_run(plane, 1)
    calls: list[int] = []

    @runbound.tool
    async def ping():
        calls.append(1)
        return "pong"

    async def drive():
        await ping()
        with pytest.raises(GuardrailTripped) as excinfo:
            await ping()
        return excinfo

    excinfo = asyncio.run(drive())
    assert excinfo.value.anomaly.detector == "fanout"
    assert calls == [1]


# --- refusal profiles apply to an envelope deny ---------------------------


def test_a_refusal_profile_applies_to_an_envelope_deny():
    runbound.init(
        max_steps=1,
        on_anomaly="raise",
        refusals={"steps": {"status": 418, "message": "no more steps for you"}},
    )
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.refusal.status == 418
    assert excinfo.value.refusal.message == "no more steps for you"


# --- fail-open -------------------------------------------------------------


def test_a_broken_stage_is_logged_and_the_call_proceeds(monkeypatch, caplog):
    """A broken door stage lets the call *through the door* (fail-open) — it
    does not disable the post-call wall behind it, which still does its job,
    exactly as it always would have without the envelope at all."""
    runbound.init(max_steps=1, on_anomaly="raise")

    def _broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(admission, "steps", _broken)
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))

    with caplog.at_level(logging.WARNING, logger="runbound"):
        client.chat.completions.create(model="gpt-4o", messages=[])
        with pytest.raises(GuardrailTripped) as excinfo:
            client.chat.completions.create(model="gpt-4o", messages=[])

    # The door did not block it: the fake was reached (fail-open at the
    # door), and the trip that did happen came from the post-call wall.
    assert client.completions.calls == 2
    assert excinfo.value.anomaly.detector == "steps"
    assert any("could not check the step limit" in r.message for r in caplog.records)


def test_a_broken_observer_never_swallows_a_refusal():
    runbound.init(max_steps=1, on_anomaly="raise")

    class BrokenObserver:
        def on_event(self, session, event):
            raise RuntimeError("boom")

        def on_anomaly(self, session, anomaly, reacted):
            raise RuntimeError("boom")

    api._ENGINE.observers.append(BrokenObserver())
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])

    assert excinfo.value.anomaly.detector == "steps"


# --- the envelope object -- also rides the heartbeat -----------------------


def test_envelope_is_on_the_public_surface():
    assert hasattr(runbound, "envelope")
    assert "envelope" in runbound.__all__


def test_envelope_is_none_before_init():
    assert runbound.envelope() is None


def test_envelope_agrees_with_budget_and_session_status_to_the_cent(plane):
    init_connected(
        budget_usd=5.0,
        max_steps=10,
        max_session_seconds=100.0,
        custom_prices={"m": (0.0, 1_000_000.0)},
    )
    enable_max_actions_per_run(plane, 4)
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 2)))
    with runbound.session("acme"):
        client.chat.completions.create(model="m", messages=[])

        env = runbound.envelope("acme")
        budget_view = runbound.budget("acme")

        assert env is not None
        assert env["budget"]["remaining"] == pytest.approx(budget_view.remaining)
        assert env["budget"]["reserved"] == pytest.approx(budget_view.reserved)
        assert env["execution"]["steps_remaining"] == 9
        assert env["posture"] == "full"


def test_envelope_reports_capabilities_under_a_posture(plane):
    init_connected()
    narrow("restricted")

    env = runbound.envelope()

    assert env["posture"] == "restricted"
    assert env["capabilities"]["read"] == "allow"
    assert env["capabilities"]["financial"] == "deny"
    assert env["capabilities"]["external"] == "deny"


def test_envelope_none_for_an_unknown_key():
    runbound.init()
    assert runbound.envelope("nobody-home") is None


# --- anomaly_to_wire scrubs the decision sub-dict -------------------------


def test_anomaly_to_wire_scrubs_the_decision_sub_dict():
    from runbound.events import Anomaly
    from runbound.plane_types import DETAIL_STRING_MAX, anomaly_to_wire

    decision = Decision(
        verdict="deny",
        kind="model_call",
        boundary="steps",
        detector="steps",
        reason="x" * (DETAIL_STRING_MAX + 50),
        evaluation={"limit": 1, "used": 2},
    )
    anomaly = Anomaly(
        detector="steps",
        severity="critical",
        message="boundary hit",
        details={"decision": decision.as_dict()},
    )

    wire = anomaly_to_wire(anomaly, "raise", None, "TS")

    assert wire.details["decision"]["verdict"] == "deny"
    assert wire.details["decision"]["evaluation"] == {"limit": 1, "used": 2}
    assert len(wire.details["decision"]["reason"]) == DETAIL_STRING_MAX
