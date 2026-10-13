"""H6 on a REAL plane, seen from workers: the legacy organisation policy document is gone from the wire and a worker that only reads it is halted.

The plane serves ``policy: null`` for every service. A runbound 1.x worker reads rows, so a service that HAD a restricting document does not halt it, and
it has no document to be warned about. A 0.x worker reads the document and no rows: for a service whose last document restricted something the plane halts
it at the hello (the whole worker: the wire has one halt boolean); for a service whose document restricted nothing it does not.

The plane keeps what the document restricted in ``legacy_document_halts`` (migration 0044). A real migration of an old database is the plane's own test; here
the table is written directly, which is what the migration leaves behind. Skips on a plane without that table (probed in its database), not on a symptom.
"""

from __future__ import annotations

import sqlite3

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, launch

pytestmark = pytest.mark.plane

RESTRICTED, UNRESTRICTED = "had-restricting-doc", "had-empty-doc"


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


@pytest.fixture
def h6(tmp_path):
    """A plane process, with the halts table filled the way migration 0044 leaves it."""
    with launch(tmp_path) as plane:
        con = sqlite3.connect(plane.db)
        try:
            exists = con.execute("select name from sqlite_master where type = 'table' and name = 'legacy_document_halts'").fetchone()
            if not exists:
                pytest.skip("this plane predates H6: it has no legacy_document_halts table")
            for service, restricts, document in ((RESTRICTED, 1, '{"deny": ["wipe"]}'), (UNRESTRICTED, 0, "{}")):
                con.execute("insert into legacy_document_halts (org_id, service, restricts, document, computed_at) values ('default', ?, ?, ?, '2026-10-13 00:00:00')",
                            (service, restricts, document))
            con.commit()
        finally:
            con.close()
        yield plane


def hello(plane, sdk, service, worker="w-h6"):
    return plane.request("POST", "/v1/hello", {"service": service, "worker_id": worker},
                         headers={"X-Runbound-Service": service, "X-Runbound-Worker": worker, "X-Runbound-SDK": sdk})


def test_a_1x_worker_on_a_service_that_had_a_restricting_document_is_not_halted_and_is_not_warned(h6, caplog):
    status, body = h6.request("GET", f"/v1/policy?service={RESTRICTED}")
    assert status == 200 and body["policy"] is None  # the document is no longer served

    with caplog.at_level("WARNING", logger="runbound"):
        runbound.init(control_plane_url=h6.url, token=KEY, service=RESTRICTED, control_plane_poll_s=0.2)
        _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
        _until(lambda: api._SHARED.link_state() == "connected", what="a connected link")
        import time

        time.sleep(1.5)  # several heartbeats and policy fetches

        @runbound.tool(effects={"read"}, reviewed=True)
        def look():
            return "ran"

        assert look() == "ran"  # not refused with reason "halt"
    assert api._SHARED.halted() is False
    assert not [r for r in caplog.records if "legacy organisation policy document" in r.getMessage()]  # no document served: nothing to deprecate


def test_a_0x_hello_on_a_service_that_had_a_restricting_document_is_halted(h6):
    status, body = hello(h6, "0.11.2", RESTRICTED)
    assert status == 200 and body["halt"] is True


def test_a_0x_hello_on_a_service_whose_document_restricted_nothing_is_not_halted(h6):
    status, body = hello(h6, "0.11.2", UNRESTRICTED)
    assert status == 200 and body["halt"] is False


def test_a_service_with_no_document_history_is_not_halted_either(h6):
    status, body = hello(h6, "0.11.2", "never-had-one")
    assert status == 200 and body["halt"] is False


def test_the_same_restricted_service_does_not_halt_a_1x_hello(h6):
    status, body = hello(h6, "1.0.0a2", RESTRICTED)
    assert status == 200 and body["halt"] is False
