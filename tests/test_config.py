"""Tests for RunboundConfig defaults and validation, plus the core models
(Event, Anomaly, GuardrailTripped) that have no test module of their own.
"""

import dataclasses

import pytest

import runbound
from runbound.config import RunboundConfig
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped


def test_defaults_match_spec():
    cfg = RunboundConfig()
    assert cfg.token is None
    assert cfg.control_plane_url is None
    assert cfg.service == "default"
    assert cfg.worker_id is None
    assert cfg.control_plane_poll_s == 5.0
    assert cfg.export_events is True
    assert cfg.send_session_keys is False
    assert cfg.on_halt == "raise"
    assert cfg.tool_policy is None
    assert cfg.capabilities is None
    assert cfg.require_rules is False
    assert cfg.max_actions_per_run is None
    assert cfg.on_event is None
    assert cfg.max_sessions == 10_000


def test_config_has_exactly_the_thirty_public_fields():
    names = {f.name for f in dataclasses.fields(RunboundConfig) if f.init}
    assert names == {
        "token", "control_plane_url", "service", "worker_id", "control_plane_poll_s",
        "export_events", "send_session_keys", "on_halt", "tool_policy", "capabilities",
        "require_rules", "max_actions_per_run", "pre_hooks", "gate_hooks", "hook_timeout_s", "post_hooks", "redact_keys", "result_limits", "otel_endpoint", "consequential_grace_s", "cache_dir", "approval_secret", "on_deferred_result", "provide", "offline_execution_s", "confirm_wait_s", "goodbye_on_exit", "approval_bind", "on_event", "max_sessions",
    }


def test_removed_kwargs_are_rejected():
    for name in ("budget_usd", "max_steps", "on_anomaly", "loop_threshold", "custom_prices"):
        with pytest.raises(TypeError):
            RunboundConfig(**{name: 1})


def test_default_config_validates():
    RunboundConfig().validate()


def test_fully_populated_config_validates():
    cfg = RunboundConfig(
        service="svc",
        worker_id="w-1",
        control_plane_poll_s=2.0,
        export_events=False,
        send_session_keys=True,
        on_halt="warn",
        tool_policy={"deny": ["rm"], "max_calls": {"search": 3}},
        capabilities={"read": "allow", "write": "deny"},
        require_rules=True,
        max_actions_per_run=10,
        on_event=lambda record: None,
        max_sessions=5,
    )
    cfg.validate()


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "3"])
def test_bad_max_actions_per_run_raises(value):
    with pytest.raises(ValueError, match="max_actions_per_run"):
        RunboundConfig(max_actions_per_run=value).validate()


def test_smallest_max_actions_per_run_is_valid():
    RunboundConfig(max_actions_per_run=1).validate()


@pytest.mark.parametrize("value", [0, -1, 1.5, "3"])
def test_bad_max_sessions_raises(value):
    with pytest.raises(ValueError, match="max_sessions"):
        RunboundConfig(max_sessions=value).validate()


@pytest.mark.parametrize("name", ["export_events", "send_session_keys", "require_rules"])
def test_boolean_fields_must_be_bool(name):
    with pytest.raises(ValueError, match=name):
        RunboundConfig(**{name: "yes"}).validate()


def test_on_event_must_be_callable():
    with pytest.raises(ValueError, match="on_event"):
        RunboundConfig(on_event="nope").validate()


def test_bad_capability_class_rule_raises():
    with pytest.raises(ValueError):
        RunboundConfig(capabilities={"read": "sometimes"}).validate()


def test_dict_tool_policy_is_coerced_to_a_tool_policy():
    cfg = RunboundConfig(tool_policy={"deny": ["rm"], "max_calls": {"search": 3}})
    cfg.validate()
    assert isinstance(cfg.tool_policy, runbound.ToolPolicy)


def test_validate_returns_none():
    assert RunboundConfig().validate() is None


# --- core models -----------------------------------------------------------


def test_event_defaults_and_immutability():
    event = Event(kind="llm_call", ts=1.5, step=1)

    assert (event.tokens_in, event.tokens_out, event.cost_usd) == (0, 0, 0.0)
    assert event.model is None
    assert event.tool_name is None
    assert event.args_hash is None
    assert event.error is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.step = 2


def test_anomaly_is_frozen():
    anomaly = Anomaly(detector="loop", severity="critical", message="m", details={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        anomaly.severity = "warn"


def test_runbound_tripped_carries_anomaly_and_message():
    anomaly = Anomaly(
        detector="budget",
        severity="critical",
        message="budget exceeded: $5.10 > $5.00",
        details={"total_cost_usd": 5.10, "budget_usd": 5.0},
    )
    exc = GuardrailTripped(anomaly)

    assert isinstance(exc, Exception)
    assert exc.anomaly is anomaly
    assert str(exc) == anomaly.message


def test_runbound_tripped_is_catchable_as_raised():
    anomaly = Anomaly(detector="steps", severity="critical", message="too many", details={})
    with pytest.raises(GuardrailTripped) as caught:
        raise GuardrailTripped(anomaly)

    assert caught.value.anomaly.detector == "steps"


def test_package_exports_version_and_core_names():
    assert runbound.__version__ == "1.0.0a7"
    assert runbound.Event is Event
    assert runbound.Anomaly is Anomaly
    assert runbound.RunboundConfig is RunboundConfig
    assert runbound.GuardrailTripped is GuardrailTripped
    from runbound.state import SessionState

    assert runbound.SessionState is SessionState


def test_the_old_config_name_is_the_same_class_and_warns_once(monkeypatch):
    import warnings

    from runbound import config as config_module

    monkeypatch.setattr(config_module, "_ALIAS_WARNED", False)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        first = runbound.GuardrailConfig
        second = runbound.GuardrailConfig
        via_module = config_module.GuardrailConfig
    assert first is second is via_module is runbound.RunboundConfig
    ours = [w for w in seen if issubclass(w.category, DeprecationWarning)]
    assert len(ours) == 1
    assert "RunboundConfig" in str(ours[0].message)


def test_a_typo_names_the_module_it_was_looked_up_on():
    from runbound import config as config_module

    with pytest.raises(AttributeError, match="module 'runbound' has no attribute 'nope'"):
        runbound.nope
    with pytest.raises(AttributeError, match="module 'runbound.config' has no attribute 'nope'"):
        config_module.nope
