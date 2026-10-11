"""The public surface is exactly what API.md lists; removed names are gone."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import runbound

API_MD = Path(__file__).resolve().parent.parent / "API.md"

REMOVED = (
    "wrap", "unpatch", "llm", "record_call", "budget", "BudgetView",
    "circuit_state", "inflight_calls", "active_sessions", "posture", "envelope",
    "safe_mode", "enter_safe_mode", "exit_safe_mode", "fleet_status",
    "is_tripped", "clear", "session_status", "key_hash",
    "verify_webhook_signature", "Refusal",
)


@pytest.mark.parametrize("name", REMOVED)
def test_removed_name_is_gone(name: str) -> None:
    assert name not in runbound.__all__
    with pytest.raises(AttributeError):
        getattr(runbound, name)


@pytest.mark.parametrize("name", REMOVED)
def test_removed_name_is_gone_from_the_api_module_too(name: str) -> None:
    from runbound import api

    assert not hasattr(api, name)


def test_removed_modules_do_not_import() -> None:
    import importlib

    for module in ("posture", "circuit", "budget", "detectors", "pricing", "wrappers.openai_wrapper"):
        with pytest.raises(ImportError):
            importlib.import_module(f"runbound.{module}")


def test_assert_guarded_raises_runtime_error_when_nothing_is_decorated() -> None:
    from runbound import api

    api._teardown_for_tests()
    with pytest.raises(RuntimeError):
        runbound.assert_guarded()


def _listed_in_api_md() -> set[str]:
    return set(re.findall(r"^- `([A-Za-z_][\w]*)`", API_MD.read_text(encoding="utf-8"), re.M)) - {"__version__"}


def test_api_md_lists_exactly_the_exported_names() -> None:
    assert _listed_in_api_md() == set(runbound.__all__) - {"__version__"}


def test_every_exported_name_resolves() -> None:
    for name in runbound.__all__:
        assert hasattr(runbound, name), name
