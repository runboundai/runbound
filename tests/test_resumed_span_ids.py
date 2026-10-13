"""A deferred approval's resumed execution reports under the run that filed it, and names the request (V7).

The resumed call runs on a fresh thread with no session bound. Its span used to carry the process's default run id and no
approval request id; it must carry the ORIGINAL run id (the trace id derives from it) and the request id. OTLP collector and
fake plane as in test_spans.py and test_approvals.py.
"""

from __future__ import annotations

import time

import pytest

import runbound
from runbound import api, approvals, otel_names as names, spans
from runbound.approvals import Pending

from test_approvals import FakePlane
from test_spans import Collector, attrs_of


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def _file_one(collector, plane, secret=None):
    """File a deferred request inside run 'run-a'; returns (item the plane would hand back, original run id, ran list)."""
    runbound.init(otel_endpoint=collector.url, token="rb_secret_key", service="acme", worker_id="w-3",
                  **({"approval_secret": secret} if secret else {}))
    api._ENGINE.shared = plane
    ran = []

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def archive(ticket: int):
        ran.append(ticket)
        return ticket

    with runbound.session("run-a") as state:
        out = archive(7)
        run_id = state.session_id
    create = [c for c in plane.calls if c[1] == "/v1/approvals"][-1][2]
    item = {"id": "a1", "tool": "archive", "args": create["args"], "request_id": out["request_id"],
            "service": create["service"], "expires_at": "2099-01-01T00:00:00Z"}
    if create.get("seal"):
        item["seal"] = create["seal"]
    return item, run_id, ran


def _resumed_span(collector, request_id):
    spans.exporter().flush(5.0)
    mine = [attrs_of(s) | {"_trace": s["traceId"]} for s in collector.spans()]
    return [a for a in mine if a.get(names.APPROVAL_REQUEST_ID) == request_id and a.get(names.OUTCOME) != "pending_approval"]


def test_a_parked_request_resumes_under_its_original_run_and_names_the_request(collector):
    item, run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})))
    assert "seal" not in item  # no secret: parked on this worker, unsealed

    approvals._execute(item)  # a fresh context: no session bound, as on the heartbeat's thread

    assert ran == [7]
    (resumed,) = _resumed_span(collector, item["request_id"])
    assert resumed[names.RUN_ID] == run_id
    assert resumed["_trace"] == otlp_trace(run_id)
    # the filing span and the resumed span share the key AND the value, so one lookup by request id finds both
    spans.exporter().flush(5.0)
    filed = [attrs_of(s) for s in collector.spans() if attrs_of(s).get(names.OUTCOME) == "pending_approval"]
    assert [a[names.APPROVAL_REQUEST_ID] for a in filed] == [resumed[names.APPROVAL_REQUEST_ID]] == [item["request_id"]]


def test_a_sealed_request_still_parked_here_resumes_under_its_original_run(collector):
    item, run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})), secret="s3cret")
    assert "seal" in item

    approvals._execute(item)

    assert ran == [7]
    (resumed,) = _resumed_span(collector, item["request_id"])
    assert resumed[names.RUN_ID] == run_id


def test_a_run_id_that_is_not_a_short_string_leaves_the_default_run(collector):
    item, _run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})))
    approvals._PENDING[item["request_id"]] = Pending("archive", item["request_id"], item["args"], "a1", run_id=["x"])

    approvals._execute(item)

    assert ran == [7]
    (resumed,) = _resumed_span(collector, item["request_id"])
    assert resumed[names.RUN_ID] != "x"


def otlp_trace(run_id):
    from runbound import otlp

    return otlp.new_trace_id(run_id)


def test_a_sealed_request_claimed_by_another_replica_takes_its_run_from_the_claim(collector):
    item, run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})), secret="s3cret")
    approvals._PENDING.clear()  # this process is the replica that did NOT file it
    item["run_id"] = run_id  # what the plane's claim carries (the row's run_id)

    approvals._execute(item)

    assert ran == [7]
    (resumed,) = _resumed_span(collector, item["request_id"])
    assert resumed[names.RUN_ID] == run_id
    assert resumed["_trace"] == otlp_trace(run_id)


def test_a_claim_without_a_run_id_from_an_older_plane_still_runs_under_the_default_run(collector):
    item, run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})), secret="s3cret")
    approvals._PENDING.clear()

    approvals._execute(item)

    assert ran == [7]
    (resumed,) = _resumed_span(collector, item["request_id"])
    assert resumed[names.RUN_ID] != run_id


@pytest.mark.parametrize("bad", [["x"], 7, "", "r" * 129])
def test_a_bad_claim_run_id_is_ignored_with_one_warning_and_never_fails_the_claim(collector, caplog, bad):
    from runbound import hooks

    hooks.reset_for_tests()
    item, run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})), secret="s3cret")
    approvals._PENDING.clear()
    item["run_id"] = bad

    with caplog.at_level("WARNING", logger="runbound"):
        approvals._execute(item)
        approvals._execute({**item, "id": "a2"})  # a second bad claim: still one line

    assert ran == [7, 7]
    assert len([r for r in caplog.records if "carried a run_id" in r.getMessage()]) == 1
    assert _resumed_span(collector, item["request_id"])[0][names.RUN_ID] != run_id


def test_an_absent_or_null_claim_run_id_is_silent(collector, caplog):
    from runbound import hooks

    hooks.reset_for_tests()
    item, _run_id, ran = _file_one(collector, FakePlane((201, {"id": "a1", "status": "pending"})), secret="s3cret")
    approvals._PENDING.clear()
    item["run_id"] = None

    with caplog.at_level("WARNING", logger="runbound"):
        approvals._execute(item)

    assert ran == [7]
    assert not [r for r in caplog.records if "run_id" in r.getMessage()]
