"""Tests for customer-set refusal responses (Wave 23, T56).

Covers, in order: the ``Refusal`` value object (headers, body), safe message
formatting, the five-layer precedence table, ``retry_after_s`` resolution
(pure and end-to-end through a real latch), the three ``GuardrailTripped``
subclasses' ``.refusal``, ``init(refusals=...)`` validation, the
``_policy_envelope``/``_fetch_policy`` wire seam, ``coverage()["refusals"]``,
``reset()`` clearing the remote profile, and the fail-open golden rule.
"""

import pytest

import runbound
from runbound import api, responses
from runbound import engine as engine_module
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly
from runbound.exceptions import CircuitOpen, GuardrailTripped, PolicyViolation
from runbound.policy import Violation
from runbound.responses import BUILTIN, Refusal, refusal_for, set_remote
from runbound.shared import RemoteState, _policy_envelope
from runbound.state import SessionState


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK.

    ``_teardown_for_tests`` also clears the remote refusal profile, so no
    test here can leak one into the next.
    """
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class FakeClock:
    """Stands in for ``time``; moved by hand, like the engine's own tests use."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    """A single fake clock installed as both ``responses.time`` and the
    engine's, so a latch stamped by one is read consistently by the other.
    """
    fake = FakeClock()
    monkeypatch.setattr(responses, "time", fake)
    monkeypatch.setattr(engine_module, "time", fake)
    return fake


def _engine(**config_kwargs) -> Engine:
    config = GuardrailConfig(**config_kwargs)
    config.validate()
    return Engine(config)


def _install(engine: Engine, state: SessionState, *, key: str | None = None) -> None:
    """Register ``state`` where :mod:`runbound.responses` will look for it."""
    with api._LOCK:
        api._ENGINE = engine
        if key is not None:
            api._REGISTRY[key] = state
        else:
            api._SESSION = state


# --- Refusal: headers and body ------------------------------------------------


def test_headers_are_empty_without_a_retry_after():
    r = Refusal(status=429, message="m", detector="d", retry_after_s=None, source="default")
    assert r.headers == {}


def test_headers_round_up_to_the_ceiling_int():
    r = Refusal(status=429, message="m", detector="d", retry_after_s=4.2, source="default")
    assert r.headers == {"Retry-After": "5"}


def test_headers_accept_an_exact_zero():
    r = Refusal(status=429, message="m", detector="d", retry_after_s=0.0, source="default")
    assert r.headers == {"Retry-After": "0"}


def test_body_shape():
    r = Refusal(status=429, message="msg", detector="budget", retry_after_s=12.0, source="local")
    assert r.body() == {
        "refused": True,
        "detector": "budget",
        "message": "msg",
        "retry_after_s": 12.0,
    }


def test_body_carries_a_none_retry_after_as_none():
    r = Refusal(status=503, message="m", detector="circuit", retry_after_s=None, source="default")
    assert r.body()["retry_after_s"] is None


# --- safe message formatting ---------------------------------------------------


def test_format_substitutes_detector_and_retry_after():
    msg = responses._safe_format(
        "Blocked ({detector}); retry in {retry_after_s}s", detector="budget", retry_after_s=5.0
    )
    assert msg == "Blocked (budget); retry in 5.0s"


def test_format_leaves_an_unknown_placeholder_verbatim():
    msg = responses._safe_format("Hello {nonsense}", detector="x", retry_after_s=None)
    assert msg == "Hello {nonsense}"


def test_format_never_raises_on_a_stray_brace():
    msg = responses._safe_format("Hello {", detector="x", retry_after_s=None)
    assert msg == "Hello {"  # unformatted, but not an exception


def test_format_never_raises_on_unbalanced_braces_mid_string():
    msg = responses._safe_format("a { b {detector} c", detector="x", retry_after_s=None)
    assert isinstance(msg, str)


# --- precedence: all five layers ----------------------------------------------


def test_precedence_builtin_when_nothing_is_configured():
    r = refusal_for(Anomaly("policy", "critical", "no", {}))
    assert r.status == 403
    assert r.message == "That action isn't allowed."
    assert r.source == "default"


def test_precedence_unconfigured_detector_falls_through_to_builtin_default():
    r = refusal_for(Anomaly("budget", "critical", "no money", {}))
    assert r.status == BUILTIN["default"]["status"]
    assert r.message == BUILTIN["default"]["message"]
    assert r.source == "default"


def test_precedence_local_default_beats_builtin():
    runbound.init(refusals={"default": {"status": 418, "message": "local default"}})
    r = refusal_for(Anomaly("loop", "critical", "looping", {}))
    assert r.status == 418
    assert r.message == "local default"
    assert r.source == "local"


def test_precedence_local_per_detector_beats_local_default():
    runbound.init(refusals={"default": {"status": 418}, "loop": {"status": 451}})
    r = refusal_for(Anomaly("loop", "critical", "looping", {}))
    assert r.status == 451
    assert r.source == "local"


def test_precedence_remote_default_beats_local_per_detector():
    runbound.init(refusals={"loop": {"status": 451}})
    set_remote({"default": {"status": 200}})
    r = refusal_for(Anomaly("loop", "critical", "looping", {}))
    assert r.status == 200
    assert r.source == "plane"


def test_precedence_remote_per_detector_beats_remote_default():
    set_remote({"default": {"status": 200}, "loop": {"status": 201}})
    r = refusal_for(Anomaly("loop", "critical", "looping", {}))
    assert r.status == 201
    assert r.source == "plane"


def test_fields_resolve_independently_across_sources():
    """Remote overriding only 'status' must not blank out local's 'message'."""
    runbound.init(refusals={"policy": {"message": "local msg for policy"}})
    set_remote({"policy": {"status": 200}})
    r = refusal_for(Anomaly("policy", "critical", "x", {}))
    assert r.status == 200
    assert r.message == "local msg for policy"
    assert r.source == "plane"


