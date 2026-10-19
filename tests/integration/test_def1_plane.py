"""DEF-1 against a REAL control plane: a deferred tool with an injected parameter and structured arguments.

The plane recomputes ``args_digest`` over the arguments it receives and refuses a request whose digest differs (400); the seal travels with the request. So the encoded
JSON the SDK files must agree with the plane's own canonical form, the injected parameter must not be in the stored request, and an approved request must run with a
provider-supplied session and the decoded types.
"""

from __future__ import annotations

import datetime
import decimal
import json
import uuid
from typing import Annotated

import pytest

import runbound
from runbound import api

from .test_approvals_plane import SECRET, decide, pending, start
from .test_real_plane import _until, plane  # noqa: F401  (plane is a fixture)

pytestmark = pytest.mark.plane


@pytest.fixture(autouse=True)
def _clean(plane):  # noqa: F811
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Session:
    made = 0

    def __init__(self):
        Session.made += 1


def test_an_injected_parameter_and_structured_arguments_cross_a_real_plane(plane):  # noqa: F811
    Session.made = 0
    results = start(plane, "def1-real", approval_secret=SECRET, provide={Session: Session})
    ran = []
    ref, at = uuid.uuid4(), datetime.datetime(2026, 10, 18, 1, 2, 3, tzinfo=datetime.timezone.utc)

    @runbound.tool(effects={"write"}, approval="required", mode="deferred", timeout_s=60)
    def book(price: decimal.Decimal, ref: uuid.UUID, at: datetime.datetime, db: Annotated[Session, runbound.Injected]):
        ran.append((price, ref, at, db))
        return {"booked": str(ref)}

    out = book(decimal.Decimal("12.50"), ref, at, db=Session())
    assert out["status"] == "pending_approval" and ran == [], out  # the plane took it: its digest agrees with the encoded arguments
    (waiting,) = _until(lambda: pending(plane, "def1-real"), what="the pending request")
    assert waiting["args"] == {"price": "12.50", "ref": str(ref), "at": "2026-10-18T01:02:03+00:00"}
    assert "db" not in waiting["args"] and waiting["seal"]["v"] == 1
    decide(plane, waiting["id"], "approve")

    _until(lambda: results, what="on_deferred_result")
    assert results[0]["outcome"] == "executed", results[0]
    (price, got_ref, got_at, db), = ran
    assert price == decimal.Decimal("12.50") and got_ref == ref and got_at == at and isinstance(db, Session)
    assert Session.made == 2, "the caller's own session and one provided at execution"
