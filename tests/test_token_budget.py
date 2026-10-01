"""A token budget, enforced in-process like the dollar one.

``budget_tokens`` is the Controls name for the key-level token limit the code
states as ``max_total_tokens``: the plane can tighten it, never loosen it. It is
enforced three ways, each proved here:

* after the call, by the budget wall (which reads the effective limit);
* at the door by the envelope's own stage (a stated cap, no hold);
* at the door by a reservation, in the same stage and the same ``session.lock``
  section as the dollar one: the worst case (``ceil(chars / 4)`` of the request
  plus its output cap) is held as ``"tokens"`` until the call closes out.

The hold is this worker's own: the fleet's settled tokens arrive at entry
(``tokens_offset``), so admission is per worker and the fleet total is applied
at entry. :func:`test_two_workers_can_each_admit_a_call_that_together_cross_it`
pins that, deliberately.
"""

import logging

import pytest

import runbound
from runbound import api, pricing
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.exceptions import GuardrailTripped
from runbound.state import CompositeHold, SessionState

from test_controls_engine import FakeControlsPlane
from test_reservation_ledger import Completions, assert_reserved_zero, openai_client

MODEL = "tok-1"
#: Unpriced on purpose (no entry): a token worst case needs no price.
PRICES = {}
SHORT = [{"role": "user", "content": "x" * 400}]  # 400 chars -> 100 input tokens
CAP = 50  # the stated output cap
WORST = pricing.estimated_tokens(pricing.request_chars({"messages": SHORT})) + CAP  # 150


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def request(cap: int | None = CAP) -> dict:
    body = {"model": MODEL, "messages": SHORT}
    if cap is not None:
        body["max_tokens"] = cap
    return body


def engine(plane_body: dict | None = None, **config) -> Engine:
    config.setdefault("on_anomaly", "raise")
    if config["on_anomaly"] == "callback":
        config.setdefault("callback", lambda anomaly: None)
    cfg = GuardrailConfig(**config)
    cfg.validate()
    return Engine(cfg, shared=FakeControlsPlane(plane_body))


def admit(eng: Engine, session: SessionState, cap: int | None = CAP):
    return eng.admit(session, "openai", MODEL, request(cap))


def test_the_worst_case_is_the_input_estimate_plus_the_stated_cap():
    assert WORST == 150
    assert pricing.admission_worst_case_tokens(CAP, 400) == 150
    assert pricing.admission_worst_case_tokens(-5, 400) == 100  # a nonsense cap is no output, never negative


# --- where the limit comes from -------------------------------------------------------------


def test_a_plane_stated_budget_tokens_is_the_effective_key_limit():
    plane = {"limits": {"org": {"budget_tokens": 500_000}, "service": {"budget_tokens": 200_000}}}
    eng = engine(plane)

    assert eng._limit("budget_tokens") == 200_000
    assert eng._effective_config().max_total_tokens == 200_000


def test_the_plane_can_tighten_the_codes_max_total_tokens_and_never_loosen_it():
    tighter = engine({"limits": {"org": {"budget_tokens": 100}}}, max_total_tokens=1_000)
    looser = engine({"limits": {"org": {"budget_tokens": 9_000}}}, max_total_tokens=1_000)

    assert tighter._effective_config().max_total_tokens == 100
    assert looser._effective_config().max_total_tokens == 1_000
    assert any(v["path"] == "limits.budget_tokens" for v in looser.controls_refusals())


def test_no_plane_and_no_code_limit_is_no_limit():
    eng = engine()

    assert eng._limit("budget_tokens") is None
    assert admit(eng, SessionState("s")) is None


# --- the door reservation -------------------------------------------------------------------


def test_a_call_whose_worst_case_is_exactly_what_remains_is_admitted_and_held():
    eng = engine(max_total_tokens=WORST)
    session = SessionState("s")

    hold = admit(eng, session)

    assert isinstance(hold, CompositeHold)
    assert session.reserved == {"tokens": WORST}


def test_one_token_over_is_refused_at_the_door_with_the_tokens_boundary():
    eng = engine(max_total_tokens=WORST - 1)
    session = SessionState("s")

    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, session)

    decision = tripped.value.decision
    assert (decision.verdict, decision.boundary, decision.detector) == ("deny", "tokens", "budget")
    assert decision.level == "key"
    assert decision.evaluation["remaining"] == WORST - 1
    assert decision.evaluation["estimate"] == WORST
    assert (decision.evaluation["limit"], decision.evaluation["reserved"]) == (WORST - 1, 0)
    assert decision.evaluation["provider_called"] is False
    assert tripped.value.anomaly.details["rule"] == "reservation"
    assert session.reserved == {}  # a refusal takes nothing
    assert session.tripped_by is None  # and, like the money estimate, latches nothing


