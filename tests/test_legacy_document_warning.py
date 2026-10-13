"""The legacy organisation policy document is deprecated, still enforced, and said so once (H6-SDK).

A plane that serves a non-null ``policy`` document gets ONE warning per process, however many heartbeats fetch it. Nothing
else changes: the document is read and enforced exactly as before. A null or absent document says nothing.
"""

import pytest

import runbound
from runbound import api, hooks
from runbound.config import RunboundConfig
from runbound.plane_types import HelloReply
from runbound.shared import LEGACY_DOCUMENT_WARNING, RemoteState

WORDS = (
    "runbound: this control plane still serves the legacy organisation policy document (deny/allow lists). "
    "It is deprecated: it is still enforced in this release and stops being read in a later one. "
    "Move its rules to per-tool rules in the console."
)


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    hooks.reset_for_tests()
    yield
    api._teardown_for_tests()
    hooks.reset_for_tests()


class Plane:
    key_state = "unknown"

    def __init__(self, served):
        self.served = served

    def policy(self, service):
        return self.served

    def hello(self, payload):
        return None

    def approvals(self, *args, **kwargs):
        return None, None


def link(tmp_path, served):
    runbound.init(cache_dir=str(tmp_path / "cache"))
    config = RunboundConfig(control_plane_url="https://plane.example", token="k", service="svc", worker_id="w", cache_dir=str(tmp_path / "cache"))
    config.validate()
    shared = RemoteState(Plane(served), None, config)
    api._ENGINE.shared = shared
    return shared


def polls(shared, versions):
    for version in versions:
        shared.apply_hello(HelloReply(policy_version=version))


def said(caplog):
    return [r for r in caplog.records if "legacy organisation policy document" in r.getMessage()]


def test_the_wording_is_exactly_the_ruled_text():
    assert LEGACY_DOCUMENT_WARNING == WORDS


def test_a_served_document_warns_once_per_process_not_once_per_poll(tmp_path, caplog):
    shared = link(tmp_path, {"version": 2, "dry_run": False, "policy": {"deny": ["wipe"]}})
    with caplog.at_level("WARNING", logger="runbound"):
        polls(shared, [2, 3, 4, 5, 6])  # every poll announces a new version, so every poll fetches
    found = said(caplog)
    assert len(found) == 1 and found[0].getMessage() == WORDS and found[0].levelname == "WARNING"


def test_the_document_is_still_enforced_exactly_as_before(tmp_path, caplog):
    shared = link(tmp_path, {"version": 2, "dry_run": False, "policy": {"deny": ["wipe"]}})
    shared.apply_hello(HelloReply(policy_version=2))

    @runbound.tool(effects={"read"}, reviewed=True)
    def wipe():
        return "ran"

    @runbound.tool(effects={"read"}, reviewed=True)
    def look():
        return "ran"

    with pytest.raises(runbound.PolicyViolation):
        wipe()
    assert look() == "ran"
    assert shared.policy() == {"deny": ["wipe"]} and shared.policy_version == 2


@pytest.mark.parametrize("served", [
    {"version": 2, "dry_run": False, "policy": None},  # a null document
    {"version": 2, "rows": [], "rows_hash": "h"},  # no document key at all
])
def test_a_null_or_absent_document_says_nothing(tmp_path, caplog, served):
    shared = link(tmp_path, served)
    with caplog.at_level("WARNING", logger="runbound"):
        polls(shared, [2, 3])
    assert said(caplog) == []


def test_an_empty_but_present_document_is_still_a_document(tmp_path, caplog):
    shared = link(tmp_path, {"version": 2, "policy": {}})
    with caplog.at_level("WARNING", logger="runbound"):
        polls(shared, [2])
    assert len(said(caplog)) == 1


def test_a_plane_that_stops_serving_it_after_a_warning_is_not_warned_about_again(tmp_path, caplog):
    shared = link(tmp_path, {"version": 2, "policy": {"deny": ["wipe"]}})
    with caplog.at_level("WARNING", logger="runbound"):
        polls(shared, [2])
        shared._client.served = {"version": 3, "policy": None}
        polls(shared, [3, 4])
    assert len(said(caplog)) == 1
