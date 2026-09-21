"""Baselines and the ladder's rung, restored through the plane.

Two levels: direct unit tests of the seeding functions in
:mod:`runbound.api` (``_apply_baseline``, ``_fresh_for_baseline``,
``_seed_rung``, ``_exit_delta``), and end-to-end tests driving the public API
against a :class:`FakePlane`, the same harness ``test_session_sync.py`` uses.
The end-to-end tests are what this feature is actually about: a worker restarted mid-spike re-enters at the same rung, a
new key is judged against the service median from its first call, a
plane-less process is unaffected, and none of this ever raises.
"""

import pytest

import runbound
from runbound import api, ladder, shared as shared_module
from runbound.config import GuardrailConfig
from runbound.plane_types import EntryDecision, HelloReply, key_hash
from runbound.state import SessionState
from spike_test_helpers import spike_controls_body, spiking_config
from test_shared_state import FakePlane

PLANE_URL = "https://plane.example"
KEY = "user:9901"


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


def start(**kwargs) -> None:
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


def enable_spike(plane: FakePlane, on_spike: str = "limit", **spike_kwargs) -> None:
    """``on_spike`` and the nine tuning keywords are real, local
    ``init()`` fields too; this helper delivers the enabling Controls
    through the fake plane instead, exactly ``test_controls_delivery.py``'s
    own ``one_heartbeat`` helper (a fresh, first-ever delivery: every
    caller here uses it right after ``start()``, before anything else has
    fetched a Controls version) -- this suite's own coverage of that path."""
    plane.controls_body = {"version": 1, "controls": spike_controls_body(on_spike, **spike_kwargs)}
    api._SHARED.apply_hello(HelloReply(controls_version=1))


# --- unit tests: _apply_baseline / _fresh_for_baseline / _seed_rung --------


class _FakeShared:
    """Just enough of :class:`~runbound.shared.SharedState` for
    ``_apply_baseline``'s own unit tests: a ``service_baseline()`` that
    answers whatever the test hands it, off the *Controls* payload,
    never the per-key ``EntryDecision``.
    """

    def __init__(self, baseline: tuple[float, float] | None = None) -> None:
        self._baseline = baseline

    def service_baseline(self) -> tuple[float, float] | None:
        return self._baseline


NO_SERVICE_BASELINE = _FakeShared()


def test_fresh_for_baseline_is_true_only_before_any_local_call():
    state = SessionState("s1", key=KEY)
    assert api._fresh_for_baseline(state) is True

    state.spike_baseline = (2.0, 100.0)
    assert api._fresh_for_baseline(state) is False


def test_apply_baseline_seeds_a_fresh_session_from_a_restored_decision():
    state = SessionState("s1", key=KEY)
    decision = EntryDecision(
        baseline_duration_s=2.0, baseline_output_tokens=100.0, baseline_samples=12
    )
    config = GuardrailConfig()

    api._apply_baseline(KEY, state, decision, config, NO_SERVICE_BASELINE)

    assert state.spike_baseline == pytest.approx((2.0, 100.0))
    assert state.spike_baseline_source == "restored"
    assert state.spike_baseline_samples is None  # not learned by this worker


def test_apply_baseline_never_overwrites_a_session_that_has_already_called():
    """Trap #5's spirit, applied to the baseline: a session this worker has
    already been serving must never have its own learning clobbered by a
    stale or merely-cached plane answer."""
    state = SessionState("s1", key=KEY)
    state.spike_baseline = (5.0, 400.0)  # this worker's own, already learned
    state.spike_baseline_source = "local"
    decision = EntryDecision(
        baseline_duration_s=999.0, baseline_output_tokens=999.0, baseline_samples=99
    )

    api._apply_baseline(KEY, state, decision, GuardrailConfig(), NO_SERVICE_BASELINE)

    assert state.spike_baseline == pytest.approx((5.0, 400.0))
    assert state.spike_baseline_source == "local"


def test_apply_baseline_always_refreshes_the_service_median():
    """Unlike the per-key baseline, the service median is advisory context
    only -- it never drives what THIS session's own history says, so it is
    safe (and correct) to keep it current on every entry. It comes off the
    Controls payload (``shared.service_baseline()``) --
    not the per-key ``EntryDecision`` -- so a bare decision with no rung or
    baseline of its own is enough to prove it."""
    state = SessionState("s1", key=KEY)
    state.spike_baseline = (5.0, 400.0)
    state.spike_baseline_source = "local"
    shared = _FakeShared(baseline=(3.0, 250.0))

    api._apply_baseline(KEY, state, EntryDecision(), GuardrailConfig(), shared)

    assert state.service_baseline == pytest.approx((3.0, 250.0))
    assert state.spike_baseline == pytest.approx((5.0, 400.0))  # untouched


def test_apply_baseline_ignores_a_zero_sample_decision():
    state = SessionState("s1", key=KEY)
    decision = EntryDecision(baseline_duration_s=2.0, baseline_output_tokens=100.0)  # samples=0

    api._apply_baseline(KEY, state, decision, GuardrailConfig(), NO_SERVICE_BASELINE)

    assert state.spike_baseline is None
    assert state.spike_baseline_source is None


