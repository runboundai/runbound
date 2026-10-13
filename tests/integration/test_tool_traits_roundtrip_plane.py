"""The tool report's traits round-trip through a REAL control plane (TR1-O1; register C7, K10).

``effects``, ``polling``, ``idempotent`` and ``retryable`` ride every tool report. The plane stores them as four typed,
nullable columns and serves them on its tools read (``GET /v1/admin/tools``): a value the SDK reported arrives with the same
value, and a value no SDK reported arrives as NULL (never guessed). The cases:

* a decorated tool with every trait SET arrives with those values;
* a decorated tool with none declared arrives as what the SDK said: effects [] and the three flags false (reported, empty);
* a tool the SDK only OBSERVED (a framework dispatched it; nothing declared) arrives NULL: the SDK sends [] / false placeholders
  for it, and the plane stores NULL for every entry with ``decorated: false`` (register K12);
* an old-style report with the keys ABSENT arrives NULL.

Skipped on a plane that predates the columns (no row carries any of the four keys).
"""

from __future__ import annotations

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, plane  # noqa: F401  (fixture)

pytestmark = pytest.mark.plane

TRAITS = ("effects", "polling", "idempotent", "retryable")


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def start(plane, service):  # noqa: F811
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2)
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def inventory(plane, service, wanted):  # noqa: F811
    def read():
        status, body = plane.request("GET", f"/v1/admin/tools?service={service}")
        assert status == 200, body
        rows = {row["tool"]: row for row in body.get("tools", body if isinstance(body, list) else [])}
        return rows if all(name in rows for name in wanted) else None

    return _until(read, what=f"the tools {wanted} in the plane's inventory")


def require_columns(rows):
    if not any(key in row for row in rows.values() for key in TRAITS):
        pytest.skip("this plane predates TR1-A1: its tools read carries none of effects/polling/idempotent/retryable")


def test_a_tool_with_every_trait_set_arrives_with_those_values(plane):  # noqa: F811
    start(plane, "traits-set")

    @runbound.tool(effects={"financial", "external"}, polling=True, idempotent=True, retryable=True, reviewed=True)
    def refund(x: int = 0):
        return "ok"

    row = inventory(plane, "traits-set", ["refund"])["refund"]
    require_columns({"refund": row})
    assert sorted(row["effects"]) == ["external", "financial"]
    assert (row["polling"], row["idempotent"], row["retryable"]) == (True, True, True)


def test_a_decorated_tool_that_declares_nothing_arrives_as_reported_empty_not_null(plane):  # noqa: F811
    start(plane, "traits-unset")

    @runbound.tool(reviewed=True)
    def plain(x: int = 0):
        return "ok"

    row = inventory(plane, "traits-unset", ["plain"])["plain"]
    require_columns({"plain": row})
    assert row["effects"] == [] and (row["polling"], row["idempotent"], row["retryable"]) == (False, False, False)


def test_a_tool_the_sdk_only_observed_arrives_null(plane):  # noqa: F811
    from runbound import _coverage

    start(plane, "traits-observed")
    _coverage.tool_requested("framework_dispatched")  # what the LangChain handler does for a tool nobody decorated
    row = inventory(plane, "traits-observed", ["framework_dispatched"])["framework_dispatched"]
    require_columns({"framework_dispatched": row})
    assert [row.get(key) for key in TRAITS] == [None, None, None, None]


def test_an_old_style_report_with_the_keys_absent_arrives_null(plane):  # noqa: F811
    status, body = plane.request("POST", "/v1/hello", {
        "service": "traits-old", "worker_id": "old-1", "sdk_version": "0.11.2",
        "tools_hash": "abc", "tools": [{"name": "legacy_tool", "decorated": True, "params": [], "rules": {}}],
    })
    assert status == 200, body
    row = inventory(plane, "traits-old", ["legacy_tool"])["legacy_tool"]
    require_columns({"legacy_tool": row})
    assert [row.get(key) for key in TRAITS] == [None, None, None, None]
