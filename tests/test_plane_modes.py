"""Two kinds of connected customer, and the one question that tells them apart.

A ``token`` on its own means the customer is on the hosted plane and we
resolve the endpoint for them; a ``control_plane_url`` means they run the
plane in their own cluster. They are
not one setting with a truthiness test, and the difference is worth a name —
:attr:`GuardrailConfig.plane_mode`.

The table test at the bottom is the regression net for the blocker the Opus
review of this wave's first cut found: two call sites asked two different
falsiness questions about ``control_plane_url``, and ``""`` fell into the gap
between them — it looked like a configured plane to one and no plane to the
other. There is now one answer, and this file asserts every reader agrees
with it for every combination of the two fields.
"""

import itertools
import logging

import pytest

from runbound import shared
from runbound.config import GuardrailConfig

URL = "http://plane.test:8090"


@pytest.fixture(autouse=True)
def _no_ambient_connection(monkeypatch):
    """Neither variable may leak in from whoever is running the suite."""
    monkeypatch.delenv("RUNBOUND_TOKEN", raising=False)
    monkeypatch.delenv("RUNBOUND_PLANE_URL", raising=False)


def _config(**kwargs) -> GuardrailConfig:
    config = GuardrailConfig(**kwargs)
    config.validate()
    return config


# --- the two modes, from the call ------------------------------------------


def test_no_token_and_no_url_is_off():
    config = _config()
    assert config.plane_mode == "off"
    assert config.control_plane_url is None


def test_a_url_is_self_hosted():
    config = _config(control_plane_url=URL, token="k")
    assert config.plane_mode == "self_hosted"
    assert config.control_plane_url == URL


def test_a_self_hosted_plane_may_have_no_auth():
    """``token=""`` is the deliberate credential of a plane behind the
    customer's own network boundary."""
    config = _config(control_plane_url=URL, token="")
    assert config.plane_mode == "self_hosted"


def test_a_url_with_no_token_at_all_is_rejected_naming_both_modes():
    with pytest.raises(ValueError) as caught:
        _config(control_plane_url=URL)
    message = str(caught.value)
    assert "self-hosted plane needs a token" in message
    # The error teaches the other mode too, because a customer who meant
    # "hosted" and pasted a url is the likeliest reader of it.
    assert "hosted plane" in message


def test_a_bare_token_is_hosted_once_there_is_a_hosted_plane(monkeypatch):
    """The mode the owner asked for: a token and nothing else connects.

    ``HOSTED_PLANE_URL`` is ``None`` until that plane is live, so this pins
    the behaviour by standing in a url the way flipping the constant will.
    """
    monkeypatch.setattr(shared, "HOSTED_PLANE_URL", "https://api.runbound.test")
    import runbound.config as config_module

    monkeypatch.setattr(config_module, "HOSTED_PLANE_URL", "https://api.runbound.test")
    config = _config(token="ag_live_x")
    assert config.plane_mode == "hosted"
    assert config.control_plane_url == "https://api.runbound.test"


