"""Tests for merging an org (remote) policy into the local one.

Fleet mode hands the SDK a policy written by somebody who is not the person
who wrote the code. These tests pin the two properties that make that safe:
the merge only ever *removes* freedom (union the bans, intersect the
permissions, take the lower limit), and a violation of an org rule is
identifiable as one at the moment it happens — so a rollout can log org rules
instead of blocking on them without touching the local ones.
"""

import pytest

from runbound.policy import ToolCall, ToolPolicy, evaluate, merge


def call(name: str = "send_email", **kwargs) -> ToolCall:
    return ToolCall(name, (), dict(kwargs), None, {})


def always(answer: bool):
    def predicate(_call: ToolCall) -> bool:
        return answer

    return predicate


# --- the empty cases --------------------------------------------------------


def test_no_policy_on_either_side_is_no_policy():
    assert merge(None, None) is None


def test_local_alone_comes_back_as_an_equal_copy():
    local = ToolPolicy(deny=["wire_money"], max_calls={"refund": 1})

    merged = merge(local, None)

    assert merged is not local
    assert merged.deny == ["wire_money"]
    assert merged.max_calls == {"refund": 1}
    merged.deny.append("send_email")
    merged.max_calls["refund"] = 99
    assert local.deny == ["wire_money"]
    assert local.max_calls == {"refund": 1}


def test_a_local_dict_alone_is_coerced_too():
    merged = merge({"deny": ["wire_money"]}, None)

    assert isinstance(merged, ToolPolicy)
    assert merged.deny == ["wire_money"]


def test_copying_a_merged_policy_keeps_its_origins():
    once = merge(ToolPolicy(), ToolPolicy(deny=["pay"]))

    twice = merge(once, None)

    assert twice._origins == {"deny:pay": {"org"}}
    assert evaluate(twice, call("pay"), 1).details["origin"] == "org"


def test_remote_alone_comes_back_as_a_policy():
    merged = merge(None, {"deny": ["wire_money"]})

    assert isinstance(merged, ToolPolicy)
    assert merged.deny == ["wire_money"]


def test_a_dict_on_either_side_is_coerced():
    merged = merge({"deny": ["a"]}, {"deny": ["b"]})

    assert isinstance(merged, ToolPolicy)
    assert sorted(merged.deny) == ["a", "b"]


def test_two_empty_policies_merge_to_an_empty_one():
    merged = merge(ToolPolicy(), ToolPolicy())

    assert merged.deny == []
    assert merged.allow is None
    assert merged.max_calls == {}
    assert evaluate(merged, call(), 1) is None


# --- the field rules --------------------------------------------------------


def test_deny_is_the_union():
    merged = merge(ToolPolicy(deny=["a", "b"]), ToolPolicy(deny=["b", "c"]))

    assert merged.deny == ["a", "b", "c"]


def test_require_approval_is_the_union():
    merged = merge(
        ToolPolicy(require_approval=["a"], approval_callback=always(True)),
        ToolPolicy(require_approval=["b"]),
    )

    assert merged.require_approval == ["a", "b"]


def test_allow_is_the_intersection_when_both_sides_set_one():
    merged = merge(ToolPolicy(allow=["a", "b"]), ToolPolicy(allow=["b", "c"]))

    assert merged.allow == ["b"]


def test_allow_is_whichever_side_set_one():
    assert merge(ToolPolicy(allow=["a"]), ToolPolicy()).allow == ["a"]
    assert merge(ToolPolicy(), ToolPolicy(allow=["a"])).allow == ["a"]
    assert merge(ToolPolicy(), ToolPolicy()).allow is None


def test_an_empty_intersection_permits_nothing():
    merged = merge(ToolPolicy(allow=["a"]), ToolPolicy(allow=["b"]))

    assert merged.allow == []
    assert evaluate(merged, call("a"), 1).rule == "allow"


def test_max_calls_takes_the_lower_limit_over_the_union_of_tools():
    merged = merge(
        ToolPolicy(max_calls={"refund": 3, "email": 5}),
        ToolPolicy(max_calls={"refund": 1, "search": 9}),
    )

    assert merged.max_calls == {"refund": 1, "email": 5, "search": 9}


def test_constraints_and_the_approval_callback_stay_local():
    local_gate = always(False)
    local = ToolPolicy(
        constraints={"send_email": local_gate},
        require_approval=["pay"],
        approval_callback=local_gate,
    )
    remote_gate = always(True)

    merged = merge(local, {"constraints": {"pay": remote_gate}})

    assert merged.constraints == {"send_email": local_gate}
    assert merged.approval_callback is local_gate


