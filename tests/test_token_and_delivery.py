"""Tests for the token as the SDK's one connection to a control plane.

``RunboundConfig.validate()`` normalizes ``token``: an unset token is read
from ``RUNBOUND_TOKEN`` (blank/whitespace counts as unset), and a non-empty
token with no ``control_plane_url`` implies the hosted plane -- which does not
exist yet (``HOSTED_PLANE_URL`` is ``None``), so today that instead logs one
WARNING and the process stays local. A url with no token still raises.

That is the *whole* of what a token does inside this process. Delivery (Slack,
PagerDuty, a signed webhook) is out of the SDK entirely: the control plane
routes and delivers. What a token gates is *fleet mode* (the plane calls in
:mod:`runbound.shared`), never enforcement: tool policy refusals must come out
identically whether or not a token is set.
"""

import logging

import pytest

from runbound import config as config_module
from runbound.config import RunboundConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolCall, ToolPolicy
from runbound.config import HOSTED_PLANE_URL
from runbound.state import SessionState

@pytest.fixture(autouse=True)
def _unwarned(monkeypatch):
    """Every test gets its own turn at the once-per-process token warning.

    ``tests/conftest.py`` already keeps a real ``RUNBOUND_TOKEN`` or
    ``RUNBOUND_PLANE_URL`` out of every test's environment; this resets
    the module-level set :func:`runbound.config._warn_once`
    guards, so one test's warning cannot silence the next.
    """
    monkeypatch.setattr(config_module, "_WARNED", set())


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash)


def call(name: str = "wire_transfer") -> ToolCall:
    return ToolCall(name=name, args=(), kwargs={}, session_key="user-9", tags={})


# --- token normalization: the environment -----------------------------------


def test_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_TOKEN", "env-secret")
    config = RunboundConfig()

    config.validate()

    assert config.token == "env-secret"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_a_blank_or_whitespace_env_token_counts_as_unset(monkeypatch, value):
    monkeypatch.setenv("RUNBOUND_TOKEN", value)
    config = RunboundConfig()

    config.validate()

    assert config.token is None
    assert config.control_plane_url is None  # nothing was set, so no url either


def test_an_explicit_token_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_TOKEN", "env-secret")
    config = RunboundConfig(token="explicit")

    config.validate()

    assert config.token == "explicit"


# --- token normalization: a bare token implies the hosted plane ------------


def test_a_bare_token_leaves_control_plane_url_at_the_hosted_default():
    """``HOSTED_PLANE_URL`` is ``None`` until a hosted plane exists, so
    today this is ``None`` too — the day hosting goes live this assertion
    starts exercising the real URL with no test change required."""
    config = RunboundConfig(token="k")

    config.validate()

    assert config.control_plane_url == HOSTED_PLANE_URL


def test_a_bare_token_with_no_hosted_plane_warns_once_per_process(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")

    RunboundConfig(token="k").validate()
    RunboundConfig(token="k2").validate()

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "nowhere to send it yet" in warnings[0]
    assert "control_plane_url" in warnings[0]


def test_token_alone_prefers_an_explicit_plane_url_env(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    monkeypatch.setenv("RUNBOUND_PLANE_URL", "https://self-hosted.example")
    config = RunboundConfig(token="k")

    config.validate()

    assert config.control_plane_url == "https://self-hosted.example"
    assert _warnings(caplog) == []  # a real plane is connected; nothing to warn about


def test_an_empty_token_does_not_invent_a_url_and_does_not_warn(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = RunboundConfig(token="")

    config.validate()

    assert config.control_plane_url is None
    assert _warnings(caplog) == []


def test_a_configured_url_is_left_alone_when_a_token_is_also_set(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = RunboundConfig(control_plane_url="https://already.example", token="k")

    config.validate()

    assert config.control_plane_url == "https://already.example"
    assert _warnings(caplog) == []  # a plane is configured; there is somewhere to send it


# --- the reworded url-requires-a-credential error ---------------------------


def test_url_without_a_token_still_raises_naming_both_modes():
    """This error is worded around the two modes; the requirement — a url
    with no credential at all is a typo, not a local fallback — is unchanged.
    See `tests/test_plane_modes.py` for the modes themselves.
    """
    with pytest.raises(ValueError) as excinfo:
        RunboundConfig(control_plane_url="https://plane.example.com").validate()

    message = str(excinfo.value)
    assert "self-hosted plane needs a token" in message
    assert 'token=""' in message
    assert "hosted plane" in message


# --- enforcement: identical either way -------------------------------------


def _synthetic_run(config: RunboundConfig) -> list:
    """Tool policy, and the latch it leaves: nothing a token may change."""
    config.tool_policy = ToolPolicy(deny=["wire_transfer"], on_violation="block_and_latch")
    config.validate()
    engine = Engine(config)
    state = SessionState("s1")
    outcomes: list = []

    try:
        engine.enforce_policy(state, call("wire_transfer"))
        outcomes.append(("policy", "allowed"))
    except PolicyViolation as exc:
        outcomes.append(("policy", exc.violation.tool, exc.violation.rule))

    # the latch it left behind reapplies on the very next event
    try:
        engine.process(state, tool_event(1))
        outcomes.append(("event", "ok"))
    except GuardrailTripped as exc:
        outcomes.append(("event", "tripped", exc.anomaly.detector, exc.anomaly.severity))

    try:
        engine.enforce_policy(SessionState("s2"), call("search"))
        outcomes.append(("allowed", "search"))
    except PolicyViolation as exc:
        outcomes.append(("policy", exc.violation.tool))

    return outcomes


def test_policy_and_latch_are_identical_with_and_without_a_token():
    without_token = RunboundConfig()
    with_token = RunboundConfig(control_plane_url="https://plane.test", token="k")

    outcomes = _synthetic_run(without_token)
    assert outcomes[0][0] == "policy" and outcomes[0][1] == "wire_transfer"
    assert outcomes == _synthetic_run(with_token)
