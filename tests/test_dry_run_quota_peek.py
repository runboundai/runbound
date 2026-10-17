"""VPC B6b (register C4, O-DRY-2, A-CNT-6): a dry-run quota row takes NO live unit and spends NO lease.

It reads the window with the plane's peek (``POST /v1/quota/take {"peek": true}``: the counter is read, never moved) and adds the dry-run calls THIS worker has
seen in that window, so ``would_refuse`` is recorded when a live row would have been over. The call itself always runs. A plane that cannot be asked is read as a
live row reads it (``quota_plane_unavailable``, recorded, as before); a plane with no peek is never asked at all. No sockets, no sleeping."""

from __future__ import annotations

import pytest

import runbound
from runbound import api
from test_policy_rows import FakeShared, row, start

QUOTA = {"limit": 2, "window": "minute", "per": "service"}


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class PeekPlane(FakeShared):
    """A plane that answers a peek the way the real one does: ``remaining`` and the window it belongs to, and never takes."""

    def __init__(self, rows, *, remaining=2, window="2026-10-14T10:00:00Z", supported=True, **kw):
        super().__init__(rows, take=self._answer, **kw)
        self.remaining, self.window, self.supported = remaining, window, supported

    def _answer(self, body):
        if not body.get("peek"):
            return 200, {"granted": True, "retry_after_s": 0}
        fits = self.remaining >= 1
        return 200, {"granted": fits, "peek": True, "limit": 2, "remaining": self.remaining, "window_start": self.window, "retry_after_s": None if fits else 30}

    def quota_peek_supported(self):
        return self.supported


def look():
    @runbound.tool(effects={"read"}, reviewed=True)
    def act():
        return "ok"

    return act


def tags():
    return runbound.tool_results()[-1]["tags"]


def test_a_dry_run_row_only_ever_peeks_it_takes_no_unit_and_spends_no_lease():
    shared = start(PeekPlane([row("act", state="dry_run", quota=QUOTA)]))
    shared.quota.apply([{"tool": "act", "window_start": "2026-10-14T10:00:00Z", "window_end": 4_000_000_000, "granted": 2}])
    act = look()
    for _ in range(2):
        assert act() == "ok"
    assert shared.takes() and all(c[2]["peek"] is True for c in shared.takes()), "every call to the quota route was a peek"
    assert shared.quota.report().get("act") == 2, "the lease this worker holds was not spent"


def test_a_dry_run_row_over_the_limit_records_would_refuse_and_the_call_still_runs():
    shared = start(PeekPlane([row("act", state="dry_run", quota=QUOTA)], remaining=2))
    act = look()
    results = []
    for _ in range(3):
        results.append(act())
        results.append("would_refuse:quota_exceeded" in tags())
    assert results == ["ok", False, "ok", False, "ok", True], "the third call is over a limit of 2: it ran, and the evidence says a live row would have refused it"


def test_the_live_use_the_plane_reports_counts_towards_what_a_dry_run_would_have_refused():
    start(PeekPlane([row("act", state="dry_run", quota=QUOTA)], remaining=1))  # one unit left by live traffic
    act = look()
    act()
    assert "would_refuse:quota_exceeded" not in tags()
    act()
    assert "would_refuse:quota_exceeded" in tags(), "one live unit left, and this worker has already seen one dry-run call"


def test_a_peek_that_says_the_window_is_full_is_a_would_refuse_on_the_first_call():
    start(PeekPlane([row("act", state="dry_run", quota=QUOTA)], remaining=0))
    assert look()() == "ok" and "would_refuse:quota_exceeded" in tags()


def test_the_simulated_count_starts_again_when_the_window_moves():
    shared = start(PeekPlane([row("act", state="dry_run", quota=QUOTA)]))
    act = look()
    act(), act()
    shared.window = "2026-10-14T10:01:00Z"
    act()
    assert "would_refuse:quota_exceeded" not in tags(), "a new window: the calls of the old one are not counted"


def test_a_per_key_dry_run_peeks_under_its_session_keys_hash():
    shared = start(PeekPlane([row("act", state="dry_run", quota={**QUOTA, "per": "key"})]))
    with runbound.session("k-1"):
        look()()
    (_m, _p, body), = shared.takes()
    assert body["peek"] is True and body.get("key"), "the plane counts it under the key, as it would a live take"


def test_an_enforced_row_still_takes_a_unit_the_peek_is_only_for_a_dry_run():
    shared = start(PeekPlane([row("act", quota=QUOTA)]))
    assert look()() == "ok"
    assert [c[2].get("peek") for c in shared.takes()] == [None], "a live row's take is a take"


def test_a_dry_run_with_an_approval_asks_nobody_and_only_peeks():
    shared = start(PeekPlane([row("act", state="dry_run", quota=QUOTA, approval_required=True)]))
    assert look()() == "ok"
    assert [c for c in shared.calls if c[1] == "/v1/approvals"] == [] and all(c[2]["peek"] for c in shared.takes())


def test_a_plane_with_no_peek_is_never_asked_because_it_would_take_a_unit():
    shared = start(PeekPlane([row("act", state="dry_run", quota=QUOTA)], supported=False, remaining=0))
    assert look()() == "ok"
    assert shared.takes() == [] and "would_refuse:quota_exceeded" not in tags(), "no unit, and no evidence from a counter that cannot be read safely"


@pytest.mark.parametrize("status", [404, 400, 503])
def test_a_plane_that_cannot_answer_is_read_as_a_live_row_reads_it_and_the_call_still_runs(status):
    shared = start(PeekPlane([row("act", state="dry_run", quota=QUOTA)]))
    shared.take = lambda body: (status, {"error": "x"})
    assert look()() == "ok" and "would_refuse:quota_plane_unavailable" in tags()


def test_a_link_that_is_down_is_the_same_unverifiable_dry_run_evidence_as_before():
    start(PeekPlane([row("act", state="dry_run", quota=QUOTA)], link="unavailable"))
    assert look()() == "ok" and "would_refuse:quota_plane_unavailable" in tags()


def test_an_open_row_with_the_plane_away_records_nothing_for_its_static_share():
    start(PeekPlane([row("act", state="dry_run", quota=QUOTA, fail_mode="open")], link="unavailable"))
    assert look()() == "ok" and "would_refuse:quota_plane_unavailable" not in tags()
