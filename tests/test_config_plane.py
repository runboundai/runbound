"""Tests for the fleet-mode configuration knobs and their validation.

A misconfigured control plane must fail at ``init()``, not halfway through a
production run, and a config that is logged must never carry the token.

The ``token``/``api_key`` normalization itself (folding, the env var, the
hosted-url default) has its own module: ``tests/test_token_and_delivery.py``.
"""

import os
import re

import pytest

from runbound.config import GuardrailConfig

#: ``<host>:<pid>:<6 lowercase hex chars>`` — the collision-proof default
#: worker id shape (T115). The host segment excludes ``:`` so the pid and
#: suffix segments split unambiguously.
_WORKER_ID_RE = re.compile(r"^[^:]+:\d+:[0-9a-f]{6}$")


def plane_config(**overrides) -> GuardrailConfig:
    fields = dict(control_plane_url="https://plane.example.com", token="k")
    fields.update(overrides)
    return GuardrailConfig(**fields)


# --- defaults ---------------------------------------------------------------


def test_fleet_defaults_are_off():
    cfg = GuardrailConfig()

    assert cfg.control_plane_url is None
    assert cfg.token is None
    assert cfg.api_key is None
    assert cfg.service == "default"
    assert cfg.worker_id is None
    assert cfg.control_plane_timeout_s == 0.15
    assert cfg.control_plane_poll_s == 5.0
    assert cfg.export_events is True
    assert cfg.send_session_keys is False
    assert cfg.on_halt == "raise"


def test_default_config_still_validates():
    GuardrailConfig().validate()


def test_a_full_fleet_config_validates():
    plane_config(
        service="checkout",
        worker_id="box-1:7",
        control_plane_timeout_s=0.5,
        control_plane_poll_s=30.0,
        export_events=False,
        send_session_keys=True,
        on_halt="warn",
    ).validate()


# --- secrets stay out of repr ----------------------------------------------


def test_repr_hides_the_token():
    text = repr(plane_config(token="sk-super-secret"))

    assert "sk-super-secret" not in text
    # "token=" rather than a bare "token" substring match, since several
    # unrelated fields ("max_total_tokens", "tokens_per_minute_limit", ...)
    # legitimately contain "token" and would false-positive otherwise.
    assert "token=" not in text


def test_repr_hides_the_api_key():
    """``api_key`` is the deprecated alias for ``token`` and is just as secret:

    it sits on the object (briefly, until validate() folds and clears it)
    exactly like token does, so a config repr'd before validate() is called
    must not leak it either.
    """
    text = repr(plane_config(token=None, api_key="sk-legacy-secret"))

    assert "sk-legacy-secret" not in text
    assert "api_key=" not in text


def test_repr_still_shows_the_plane_url():
    assert "plane.example.com" in repr(plane_config())


# --- url / token -------------------------------------------------------


def test_url_without_a_token_is_rejected():
    with pytest.raises(ValueError, match="token"):
        GuardrailConfig(control_plane_url="https://plane.example.com").validate()


def test_empty_token_is_allowed_for_a_self_hosted_plane():
    GuardrailConfig(control_plane_url="http://localhost:8080", token="").validate()


def test_token_without_a_url_is_allowed():
    GuardrailConfig(token="k").validate()


# --- timeout ----------------------------------------------------------------


@pytest.mark.parametrize("timeout", [0.001, 0.15, 1.0, 2.0])
def test_timeouts_inside_the_window_are_accepted(timeout):
    plane_config(control_plane_timeout_s=timeout).validate()


@pytest.mark.parametrize("timeout", [0.0, -1.0, 2.0001, 30.0])
def test_timeouts_outside_the_window_are_rejected(timeout):
    with pytest.raises(ValueError, match="control_plane_timeout_s"):
        plane_config(control_plane_timeout_s=timeout).validate()


def test_the_timeout_is_checked_even_without_a_plane_url():
    with pytest.raises(ValueError, match="control_plane_timeout_s"):
        GuardrailConfig(control_plane_timeout_s=10.0).validate()


# --- poll interval ----------------------------------------------------------


@pytest.mark.parametrize("poll", [0.0, -5.0])
def test_non_positive_poll_intervals_are_rejected(poll):
    with pytest.raises(ValueError, match="control_plane_poll_s"):
        plane_config(control_plane_poll_s=poll).validate()


def test_a_long_poll_interval_is_fine():
    plane_config(control_plane_poll_s=3600.0).validate()


# --- on_halt ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["raise", "warn"])
def test_valid_halt_modes(mode):
    plane_config(on_halt=mode).validate()


@pytest.mark.parametrize("mode", ["stop", "", None, "RAISE"])
def test_invalid_halt_modes_are_rejected(mode):
    with pytest.raises(ValueError, match="on_halt"):
        plane_config(on_halt=mode).validate()


# --- resolved_worker_id -----------------------------------------------------


def test_resolved_worker_id_prefers_the_configured_value():
    assert plane_config(worker_id="box-1:7").resolved_worker_id() == "box-1:7"


def test_resolved_worker_id_falls_back_to_host_and_pid_and_a_hex_suffix():
    resolved = GuardrailConfig().resolved_worker_id()

    assert _WORKER_ID_RE.match(resolved)
    host, pid, _suffix = resolved.split(":")
    assert len(host) > 0
    assert pid == str(os.getpid())


def test_resolved_worker_id_is_stable_across_calls():
    """Computed once per instance, per the spec — not re-rolled each call."""
    cfg = GuardrailConfig()

    assert cfg.resolved_worker_id() == cfg.resolved_worker_id()


def test_two_configs_in_one_process_get_different_worker_ids():
    """Two containers sharing a hostname must not collide, and neither may

    two instances built back to back in the same process — the whole point
    of the random suffix.
    """
    first = GuardrailConfig().resolved_worker_id()
    second = GuardrailConfig().resolved_worker_id()

    assert first != second


def test_resolved_worker_id_survives_a_hostname_lookup_failure(monkeypatch):
    import socket

    def boom() -> str:
        raise OSError("no hostname")

    monkeypatch.setattr(socket, "gethostname", boom)

    resolved = GuardrailConfig().resolved_worker_id()

    assert re.match(rf"^unknown:{os.getpid()}:[0-9a-f]{{6}}$", resolved)


def test_resolved_worker_id_falls_back_to_a_fixed_suffix_when_secrets_fails(
    monkeypatch,
):
    """``secrets`` failing must never crash worker-id resolution (fail-open):

    the suffix becomes the fixed placeholder ``"000000"`` instead.
    """
    import secrets

    def boom(_n: int) -> str:
        raise OSError("no randomness available")

    monkeypatch.setattr(secrets, "token_hex", boom)

    resolved = GuardrailConfig().resolved_worker_id()

    assert resolved == f"{resolved.split(':')[0]}:{os.getpid()}:000000"


def test_resolved_worker_id_ignores_an_empty_configured_value():
    resolved = GuardrailConfig(worker_id="").resolved_worker_id()

    assert _WORKER_ID_RE.match(resolved)


def test_an_explicit_worker_id_is_used_verbatim_unchanged():
    assert GuardrailConfig(worker_id="box-1:7").resolved_worker_id() == "box-1:7"
