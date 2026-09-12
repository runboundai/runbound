"""Tests for the token as the SDK's one connection to a control plane.

``GuardrailConfig.validate()`` normalizes ``token``: an ``api_key`` folds into
it (with a one-time deprecation warning, whether or not ``token`` was also
set), an unset token is read from ``RUNBOUND_TOKEN`` (blank/whitespace
counts as unset), and a non-empty token with no ``control_plane_url`` implies
the hosted plane — which does not exist yet (``HOSTED_PLANE_URL`` is
``None``), so today that instead logs one WARNING and the process stays
local. A url with no token still raises, worded around ``token`` now instead
of ``api_key``.

That is the *whole* of what a token does inside this process. Wave 31 moved
delivery (Slack, PagerDuty, a signed webhook) out of the SDK entirely — the
owner's call was that the free SDK detects and the control plane, paid,
routes and delivers — so there is no "alerters built from config" interface
left to test here at all: `Engine` has no `alerters` and builds nothing to
send. What stays token-gated is *fleet mode* (the plane calls in
:mod:`runbound.shared`), not any notification channel.

Neither of those interfaces is allowed to touch detection: every detector,
the latch, tool policy, refusals and the provider circuit must come out
identically whether or not a token is set (see
``test_detection_latch_policy_and_circuit_are_identical_with_and_without_a_token``
below), and every ``on_anomaly`` reaction — ``raise``, ``callback``, ``warn``
— must behave identically too, with or without a token, because none of them
were ever alerters: they are the free, token-free way an anomaly reaches the
customer's own process (see
``test_on_anomaly_reactions_are_identical_with_and_without_a_token``, cited by
name in ``INVARIANTS.md``).
"""

import logging

import pytest

from runbound import config as config_module
from runbound.config import GuardrailConfig
from runbound.engine import Engine
from runbound.events import Anomaly, Event
from runbound.exceptions import GuardrailTripped, PolicyViolation
from runbound.policy import ToolCall, ToolPolicy
from runbound.shared import HOSTED_PLANE_URL
from runbound.state import SessionState

CRITICAL = Anomaly("budget", "critical", "out of money", {})


@pytest.fixture(autouse=True)
def _unwarned(monkeypatch):
    """Every test gets its own turn at the once-per-process token warning.

    ``tests/conftest.py`` already keeps a real ``RUNBOUND_TOKEN`` or
    ``RUNBOUND_PLANE_URL`` out of every test's environment; this resets
    the module-level flag :func:`runbound.config._warn_token_has_no_plane`
    guards, so one test's warning cannot silence the next.
    """
    monkeypatch.setattr(config_module, "_TOKEN_NO_PLANE_WARNED", False)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class StubDetector:
    """Returns a fixed anomaly (or None) every time it is checked."""

    def __init__(self, anomaly: Anomaly | None, name: str = "stub") -> None:
        self.anomaly = anomaly
        self.name = name
        self.checked = 0

    def check(self, state, event, config):
        self.checked += 1
        return self.anomaly


def tool_event(step: int, args_hash: str = "h1") -> Event:
    return Event(kind="tool_call", ts=float(step), step=step, tool_name="search", args_hash=args_hash)


def call(name: str = "wire_transfer") -> ToolCall:
    return ToolCall(name=name, args=(), kwargs={}, session_key="user-9", tags={})


# --- token normalization: the environment -----------------------------------


def test_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_TOKEN", "env-secret")
    config = GuardrailConfig()

    config.validate()

    assert config.token == "env-secret"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_a_blank_or_whitespace_env_token_counts_as_unset(monkeypatch, value):
    monkeypatch.setenv("RUNBOUND_TOKEN", value)
    config = GuardrailConfig()

    config.validate()

    assert config.token is None
    assert config.control_plane_url is None  # nothing was set, so no url either


def test_an_explicit_token_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_TOKEN", "env-secret")
    config = GuardrailConfig(token="explicit")

    config.validate()

    assert config.token == "explicit"


# --- token normalization: api_key folds in ----------------------------------