def test_apply_baseline_restores_a_limited_rung_and_its_posture():
    state = SessionState("s1", key=KEY)
    decision = EntryDecision(rung_level=2, rung_allowance=3, rung_allowance_start=5)
    config = spiking_config(on_spike="limit", on_trip="latch")

    api._apply_baseline(KEY, state, decision, config, NO_SERVICE_BASELINE)

    assert state.spike_level == ladder.LEVEL_LIMITED
    assert state.spike_allowance == 3
    assert state.spike_allowance_start == 5
    assert state.posture is not None
    assert state.posture.name == "restricted"
    assert state.ladder_history[-1][3] == "restored"


def test_apply_baseline_restores_watching_without_a_posture_change():
    state = SessionState("s1", key=KEY)
    decision = EntryDecision(rung_level=1)
    config = spiking_config(on_spike="limit", on_trip="latch")

    api._apply_baseline(KEY, state, decision, config, NO_SERVICE_BASELINE)

    assert state.spike_level == ladder.LEVEL_WATCHING
    assert state.posture is None


def test_apply_baseline_ignores_the_rung_unless_on_spike_is_limit():
    """Trap #4: a restored rung must never change what on_spike does --
    under any mode but "limit" there is no ladder at all, so nothing about
    level or posture is restored either."""
    state = SessionState("s1", key=KEY)
    decision = EntryDecision(rung_level=2, rung_allowance=1, rung_allowance_start=5)

    api._apply_baseline(
        KEY, state, decision, spiking_config(on_spike="notify"), NO_SERVICE_BASELINE
    )

    assert state.spike_level == 0
    assert state.posture is None


def test_apply_baseline_never_restores_level_3_closed():
    """A closed key's cooldown already rides the remote latch; the rung
    seeding path only ever restores 1 or 2."""
    state = SessionState("s1", key=KEY)
    decision = EntryDecision(rung_level=3)
    config = spiking_config(on_spike="limit", on_trip="latch")

    api._apply_baseline(KEY, state, decision, config, NO_SERVICE_BASELINE)

    assert state.spike_level == 0


def test_apply_baseline_never_raises_on_a_malformed_decision():
    state = SessionState("s1", key=KEY)

    class Junk:
        pass

    api._apply_baseline(
        KEY, state, Junk(), spiking_config(on_spike="limit", on_trip="latch"), NO_SERVICE_BASELINE
    )
    # nothing to assert beyond "did not raise" -- fail-open


def test_apply_baseline_never_raises_when_shared_has_no_service_baseline_method():
    """Fail-open for a test double or an older in-tree fake that predates
    the Controls-based service median entirely."""
    state = SessionState("s1", key=KEY)

    class BareShared:
        pass

    api._apply_baseline(
        KEY, state, EntryDecision(), spiking_config(on_spike="limit", on_trip="latch"), BareShared()
    )

    assert state.service_baseline is None


def test_apply_baseline_never_raises_when_service_baseline_itself_raises():
    state = SessionState("s1", key=KEY)

    class ExplodingShared:
        def service_baseline(self):
            raise RuntimeError("boom")

    api._apply_baseline(
        KEY, state, EntryDecision(), spiking_config(on_spike="limit", on_trip="latch"), ExplodingShared()
    )

    assert state.service_baseline is None


def test_exit_delta_reports_the_held_baseline_when_learned_locally():
    state = SessionState("s1", key=KEY)
    state.spike_baseline = (2.0, 100.0)
    state.spike_baseline_source = "local"
    state.spike_baseline_samples = 12

    delta = api._exit_delta(KEY, state)

    assert delta.baseline_duration_s == pytest.approx(2.0)
    assert delta.baseline_output_tokens == pytest.approx(100.0)
    assert delta.baseline_samples == 12


def test_exit_delta_never_reports_a_restored_or_peer_baseline_as_this_keys_own():
    """The other half of trap #1/#2: a worker must never launder a peer or
    restored number back to the plane as if it had measured this key."""
    for source in ("restored", "peer"):
        state = SessionState("s1", key=f"{KEY}-{source}")
        state.spike_baseline = (2.0, 100.0)
        state.spike_baseline_source = source
        state.spike_baseline_samples = None

        delta = api._exit_delta(f"{KEY}-{source}", state)

        assert delta.baseline_samples == 0
        assert delta.baseline_duration_s == 0.0


def test_exit_delta_reports_the_live_rung():
    state = SessionState("s1", key=KEY)
    state.spike_level = ladder.LEVEL_LIMITED
    state.spike_allowance = 2
    state.spike_allowance_start = 5

    delta = api._exit_delta(KEY, state)

    assert delta.rung_level == ladder.LEVEL_LIMITED
    assert delta.rung_allowance == 2
    assert delta.rung_allowance_start == 5


# --- end to end: a worker restarted mid-spike re-enters at the same rung ---