def test_a_plane_stated_limit_refuses_at_the_door_too():
    eng = engine({"limits": {"service": {"budget_tokens": WORST - 1}}})

    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, SessionState("s"))

    assert tripped.value.decision.evaluation["limit"] == WORST - 1


def test_settled_tokens_and_the_fleets_count_against_the_limit():
    eng = engine(max_total_tokens=1_000)
    session = SessionState("s")
    session.total_tokens = 400
    session.tokens_offset = 451  # what the rest of the fleet settled, applied at entry

    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, session)  # 400 + 451 + 150 = 1001 > 1000

    assert tripped.value.decision.evaluation["remaining"] == 1000 - 400 - 451
    session.tokens_offset = 450
    assert admit(eng, session) is not None  # 400 + 450 + 150 == 1000: exactly at the limit


def test_a_second_concurrent_call_is_compared_against_what_the_first_holds():
    eng = engine(max_total_tokens=WORST * 2 - 1)
    session = SessionState("s")

    first = admit(eng, session)
    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, session)

    assert tripped.value.decision.evaluation["reserved"] == WORST
    first.release()
    assert session.reserved == {}
    assert admit(eng, session) is not None  # room again once the first gave it back


def test_reserved_more_than_used_leaves_exactly_what_was_used_settled():
    eng = engine(max_total_tokens=300)
    session = SessionState("s")

    hold = admit(eng, session)  # reserves 150
    session.total_tokens += 80  # the call actually used 80
    hold.settle(0.0)
    assert session.reserved == {} and session.total_tokens == 80

    second = admit(eng, session)  # 80 + 150 <= 300: holds 150 again
    assert second is not None
    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, session)  # 80 + 150 held + 150 > 300
    assert tripped.value.decision.evaluation["remaining"] == 300 - 80 - WORST


def test_an_uncapped_call_is_reserved_at_the_assumed_output_cap_unless_capped_mode():
    assumed = engine(max_total_tokens=10_000, budget_admission=True, admission_output_tokens=500)
    session = SessionState("s")
    assert admit(assumed, session, cap=None) is not None
    assert session.reserved == {"tokens": 100 + 500}

    capped = engine(max_total_tokens=1, budget_admission="capped")
    assert admit(capped, SessionState("t"), cap=None) is None  # nothing exact to reserve: the wall is the check


def test_an_unpriced_model_is_still_reserved_in_tokens():
    eng = engine(max_total_tokens=WORST - 1)

    with pytest.raises(GuardrailTripped):
        eng.admit(SessionState("s"), "openai", "a-model-nobody-priced", request())


def test_a_dollar_refusal_leaves_no_token_hold_behind_and_the_reverse():
    prices = {MODEL: (0.0, 1_000_000.0)}  # $1 per output token: the cap of 50 is $50
    money_first = engine(max_total_tokens=10_000, budget_usd=1.0, custom_prices=prices)
    session = SessionState("s")
    with pytest.raises(GuardrailTripped) as tripped:
        admit(money_first, session)
    assert tripped.value.decision.boundary == "money" and session.reserved == {}

    tokens_first = engine(max_total_tokens=WORST - 1, budget_usd=1_000.0, custom_prices=prices)
    with pytest.raises(GuardrailTripped) as tripped:
        admit(tokens_first, session)
    assert tripped.value.decision.boundary == "tokens" and session.reserved == {}


def test_both_limits_are_held_together_and_given_back_together():
    prices = {MODEL: (0.0, 1.0)}
    eng = engine(max_total_tokens=10_000, budget_usd=100.0, custom_prices=prices)
    session = SessionState("s")

    hold = admit(eng, session)

    assert set(session.reserved) == {"usd", "tokens"}
    hold.release()
    assert session.reserved == {}


def test_a_dollar_only_budget_still_returns_the_plain_hold():
    eng = engine(budget_usd=100.0, custom_prices={MODEL: (0.0, 1.0)})

    assert not isinstance(admit(eng, SessionState("s")), CompositeHold)


