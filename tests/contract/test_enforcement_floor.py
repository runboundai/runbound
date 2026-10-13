"""The enforcement floor (TPD §4d; register C2, C3, C9 and the first-handshake row). Ids O-FLR-*.

Each test asserts the CONTRACT against today's engine: it either passes on main, or is a strict xfail that
the release named in ``ships=`` must flip in the same commit as the change (register §1). Nothing is fixed in R0.
"""

import pytest

import runbound
from contract_support import Clock, engine_on_link, outcome_of
from runbound import api
from runbound.plane_types import HelloReply

pytestmark = [pytest.mark.contract(item="§4d floor")]


def _halted(tmp_path, on_halt):
    clock = Clock()
    shared = engine_on_link(tmp_path, clock=clock, on_halt=on_halt)
    shared.apply_hello(HelloReply(halt=True))
    return shared


def test_o_flr_1a_a_halt_is_refused_with_the_default_on_halt_raise(tmp_path):
    _halted(tmp_path, "raise")

    @runbound.tool(effects={"read"})
    def ping():
        return "ran"

    assert outcome_of(ping) == "refused:halt"


def test_o_flr_1_a_halt_is_refused_even_with_on_halt_warn(tmp_path):
    _halted(tmp_path, "warn")

    @runbound.tool(effects={"read"})
    def ping():
        return "ran"

    assert outcome_of(ping) == "refused:halt"


def _rows_link(tmp_path, rows):
    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply())  # a first handshake, so the link is connected
    shared._set_rows(rows, "h")
    return shared


def test_o_flr_2_a_malformed_row_refuses_its_tool(tmp_path):
    _rows_link(tmp_path, [{"tool": "wire", "state": "bogus", "blocked": True}, {"tool": "other", "state": "enforced", "blocked": False}])

    @runbound.tool(effects={"read"})
    def wire():
        return "ran"

    @runbound.tool(effects={"read"})
    def other():
        return "ran"

    assert outcome_of(wire) == "refused:policy_unsupported"
    assert outcome_of(other) == "ran"  # the other tools are unaffected


def test_o_flr_3_a_row_with_an_unknown_restriction_key_refuses_its_tool(tmp_path):
    _rows_link(tmp_path, [{"tool": "wire", "state": "enforced", "geo_fence": "eu"}])

    @runbound.tool(effects={"read"})
    def wire():
        return "ran"

    assert outcome_of(wire) == "refused:policy_unsupported"


def test_o_flr_3a_a_known_restriction_in_the_same_row_still_refuses(tmp_path):
    _rows_link(tmp_path, [{"tool": "wire", "state": "enforced", "blocked": True, "geo_fence": "eu"}])

    @runbound.tool(effects={"read"})
    def wire():
        return "ran"

    assert outcome_of(wire) == "refused:policy_unsupported"  # R2: the row is not read at all; its tool is refused, never half-obeyed


@pytest.mark.parametrize("effects", [{"financial"}, {"destructive"}, {"privileged"}, set()])
def test_o_flr_4b_a_consequential_or_unclassified_tool_does_not_run_before_the_first_handshake(tmp_path, effects):
    engine_on_link(tmp_path, clock=Clock())  # connecting: no hello, no cache

    @runbound.tool(effects=effects or None)
    def act():
        return "ran"

    assert outcome_of(act) == "refused:plane_unavailable"


@pytest.mark.parametrize("effects", [{"read"}, {"write"}, {"external"}])
def test_o_flr_4_no_governed_call_runs_before_the_first_handshake_or_sealed_cache_load(tmp_path, effects):
    engine_on_link(tmp_path, clock=Clock())  # connecting: no hello, no cache

    @runbound.tool(effects=effects)
    def act():
        return "ran"

    assert outcome_of(act) == "refused:policy_unavailable"


def test_o_flr_4a_a_sealed_cache_loaded_at_start_lets_a_consequential_tool_run(tmp_path):
    """The other half of the floor line: a sealed-cache load counts as the handshake (15 min fresh, shared.py:235-258)."""
    from runbound import cache

    clock = Clock()
    first = engine_on_link(tmp_path, clock=clock)
    first.apply_hello(HelloReply(policy_version=3))  # serves nothing, but writes the cache the next worker loads
    first._save_cache(force=True)
    second = engine_on_link(tmp_path, clock=Clock())
    assert second.cache_age_s() is not None and second.cache_age_s() < cache.FRESH_FOR_CONSEQUENTIAL_S

    @runbound.tool(effects={"destructive"})
    def wipe():
        return "ran"

    assert outcome_of(wipe) == "ran"


def _rows_then_read_error(tmp_path):
    """Hold a blocking row, then let the plane answer ``/v1/policy`` with its read-error marker (rows [], rows_hash "")."""
    from runbound import cache

    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply())
    shared._set_rows([{"tool": "wire", "state": "enforced", "blocked": True}], "hash-1")
    shared._save_cache(force=True)
    shared._client.served = {"version": 3, "policy": None, "rows": [], "rows_hash": ""}
    shared.apply_hello(HelloReply(policy_version=3))
    held, _status = cache.read(shared._cache_path, "k", "svc")
    return shared, held


def test_o_flr_5_a_policy_answer_with_the_read_error_marker_neither_replaces_the_held_rows_nor_reaches_the_cache(tmp_path):
    """C12, fixed: the marker (rows_hash "") is not an answer."""
    shared, held = _rows_then_read_error(tmp_path)

    @runbound.tool(effects={"read"})
    def wire():
        return "ran"

    assert outcome_of(wire) == "refused:tool_blocked"  # the held row still governs
    assert held is not None and held["rows_hash"] == "hash-1" and held["rows"], held  # and the cache still holds it


