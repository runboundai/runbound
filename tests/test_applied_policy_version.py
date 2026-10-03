"""TIM-3b: ``plane_status().applied_policy_version`` — the policy version this worker has actually installed.

Read-only, on the SDK, no new wire: the heartbeat's announced version is not it (the policy may not have been fetched
yet, or the fetch may have failed), so a harness that wants to know "has this worker got the policy" reads this."""

from __future__ import annotations

import runbound
from runbound import api
from runbound.plane_types import HelloReply, PlaneStatus
from runbound.shared import LocalState

from test_shared_state import FakePlane, MovableClock, remote


def test_it_is_none_before_any_policy_is_applied():
    shared = remote(FakePlane(policy_body={"version": 4, "policy": {"deny": ["wire"]}}), MovableClock())
    assert shared.status().applied_policy_version is None


def test_it_is_the_fetched_version_not_the_announced_one():
    plane = FakePlane(policy_body={"version": 4, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=4))
    assert shared.status().applied_policy_version == 4


def test_a_failed_fetch_leaves_the_applied_version_where_it_was():
    plane = FakePlane(policy_body={"version": 1, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=1))
    plane.policy_body = None
    shared.apply_hello(HelloReply(policy_version=2))  # announced 2, never fetched
    assert shared.status().applied_policy_version == 1


def test_a_new_fetch_moves_it():
    plane = FakePlane(policy_body={"version": 4, "policy": {"deny": ["wire"]}})
    shared = remote(plane, MovableClock())
    shared.apply_hello(HelloReply(policy_version=4))
    plane.policy_body = {"version": 5, "policy": {"deny": ["refund"]}}
    shared.apply_hello(HelloReply(policy_version=5))
    assert shared.status().applied_policy_version == 5


def test_without_a_plane_it_is_none_and_the_field_defaults_to_none():
    assert LocalState().status().applied_policy_version is None
    assert PlaneStatus().applied_policy_version is None


def test_the_public_call_carries_it(monkeypatch):
    api._teardown_for_tests()
    runbound.init()
    assert runbound.plane_status().applied_policy_version is None
    api._teardown_for_tests()