def test_the_run_token_budget_binds_when_it_is_the_tighter_one():
    eng = engine(max_total_tokens=10_000, run_max_total_tokens=WORST - 1)
    session = SessionState("s")

    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, session)

    assert tripped.value.decision.level == "run"
    assert tripped.value.anomaly.details["budget_tokens"] == WORST - 1


def test_the_door_rolls_the_budget_window_before_it_reads_the_tokens():
    # budget_window governs the key's counters (config requires a dollar budget to name one).
    eng = engine(max_total_tokens=WORST, budget_usd=1e9, budget_window=3600, custom_prices={MODEL: (0.0, 0.0)})
    session = SessionState("s")
    session.total_tokens = 1_000  # spent in a window that has since rolled
    rolled = []

    def roll(window):
        rolled.append(window)
        session.total_tokens = 0  # what a real roll does to the key's counters
        return True

    session.roll_budget_window = roll

    assert admit(eng, session) is not None  # 0 + WORST <= WORST: only true if the roll came first
    assert rolled and set(rolled) == {3600}  # the envelope stage and the reservation each roll first


# --- the fleet: admission is per worker; the fleet total is applied at entry ----------------


def test_two_workers_can_each_admit_a_call_that_together_cross_it():
    """The pinned gap. Each worker holds only its own reservation, so two calls in
    flight on two workers are both admitted; nothing has settled yet for either to
    see. The wall catches the crossing after the calls, and the next entry applies
    the fleet's settled total."""
    limit = WORST + WORST // 2  # room for one worst case, not two
    a, b = engine(max_total_tokens=limit), engine(max_total_tokens=limit)
    sa, sb = SessionState("k"), SessionState("k")

    hold_a, hold_b = admit(a, sa), admit(b, sb)

    assert hold_a is not None and hold_b is not None  # both admitted: 2 * WORST > limit
    sa.total_tokens = WORST  # both calls then settle on their own workers
    sb.total_tokens = WORST
    sb.tokens_offset = WORST  # and the next entry on B sees A's settled tokens
    hold_a.settle(0.0), hold_b.settle(0.0)
    with pytest.raises(GuardrailTripped):
        admit(b, sb)


# --- the wall: a plane-stated limit trips the budget detector after the call ----------------


def test_a_plane_stated_token_budget_trips_the_wall_after_the_call(caplog):
    runbound.init(on_anomaly="raise", budget_admission=False, custom_prices={MODEL: (0.0, 0.0)})
    api._ENGINE.shared = FakeControlsPlane({"limits": {"org": {"budget_tokens": 100}}})
    client = openai_client(Completions(tokens_out=500))

    with pytest.raises(GuardrailTripped) as tripped:
        client.chat.completions.create(model=MODEL, messages=SHORT)

    assert tripped.value.anomaly.detector == "budget"
    assert tripped.value.decision.boundary == "tokens"
    assert tripped.value.decision.evaluation["limit"] == 100


# --- the hold is given back on every close-out path -----------------------------------------


def test_the_token_hold_is_released_on_success_error_and_refusal():
    runbound.init(on_anomaly="raise", max_total_tokens=10_000, custom_prices=PRICES)
    ok = openai_client(Completions(tokens_out=20))
    with runbound.session("k"):
        ok.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=CAP)
    assert_reserved_zero()

    boom = openai_client(Completions(error=RuntimeError("provider down")))
    with runbound.session("k"), pytest.raises(RuntimeError):
        boom.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=CAP)
    assert_reserved_zero()

    api._teardown_for_tests()
    runbound.init(on_anomaly="raise", max_total_tokens=WORST - 1, custom_prices=PRICES)
    refused = openai_client(Completions())
    with runbound.session("k"), pytest.raises(GuardrailTripped):
        refused.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=CAP)
    assert_reserved_zero()
    assert refused.chat.completions.calls == 0 if hasattr(refused.chat.completions, "calls") else True


def test_a_malformed_plane_token_budget_is_ignored_and_the_code_limit_stands(caplog):
    eng = engine({"limits": {"org": {"budget_tokens": "lots"}}}, max_total_tokens=WORST - 1)

    with caplog.at_level(logging.WARNING, logger="runbound"), pytest.raises(GuardrailTripped):
        admit(eng, SessionState("s"))

    assert eng._limit("budget_tokens") == WORST - 1


# --- the reservation follows on_anomaly, like the envelope's own token stage -----------------


class Recorder:
    """An observer: what reached it, and as what."""

    def __init__(self):
        self.seen = []

    def on_event(self, session, event):
        pass

    def on_anomaly(self, session, anomaly, reacted):
        self.seen.append((anomaly.details.get("rule"), anomaly.severity, reacted))


