"""Engine helpers for the R0 tests: a hand-moved link over the SDK's own RemoteState.

Imports nothing from ``runbound._rules``: the legacy tests use it too, and they must collect and pass on a source
tree that has no evaluator at all.
"""

from __future__ import annotations

from typing import Any


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


#: What a plane serves a service that has nothing: an empty policy. A worker that reads it holds a policy (an empty one); one whose read fails holds none.
EMPTY_POLICY = {"version": 0, "policy": None, "rows": [], "rows_hash": "h"}


class FakeClient:
    """A plane double that is never reached: ``policy()`` serves ``served`` once, nothing else answers."""

    key_state = "unknown"

    def __init__(self, served: Any = None) -> None:
        self.served = served

    def policy(self, service: str) -> Any:
        return self.served

    def hello(self, payload: Any) -> Any:
        return None

    def approvals(self, *args: Any, **kwargs: Any) -> tuple:
        return None, None


def engine_on_link(tmp_path: Any, *, rows: Any = None, served: Any = None, clock: "Clock | None" = None, **init: Any) -> Any:
    """``runbound.init`` (local), then a ``RemoteState`` with no poller as the engine's shared state. Returns it."""
    import runbound
    from runbound import api
    from runbound.config import RunboundConfig
    from runbound.shared import RemoteState

    api._teardown_for_tests()
    runbound.init(cache_dir=str(tmp_path / "cache"), **init)
    cfg = RunboundConfig(control_plane_url="https://plane.example", token="k", service="svc", worker_id="w",
                         cache_dir=str(tmp_path / "cache"))
    cfg.validate()
    shared = RemoteState(FakeClient(served), None, cfg, **({} if clock is None else {"now": clock}))
    api._ENGINE.shared = shared
    if rows is not None:
        shared._set_rows(rows, "hash-1")
    return shared


def outcome_of(fn: Any, *args: Any) -> str:
    """``"ran"`` or ``"refused:<reason>"`` for one call of a decorated tool."""
    from runbound.exceptions import GuardrailTripped

    try:
        fn(*args)
        return "ran"
    except GuardrailTripped as refusal:
        return f"refused:{getattr(refusal, 'reason', '?')}"
