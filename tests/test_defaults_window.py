"""The defaults window (MCP-X1 finding, fail-open): a destructive call must not run unasked between a worker's first answered hello and its first policy.

A new service starts with enforced standard defaults (destructive: ask), but the plane's first hello announces ``policy_version: 0`` because writing those defaults does not
bump the live version. The SDK used to read version 0 as "this service has no policy, so there is nothing to hold" and lifted its refusal of consequential classes at once,
with no defaults applied: the no-defaults path then ran the call, until a later hello announced version 1 and the policy (with the defaults) was fetched, about two seconds on.
The worker now fetches its policy once after the first answered hello whatever version was announced, and a policy it does not yet hold refuses consequential classes.
"""

import threading

import pytest

from runbound.plane_types import HelloReply
from test_service_defaults import served
from test_shared_state import FakePlane, MovableClock, remote

DESTRUCTIVE = frozenset({"destructive"})


def standard_policy_body():
    """What ``GET /v1/policy`` serves a new service: no rows, and the enforced standard defaults (destructive asks)."""
    return {"version": 1, "policy": None, "rows": [], "rows_hash": "h0", "cfg_hash": "h0",
            "service_defaults": served(state="enforced", revision=1, decision={"destructive": "ask"})}


class SlowPolicy(FakePlane):
    """The plane's policy answer is held until the test lets it go, so the moment between the first hello and the first policy can be looked at."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.asked, self.release = threading.Event(), threading.Event()

    def policy(self, service):
        self.asked.set()
        assert self.release.wait(10), "the test never released the policy"
        return super().policy(service)


def test_the_first_hello_fetches_the_policy_even_when_it_announces_version_zero():
    plane = FakePlane(policy_body=standard_policy_body())
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=0))

    assert "policy" in plane.names(), "version 0 is not 'no policy': the new service's standard defaults are served by the policy read"
    assert shared.defaults() is not None and shared.defaults().decision["destructive"] == "ask"


def test_the_startup_floor_stays_up_while_the_first_policy_is_being_read():
    plane = SlowPolicy(policy_body=standard_policy_body())
    shared = remote(plane, MovableClock())
    worker = threading.Thread(target=shared.apply_hello, args=(HelloReply(policy_version=0),), daemon=True)
    worker.start()
    try:
        assert plane.asked.wait(5), "the worker should be fetching its policy now"
        assert shared.awaiting_first_policy() is True, "the startup floor stays up until the first policy is in hand, not until the hello alone"
        assert shared.link_state() == "connecting"
    finally:
        plane.release.set()
        worker.join(5)
    assert shared.awaiting_first_policy() is False and shared.defaults() is not None  # and once the policy is applied the floor lifts and the defaults decide (they ask)


def test_a_service_that_really_has_no_policy_is_held_to_what_it_always_was():
    """An empty policy read is an answer: the worker then holds a policy (an empty one) and consequential classes follow the old per-class rules."""
    plane = FakePlane(policy_body={"version": 0, "policy": None, "rows": [], "rows_hash": "h0"})
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=0))

    assert "policy" in plane.names() and shared.defaults() is None
    assert shared.plane_refusal(DESTRUCTIVE) is None  # within the grace, as for every existing service without defaults


def test_a_failed_first_policy_read_keeps_consequential_classes_refused_and_the_next_hello_tries_again():
    plane = FakePlane(ok=True, policy_body=None)  # the policy read fails (None)
    shared = remote(plane, MovableClock())

    shared.apply_hello(HelloReply(policy_version=0))
    assert shared.awaiting_first_policy() is False  # the hello was answered, so the floor is down...
    assert shared.plane_refusal(DESTRUCTIVE) is not None  # ...but a destructive call holds no policy to run on

    plane.policy_body = standard_policy_body()
    shared.apply_hello(HelloReply(policy_version=0))
    assert plane.names().count("policy") == 2 and shared.defaults() is not None