def test_api_key_folds_into_token_and_warns_once(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(control_plane_url="https://plane.example", api_key="legacy-secret")

    config.validate()

    assert config.token == "legacy-secret"
    assert config.api_key is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "api_key is now token" in warnings[0]
    assert "api_key still works in 0.2.x" in warnings[0]


def test_api_key_alone_still_satisfies_a_plane_url():
    """The alias keeps working end to end, not just as a bare field rename."""
    config = GuardrailConfig(control_plane_url="https://plane.example", api_key="k")

    config.validate()  # must not raise: api_key folded into token before the check


def test_api_key_set_to_none_is_not_a_fold_and_does_not_warn(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(control_plane_url="https://plane.example", token="already-set")

    config.validate()

    assert config.token == "already-set"
    assert _warnings(caplog) == []


def test_api_key_and_token_both_set_warns_and_clears_api_key_but_keeps_the_token(caplog):
    """Review finding: both-passed used to be silent and left two secrets on
    the object. The explicit ``token`` still wins — ``api_key`` never
    overwrites a token the caller actually set — but the fold still warns
    and still clears ``api_key``, exactly as it does when ``token`` was
    unset."""
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(
        control_plane_url="https://plane.example",
        token="explicit-token",
        api_key="legacy-secret",
    )

    config.validate()

    assert config.token == "explicit-token"
    assert config.api_key is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "api_key is now token" in warnings[0]


# --- token normalization: a bare token implies the hosted plane ------------


def test_a_bare_token_leaves_control_plane_url_at_the_hosted_default():
    """``HOSTED_PLANE_URL`` is ``None`` until that plane exists (T108), so
    today this is ``None`` too — the day hosting goes live this assertion
    starts exercising the real URL with no test change required."""
    config = GuardrailConfig(token="k")

    config.validate()

    assert config.control_plane_url == HOSTED_PLANE_URL


def test_a_bare_token_with_no_hosted_plane_warns_once_per_process(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")

    GuardrailConfig(token="k").validate()
    GuardrailConfig(token="k2").validate()

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "nowhere to send it yet" in warnings[0]
    assert "control_plane_url" in warnings[0]


def test_token_alone_prefers_an_explicit_plane_url_env(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    monkeypatch.setenv("RUNBOUND_PLANE_URL", "https://self-hosted.example")
    config = GuardrailConfig(token="k")

    config.validate()

    assert config.control_plane_url == "https://self-hosted.example"
    assert _warnings(caplog) == []  # a real plane is connected; nothing to warn about


def test_an_empty_token_does_not_invent_a_url_and_does_not_warn(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(token="")

    config.validate()

    assert config.control_plane_url is None
    assert _warnings(caplog) == []


def test_a_configured_url_is_left_alone_when_a_token_is_also_set(caplog):
    caplog.set_level(logging.WARNING, logger="runbound")
    config = GuardrailConfig(control_plane_url="https://already.example", token="k")

    config.validate()

    assert config.control_plane_url == "https://already.example"
    assert _warnings(caplog) == []  # a plane is configured; there is somewhere to send it


# --- the reworded url-requires-a-credential error ---------------------------


def test_url_without_a_token_still_raises_naming_both_modes():
    """T111 reworded this error around the two modes; the requirement — a url
    with no credential at all is a typo, not a local fallback — is unchanged.
    See `tests/test_plane_modes.py` for the modes themselves.
    """
    with pytest.raises(ValueError) as excinfo:
        GuardrailConfig(control_plane_url="https://plane.example.com").validate()

    message = str(excinfo.value)
    assert "self-hosted plane needs a token" in message
    assert 'token=""' in message
    assert "hosted plane" in message


# --- detection, the latch, policy and the circuit: identical either way ----


def _synthetic_run(config: GuardrailConfig) -> list:
    """Tool policy, detection, the latch it leaves, and the provider circuit —
    everything the engine enforces, none of which a token may change.
    """
    config.tool_policy = ToolPolicy(deny=["wire_transfer"], on_violation="block_and_latch")
    config.validate()
    engine = Engine(config, detectors=[StubDetector(CRITICAL)])
    state = SessionState("s1")
    outcomes: list = []

    # a tool policy violation latches the session
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

    # the provider circuit is engine state too, and no token reaches it either
    for _ in range(config.circuit_failure_threshold):
        engine.circuit.record_failure("openai@default")
    outcomes.append(("circuit", engine.circuit.state("openai@default")))

    return outcomes


def test_detection_latch_policy_and_circuit_are_identical_with_and_without_a_token():
    without_token = GuardrailConfig(on_anomaly="raise")
    with_token = GuardrailConfig(
        on_anomaly="raise", control_plane_url="https://plane.test", token="k"
    )

    assert _synthetic_run(without_token) == _synthetic_run(with_token)


# --- on_anomaly reactions: identical, and observably a raise/callback/warn -


@pytest.mark.parametrize("on_anomaly", ["warn", "raise", "callback"])
def test_on_anomaly_reactions_are_identical_with_and_without_a_token(on_anomaly, caplog):
    """Rewritten (T108 review): rather than compare only the anomaly detector
    a raise carries, this asserts the actual observable difference each
    reaction makes — ``raise`` raises, ``callback`` receives the anomaly,
    ``warn`` logs it — and does so identically with and without a token,
    which is the whole point: none of these three is, or was ever, gated by
    one. ``validate()`` is called explicitly on both arms too, so a token
    that made validation itself behave differently would be caught here,
    not just a difference in engine behavior post-validation.
    """
    caplog.set_level(logging.WARNING, logger="runbound")

    def run(config_kwargs: dict) -> dict:
        seen: list[Anomaly] = []
        kwargs = {"callback": seen.append} if on_anomaly == "callback" else {}
        config = GuardrailConfig(on_anomaly=on_anomaly, **kwargs, **config_kwargs)
        config.validate()
        engine = Engine(config, detectors=[StubDetector(CRITICAL)])
        caplog.clear()

        raised = None
        try:
            engine.process(SessionState("s1"), tool_event(1))
        except GuardrailTripped as exc:
            raised = exc.anomaly

        return {
            "raised": raised,
            "received": list(seen),
            "logged": CRITICAL.message in caplog.text,
        }

    without_token = run({})
    with_token = run({"control_plane_url": "https://plane.test", "token": "k"})

    assert without_token == with_token

    outcome = without_token
    if on_anomaly == "raise":
        assert outcome["raised"] is CRITICAL
        assert outcome["received"] == []
        # The three reactions are alternatives, not layers: raising does not
        # also log. INVARIANTS.md cites this test by name for exactly that
        # sentence, so it has to be the thing actually asserted here.
        assert outcome["logged"] is False
    elif on_anomaly == "callback":
        assert outcome["raised"] is None
        assert outcome["received"] == [CRITICAL]
        # Same alternative-not-layer property as above: a callback receiving
        # the anomaly does not also log it.
        assert outcome["logged"] is False
    else:
        assert outcome["raised"] is None
        assert outcome["received"] == []
        assert outcome["logged"] is True
