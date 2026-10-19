"""PS-OFFLINE-2 against the REAL producer (OFFLINE-1): a new service is served a 15-minute offline window as a sibling of its ``defaults``, the worker reads and applies it (its
defaults are still read: no ``defaults_problem``), reports it on the hello, and once the plane is gone and the window has ended every consequential tool refuses
``plane_unavailable``, naming the window. The clock is moved by editing the confirmation the worker holds, not by sleeping 15 minutes."""

from __future__ import annotations

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


def test_a_new_service_is_served_15_minutes_and_the_worker_applies_it_and_refuses_after_it(private_plane):  # noqa: F811
    plane = private_plane
    runbound.init(control_plane_url=plane.url, token=KEY, service="offline-real", control_plane_poll_s=0.2)
    ran = []

    @runbound.tool(effects={"destructive"}, reviewed=True)
    def drop_table(name: str):
        ran.append(name)
        return "dropped"

    _until(lambda: runbound.plane_status().mode == "connected", what="a hello")
    shared = api._SHARED
    _until(lambda: shared.defaults() is not None, what="the standard defaults")
    raw = (shared._defaults_raw or {}).get("offline")
    if raw is None:
        pytest.skip("this plane does not serve the offline window (OFFLINE-1)")
    assert raw["execution_s"] == 900 and raw["source"] == "default" and raw["review"] is False
    assert shared.defaults_problem() is None and shared.policy_unsupported() == [], "the defaults are still read with the sibling key present"
    assert shared.offline_window_s() == 900
    status = runbound.plane_status()
    assert status.offline_window_s == 900 and status.offline_deadline is not None

    plane.kill()
    _until(lambda: runbound.plane_status().mode == "degraded", 10, "three missed hellos")
    shared._confirmed_wall -= 901  # the window ends (a stand-in for waiting 15 minutes)
    shared._confirmed_mono -= 901
    with pytest.raises(runbound.ExecutionRefused) as refused:
        drop_table("t")
    assert refused.value.reason == "plane_unavailable" and "offline-execution window of 900 s" in str(refused.value) and ran == []