def test_a_remote_approval_callback_is_ignored():
    local_gate = always(True)
    merged = merge(
        ToolPolicy(require_approval=["pay"], approval_callback=local_gate),
        ToolPolicy(approval_callback=always(False)),
    )

    assert merged.approval_callback is local_gate
    assert evaluate(merged, call("pay"), 1) is None


# --- on_violation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("local_mode", "remote_mode", "expected"),
    [
        ("dry_run", "block", "block"),
        ("block", "dry_run", "block"),
        ("block", "block_and_latch", "block_and_latch"),
        ("block_and_latch", "block", "block_and_latch"),
        ("dry_run", "dry_run", "dry_run"),
        ("block_and_latch", "block_and_latch", "block_and_latch"),
    ],
)
def test_the_stricter_on_violation_wins(local_mode, remote_mode, expected):
    merged = merge(
        ToolPolicy(on_violation=local_mode), ToolPolicy(on_violation=remote_mode)
    )

    assert merged.on_violation == expected


def test_a_remote_that_states_no_mode_leaves_the_local_one_alone():
    merged = merge(ToolPolicy(on_violation="dry_run"), {"deny": ["a"]})

    assert merged.on_violation == "dry_run"


def test_a_dry_run_remote_never_changes_the_local_mode():
    merged = merge(
        ToolPolicy(on_violation="dry_run"),
        ToolPolicy(on_violation="block_and_latch"),
        remote_dry_run=True,
    )

    assert merged.on_violation == "dry_run"


# --- purity -----------------------------------------------------------------


def test_merge_never_mutates_either_input():
    local = ToolPolicy(
        deny=["a"], allow=["x", "y"], max_calls={"t": 2}, require_approval=["p"],
        approval_callback=always(True),
    )
    remote = ToolPolicy(
        deny=["b"], allow=["y"], max_calls={"t": 1}, require_approval=["q"]
    )

    merge(local, remote)

    assert local.deny == ["a"]
    assert local.allow == ["x", "y"]
    assert local.max_calls == {"t": 2}
    assert local.require_approval == ["p"]
    assert remote.deny == ["b"]
    assert remote.allow == ["y"]
    assert remote.max_calls == {"t": 1}
    assert remote.require_approval == ["q"]


def test_the_merged_lists_are_not_shared_with_the_inputs():
    local = ToolPolicy(deny=["a"], allow=["x"])
    merged = merge(local, ToolPolicy())

    merged.deny.append("b")
    merged.allow.append("z")

    assert local.deny == ["a"]
    assert local.allow == ["x"]


# --- validation -------------------------------------------------------------


def test_deny_beats_a_local_allow_list_instead_of_conflicting():
    merged = merge(ToolPolicy(allow=["pay", "search"]), ToolPolicy(deny=["pay"]))

    assert merged.deny == ["pay"]
    assert merged.allow == ["search"]
    assert evaluate(merged, call("pay"), 1).rule == "deny"


def test_a_remote_rule_nobody_can_answer_is_rejected_loudly():
    with pytest.raises(ValueError, match="approval_callback"):
        merge(ToolPolicy(), ToolPolicy(require_approval=["pay"]))


def test_a_remote_limit_that_cannot_be_enforced_is_rejected():
    with pytest.raises(ValueError, match="max_calls"):
        merge(ToolPolicy(), {"max_calls": {"pay": 0}})


def test_a_remote_mode_that_does_not_exist_is_rejected():
    with pytest.raises(ValueError, match="on_violation"):
        merge(ToolPolicy(), {"on_violation": "explode"})


def test_a_remote_dict_with_an_unknown_field_is_rejected():
    with pytest.raises(ValueError, match="ToolPolicy"):
        merge(ToolPolicy(), {"denied": ["pay"]})


# --- origins ----------------------------------------------------------------


def test_org_rules_are_tracked_and_local_ones_are_not():
    merged = merge(ToolPolicy(deny=["local_only"]), ToolPolicy(deny=["send_email"]))

    assert merged._origins == {"deny:send_email": {"org"}}


def test_a_rule_both_sides_state_is_the_local_one():
    merged = merge(ToolPolicy(deny=["pay"]), ToolPolicy(deny=["pay"]))

    assert merged._origins == {}
    assert evaluate(merged, call("pay"), 1).details.get("origin") is None


def test_a_policy_that_was_never_merged_reports_no_origin():
    policy = ToolPolicy(deny=["pay"])

    assert evaluate(policy, call("pay"), 1).details == {"tool": "pay", "rule": "deny"}