def test_a_bare_token_with_no_hosted_plane_yet_warns_once_and_stays_off(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    # The warning is once per process on purpose, so an earlier test in this
    # run may already have spent it; this asks for the first one again.
    import runbound.config as config_module

    monkeypatch.setattr(config_module, "_TOKEN_NO_PLANE_WARNED", False)
    config = _config(token="ag_live_x")
    assert config.plane_mode == "off"
    assert config.control_plane_url is None
    assert "nowhere to send it yet" in caplog.text
    # The token itself is never in the warning.
    assert "ag_live_x" not in caplog.text


# --- the two modes, from the environment -----------------------------------


def test_an_env_url_with_no_token_warns_once_and_stays_local(caplog, monkeypatch):
    """The env counterpart of ``test_a_url_with_no_token_at_all_is_rejected_naming_both_modes``,
    and the opposite outcome on purpose. A url passed to ``init()`` is one
    engineer's typo and still raises; a url that showed up via
    ``RUNBOUND_PLANE_URL`` is a deployment fact that predates every
    service's token, so it cannot be allowed to fail ``init()`` — it warns
    once and the process guards locally instead."""
    caplog.set_level(logging.WARNING, logger="runbound")
    import runbound.config as config_module

    # Once per process, like the token-side warning above; ask for the
    # first one again in case an earlier test in this run already spent it.
    monkeypatch.setattr(config_module, "_ENV_PLANE_URL_NO_TOKEN_WARNED", False)
    monkeypatch.setenv("RUNBOUND_PLANE_URL", URL)
    config = _config()
    assert config.plane_mode == "off"
    assert config.control_plane_url is None
    assert "RUNBOUND_PLANE_URL" in caplog.text
    assert "no token" in caplog.text or "token was found" in caplog.text
    # The url itself is never in the warning.
    assert URL not in caplog.text


def test_an_env_url_with_no_token_warns_only_once(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="runbound")
    import runbound.config as config_module

    monkeypatch.setattr(config_module, "_ENV_PLANE_URL_NO_TOKEN_WARNED", False)
    monkeypatch.setenv("RUNBOUND_PLANE_URL", URL)
    _config()
    _config()
    assert caplog.text.count("RUNBOUND_PLANE_URL is set") == 1


def test_the_url_comes_from_the_environment_too(monkeypatch):
    monkeypatch.setenv("RUNBOUND_PLANE_URL", URL)
    config = _config(token="k")
    assert config.plane_mode == "self_hosted"
    assert config.control_plane_url == URL


def test_an_explicit_url_beats_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_PLANE_URL", "http://wrong.test")
    config = _config(control_plane_url=URL, token="k")
    assert config.control_plane_url == URL


def test_both_facts_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RUNBOUND_TOKEN", "ag_live_env")
    monkeypatch.setenv("RUNBOUND_PLANE_URL", URL)
    config = _config()
    assert config.plane_mode == "self_hosted"
    assert config.token == "ag_live_env"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_url_from_the_environment_is_no_plane(monkeypatch, blank):
    monkeypatch.setenv("RUNBOUND_PLANE_URL", blank)
    config = _config()
    assert config.plane_mode == "off"
    assert config.control_plane_url is None


def test_an_empty_token_never_reads_runbound_token_from_the_environment(monkeypatch):
    """``token=""`` answers "what is my credential", nothing more: it stops
    ``RUNBOUND_TOKEN`` from being read, but it says nothing about
    ``control_plane_url`` — a ``RUNBOUND_PLANE_URL`` in the environment
    still names a plane, and ``token=""`` then correctly reads as that
    plane's deliberate "no auth" credential (self-hosted), not as "off".
    This used to be misnamed as the process-local pin; it never was one —
    see ``test_an_empty_control_plane_url_pins_a_process_local_whatever_the_environment_says``
    below for the setting that actually does that."""
    monkeypatch.setenv("RUNBOUND_TOKEN", "ag_live_env")
    config = _config(token="")
    assert config.token == ""
    assert config.plane_mode == "off"
    assert config.control_plane_url is None


def test_an_empty_token_with_an_env_url_is_self_hosted_with_no_auth(monkeypatch):
    """The other half of the property above: with a plane url in the
    environment, ``token=""`` does not pin local — it means "that plane, no
    auth", exactly as if ``control_plane_url=""`` had been passed directly."""
    monkeypatch.setenv("RUNBOUND_TOKEN", "ag_live_env")
    monkeypatch.setenv("RUNBOUND_PLANE_URL", URL)
    config = _config(token="")
    assert config.plane_mode == "self_hosted"
    assert config.control_plane_url == URL
    assert config.token == ""


def test_an_empty_control_plane_url_pins_a_process_local_whatever_the_environment_says(
    monkeypatch,
):
    """The actual "never connect, whatever the shell exports" pin, for a
    library embedding this SDK or a test harness that must not inherit
    ambient connection settings. Passing ``control_plane_url=""`` explicitly
    means it is never ``None``, so the environment is never consulted for it
    either — and it normalizes to ``None`` the same as any other blank url.
    Both variables are set here on purpose: the pin has to hold against both
    at once, not just against the one it happens to touch directly."""
    monkeypatch.setenv("RUNBOUND_TOKEN", "ag_live_env")
    monkeypatch.setenv("RUNBOUND_PLANE_URL", URL)
    config = _config(token="", control_plane_url="")
    assert config.plane_mode == "off"
    assert config.control_plane_url is None
    assert shared.build(config).__class__.__name__ == "LocalState"


# --- blank is not a value --------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_url_passed_to_init_is_no_plane(blank):
    config = _config(control_plane_url=blank)
    assert config.plane_mode == "off"
    assert config.control_plane_url is None
    assert shared.build(config).__class__.__name__ == "LocalState"


def test_plane_mode_cannot_be_passed_in():
    """It is a conclusion, not a setting."""
    with pytest.raises(TypeError):
        GuardrailConfig(plane_mode="hosted")


# --- the regression net ----------------------------------------------------


@pytest.mark.parametrize(
    "token,url", list(itertools.product([None, "", "x"], [None, "", URL]))
)
def test_plane_mode_and_shared_build_agree_for_every_combination(token, url):
    """The blocker this file exists for: one falsiness question, one answer.

    ``plane_mode == "off"`` and ``shared.build`` returning ``LocalState``
    must mean exactly the same thing for all nine pairs — in particular for
    the two that used to disagree, where ``control_plane_url`` was ``""``.
    """
    kwargs = {}
    if token is not None:
        kwargs["token"] = token
    if url is not None:
        kwargs["control_plane_url"] = url

    try:
        config = _config(**kwargs)
    except ValueError:
        # A url with no credential at all is rejected outright, which is the
        # third possible answer and never a silent local fallback.
        assert url == URL and token is None
        return

    local = shared.build(config).__class__.__name__ == "LocalState"
    assert local == (config.plane_mode == "off"), (
        f"token={token!r} url={url!r}: plane_mode={config.plane_mode!r} "
        f"but shared.build said {'local' if local else 'remote'}"
    )