def test_o_flr_5a_the_cache_file_is_byte_identical_after_the_marker(tmp_path):
    from runbound import cache

    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply())
    shared._set_rows([{"tool": "wire", "state": "enforced", "blocked": True}], "hash-1")
    shared._save_cache(force=True)
    before = shared._cache_path.read_bytes()
    shared._client.served = {"version": 3, "policy": None, "rows": [], "rows_hash": ""}
    shared.apply_hello(HelloReply(policy_version=3))
    assert shared._cache_path.read_bytes() == before


def test_o_flr_5b_the_marker_is_retried_on_the_next_hello_and_the_rows_then_apply(tmp_path):
    shared, _held = _rows_then_read_error(tmp_path)
    assert shared.policy_version != 3  # the failed answer did not advance the version, so the next hello fetches again
    shared._client.served = {"version": 3, "policy": None, "rows": [{"tool": "wire", "state": "enforced", "max_calls_per_session": 5}],
                             "rows_hash": "hash-2"}
    shared.apply_hello(HelloReply(policy_version=3))
    assert shared.policy_version == 3 and shared.row_for("wire").max_calls == 5


def test_o_flr_5c_a_deliberate_empty_set_with_a_real_hash_still_clears_the_rows(tmp_path):
    shared = engine_on_link(tmp_path, clock=Clock())
    shared.apply_hello(HelloReply())
    shared._set_rows([{"tool": "wire", "state": "enforced", "blocked": True}], "hash-1")
    shared._client.served = {"version": 3, "policy": None, "rows": [], "rows_hash": "hash-of-the-empty-set"}
    shared.apply_hello(HelloReply(policy_version=3))
    assert shared.row_for("wire") is None  # "no rules" said on purpose is still an answer


def test_o_flr_5d_nothing_held_and_the_marker_arrives_holds_nothing_and_writes_no_cache(tmp_path):
    shared = engine_on_link(tmp_path, clock=Clock())
    shared._client.served = {"version": 3, "policy": None, "rows": [], "rows_hash": ""}
    shared.apply_hello(HelloReply(policy_version=3))
    assert shared.row_for("wire") is None and not shared._cache_path.exists()


def test_o_flr_5e_the_marker_cannot_by_itself_push_a_connected_worker_into_plane_loss_or_grace_refusals(tmp_path):
    """C12/C22: ``_fetch_policy`` records the marker as a failed call (``_record(False)``), which only feeds ``_failures``.
    ``link_state`` and ``plane_refusal`` read the heartbeat (``_last_success``, ``_misses``) and never ``_failures``, so
    while hellos succeed the worker stays connected, its fail-closed classes run, and the status shows no failures."""
    clock = Clock()
    shared = engine_on_link(tmp_path, clock=clock)
    shared._client.served = {"version": 2, "policy": None, "hash": 0, "rows_hash": "hash-1",
                             "rows": [{"tool": "wire", "state": "enforced", "max_calls_per_session": 100}]}
    shared.apply_hello(HelloReply(policy_version=2))  # a real first fetch: the worker holds a policy
    shared._client.served = {"version": 3, "policy": None, "rows": [], "rows_hash": ""}
    for poll in range(1, 8):  # seven polls, each a successful hello followed by a marker answer
        clock.t += 20  # 140 s in all: far past the 60 s consequential grace if hellos had stopped
        shared.apply_hello(HelloReply(policy_version=3))
    assert shared._failures == 1  # the marker WAS recorded as a failed call (and each fetch's own success resets the run)...
    assert shared.link_state() == "connected" and shared.status().consecutive_failures == 0  # ...and moved nothing the link reads
    assert shared.plane_refusal(frozenset({"financial"})) is None and shared.plane_refusal(None) is None

    @runbound.tool(effects={"financial"})
    def pay():
        return "ran"

    assert outcome_of(pay) == "ran"


def test_o_flr_2b_the_worker_lists_the_tools_it_refuses_as_unsupported(tmp_path):
    shared = _rows_link(tmp_path, [{"tool": "wire", "state": "bogus"}, {"tool": "ok", "state": "enforced", "blocked": True},
                                   {"tool": "ghost", "geo_fence": "eu"}, "junk"])
    assert [u["tool"] for u in shared.policy_unsupported()] == ["ghost", "wire", None]
    assert shared.row_for("ok").unsupported is None


def test_o_flr_2c_a_dry_run_row_it_cannot_read_refuses_nothing_but_records_the_would_refuse(tmp_path):
    _rows_link(tmp_path, [{"tool": "wire", "state": "dry_run", "geo_fence": "eu"}])

    @runbound.tool(effects={"read"})
    def wire():
        return "ran"

    assert outcome_of(wire) == "ran"


def test_o_flr_1b_a_served_halt_survives_a_restart_while_the_plane_is_down(tmp_path):
    """Ruling R2-1: a halt holds until the plane lifts it, so the sealed cache carries it and a restarted worker starts halted."""
    first = engine_on_link(tmp_path, clock=Clock())
    first.apply_hello(HelloReply(halt=True))
    second = engine_on_link(tmp_path, clock=Clock())  # a restart; the plane never answers
    assert second.halted() and second.halt_mode() == "stop"

    @runbound.tool(effects={"read"})
    def ping():
        return "ran"

    assert outcome_of(ping) == "refused:halt"


def test_o_flr_1c_a_lifted_halt_is_off_the_disk_too(tmp_path):
    first = engine_on_link(tmp_path, clock=Clock())
    first.apply_hello(HelloReply(halt=True))
    first.apply_hello(HelloReply(halt=False))
    assert not engine_on_link(tmp_path, clock=Clock()).halted()