def test_a_deny_from_the_org_is_reported_as_such():
    merged = merge(ToolPolicy(), ToolPolicy(deny=["send_email"]))

    violation = evaluate(merged, call("send_email"), 1)

    assert violation.rule == "deny"
    assert violation.details["origin"] == "org"


def test_a_deny_from_the_local_policy_carries_no_origin():
    merged = merge(ToolPolicy(deny=["send_email"]), ToolPolicy(deny=["other"]))

    violation = evaluate(merged, call("send_email"), 1)

    assert "origin" not in violation.details


def test_an_allow_list_the_org_alone_imposed_is_org_origin():
    merged = merge(ToolPolicy(), ToolPolicy(allow=["search"]))

    violation = evaluate(merged, call("send_email"), 1)

    assert violation.rule == "allow"
    assert violation.details["origin"] == "org"


def test_a_tool_the_org_alone_removed_from_the_allow_list_is_org_origin():
    merged = merge(ToolPolicy(allow=["search", "pay"]), ToolPolicy(allow=["search"]))

    assert evaluate(merged, call("pay"), 1).details["origin"] == "org"
    # never on the local list either: the local policy already refused it
    assert "origin" not in evaluate(merged, call("wire_money"), 1).details


def test_the_lower_limit_carries_the_origin_of_the_side_that_set_it():
    org, local = {"pay": 1}, {"pay": 5}
    stricter_org = merge(ToolPolicy(max_calls=local), ToolPolicy(max_calls=org))
    stricter_local = merge(ToolPolicy(max_calls=org), ToolPolicy(max_calls=local))

    org_violation = evaluate(stricter_org, call("pay"), 2)
    assert org_violation.details["origin"] == "org"
    assert org_violation.details["limit"] == 1
    assert "origin" not in evaluate(stricter_local, call("pay"), 2).details


def test_an_approval_the_org_requires_is_org_origin():
    merged = merge(
        ToolPolicy(approval_callback=always(False)),
        ToolPolicy(require_approval=["pay"]),
    )

    violation = evaluate(merged, call("pay"), 1)

    assert violation.rule == "approval"
    assert violation.details["origin"] == "org"


# --- dry-run rollout of the org policy --------------------------------------


def test_a_dry_run_org_rule_is_flagged_but_still_reported_as_its_rule():
    merged = merge(ToolPolicy(), ToolPolicy(deny=["send_email"]), remote_dry_run=True)

    violation = evaluate(merged, call("send_email"), 1)

    assert violation.rule == "deny"
    assert violation.details["origin"] == "org"
    assert violation.details["dry_run"] is True


def test_a_dry_run_org_policy_leaves_local_rules_blocking():
    merged = merge(
        ToolPolicy(deny=["local"]), ToolPolicy(deny=["org"]), remote_dry_run=True
    )

    local_violation = evaluate(merged, call("local"), 1)

    assert "dry_run" not in local_violation.details
    assert "origin" not in local_violation.details
    assert merged.on_violation == "block"


def test_org_rules_are_not_flagged_when_the_org_policy_is_enforced():
    merged = merge(ToolPolicy(), ToolPolicy(deny=["send_email"]))

    assert "dry_run" not in evaluate(merged, call("send_email"), 1).details


@pytest.mark.parametrize(
    ("kwargs", "tool", "rule"),
    [
        ({"deny": ["pay"]}, "pay", "deny"),
        ({"allow": ["search"]}, "pay", "allow"),
        ({"max_calls": {"pay": 1}}, "pay", "max_calls"),
        ({"require_approval": ["pay"]}, "pay", "approval"),
    ],
)
def test_every_remote_rule_can_be_rolled_out_dry(kwargs, tool, rule):
    merged = merge(
        ToolPolicy(approval_callback=always(False)),
        ToolPolicy(**kwargs),
        remote_dry_run=True,
    )

    violation = evaluate(merged, call(tool), 2)

    assert violation.rule == rule
    assert violation.details["origin"] == "org"
    assert violation.details["dry_run"] is True


def test_a_merged_policy_still_evaluates_in_rule_order():
    merged = merge(
        ToolPolicy(allow=["pay"], max_calls={"pay": 1}), ToolPolicy(deny=["pay"])
    )

    assert evaluate(merged, call("pay"), 99).rule == "deny"


def test_the_private_bookkeeping_stays_out_of_repr_and_equality():
    merged = merge(ToolPolicy(), ToolPolicy(deny=["pay"]))

    assert merged == ToolPolicy(deny=["pay"])
    assert "_origins" not in repr(merged)