def test_a_malformed_remote_entry_is_ignored_field_by_field():
    """An out-of-range status in the plane profile falls through, not crashes."""
    runbound.init(refusals={"policy": {"status": 451}})
    set_remote({"policy": {"status": 9999, "message": "x" * 600}})
    r = refusal_for(Anomaly("policy", "critical", "x", {}))
    assert r.status == 451  # local, since remote's status/message were invalid
    assert r.source == "local"


# --- retry_after_s: pure logic against api's registry --------------------------


def test_retry_after_s_is_none_when_the_anomaly_names_no_session():
    anomaly = Anomaly("circuit", "critical", "circuit open", {"provider": "openai"})
    assert responses._retry_after_s(anomaly) is None


def test_retry_after_s_is_none_before_init():
    anomaly = Anomaly("budget", "critical", "x", {"key": "user:abuser"})
    assert responses._retry_after_s(anomaly) is None


def test_retry_after_s_is_none_when_the_session_cannot_be_found():
    runbound.init()
    anomaly = Anomaly("budget", "critical", "x", {"key": "ghost"})
    assert responses._retry_after_s(anomaly) is None


def test_retry_after_s_is_none_for_a_session_that_is_not_latched():
    engine = _engine()
    state = SessionState("s1", key="user:fine")
    _install(engine, state, key="user:fine")
    anomaly = Anomaly("budget", "critical", "x", {"key": "user:fine"})
    assert responses._retry_after_s(anomaly) is None


def test_retry_after_s_is_none_for_a_fresh_trip_with_no_ttl_configured(clock):
    """No ``latch_ttl_seconds`` and no cooldown override: the latch is
    permanent, so there is nothing to count down — the documented ``None``.
    """
    engine = _engine(latch_ttl_seconds=None)
    state = SessionState("s1", key="user:fresh")
    state.tripped_by = Anomaly("budget", "critical", "over budget", {})
    state.tripped_at = clock.now
    _install(engine, state, key="user:fresh")

    anomaly = Anomaly("budget", "critical", "over budget", {"key": "user:fresh"})
    assert responses._retry_after_s(anomaly) is None


def test_retry_after_s_from_a_latched_sessions_remaining_ttl(clock):
    engine = _engine(latch_ttl_seconds=60.0)
    state = SessionState("s1", key="user:abuser")
    state.tripped_by = Anomaly("budget", "critical", "over budget", {"session_id": "s1"})
    state.tripped_at = clock.now
    _install(engine, state, key="user:abuser")
    clock.advance(20.0)

    anomaly = Anomaly(
        "budget", "critical", "over budget", {"key": "user:abuser", "session_id": "s1"}
    )
    assert responses._retry_after_s(anomaly) == pytest.approx(40.0)