def exceeded(on_anomaly: str, **extra):
    """An engine-backed client whose capped call cannot fit: worst case WORST > limit."""
    callbacks = []
    kwargs = dict(extra)
    if on_anomaly == "callback":
        kwargs["callback"] = callbacks.append
    runbound.init(on_anomaly=on_anomaly, max_total_tokens=WORST - 1, envelope=False,
                  custom_prices={MODEL: (0.0, 0.0)}, **kwargs)
    recorder = Recorder()
    api._ENGINE.observers.append(recorder)
    provider = Completions(tokens_out=1)
    return openai_client(provider), provider, recorder, callbacks


def call(client):
    with runbound.session("k"):
        return client.chat.completions.create(model=MODEL, messages=SHORT, max_tokens=CAP)


def test_under_raise_the_reservation_refuses_at_the_door():
    client, provider, recorder, _ = exceeded("raise")

    with pytest.raises(GuardrailTripped) as tripped:
        call(client)

    assert tripped.value.decision.boundary == "tokens"
    assert provider.calls == 0  # nothing went out
    assert recorder.seen == [("reservation", "critical", "door")]  # the record and the notice
    assert_reserved_zero()


@pytest.mark.parametrize("mode", ["warn", "callback"])
def test_under_warn_and_callback_the_reservation_records_and_notifies_but_does_not_refuse(mode):
    client, provider, recorder, callbacks = exceeded(mode)

    call(client)  # does not raise

    assert provider.calls == 1  # the call went out
    assert recorder.seen == [("reservation", "critical", "warn")]  # filed and notified, as a warn
    assert callbacks == []  # the door never invokes the callback; only a real crossing does
    assert_reserved_zero()  # and nothing is left held once the call closed out


@pytest.mark.parametrize("mode", ["warn", "callback"])
def test_a_let_through_call_holds_its_dollars_and_never_tokens(mode):
    prices = {MODEL: (0.0, 1.0)}
    eng = engine(max_total_tokens=WORST - 1, budget_usd=100.0, custom_prices=prices, on_anomaly=mode)
    session = SessionState("s")

    hold = admit(eng, session)

    assert not isinstance(hold, CompositeHold) and hold.resource == "usd"
    assert set(session.reserved) == {"usd"}  # the dollars it was admitted on; no token hold
    hold.release()
    assert session.reserved == {}


def test_under_warn_with_only_tokens_limited_no_hold_is_left_at_all():
    eng = engine(max_total_tokens=WORST - 1, on_anomaly="warn")
    session = SessionState("s")

    assert admit(eng, session) is None
    assert session.reserved == {} and session.tripped_by is None  # never latched


def test_a_plane_stop_on_the_budget_detector_refuses_even_under_callback():
    plane = {"detectors": {"budget": {"action": "stop", "mode": "enforce"}}}
    eng = engine(plane, max_total_tokens=WORST - 1, on_anomaly="callback")

    with pytest.raises(GuardrailTripped):
        admit(eng, SessionState("s"))  # the plane's stop applies at the door, as at every door stage


def test_the_envelope_stage_and_the_reservation_say_one_thing_not_two():
    runbound.init(on_anomaly="warn", max_total_tokens=120, custom_prices={MODEL: (0.0, 0.0)})  # envelope on
    recorder = Recorder()
    api._ENGINE.observers.append(recorder)
    client = openai_client(Completions(tokens_out=100))

    call(client)  # 150 worst case > 120: the reservation speaks (the envelope's 0 + 50 fits)
    call(client)  # 100 used + 50 cap > 120: the envelope speaks, and the reservation does not repeat it

    assert [rule for rule, _sev, _r in recorder.seen][:2] == ["reservation", "envelope"]
    assert recorder.seen.count(("reservation", "critical", "warn")) == 1
    assert_reserved_zero()


def test_under_raise_a_token_refusal_gives_back_the_dollars_it_had_taken():
    eng = engine(max_total_tokens=WORST - 1, budget_usd=100.0, custom_prices={MODEL: (0.0, 1.0)}, on_anomaly="raise")
    session = SessionState("s")

    with pytest.raises(GuardrailTripped) as tripped:
        admit(eng, session)

    assert tripped.value.decision.boundary == "tokens"
    assert session.reserved == {}  # no orphan dollar hold behind the refusal
