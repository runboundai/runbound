"""``coverage()`` and ``assert_guarded()``.

``init()`` computes nothing. The sensors are ``@runbound.tool`` and
``session()`` — so a process that called ``init()`` and decorated no tool is
blind while reporting green. These tests are about the SDK being honest about
that: counting what it can see, and ``assert_guarded()`` refusing to pass when
nothing is decorated.
"""

import pytest

import runbound
from runbound import _coverage, api


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


# --- the report itself ------------------------------------------------------


def test_coverage_before_init_reports_zeros():
    report = runbound.coverage()

    assert report == {
        "decorated_tools": 0,
        "decorated_tool_names": [],
        "tool_calls_seen": 0,
        "keyed_sessions_seen": 0,
        "tools_by_class": {"read": 0, "write": 0, "external": 0, "financial": 0,
                           "destructive": 0, "privileged": 0, "unclassified": 0},
        "middleware_installed": False,
        "middleware_tool_names": [],
        "fleet": "local protection active; fleet coordination: not connected",
        "fleet_pending": None,  # no plane before init()
    }


def test_coverage_reports_zeros_rather_than_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("the report is broken")

    monkeypatch.setattr(_coverage, "snapshot", boom)

    report = runbound.coverage()

    assert report["decorated_tools"] == 0
    assert report["tool_calls_seen"] == 0
    assert report["fleet_pending"] is None


# --- counting the sensors ---------------------------------------------------


def test_every_tool_decoration_is_counted():
    runbound.init()

    @runbound.tool
    def search(query):
        return query

    @runbound.tool(name="renamed")
    def other():
        return None

    report = runbound.coverage()
    assert report["decorated_tools"] == 2
    assert report["decorated_tool_names"] == ["renamed", "search"]


def test_tools_are_counted_per_capability_class():
    runbound.init()

    @runbound.tool(effects={"read"})
    def look():
        return None

    @runbound.tool(effects={"financial", "write"})
    def pay():
        return None

    @runbound.tool
    def mystery():
        return None

    by_class = runbound.coverage()["tools_by_class"]
    assert (by_class["read"], by_class["write"], by_class["financial"]) == (1, 1, 1)
    assert by_class["unclassified"] == 1


def test_every_attempted_tool_call_is_counted_even_a_refused_one():
    runbound.init()

    @runbound.tool(max_calls=1)
    def search(query):
        return query

    search("a")
    with pytest.raises(runbound.PolicyViolation):
        search("b")

    assert runbound.coverage()["tool_calls_seen"] == 2


def test_keyed_sessions_are_counted_per_key_not_per_block():
    runbound.init()

    with runbound.session("alice"):
        pass
    with runbound.session("alice"):
        pass
    with runbound.session("bob"):
        pass

    assert runbound.coverage()["keyed_sessions_seen"] == 2


# --- assert_guarded ---------------------------------------------------------


def test_assert_guarded_raises_when_nothing_is_decorated():
    runbound.init()

    with pytest.raises(RuntimeError) as excinfo:
        runbound.assert_guarded()

    assert "guards no tool" in str(excinfo.value)


def test_assert_guarded_raises_before_init_too():
    with pytest.raises(RuntimeError):
        runbound.assert_guarded()


def test_assert_guarded_passes_once_a_tool_is_decorated():
    runbound.init()

    @runbound.tool
    def search(query):
        return query

    runbound.assert_guarded()  # does not raise


def test_assert_guarded_never_raises_anything_else(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("the report is broken")

    monkeypatch.setattr(_coverage, "snapshot", boom)

    runbound.assert_guarded()  # fail-open: its own bug is logged, not raised


# --- coverage()["fleet"]: the one nudge, with and without a token -----------


def test_coverage_fleet_without_a_token_is_the_honest_local_notice():
    runbound.init()

    assert runbound.coverage()["fleet"] == (
        "local protection active; fleet coordination: not connected"
    )


def test_coverage_fleet_with_a_token_and_an_answering_plane_says_connected(monkeypatch):
    from runbound import shared as shared_module
    from runbound.plane_types import HelloReply
    from test_shared_state import FakePlane

    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        fake.url, fake.token = url, token
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    runbound.init(
        control_plane_url="https://plane.example",
        token="k",
        export_events=False,
    )
    assert runbound.coverage()["fleet"].startswith("local protection")  # no contact yet

    api._SHARED.apply_hello(HelloReply())  # the first heartbeat is answered

    assert runbound.coverage()["fleet"] == "connected"


def test_coverage_fleet_pending_reports_the_exporters_backlog(monkeypatch):
    from runbound import shared as shared_module
    from runbound.events import Anomaly
    from runbound.state import SessionState
    from test_shared_state import FakePlane

    fake = FakePlane()

    def factory(url, token, service, worker_id, timeout_s=0.15, **kwargs):
        return fake

    monkeypatch.setattr(shared_module, "PlaneClient", factory)
    runbound.init(control_plane_url="https://plane.example", token="k")

    shared = api._SHARED
    for _ in range(2):
        shared._exporter.on_anomaly(
            SessionState("s"), Anomaly("policy", "critical", "denied", {"tool": "t"}), "blocked"
        )

    assert runbound.coverage()["fleet_pending"] == 2


def test_coverage_fleet_pending_is_none_without_a_plane():
    runbound.init()

    assert runbound.coverage()["fleet_pending"] is None
