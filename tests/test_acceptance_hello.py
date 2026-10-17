"""VPC-B5 (WIRE-VPC 2, 3, 11): the hello says WHICH process this is and WHAT it is enforcing, and the SDK accepts the served ``rule_revision``.

``boot_id`` is drawn once per process (a new ``Shared`` is a new boot), ``seq`` only goes up, and ``accepted_cfg`` is the hash the plane served, echoed as it came
and never computed here, and only when every row was read and applied. No sockets, no sleeping: the plane is the in-memory one of ``test_shared_state``."""

from __future__ import annotations

import re

from runbound import lines, policy_rows
from runbound.plane_types import HelloReply

from test_shared_state import FakePlane, MovableClock, remote

UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ROW = {"tool": "look", "version": 4, "state": "enforced", "blocked": True}


def policy(rows, **extra):
    return {"version": 4, "policy": None, "rows": rows, "rows_hash": "rh-served", "cfg_hash": "cfg-served", **extra}


def held(plane_body, tmp_path):
    shared = remote(FakePlane(policy_body=plane_body), MovableClock(), cache_dir=str(tmp_path))  # a cache of its own: nothing is loaded from, or left in, another test's
    shared.apply_hello(HelloReply(policy_version=4))
    return shared


def test_the_boot_id_is_one_uuid_per_process_and_the_same_on_every_hello(tmp_path):
    shared = remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path))
    first, second = shared._hello_payload(), shared._hello_payload()
    assert UUID4.match(first["boot_id"]) and first["boot_id"] == second["boot_id"]


def test_seq_counts_this_boots_hellos_from_one_and_only_goes_up(tmp_path):
    shared = remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path))
    assert [shared._hello_payload()["seq"] for _ in range(4)] == [1, 2, 3, 4]


def test_a_new_process_is_a_new_boot_and_starts_counting_again(tmp_path):
    one, two = remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path / 'a')), remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path / 'b'))
    a, b = one._hello_payload(), two._hello_payload()
    assert a["boot_id"] != b["boot_id"] and (a["seq"], b["seq"]) == (1, 1)


def test_seq_is_strictly_increasing_when_hellos_are_built_from_several_threads(tmp_path):
    import threading

    shared = remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path))
    seen, lock = [], threading.Lock()

    def build():
        for _ in range(50):
            value = shared._hello_payload()["seq"]
            with lock:
                seen.append(value)

    threads = [threading.Thread(target=build) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(seen) == list(range(1, 201)), "every number once: none repeated, none skipped"


def test_nothing_is_claimed_before_a_policy_has_been_applied(tmp_path):
    assert "accepted_cfg" not in remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path))._hello_payload()


def test_the_hash_the_plane_served_is_echoed_exactly_as_served_and_never_computed(tmp_path):
    shared = held(policy([ROW]), tmp_path)
    assert shared._hello_payload()["accepted_cfg"] == "cfg-served"


def test_without_a_cfg_hash_in_the_envelope_the_rows_hash_stands_in_and_without_either_it_is_empty(tmp_path):
    assert held(policy([ROW], cfg_hash=None), tmp_path / "first")._hello_payload()["accepted_cfg"] == "rh-served"
    assert held({"version": 4, "policy": {"deny": []}}, tmp_path / "second")._hello_payload()["accepted_cfg"] == ""


def test_acceptance_is_withheld_while_any_row_is_unreadable_or_unsupported(tmp_path):
    unknown_key = {"tool": "odd", "version": 4, "state": "enforced", "surprise": 1}
    nameless = {"version": 4, "state": "enforced", "blocked": True}
    for rows in ([ROW, unknown_key], [ROW, nameless]):
        payload = held(policy(rows), tmp_path / str(len(rows)) / str(id(rows)))._hello_payload()
        assert "accepted_cfg" in payload and payload["accepted_cfg"] is None, "the worker is not enforcing what was served, so it says nothing is accepted"


def test_unreadable_service_defaults_also_withhold_acceptance(tmp_path):
    payload = held(policy([ROW], service_defaults={"state": "enforced", "defaults": "not a mapping"}), tmp_path)._hello_payload()
    assert payload["accepted_cfg"] is None


def test_a_later_envelope_without_the_hash_does_not_keep_the_old_one(tmp_path):
    plane = FakePlane(policy_body=policy([ROW]))
    shared = remote(plane, MovableClock(), cache_dir=str(tmp_path))
    shared.apply_hello(HelloReply(policy_version=4))
    assert shared._hello_payload()["accepted_cfg"] == "cfg-served"
    plane.policy_body = {"version": 5, "policy": None, "rows": [ROW], "rows_hash": "rh-2"}
    shared.apply_hello(HelloReply(policy_version=5))
    assert shared._hello_payload()["accepted_cfg"] == "rh-2"


def test_the_worker_reports_acceptance_and_revisions_among_its_features(tmp_path):
    assert {"acceptance_v1", "revisions_v1"} <= set(lines.FEATURES)
    assert {"acceptance_v1", "revisions_v1"} <= set(remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path))._hello_payload()["policy_features"])


# --- rule_revision: a served row and its candidate carry it and still enforce ------------------------------------------------------------


def test_a_row_and_its_candidate_may_carry_a_rule_revision_and_it_changes_nothing_about_enforcement(tmp_path):
    plain = policy_rows.parse([ROW])["look"]
    revised = policy_rows.parse([{**ROW, "rule_revision": 3, "candidate": {"state": "dry_run", "version": 5, "rule_revision": 4, "blocked": False}}])["look"]
    assert not revised.unsupported and revised.blocked is plain.blocked is True
    assert revised.candidate is not None and not revised.candidate.unsupported


def test_a_rule_revision_that_is_not_a_whole_number_refuses_the_tool_rather_than_being_guessed(tmp_path):
    for bad in (0, -1, "3", 2.5, True, None):
        row = policy_rows.parse([{**ROW, "rule_revision": bad}])["look"]
        assert row.unsupported, bad


def test_a_served_key_this_sdk_does_not_know_still_refuses_the_tool(tmp_path):
    assert policy_rows.parse([{**ROW, "something_else": 1}])["look"].unsupported


def test_a_worker_restarted_from_its_cache_echoes_the_hash_it_holds_before_it_hears_from_the_plane(tmp_path):
    held(policy([ROW]), tmp_path)  # the first process applied a policy and wrote its cache
    again = remote(FakePlane(), MovableClock(), cache_dir=str(tmp_path))
    payload = again._hello_payload()
    assert payload["accepted_cfg"] == "rh-served", "the rows it enforces are the cached ones; their hash is the plane's rows_hash"
    assert UUID4.match(payload["boot_id"]) and payload["seq"] == 1
