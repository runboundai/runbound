"""Plane loss against a real plane: kill it, and the guard fails closed where it must (S7)."""

from __future__ import annotations

import threading
import time

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def start(plane, service="plane-loss", **config):
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2,
                  consequential_grace_s=15, **config)  # the shortest grace, so the test does not wait a minute


def bounded(fn, seconds, what):
    """Run ``fn`` on a daemon thread and give it ``seconds``: a call that waits for an answer nobody can give FAILS the test instead of hanging the run. Returns ``("ok", value)`` or ``("raised", exception)``."""
    box = []

    def run():
        try:
            box.append(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - the test inspects whatever the call raised
            box.append(("raised", exc))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, f"{what} did not return within {seconds} s (a call that waits for an approval nobody can give)"
    return box[0]


def tools():
    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop_table(name: str):
        return "dropped"

    @runbound.tool(effects={"read"}, reviewed=True)
    def look(name: str):
        return "found"

    return drop_table, look


def start_without_defaults(plane, service):
    """A service that never adopted the standard defaults (one that predates them): the plane gives a NEW service enforced defaults the moment its first hello creates it,
    so create it with a hello of our own and take the defaults row away before the worker under test starts. Its worker then keeps today's per-class grace."""
    import sqlite3

    status, body = plane.request("POST", "/v1/hello", {"service": service, "worker_id": "seed", "sdk_version": "0.11.2", "tools_hash": "x", "tools": []},
                                 headers={"X-Runbound-Service": service, "X-Runbound-Worker": "seed"})
    assert status == 200, body
    connection = sqlite3.connect(str(plane.db))
    removed = connection.execute("DELETE FROM service_defaults WHERE service = ?", (service,)).rowcount
    assert removed == 1, "the seed hello should have created the service with its standard defaults"
    connection.commit()
    connection.close()
    start(plane, service)


def test_killing_the_plane_refuses_a_destructive_tool_after_the_grace_where_the_service_has_no_defaults_and_a_read_tool_runs(private_plane):  # noqa: F811
    start_without_defaults(private_plane, "plane-loss-legacy")
    drop_table, look = tools()
    _until(lambda: runbound.plane_status().mode == "connected", what="a hello")
    assert api._SHARED.defaults() is None
    assert drop_table("t") == "dropped" and look("t") == "found"  # no defaults: nobody is asked, the old per-class rules apply

    private_plane.kill()
    _until(lambda: runbound.plane_status().mode == "degraded", 10, "three missed hellos")
    assert drop_table("t") == "dropped"  # degraded: everything still runs on the cache
    assert look("t") == "found"

    def refused():
        try:
            drop_table("t")
        except runbound.ExecutionRefused as exc:
            return exc

    caught = _until(refused, 30, "the grace to run out")  # 15 s of silence
    assert caught.reason == "plane_unavailable" and caught.retryable
    assert look("t") == "found"  # a read tool keeps running on what it has


def test_killing_the_plane_does_not_refuse_a_destructive_tool_a_row_allows_while_the_service_holds_its_defaults(private_plane):  # noqa: F811
    """A NEW service holds enforced standard defaults from its first hello (destructive: ask). Under them the sealed policy is enforced in full for its validity window
    (24 h): a tool an operator's row allows keeps running after the plane is gone, past the 15 s grace, which applies only to a service without defaults."""
    start(private_plane, "plane-loss-defaults")
    drop_table, look = tools()
    _until(lambda: runbound.plane_status().mode == "connected", what="a hello")
    _until(lambda: api._SHARED.defaults() is not None, what="the standard defaults to be applied")
    status, body = private_plane.request("POST", "/v1/admin/tool-policies", {"service": "plane-loss-defaults", "tool": "drop_table", "lines": [{"decision": "allow"}], "otherwise": "allow"})
    assert status in (200, 201), body
    status, body = private_plane.request("POST", "/v1/admin/tool-policies/plane-loss-defaults/drop_table/rollout", {"state": "enforced", "comment": "plane loss"})
    assert status == 200, body
    _until(lambda: api._SHARED.row_for("drop_table") is not None, what="the row for drop_table")
    assert drop_table("t") == "dropped"

    private_plane.kill()
    _until(lambda: runbound.plane_status().mode == "degraded", 10, "three missed hellos")
    time.sleep(18)  # past the 15 s grace
    assert drop_table("t") == "dropped" and look("t") == "found"  # the sealed policy and its defaults are enforced in full


def test_killing_the_plane_refuses_an_unruled_destructive_tool_approval_unavailable_while_the_service_holds_its_defaults(private_plane):  # noqa: F811
    """Under the enforced defaults an unruled destructive tool ASKS; with the plane gone nobody can be asked, so it is refused at once (no grace involved) and its body never runs."""
    start(private_plane, "plane-loss-unruled")
    ran = []

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop_table(name: str):
        ran.append(name)
        return "dropped"

    _until(lambda: runbound.plane_status().mode == "connected", what="a hello")
    _until(lambda: api._SHARED.defaults() is not None, what="the standard defaults to be applied")
    private_plane.kill()
    _until(lambda: runbound.plane_status().mode == "degraded", 10, "three missed hellos")

    outcome, caught = bounded(lambda: drop_table("t"), 10, "the unruled destructive call")
    assert outcome == "raised" and isinstance(caught, runbound.ExecutionRefused), f"the call must be refused, got {outcome}: {caught!r}"
    assert caught.reason == "approval_unavailable" and ran == []


def test_a_restart_enforces_the_cached_rows_before_the_first_hello(private_plane, tmp_path):  # noqa: F811
    # H6: the organisation document is gone from the plane (410), so the cached rule under test is a console row.
    status, body = private_plane.request("POST", "/v1/admin/tool-policies", {"service": "plane-loss-boot", "tool": "look", "blocked": True})
    assert status in (200, 201), body
    status, body = private_plane.request("POST", "/v1/admin/tool-policies/plane-loss-boot/look/rollout", {"state": "enforced", "comment": "test"})
    assert status == 200, body
    start(private_plane, "plane-loss-boot")
    _, look = tools()
    _until(lambda: api._SHARED.row_for("look"), what="the row for look")
    with pytest.raises(runbound.ExecutionRefused) as refused:
        look("t")
    assert refused.value.reason == "tool_blocked"
    private_plane.kill()
    api._teardown_for_tests()  # the worker restarts with the plane still dead

    start(private_plane, "plane-loss-boot")
    _, look = tools()
    assert runbound.plane_status().mode in ("connecting", "degraded")  # no hello has been answered
    with pytest.raises(runbound.ExecutionRefused) as refused:  # enforced from the cache, before any hello
        look("t")
    assert refused.value.reason == "tool_blocked"


def test_a_destructive_tool_waits_for_a_hello_when_there_is_neither_a_hello_nor_a_cache(private_plane, tmp_path):  # noqa: F811
    # The precondition is made, not hoped for: the plane is dead before the worker starts (so no hello can ever be answered) and its cache directory is this test's own (so no
    # earlier run's cache can be loaded). It used to start the worker and kill the plane at once, which a loaded machine could lose: the first hello and policy landed first.
    private_plane.kill()
    start(private_plane, "plane-loss-nocache", cache_dir=str(tmp_path))
    drop_table, _ = tools()
    shared = api._SHARED
    assert shared.awaiting_first_policy() is True and shared._last_success is None and not shared._sealed_cache_loaded
    with pytest.raises(runbound.ExecutionRefused) as caught:
        drop_table("t")
    assert caught.value.reason == "plane_unavailable"
