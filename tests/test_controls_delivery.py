"""One dashboard change reaches a worker within one heartbeat and is
enforced — one scenario per control.

Built on ``test_session_sync.py``'s own harness: a :class:`FakePlane` stands
in for the control plane exactly where ``runbound.init`` builds its client,
and everything else — the engine, the session registry, the wrappers — is
the real thing. "One heartbeat" is literal: each test calls
``api._SHARED.apply_hello(...)`` exactly once, the same call the real
:class:`~runbound.plane.Poller` thread makes on every poll, with
``plane.controls_body`` already set to what a dashboard save would have
produced — so this is the SDK half of "a dashboard change reaches a worker
within one heartbeat and is enforced"; the plane half (a real Controls row,
served, read back through ``/v1/controls``) is covered in the control
plane's own suite.
"""

import pytest

import runbound
from runbound import api, shared as shared_module
from runbound.exceptions import GuardrailTripped, SafeModeViolation
from runbound.plane_types import EntryDecision, HelloReply
from test_envelope import FakeOpenAI
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


def one_heartbeat(plane: FakePlane, controls: dict, version: int = 1) -> None:
    """Exactly what one real poll does: the plane answers ``/v1/controls``
    with ``controls`` and the next heartbeat names its version."""
    plane.controls_body = {"version": version, "controls": controls}
    api._SHARED.apply_hello(HelloReply(controls_version=version))


# --- money -------------------------------------------------------------


def test_a_dashboard_budget_cut_is_enforced_within_one_heartbeat(plane):
    plane.decision = EntryDecision()
    start(
        budget_usd=100.0,
        budget_admission=True,
        admission_output_tokens=1,
        custom_prices={"m": (0.0, 1_000_000.0)},  # $1/output token
        on_anomaly="raise",
    )
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 1)))
    client.chat.completions.create(model="m", messages=[])  # spends $1, well under $100

    one_heartbeat(plane, {"limits": {"org": {"budget_usd": 0.5}}})

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="m", messages=[])
    assert excinfo.value.anomaly.details["decision"]["boundary"] == "money"
    assert client.completions.calls == 1  # the offending call never reached the fake


def test_the_plane_cannot_loosen_a_tighter_local_budget(plane):
    """Invariant 3, the other direction: the code's own $1 stays $1 even
    though the plane states $100."""
    plane.decision = EntryDecision()
    start(
        budget_usd=1.0,
        budget_admission=True,
        admission_output_tokens=1,
        custom_prices={"m": (0.0, 1_000_000.0)},
        on_anomaly="raise",
    )
    client = runbound.wrap(FakeOpenAI(model="m", tokens=(0, 1)))
    client.chat.completions.create(model="m", messages=[])  # spends the whole $1

    one_heartbeat(plane, {"limits": {"org": {"budget_usd": 100.0}}})

    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="m", messages=[])
    assert api._ENGINE.controls_refusals() == [
        {"path": "limits.budget_usd", "base": 1.0, "candidate": 100.0}
    ]


# --- steps (the door) ----------------------------------------------------


def test_a_dashboard_step_cut_reaches_the_door_within_one_heartbeat(plane):
    plane.decision = EntryDecision()
    start(max_steps=100, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    one_heartbeat(plane, {"limits": {"service": {"max_steps": 1}}})

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 1  # refused at the door, never reached the fake


# --- capabilities ---------------------------------------------------------


def test_a_dashboard_capability_rule_refuses_a_tool_within_one_heartbeat(plane):
    plane.decision = EntryDecision()
    start(on_anomaly="raise")

    @runbound.tool(effects={"financial"}, max_calls=99)
    def issue_refund():
        return "done"

    assert issue_refund() == "done"

    one_heartbeat(plane, {"capabilities": {"financial": "deny"}})

    with pytest.raises(SafeModeViolation):
        issue_refund()


# --- envelope (the on/off switch) ------------------------------------------


def test_envelope_off_locally_lets_the_offending_call_reach_the_fake(plane):
    """The baseline this test's twin (below) shows a heartbeat changing."""
    plane.decision = EntryDecision()
    start(max_steps=1, on_anomaly="raise", envelope=False)
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped):
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert client.completions.calls == 2  # the wall tripped it, not a door