def test_a_restarted_worker_re_enters_a_limited_key_at_the_same_rung(plane):
    """Simulates a restart: a fresh process (a new ``init()``, an empty
    registry, an empty ``_STRIKES``) whose very first entry for this key
    gets back exactly the rung the plane last knew -- not level 0
    (forgiveness by redeploy) and not escalated either."""
    plane.decision = EntryDecision(rung_level=2, rung_allowance=2, rung_allowance_start=5)
    start(on_trip="latch")
    enable_spike(plane, on_spike="limit")

    with runbound.session(KEY):
        pass  # the mere entry restores the rung; no call is needed

    status = runbound.session_status(KEY)
    assert status["level"] == 2
    assert status["allowance_left"] == 2
    assert status["posture"]["name"] == "restricted"


def test_the_restored_rung_does_not_reset_a_key_a_worker_had_already_limited(plane):
    """Mutation guard: if seeding ever ran unconditionally (ignoring
    _fresh_for_baseline), a plane answer reporting level 0 -- the ordinary
    case for a key nothing has told the plane about yet -- would reset an
    already-limited local session back to quiet. It must not."""
    plane.decision = EntryDecision()  # rung_level defaults to 0
    start(on_trip="latch")
    enable_spike(plane, on_spike="limit", spike_limit_calls=2, spike_warmup_calls=2)

    with runbound.session(KEY):
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=2.0)
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=2.0)
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=40.0)
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=40.0)

    assert runbound.session_status(KEY)["level"] == 2  # confirmed and limited

    api._SHARED._cache.clear()  # force a fresh /enter, as a new request would
    with runbound.session(KEY):
        pass

    assert runbound.session_status(KEY)["level"] == 2  # unchanged, not reset


# --- end to end: a new key is judged against the service median -----------


def test_a_new_keys_first_call_is_judged_against_the_service_median(plane):
    # The service median rides Controls (the heartbeat),
    # not the per-key EntryDecision -- so it has to actually be applied by
    # a hello before this key's own (unrelated) entry can see it.
    plane.controls_body = {
        "version": 1, "dry_run": False,
        "controls": {
            "service_baseline_duration_s": 2.0, "service_baseline_output_tokens": 100.0,
            "service_baseline_keys": 6,
            # on_spike="notify", the default -- but the ratio judgement itself
            # is plane-gated too (delivered here for this test), so it has
            # to be turned on here too or nothing would ever fire.
            **spike_controls_body("notify"),
        },
    }
    plane.decision = EntryDecision()  # so _apply_baseline runs at all on entry
    start()
    api._SHARED.apply_hello(HelloReply(controls_version=1))
    seen = []
    api._ENGINE.observers.append(
        type("Observer", (), {"on_event": lambda self, s, e: None,
                              "on_anomaly": lambda self, s, a, r: seen.append(a)})()
    )

    with runbound.session(KEY):
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=40.0)

    spikes = [a for a in seen if a.detector == "spike"]
    assert spikes, "expected the very first call to be flagged"
    assert spikes[0].details["baseline_source"] == "peer"
    assert spikes[0].details["median"] == pytest.approx(2.0)


def test_on_spike_limit_a_new_keys_single_long_call_only_notifies(plane):
    """Trap #3, end to end: the default on_spike="notify" means a
    peer-judged confirmed spike is never allowed to stop or restrict a
    session, whatever on_spike says -- and here on_spike="limit" too, one
    call is never enough on its own (spike_confirm still applies)."""
    plane.controls_body = {
        "version": 1, "dry_run": False,
        "controls": {
            "service_baseline_duration_s": 2.0, "service_baseline_output_tokens": 100.0,
            "service_baseline_keys": 6,
            **spike_controls_body("limit"),
        },
    }
    plane.decision = EntryDecision()  # so _apply_baseline runs at all on entry
    start(on_trip="latch")
    api._SHARED.apply_hello(HelloReply(controls_version=1))

    with runbound.session(KEY):
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=40.0)

    status = runbound.session_status(KEY)
    assert status["level"] == 1  # watching, never limited on one call
    assert status["posture"] is None


# --- end to end: a plane-less process is unaffected ------------------------


def test_a_plane_less_process_never_sees_a_restored_or_peer_baseline():
    runbound.init(budget_usd=5.0)

    with runbound.session(KEY):
        pass

    status = runbound.session_status(KEY)
    assert status["why"]["baseline"] is None
    assert status["why"]["service_baseline"] is None


# --- content independence: the raw key never reaches the plane -----------


def test_the_raw_key_never_rides_a_baseline_or_rung_report(plane):
    plane.decision = EntryDecision(rung_level=2, rung_allowance=1, rung_allowance_start=5)
    start(on_trip="latch", export_events=True)
    enable_spike(plane, on_spike="limit")

    with runbound.session(KEY):
        runbound.record_call("gpt", tokens_in=10, tokens_out=10, duration_s=2.0)
    api._SHARED._exporter.flush(1.0)

    for name in ("enter",):
        for payload in plane.payloads(name):
            assert payload.get("key_hash") == key_hash(KEY)
            assert "key" not in payload or payload["key"] is None
    for payload in plane.payloads("events"):
        for exit_delta in payload.get("exits", []):
            assert exit_delta.get("key_hash") == key_hash(KEY)
            assert KEY not in str(exit_delta)
