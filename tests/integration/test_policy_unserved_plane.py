"""C22 / C12 against the REAL control plane's locked failure: ``GET /v1/policy`` -> 503 ``policy_rows_unavailable``.

The plane runs as its own process, so the failure is injected the way the plane's own tests inject it, at the row read
(the plane's row read raises) but through a small wrapper app written into the test's directory: while
a flag file exists the read raises, and the plane answers the locked 503. Everything else is the plane as shipped.

* a worker with NO policy and NO cache that gets the 503 is refused for the classes that fail closed (never allow-all);
  read tools run per their fail mode; once the plane answers, the worker is governed;
* a worker that HOLDS rows keeps them through the 503, and its cache file is byte-identical afterwards.
"""

from __future__ import annotations

import textwrap
import time

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _plane_target, _until, launch
from .test_rows_quota_plane import have_row, put_row

pytestmark = pytest.mark.plane

WRAPPER = textwrap.dedent('''
    import contextvars
    import importlib
    import os
    import pathlib

    _PKG = os.environ["C12_PLANE_PACKAGE"]
    _create = importlib.import_module(_PKG + ".app").create_app
    tool_rows = importlib.import_module(_PKG + ".policy.tool_rows")

    _FLAG = pathlib.Path(os.environ["C12_FAIL_FLAG"])
    _real = tool_rows.served


    # The fault hits only the worker's policy read (GET /v1/policy): the admin write that announces a new version
    # also reads the rows (the promotion gate), and must succeed for the test to have a version it cannot serve.
    _IN_POLICY_READ = contextvars.ContextVar("in_policy_read", default=False)


    def _served(*args, **kwargs):
        if _FLAG.exists() and _IN_POLICY_READ.get():
            raise RuntimeError("the ledger read failed (test)")
        return _real(*args, **kwargs)


    tool_rows.served = _served


    def create_app():
        app = _create()

        @app.middleware("http")
        async def _mark_policy_read(request, call_next):
            token = _IN_POLICY_READ.set(request.method == "GET" and request.url.path == "/v1/policy")
            try:
                return await call_next(request)
            finally:
                _IN_POLICY_READ.reset(token)

        return app
''')


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def _plane_package():
    """The plane's package name, taken from the app target so this public test names no private tree."""
    _python, _directory, app = _plane_target()  # skips the test when no plane is configured
    return app.split(":", 1)[0].split(".", 1)[0]


@pytest.fixture
def flaky(tmp_path):
    """(plane, flag): the flag file makes the plane's row read raise. Skips on a plane without the locked 503."""
    (tmp_path / "c12_wrapper.py").write_text(WRAPPER)
    flag = tmp_path / "fail.flag"
    with launch(tmp_path, app="c12_wrapper:create_app", extra_pythonpath=tmp_path, extra_env={"C12_FAIL_FLAG": str(flag), "C12_PLANE_PACKAGE": _plane_package()}) as plane:
        flag.write_text("x")
        status, body = plane.request("GET", "/v1/policy?service=probe")
        flag.unlink()
        if not (status == 503 and isinstance(body, dict) and body.get("error") == "policy_rows_unavailable"):
            pytest.skip("this plane predates HC12-P: it does not answer a failed row read with the locked 503")
        yield plane, flag


def start(plane, service, cache_dir):
    runbound.init(control_plane_url=plane.url, token=KEY, service=service, control_plane_poll_s=0.2, cache_dir=str(cache_dir))
    _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")


def outcome(name, effects):
    def body():
        return "ran"

    body.__name__ = name
    tool = runbound.tool(effects=set(effects) or None)(body)
    try:
        tool()
        return "ran"
    except runbound.ExecutionRefused as refusal:
        return f"refused:{refusal.reason}"


def test_a_worker_with_no_policy_and_no_cache_that_gets_the_503_is_never_allow_all(flaky, tmp_path):
    plane, flag = flaky
    put_row(plane, "c22", "wire", blocked=True)  # a policy exists on the plane (version > 0), but it cannot be read
    flag.write_text("x")
    start(plane, "c22", tmp_path / "cache")
    time.sleep(2.5)  # several polls: every fetch gets the 503
    assert api._SHARED.row_for("wire") is None
    assert outcome("pay", ["financial"]) == "refused:plane_unavailable"
    assert outcome("wipe", ["destructive"]) == "refused:plane_unavailable"
    assert outcome("anything", []) == "refused:plane_unavailable"
    assert outcome("look", ["read"]) == "ran"  # the open classes run per their fail mode
    flag.unlink()  # the ledger is back
    have_row("wire")
    assert outcome("wire", ["read"]) == "refused:tool_blocked"
    # governed now, and not gated: an external tool runs (its standard default is allow; a new service's financial default would ask)
    assert outcome("call_out2", ["external"]) == "ran"


def test_a_worker_holding_rows_keeps_them_through_the_503_and_its_cache_is_unchanged(flaky, tmp_path):
    plane, flag = flaky
    put_row(plane, "c12", "wire", blocked=True)
    start(plane, "c12", tmp_path / "cache")
    have_row("wire")
    shared = api._SHARED
    _until(lambda: shared._cache_path is not None and shared._cache_path.exists(), what="the cache file")
    before = shared._cache_path.read_bytes()
    held_version = shared.policy_version
    flag.write_text("x")
    put_row(plane, "c12", "other", max_calls_per_session=3)  # a new policy version is announced; its rows cannot be read
    _until(lambda: plane.request("GET", "/v1/policy?service=c12")[0] == 503, what="the 503")
    time.sleep(3)  # polls of 0.2 s: the worker has asked again and again
    assert shared.policy_version == held_version  # nothing advanced
    assert shared.row_for("wire") is not None and shared.row_for("other") is None
    assert outcome("wire", ["read"]) == "refused:tool_blocked"  # what it holds still governs
    assert outcome("call_out", ["external"]) == "ran"  # it holds a policy, so it is not gated (external's standard default is allow)
    assert shared._cache_path.read_bytes() == before
    flag.unlink()
    have_row("other")  # the next poll after recovery picks the new rows up
