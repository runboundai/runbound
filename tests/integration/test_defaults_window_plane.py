"""The defaults window against a REAL plane (MCP-X1 finding): a destructive tool of a brand-new service never runs unasked right after the worker connects.

A new service starts with enforced standard defaults (destructive: ask) but its first hello announces ``policy_version: 0``. The worker used to lift its refusal of consequential
classes on that hello and run the call with no defaults applied, until a later hello announced the policy about two seconds on. The call is made the moment the link reports
``connected`` (the first answered hello), which is exactly where the window was, and again once the worker is steady.
"""

from __future__ import annotations

import threading
import time

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, plane  # noqa: F401  (fixture)

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_a_destructive_tool_with_no_row_never_runs_unasked_the_moment_a_new_service_connects(plane):  # noqa: F811
    service, ran = "window-new", []

    @runbound.tool(effects={"destructive"})
    def issue_refund(customer_id: str, amount: float):
        ran.append(amount)
        return "refunded"

    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")  # the instant the floor used to lift

    outcome = {}

    def call(amount):
        try:
            outcome[amount] = ("ran", issue_refund("c-100", amount))
        except runbound.ExecutionRefused as refused:
            outcome[amount] = ("refused", refused.reason)

    first = threading.Thread(target=call, args=(1.0,), daemon=True)
    first.start()
    first.join(2.0)
    assert not ran, f"the destructive call ran with nobody asked: {outcome}"  # refused by the floor, or waiting for a person: never run

    # a settled worker: the defaults are applied and the call asks
    time.sleep(0.5)
    _until(lambda: runbound.tool_results() is not None and api._SHARED.defaults() is not None, what="the standard defaults to be applied")
    second = threading.Thread(target=call, args=(2.0,), daemon=True)
    second.start()
    _until(lambda: plane.request("GET", f"/v1/admin/approvals?status=pending&service={service}")[1].get("approvals"), what="the approval request of the settled worker")
    assert not ran, "still waiting for a person"