def test_retry_after_s_from_a_rollover_cooldown_override(clock):
    """``latch_ttl_override`` (a spike rollover's cooldown) wins even with no
    ``latch_ttl_seconds`` configured at all.
    """
    engine = _engine(latch_ttl_seconds=None)
    state = SessionState("s2", key="user:rolled", latch_ttl_override=30.0)
    state.tripped_by = Anomaly("spike", "critical", "rolled over", {"action": "rollover"})
    state.tripped_at = clock.now
    _install(engine, state, key="user:rolled")
    clock.advance(10.0)

    anomaly = Anomaly("spike", "critical", "rolled over", {"key": "user:rolled"})
    assert responses._retry_after_s(anomaly) == pytest.approx(20.0)


def test_retry_after_s_never_goes_negative_past_expiry(clock):
    engine = _engine(latch_ttl_seconds=10.0)
    state = SessionState("s3", key="user:expired")
    state.tripped_by = Anomaly("budget", "critical", "x", {})
    state.tripped_at = clock.now
    _install(engine, state, key="user:expired")
    clock.advance(999.0)

    anomaly = Anomaly("budget", "critical", "x", {"key": "user:expired"})
    assert responses._retry_after_s(anomaly) == 0.0


def test_retry_after_s_falls_back_to_the_session_id_scan_without_a_key(clock):
    """A halt anomaly carries only a hashed key, never the raw one; the
    (unhashed, local) ``session_id`` is what finds the session.
    """
    engine = _engine(latch_ttl_seconds=60.0)
    state = SessionState("s4")
    state.tripped_by = Anomaly("halt", "critical", "halted", {"session_id": "s4"})
    state.tripped_at = clock.now
    _install(engine, state)  # default/unkeyed session
    clock.advance(15.0)

    anomaly = Anomaly("halt", "critical", "halted", {"session_id": "s4", "key_hash": "abc"})
    assert responses._retry_after_s(anomaly) == pytest.approx(45.0)


# --- retry_after_s: end to end through a real latch -----------------------------


_BUDGET = 0.02


def _chat(key: str = "user:abuser") -> None:
    """One chatbot turn: enter the user's session, make one model call."""
    with runbound.session(key):
        api._record_llm_call("gpt-4o", 2000, 500)


def _latch_the_key(key: str = "user:abuser", turns: int = 3) -> None:
    """Spend past ``_BUDGET`` so ``key`` ends up latched."""
    for _ in range(turns):
        try:
            _chat(key)
        except GuardrailTripped:
            pass


def test_a_freshly_raised_trip_has_no_retry_after_by_default():
    runbound.init(budget_usd=_BUDGET, on_anomaly="raise")
    _latch_the_key()

    with pytest.raises(GuardrailTripped) as exc_info:
        with runbound.session("user:abuser"):
            pass  # refused at the door, on the very session that just tripped

    assert exc_info.value.refusal.retry_after_s is None


def test_a_real_latched_sessions_retry_after_counts_down(clock):
    runbound.init(budget_usd=_BUDGET, on_anomaly="raise", latch_ttl_seconds=60.0)
    _latch_the_key()

    clock.advance(20.0)

    with pytest.raises(GuardrailTripped) as exc_info:
        with runbound.session("user:abuser"):
            pass  # refused at the door

    assert exc_info.value.refusal.retry_after_s == pytest.approx(40.0)
    assert exc_info.value.refusal.headers == {"Retry-After": "40"}


# --- the three GuardrailTripped subclasses --------------------------------------


def test_policy_violation_refusal_is_builtin_403_by_default():
    violation = Violation("search", "denied_tool", "not allowed", {})
    exc = PolicyViolation(Anomaly("policy", "critical", "blocked", {}), violation)
    assert exc.refusal.status == 403
    assert exc.refusal.detector == "policy"
    assert exc.refusal.source == "default"


def test_policy_violation_refusal_is_overridable_via_init():
    runbound.init(
        refusals={"policy": {"status": 451, "message": "A human needs to approve that"}}
    )
    violation = Violation("search", "denied_tool", "not allowed", {})
    exc = PolicyViolation(Anomaly("policy", "critical", "blocked", {}), violation)
    assert exc.refusal.status == 451
    assert exc.refusal.message == "A human needs to approve that"
    assert exc.refusal.source == "local"


def test_circuit_open_resolves_the_circuit_entry():
    exc = CircuitOpen(Anomaly("circuit", "critical", "open", {"provider": "openai"}))
    assert exc.refusal.status == 503
    assert exc.refusal.detector == "circuit"
    assert exc.refusal.message == BUILTIN["circuit"]["message"]


