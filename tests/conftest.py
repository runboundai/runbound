"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _clean_plane_environment(monkeypatch):
    """No real ``RUNBOUND_TOKEN``/``RUNBOUND_PLANE_URL`` leaks into a test.

    Without this, a developer's (or CI's) own shell can turn a "no plane
    configured" test into a "plane configured, and unreachable" test: three
    of Wave 31's own regression tests passed only because the shell running
    them happened to be clean, and a stray ``RUNBOUND_TOKEN`` was enough to
    send every ``init()`` in the suite hunting for a control plane that was
    never going to answer — a ~600s timeout across the run, not a failure
    anyone would have connected to an environment variable.
    """
    monkeypatch.delenv("RUNBOUND_TOKEN", raising=False)
    monkeypatch.delenv("RUNBOUND_PLANE_URL", raising=False)
