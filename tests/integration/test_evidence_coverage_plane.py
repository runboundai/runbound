"""PS-AAI-03m against a REAL plane: the hello with the new evidence keys is accepted as it always was (a plane that does not know them ignores them)."""

import pytest

import runbound
from runbound import api

from .test_real_plane import KEY, _until, private_plane  # noqa: F401  (private_plane is a fixture)

pytestmark = pytest.mark.plane


def test_a_plane_that_does_not_know_the_evidence_keys_still_answers_every_hello(private_plane):  # noqa: F811
    api._teardown_for_tests()
    try:
        runbound.init(control_plane_url=private_plane.url, token=KEY, service="evidence-real", control_plane_poll_s=0.2, otel_endpoint="http://127.0.0.1:9")
        _until(lambda: runbound.plane_status().mode == "connected", what="the first hello")
        payload = api._SHARED._hello_payload()  # the plane has answered the first hello, so only the digest rides now
        assert "coverage_digest" in payload and api._SHARED._evidence_state()["otel_endpoint_set"] is True
        _until(lambda: runbound.plane_status().consecutive_failures == 0 and runbound.plane_status().last_contact_age_s is not None, what="answered hellos")
        assert runbound.plane_status().mode == "connected"
    finally:
        api._teardown_for_tests()