def test_halt_refusal_resolves_the_halt_entry():
    exc = GuardrailTripped(Anomaly("halt", "critical", "halted", {}))
    assert exc.refusal.status == 503
    assert exc.refusal.message == BUILTIN["halt"]["message"]


def test_refusal_is_lazy_and_reflects_a_profile_set_after_the_raise():
    """A profile the customer changes on the plane reaches an exception that
    was raised moments earlier, since ``.refusal`` is resolved on access.
    """
    exc = GuardrailTripped(Anomaly("policy", "critical", "blocked", {}))
    assert exc.refusal.status == 403
    set_remote({"policy": {"status": 200, "message": "reopened"}})
    assert exc.refusal.status == 200
    assert exc.refusal.message == "reopened"


# --- init(refusals=...) validation ----------------------------------------------


def test_validate_accepts_none():
    GuardrailConfig(refusals=None).validate()  # must not raise


def test_validate_accepts_a_well_formed_profile():
    GuardrailConfig(refusals={"policy": {"status": 403, "message": "no"}}).validate()  # ok


def test_validate_rejects_a_non_dict_profile():
    with pytest.raises(ValueError, match="refusals"):
        GuardrailConfig(refusals="nope").validate()


def test_init_rejects_an_entry_that_is_not_a_dict_naming_the_key():
    with pytest.raises(ValueError) as exc_info:
        runbound.init(refusals={"policy": "nope"})
    assert "policy" in str(exc_info.value)


def test_init_rejects_status_out_of_range_naming_the_key():
    with pytest.raises(ValueError) as exc_info:
        runbound.init(refusals={"policy": {"status": 999}})
    message = str(exc_info.value)
    assert "policy" in message and "status" in message


def test_init_rejects_a_non_int_status_naming_the_key():
    with pytest.raises(ValueError) as exc_info:
        runbound.init(refusals={"policy": {"status": "429"}})
    message = str(exc_info.value)
    assert "policy" in message and "status" in message


def test_init_rejects_a_bool_status_naming_the_key():
    """``bool`` is an ``int`` subclass in Python; must not sneak past as one."""
    with pytest.raises(ValueError) as exc_info:
        runbound.init(refusals={"policy": {"status": True}})
    assert "status" in str(exc_info.value)


def test_init_rejects_a_message_too_long_naming_the_key():
    with pytest.raises(ValueError) as exc_info:
        runbound.init(refusals={"policy": {"message": "x" * 501}})
    message = str(exc_info.value)
    assert "policy" in message and "message" in message


def test_init_rejects_a_non_string_message_naming_the_key():
    with pytest.raises(ValueError) as exc_info:
        runbound.init(refusals={"policy": {"message": 12345}})
    message = str(exc_info.value)
    assert "policy" in message and "message" in message


def test_a_message_at_exactly_500_chars_is_accepted():
    GuardrailConfig(refusals={"default": {"message": "x" * 500}}).validate()  # ok


# --- the wire seam: _policy_envelope and _fetch_policy --------------------------


def test_policy_envelope_strips_refusals_and_returns_it_separately():
    data = {
        "policy": {"tool_policy": "x"},
        "version": 3,
        "dry_run": True,
        "refusals": {"default": {"status": 200}},
    }
    body, version, dry_run, refusals = _policy_envelope(data, 1)
    assert body == {"tool_policy": "x"}
    assert version == 3
    assert dry_run is True
    assert refusals == {"default": {"status": 200}}


def test_policy_envelope_without_refusals_is_byte_for_byte_unchanged():
    data = {"policy": {"tool_policy": "x"}, "version": 3, "dry_run": False}
    body, version, dry_run, refusals = _policy_envelope(data, 1)
    assert body == {"tool_policy": "x"}
    assert version == 3
    assert dry_run is False
    assert refusals is None


def test_policy_envelope_flat_shape_also_strips_refusals():
    data = {"max_steps": 5, "refusals": {"halt": {"status": 200}}, "version": 2}
    body, version, dry_run, refusals = _policy_envelope(data, 1)
    assert body == {"max_steps": 5}
    assert refusals == {"halt": {"status": 200}}