def test_a_dashboard_envelope_on_gates_the_steps_door_within_one_heartbeat(plane):
    plane.decision = EntryDecision()
    start(max_steps=1, on_anomaly="raise", envelope=False)

    one_heartbeat(plane, {"envelope": True})

    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))
    client.chat.completions.create(model="gpt-4o", messages=[])

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 1  # refused at the door this time


# --- detectors: the tighten-only rule, end to end --------------------------
#
# "the plane can only tighten ... notify -> stop but never stop -> notify"
# and "on_anomaly ... cannot be set from a dashboard". The pure-merge and
# engine-level cases live in ``tests/test_controls_engine.py``; these three
# are the coordinator's own probes, driven through the public API and a
# real wrapped client, so the fix is visible exactly where it was found.


def test_probe_a_a_dashboard_notify_cannot_switch_off_a_raise_mode_workers_steps_wall(plane):
    plane.decision = EntryDecision()
    start(max_steps=2, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))

    one_heartbeat(plane, {"detectors": {"steps": {"action": "notify", "mode": "enforce"}}})

    client.chat.completions.create(model="gpt-4o", messages=[])
    client.chat.completions.create(model="gpt-4o", messages=[])
    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert excinfo.value.anomaly.detector == "steps"
    assert client.completions.calls == 2  # the third call never reached the fake
    assert api._ENGINE.controls_refusals() == [
        {"path": "detectors.steps.action", "base": "stop", "candidate": "notify"}
    ]


def test_probe_c_a_dashboard_notify_cannot_switch_off_a_raise_mode_workers_money_wall(plane):
    plane.decision = EntryDecision()
    start(budget_usd=0.0001, on_anomaly="raise")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1_000_000)))

    one_heartbeat(plane, {"detectors": {"budget": {"action": "notify", "mode": "enforce"}}})

    with pytest.raises(GuardrailTripped) as excinfo:
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert excinfo.value.anomaly.detector == "budget"
    assert runbound.is_tripped() is not None
    assert api._ENGINE.controls_refusals() == [
        {"path": "detectors.budget.action", "base": "stop", "candidate": "notify"}
    ]


def test_probe_b_a_dashboard_stop_cannot_be_forced_on_a_warn_mode_worker(plane):
    plane.decision = EntryDecision()
    start(max_steps=2, on_anomaly="warn")
    client = runbound.wrap(FakeOpenAI(model="gpt-4o", tokens=(0, 1)))

    one_heartbeat(plane, {"detectors": {"steps": {"action": "stop", "mode": "enforce"}}})

    for _ in range(5):
        client.chat.completions.create(model="gpt-4o", messages=[])  # never raises
    assert client.completions.calls == 5
    assert runbound.is_tripped() is None
    assert api._ENGINE.controls_refusals() == [
        {
            "path": "detectors.steps.action",
            "base": "notify",
            "candidate": "stop",
            "reason": "cannot_stop",
        }
    ]


def test_a_dashboard_stop_escalates_a_raise_mode_workers_own_notify_spike(plane):
    """Code: ``on_spike="notify"`` (a spike watches but never stops on its
    own) -- the resolved default, whether or not a plane ever states
    anything: ``Engine._effective_config`` falls back to it without a
    plane bundle ever having to state one. Dashboard: spike -> stop. This
    worker's
    ``on_anomaly`` can raise at all, so the escalation is a real
    tightening and is applied."""
    plane.decision = EntryDecision()
    start(on_anomaly="raise")

    one_heartbeat(plane, {"detectors": {"spike": {"action": "stop", "mode": "enforce"}}})

    assert api._ENGINE.controls_detector_override("spike") == "stop"
    assert api._ENGINE.controls_refusals() == []
