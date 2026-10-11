"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_policy_cache(monkeypatch, tmp_path):
    """Every test gets its own policy cache directory: a cache one test wrote must never be
    enforced by the next test that happens to use the same token and service."""
    monkeypatch.setenv("RUNBOUND_CACHE_DIR", str(tmp_path / "policy-cache"))


@pytest.fixture(autouse=True)
def _clean_plane_environment(monkeypatch):
    """No real ``RUNBOUND_TOKEN``/``RUNBOUND_PLANE_URL`` leaks into a test.

    Without this, a developer's (or CI's) own shell can turn a "no plane
    configured" test into a "plane configured, and unreachable" test: a
    handful of regression tests once passed only because the shell running
    them happened to be clean, and a stray ``RUNBOUND_TOKEN`` was enough to
    send every ``init()`` in the suite hunting for a control plane that was
    never going to answer — a ~600s timeout across the run, not a failure
    anyone would have connected to an environment variable.
    """
    monkeypatch.delenv("RUNBOUND_TOKEN", raising=False)
    monkeypatch.delenv("RUNBOUND_PLANE_URL", raising=False)


REMOVED_API = (
    "wrap|unpatch|llm|record_call|budget|BudgetView|circuit_state|inflight_calls|"
    "active_sessions|posture|envelope|safe_mode|enter_safe_mode|exit_safe_mode|"
    "fleet_status|is_tripped|clear|session_status|key_hash|verify_webhook_signature|Refusal"
)


def skip_if_uses_removed_api(path) -> None:
    """Skip an example that teaches a name API.md no longer lists.

    The examples are deleted or rewritten with the docs (S2 / phase D); until
    then they must not keep the suite red for an API that is gone on purpose.
    """
    import re

    src = path.read_text(encoding="utf-8")
    if re.search(rf"\brunbound\.({REMOVED_API}|demo)\b|from runbound(\.demo)? import[^\n]*\b({REMOVED_API}|main)\b", src):
        pytest.skip(f"{path.name} uses an API removed in S1 (see API.md)")