def test_policy_envelope_null_body_carries_no_refusals():
    body, version, dry_run, refusals = _policy_envelope(None, 5)
    assert body is None
    assert version == 5
    assert refusals is None


class _FakePlaneClient:
    """Just enough of a plane client for ``RemoteState._fetch_policy``."""

    def __init__(self, policy_body):
        self._policy_body = policy_body

    def policy(self, service):
        return self._policy_body


def test_fetch_policy_installs_the_remote_refusal_profile():
    client = _FakePlaneClient(
        {"version": 4, "policy": {"deny": ["wire"]}, "refusals": {"default": {"status": 200}}}
    )
    remote_state = RemoteState(client, None, GuardrailConfig())

    remote_state._fetch_policy(4)

    assert responses.has_remote() is True
    assert responses._get_remote() == {"default": {"status": 200}}
    assert remote_state.policy() == {"deny": ["wire"]}  # policy body itself is untouched


def test_fetch_policy_withdraws_an_absent_refusals_key():
    set_remote({"default": {"status": 200}})
    client = _FakePlaneClient({"version": 5, "policy": {"deny": ["wire"]}})
    remote_state = RemoteState(client, None, GuardrailConfig())

    remote_state._fetch_policy(5)

    assert responses.has_remote() is False


# --- coverage()["refusals"] ------------------------------------------------------


def test_coverage_refusals_is_default_before_init():
    assert runbound.coverage()["refusals"] == "default"


def test_coverage_refusals_is_default_with_a_plain_init():
    runbound.init()
    assert runbound.coverage()["refusals"] == "default"


def test_coverage_refusals_is_local_when_config_sets_a_profile():
    runbound.init(refusals={"policy": {"status": 400}})
    assert runbound.coverage()["refusals"] == "local"


def test_coverage_refusals_is_plane_when_a_remote_profile_is_set():
    runbound.init(refusals={"policy": {"status": 400}})
    set_remote({"default": {"status": 200}})
    assert runbound.coverage()["refusals"] == "plane"


# --- reset() and _teardown_for_tests() clear the remote profile ------------------


def test_reset_clears_the_remote_profile():
    runbound.init()
    set_remote({"default": {"status": 200}})
    assert responses.has_remote() is True

    runbound.reset()

    assert responses.has_remote() is False


def test_reset_clears_the_remote_profile_even_before_init():
    set_remote({"default": {"status": 200}})
    runbound.reset()  # a no-op for the engine, but not for the profile
    assert responses.has_remote() is False


def test_a_second_init_does_not_inherit_the_previous_ones_remote_profile():
    runbound.init(control_plane_url=None)
    set_remote({"default": {"status": 200}})

    runbound.init()  # a fresh configuration, no plane of its own (yet)

    assert responses.has_remote() is False


# --- Refusal is importable from the top-level package ----------------------------


def test_refusal_is_importable_from_the_top_level_package():
    assert runbound.Refusal is Refusal


# --- fail-open -------------------------------------------------------------------


def test_refusal_for_never_raises_even_if_resolution_explodes(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(responses, "_profile_fields", boom)

    r = refusal_for(Anomaly("policy", "critical", "x", {}))

    assert isinstance(r, Refusal)
    assert r.status == 403
    assert r.source == "default"


def test_retry_after_s_fails_open_when_the_registry_lookup_explodes(monkeypatch):
    runbound.init()

    class ExplodingLock:
        def __enter__(self):
            raise RuntimeError("boom")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(api, "_LOCK", ExplodingLock())
    anomaly = Anomaly("budget", "critical", "x", {"key": "user:x"})

    assert responses._retry_after_s(anomaly) is None

    # Undo here, explicitly, rather than let monkeypatch's own finalizer do
    # it: the autouse ``_uninitialized`` fixture above tears down after this
    # test with its own call to ``_teardown_for_tests()``, which takes
    # ``api._LOCK`` — and needs the *real* lock back to do it, not this
    # exploding stand-in, pre-existing fixture-teardown-order gap unrelated
    # to what this test is actually checking.
    monkeypatch.undo()


def test_local_profile_lookup_fails_open_on_a_broken_config():
    runbound.init()

    class ExplodingConfig:
        @property
        def refusals(self):
            raise RuntimeError("boom")

    api._ENGINE.config = ExplodingConfig()

    assert responses._local_profile() is None
    r = refusal_for(Anomaly("policy", "critical", "x", {}))
    assert r.status == 403  # BUILTIN, not a crash
