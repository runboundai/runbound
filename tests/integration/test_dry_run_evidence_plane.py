"""The console's dry-run evidence counts every call the candidate row would have refused, through a REAL plane (C24 / J-EVD-1).

A dry-run row that blocks a tool; one session makes N calls (all run: nothing is refused); the plane's
``GET /v1/admin/tool-policies/{service}/{tool}`` reports ``evidence.would_refuse_7d == N``. It was 1 (one event per
session x rule x tool).
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, plane  # noqa: F401  (fixture)
from .test_rows_quota_plane import have_row, put_row, start

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def evidence(plane, service, tool):  # noqa: F811
    status, body = plane.request("GET", f"/v1/admin/tool-policies/{service}/{tool}")
    assert status == 200, body
    return body["row"]["evidence"]["would_refuse_7d"]


def test_the_evidence_counts_every_call_a_dry_run_row_would_have_refused(plane):  # noqa: F811
    start(plane, "evd")
    put_row(plane, "evd", "wire", state="dry_run", blocked=True)

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire(x: int = 0):
        return "sent"

    have_row("wire")
    with runbound.session("run-evd"):
        for i in range(7):
            assert wire(i) == "sent"  # a dry run refuses nothing
    assert _until(lambda: evidence(plane, "evd", "wire") == 7, what="seven would-refuse events in the evidence") is True


def test_two_sessions_add_up(plane):  # noqa: F811
    start(plane, "evd2")
    put_row(plane, "evd2", "wire", state="dry_run", blocked=True)

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire(x: int = 0):
        return "sent"

    have_row("wire")
    for key, calls in (("a", 3), ("b", 4)):
        with runbound.session(key):
            for i in range(calls):
                wire(i)
    assert _until(lambda: evidence(plane, "evd2", "wire") == 7, what="3 + 4 would-refuse events") is True


def test_a_dry_run_record_is_not_counted_as_a_refusal_by_the_planes_reads(plane):  # noqa: F811
    """The console's Refused facet is 'rule-stopped calls only'. A dry-run record is told apart by ``payload.reacted == "dry_run"``
    (and ``dry_run: true`` on the anomaly event); the reads must leave it out of ``refusals``."""
    start(plane, "evd3")
    put_row(plane, "evd3", "wire", state="dry_run", blocked=True)

    @runbound.tool(effects={"read"}, reviewed=True)
    def wire(x: int = 0):
        return "sent"

    have_row("wire")
    with runbound.session("run-evd3"):
        for i in range(5):
            wire(i)
    _until(lambda: evidence(plane, "evd3", "wire") == 5, what="the evidence")
    status, body = plane.request("GET", "/v1/admin/tools?service=evd3")
    assert status == 200, body
    tool = next(t for t in body["tools"] if t["tool"] == "wire")
    assert tool["refusals"] == 0, tool  # nothing was stopped
