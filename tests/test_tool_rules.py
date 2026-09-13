"""Rules on the decorator: ``@runbound.tool(max_calls=1, ...)``.

The rule lives on the tool, in the same line and the same diff as the function
it governs — no file, no CLI, no second place to look. These tests pin the two
things that make that safe: the fold is **live**, so a decorator that runs
after ``init()`` is still enforced (which is the ordering every real app has),
and the decorator's ``reviewed=True`` never reaches :attr:`ToolPolicy.allow`,
which would invert into an allow-list and deny every other tool in the process.

``evaluate`` and ``merge`` are untouched by all of this: the decorator only
builds the same :class:`~runbound.policy.ToolPolicy` a customer could have
written by hand.
"""

import logging

import pytest

import runbound
from runbound import api, policy as policy_mod
from runbound.exceptions import PolicyViolation
from runbound.policy import ToolCall, ToolPolicy, ToolRules, evaluate, from_decorators


@pytest.fixture(autouse=True)
def _uninitialized():
    """Every test starts and ends with a pristine, uninitialized SDK."""
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


class Counter:
    """A tool body that counts the times it actually ran."""

    def __init__(self) -> None:
        self.runs = 0

    def __call__(self, *args, **kwargs):
        self.runs += 1
        return "ran"


def local_policy() -> ToolPolicy | None:
    """The effective local policy the engine would enforce right now."""
    return api._ENGINE._local_policy()


def refuse(func, *args, **kwargs) -> PolicyViolation:
    """Call ``func`` expecting the policy to refuse it, and return the refusal."""
    with pytest.raises(PolicyViolation) as caught:
        func(*args, **kwargs)
    return caught.value


# --- acceptance 2: the fold is live, not snapshotted at init() ---------------


def test_a_tool_decorated_after_init_is_still_enforced():
    """The demo's real ordering: init() at line 47, the decorators at line 69.

    A policy folded once inside ``init()`` would see an empty registry and
    enforce nothing at all — the exact failure this feature exists to prevent.
    """
    runbound.init()  # before any @runbound.tool has run

    ran = Counter()

    @runbound.tool(max_calls=1)
    def issue_refund(user, amount):
        return ran(user, amount)

    assert issue_refund("u", 10.0) == "ran"
    violation = refuse(issue_refund, "u", 20.0)

    assert violation.violation.rule == "max_calls"
    assert violation.violation.tool == "issue_refund"
    assert ran.runs == 1, "the refused call must not have run the tool body"


def test_the_folded_policy_is_the_same_object_while_nothing_changes():
    """``Engine._policy`` keys its org merge on ``id(local)``.

    A fold that rebuilt the policy on every tool call would thrash that cache
    on every call in the process, so the same object must come back.
    """
    runbound.init()

    @runbound.tool(max_calls=2)
    def tool_a(x): ...

    first = local_policy()
    assert local_policy() is first

    @runbound.tool(blocked=True)
    def tool_b(x): ...

    assert local_policy() is not first, "a new decorator must refold"
    assert local_policy() is local_policy()


# --- acceptance 1: each keyword folds to the right ToolPolicy field ----------


def test_blocked_folds_to_deny_so_the_ledger_rule_name_does_not_change():
    runbound.init()

    ran = Counter()

    @runbound.tool(blocked=True)
    def send_email(to):
        return ran(to)

    assert local_policy().deny == ["send_email"]
    assert refuse(send_email, "a@b.c").violation.rule == "deny"
    assert ran.runs == 0


def test_max_calls_folds_to_max_calls():
    runbound.init()

    @runbound.tool(max_calls=3)
    def issue_refund(user): ...

    assert local_policy().max_calls == {"issue_refund": 3}


def test_constraint_folds_to_constraints_and_refuses_on_the_real_arguments():
    runbound.init()

    ran = Counter()

    def under_500(call: ToolCall) -> bool:
        return call.args[1] <= 500

    @runbound.tool(constraint=under_500)
    def issue_refund(user, amount):
        return ran(user, amount)

    assert local_policy().constraints == {"issue_refund": under_500}
    assert issue_refund("u", 100) == "ran"
    assert refuse(issue_refund, "u", 501).violation.rule == "constraint"
    assert ran.runs == 1


def test_require_approval_folds_to_the_list_and_a_callback():
    runbound.init()

    @runbound.tool(require_approval=lambda call: True)
    def wire_money(account, amount): ...

    folded = local_policy()
    assert folded.require_approval == ["wire_money"]
    assert folded.approval_callback is not None


def test_the_folded_policy_refuses_exactly_as_the_hand_written_one_does():
    """The decorator is sugar: ``evaluate`` must not be able to tell them apart."""
    runbound.init()

    def under_500(call: ToolCall) -> bool:
        return call.kwargs.get("amount", 0) <= 500

    @runbound.tool(max_calls=1, constraint=under_500)
    def issue_refund(user, amount): ...

    @runbound.tool(blocked=True)
    def send_email(to): ...

    folded = local_policy()
    handwritten = ToolPolicy(
        deny=["send_email"],
        max_calls={"issue_refund": 1},
        constraints={"issue_refund": under_500},
    )
    calls = [
        (ToolCall("issue_refund", (), {"amount": 100}, None, {}), 1),
        (ToolCall("issue_refund", (), {"amount": 100}, None, {}), 2),
        (ToolCall("issue_refund", (), {"amount": 900}, None, {}), 1),
        (ToolCall("send_email", (), {}, None, {}), 1),
        (ToolCall("lookup_order", (), {}, None, {}), 9),
    ]
    for call, seen in calls:
        mine = evaluate(folded, call, seen)
        theirs = evaluate(handwritten, call, seen)
        assert mine == theirs, f"{call.name} at {seen} calls"


def test_a_bare_decorator_states_no_rule_and_restricts_nothing():
    runbound.init()

    @runbound.tool
    def lookup_order(order_id): ...

    assert local_policy() is None
    assert lookup_order("o-1") is None


# --- acceptance 3 and 4: one approval callback per tool ----------------------


def test_two_tools_with_two_callbacks_each_get_their_own():
    runbound.init()

    asked: list[str] = []

    def approve_wire(call: ToolCall) -> bool:
        asked.append("wire:" + call.name)
        return True

    def refuse_delete(call: ToolCall) -> bool:
        asked.append("delete:" + call.name)
        return False

    @runbound.tool(require_approval=approve_wire)
    def wire_money(account): ...

    @runbound.tool(require_approval=refuse_delete)
    def delete_account(user): ...

    wire_money("acct-1")
    assert refuse(delete_account, "u-1").violation.rule == "approval"
    assert asked == ["wire:wire_money", "delete:delete_account"]


def test_a_tool_needing_approval_with_no_callable_anywhere_is_refused():
    """Fail-CLOSED. A gate nobody can answer must never wave the call through."""
    runbound.init()

    @runbound.tool(require_approval=lambda call: True)
    def wire_money(account): ...

    folded = local_policy()
    orphan = ToolCall("delete_account", (), {}, None, {})
    assert folded.approval_callback(orphan) is False

    with_orphan = ToolPolicy(
        require_approval=["delete_account"],
        approval_callback=folded.approval_callback,
    )
    violation = evaluate(with_orphan, orphan, 1)
    assert violation is not None and violation.rule == "approval"


def test_a_tool_with_no_callable_falls_through_to_the_init_callback():
    asked: list[str] = []

    def ask_a_human(call: ToolCall) -> bool:
        asked.append(call.name)
        return False

    runbound.init(
        tool_policy={
            "require_approval": ["delete_account"],
            "approval_callback": ask_a_human,
        }
    )

    @runbound.tool(require_approval=lambda call: True)
    def wire_money(account): ...

    @runbound.tool
    def delete_account(user): ...

    wire_money("acct-1")
    assert refuse(delete_account, "u-1").violation.rule == "approval"
    assert asked == ["delete_account"]


def test_an_approval_callback_that_raises_still_refuses():
    """policy.py is fail-closed for gates; the dispatcher must not soften it."""
    runbound.init()

    def explode(call: ToolCall) -> bool:
        raise KeyError("no reviewer configured")

    @runbound.tool(require_approval=explode)
    def wire_money(account): ...

    violation = refuse(wire_money, "acct-1").violation
    assert violation.rule == "approval"
    assert violation.details["error"] == "KeyError"


# --- acceptance 5: reviewed=True is not ToolPolicy.allow ------------------------


def test_reviewed_true_must_never_reach_tool_policy_allow_which_would_deny_everything():
    """``ToolPolicy.allow`` is an INVERTING allow-list.

    The decorator's ``reviewed=True`` means "reviewed, deliberately unrestricted"
    and exists only to satisfy ``require_rules``. Folding it into
    ``ToolPolicy.allow`` would make the listed tool the *only* permitted one
    and deny every other tool in the process. Do not 'simplify' this away.
    """
    runbound.init()

    @runbound.tool(reviewed=True)
    def lookup_order(order_id):
        return "ran"

    @runbound.tool
    def read_profile(user):
        return "ran"

    @runbound.tool
    def list_orders(user):
        return "ran"

    @runbound.tool
    def check_stock(sku):
        return "ran"

    folded = local_policy()
    assert folded is None or folded.allow is None, (
        "reviewed=True leaked into ToolPolicy.allow, which INVERTS: every tool "
        f"not listed is now denied. allow={getattr(folded, 'allow', None)!r}"
    )
    assert lookup_order("o") == "ran"
    assert read_profile("u") == "ran"
    assert list_orders("u") == "ran"
    assert check_stock("s") == "ran"


def test_reviewed_true_leaves_a_configured_fleet_allow_list_alone():
    """``init(tool_policy=...)`` stays the one place a real allow-list is said."""
    runbound.init(tool_policy={"allow": ["lookup_order", "read_profile"]})

    @runbound.tool(reviewed=True)
    def lookup_order(order_id):
        return "ran"

    @runbound.tool(reviewed=True)
    def send_email(to):
        return "ran"

    assert local_policy().allow == ["lookup_order", "read_profile"]
    assert lookup_order("o") == "ran"
    assert refuse(send_email, "a@b.c").violation.rule == "allow"


# --- acceptance 6: the decorator wins, and init() warns once -----------------


def test_a_tool_named_in_both_places_takes_the_decorator_rule_and_warns_once(caplog):
    runbound.init(tool_policy={"max_calls": {"issue_refund": 5}})

    @runbound.tool(max_calls=1)
    def issue_refund(user): ...

    with caplog.at_level(logging.WARNING, logger="runbound"):
        assert local_policy().max_calls == {"issue_refund": 1}
        for _ in range(5):
            local_policy()

    warnings = [r for r in caplog.records if "issue_refund" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "decorator" in warnings[0].getMessage()

    issue_refund("u")
    assert refuse(issue_refund, "u").violation.details["limit"] == 1


def test_a_fleet_allow_list_entry_is_not_a_conflict_and_is_not_warned_about(caplog):
    """The decorator took nothing away: the tool is still on the allow list."""
    runbound.init(tool_policy={"allow": ["issue_refund", "lookup_order"]})

    with caplog.at_level(logging.WARNING, logger="runbound"):

        @runbound.tool(max_calls=1)
        def issue_refund(user): ...

        local_policy()

    assert not [r for r in caplog.records if "issue_refund" in r.getMessage()]
    assert local_policy().allow == ["issue_refund", "lookup_order"]


def test_two_sides_denying_the_same_tool_agree_and_are_not_warned_about(caplog):
    runbound.init(tool_policy={"deny": ["send_email"]})

    with caplog.at_level(logging.WARNING, logger="runbound"):

        @runbound.tool(blocked=True)
        def send_email(to): ...

        assert local_policy().deny == ["send_email"]

    assert not [r for r in caplog.records if "send_email" in r.getMessage()]


def test_a_decorator_block_wins_over_an_init_allow_entry_for_the_same_tool():
    """Deny always wins, exactly as :func:`policy.merge` resolves it."""
    runbound.init(tool_policy={"allow": ["send_email", "lookup_order"]})

    @runbound.tool(blocked=True)
    def send_email(to): ...

    folded = local_policy()
    assert folded.deny == ["send_email"]
    assert folded.allow == ["lookup_order"]
    folded.validate()  # deny and allow must not disagree about the same tool


# --- acceptance 7: require_rules ---------------------------------------------


def test_require_rules_raises_at_init_naming_every_unruled_tool():
    @runbound.tool
    def lookup_order(order_id): ...

    @runbound.tool
    def read_profile(user): ...

    @runbound.tool(max_calls=1)
    def issue_refund(user): ...

    with pytest.raises(ValueError) as caught:
        runbound.init(require_rules=True)

    message = str(caught.value)
    assert "lookup_order" in message and "read_profile" in message
    assert "issue_refund" not in message


def test_require_rules_raises_at_decoration_time_for_a_tool_declared_afterwards():
    """The CI gate: decorators run *after* ``init()``, so ``init()`` alone is not enough."""
    runbound.init(require_rules=True)

    with pytest.raises(ValueError) as caught:

        @runbound.tool
        def lookup_order(order_id): ...

    assert "lookup_order" in str(caught.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"blocked": True},
        {"max_calls": 1},
        {"constraint": lambda call: True},
        {"require_approval": lambda call: True},
        {"reviewed": True},
    ],
    ids=["blocked", "max_calls", "constraint", "require_approval", "reviewed"],
)
def test_any_real_rule_satisfies_require_rules(kwargs):
    runbound.init(require_rules=True)

    @runbound.tool(**kwargs)
    def wire_money(account): ...

    assert wire_money  # decorating did not raise


def test_repeatable_and_name_are_not_rules():
    runbound.init(require_rules=True)

    with pytest.raises(ValueError):

        @runbound.tool(name="refund", repeatable=True)
        def issue_refund(user): ...


def test_require_rules_off_by_default_lets_an_unruled_tool_through():
    runbound.init()

    @runbound.tool
    def lookup_order(order_id):
        return "ran"

    assert lookup_order("o-1") == "ran"


def test_require_rules_must_be_a_bool():
    with pytest.raises(ValueError):
        runbound.init(require_rules="yes")


# --- acceptance 8: the demo's LOCAL_POLICY, before and after -----------------


def test_the_acme_refunds_rules_behave_exactly_as_the_dict_they_replaced():
    """``{"max_calls": {"issue_refund": 1}, "on_violation": "block"}``, on the tools.

    The ledger records the rule a refusal names, so the demo's scorecard reads
    the same rows only if the folded policy is the same policy. It is: the
    limit is still ``max_calls``, ``on_violation`` still lives on ``init()``
    because it is policy-wide, and the two reviewed tools carry ``reviewed=True``,
    which restricts nothing.
    """
    before = ToolPolicy(max_calls={"issue_refund": 1}, on_violation="block")

    runbound.init(tool_policy={"on_violation": "block"})

    @runbound.tool(max_calls=1)
    def issue_refund(user, amount): ...

    @runbound.tool(reviewed=True)
    def lookup_order(order_id): ...

    @runbound.tool(reviewed=True)
    def send_email(to): ...

    after = local_policy()
    assert after == before

    calls = [
        (ToolCall("issue_refund", ("u", 40.0), {}, "user:1", {}), 1),
        (ToolCall("issue_refund", ("u", 40.0), {}, "user:1", {}), 2),
        (ToolCall("lookup_order", ("o-1",), {}, "user:1", {}), 7),
        (ToolCall("send_email", ("a@b.c",), {}, "user:1", {}), 7),
    ]
    for call, seen in calls:
        assert evaluate(after, call, seen) == evaluate(before, call, seen)

    assert evaluate(after, *calls[1]).rule == "max_calls"


def test_the_acme_org_policy_scenario_still_denies_send_email_on_top():
    """Scenario 5: the plane pushes a deny, and the merge only removes freedom."""
    from runbound.policy import merge

    local = from_decorators(
        {
            "issue_refund": ToolRules(max_calls=1),
            "lookup_order": ToolRules(reviewed=True),
            "send_email": ToolRules(reviewed=True),
        },
        {"on_violation": "block"},
    )
    merged = merge(local, {"deny": ["send_email"]})

    assert merged.deny == ["send_email"]
    assert merged.max_calls == {"issue_refund": 1}
    denied = evaluate(merged, ToolCall("send_email", (), {}, "user:1", {}), 1)
    assert denied.rule == "deny" and denied.details["origin"] == "org"


# --- acceptance 9: the tool report carries the rules -------------------------


def test_the_tool_report_carries_the_rules_with_callables_as_dotted_names():
    def under_500(call: ToolCall) -> bool:
        return True

    @runbound.tool(max_calls=1, constraint=under_500)
    def issue_refund(user, amount): ...

    @runbound.tool(blocked=True)
    def send_email(to): ...

    @runbound.tool
    def lookup_order(order_id): ...

    report = {entry["name"]: entry for entry in runbound.tools()}
    assert report["issue_refund"]["rules"] == {
        "max_calls": 1,
        "constraint": f"{__name__}:test_the_tool_report_carries_the_rules_with_callables_as_dotted_names.<locals>.under_500",
    }
    assert report["send_email"]["rules"] == {"blocked": True}
    assert report["lookup_order"]["rules"] == {}


def test_no_callable_and_no_memory_address_ever_reaches_the_report():
    """The report is JSON on the wire; a callable in it is a leak and an unstable hash."""
    import json

    @runbound.tool(require_approval=lambda call: True, reviewed=False)
    def wire_money(account): ...

    @runbound.tool(constraint=lambda call: True)
    def issue_refund(user): ...

    report = runbound.tools()
    text = json.dumps(report)  # would raise on a callable
    assert " at 0x" not in text
    for entry in report:
        for value in entry["rules"].values():
            assert isinstance(value, (bool, int, str))


def test_editing_a_returned_report_cannot_edit_the_next_one():
    @runbound.tool(max_calls=1)
    def issue_refund(user): ...

    first = runbound.tools()
    first[0]["rules"]["max_calls"] = 99
    assert runbound.tools()[0]["rules"] == {"max_calls": 1}


def test_a_tool_the_model_asked_for_reports_no_rules():
    runbound.init()
    api._ENGINE  # the tool was never decorated; the model simply named it
    from runbound import _coverage

    _coverage.tool_requested("wire_money")
    entry = [e for e in runbound.tools() if e["name"] == "wire_money"][0]
    assert entry["decorated"] is False
    assert entry["rules"] == {}


# --- the builder itself ------------------------------------------------------


def test_from_decorators_returns_the_base_untouched_when_no_rule_is_stated():
    base = ToolPolicy(deny=["wire_money"])
    assert from_decorators({}, base) is base
    assert from_decorators({"a": ToolRules()}, base) is base
    assert from_decorators({}, None) is None


def test_from_decorators_does_not_mutate_the_base_policy():
    base = ToolPolicy(deny=["wire_money"], max_calls={"send_email": 2})
    folded = from_decorators({"issue_refund": ToolRules(max_calls=1)}, base)
    assert base.deny == ["wire_money"]
    assert base.max_calls == {"send_email": 2}
    assert folded.max_calls == {"send_email": 2, "issue_refund": 1}
    assert folded.deny == ["wire_money"]


def test_from_decorators_accepts_a_dict_base():
    folded = from_decorators(
        {"send_email": ToolRules(blocked=True)}, {"on_violation": "dry_run"}
    )
    assert folded.deny == ["send_email"]
    assert folded.on_violation == "dry_run"


def test_tool_rules_reports_only_what_was_stated():
    assert ToolRules().as_report() == {}
    assert ToolRules(blocked=True).as_report() == {"blocked": True}
    assert ToolRules(reviewed=True).as_report() == {"reviewed": True}
    assert ToolRules(max_calls=4).as_report() == {"max_calls": 4}


def test_a_bad_decorator_keyword_is_rejected_at_decoration_time():
    """Configuration errors fail loudly where they are written, as ``init()`` does."""
    for kwargs in (
        {"max_calls": 0},
        {"max_calls": 1.5},
        {"max_calls": True},
        {"constraint": "under_500"},
        {"require_approval": 7},
        {"blocked": "yes"},
        {"reviewed": 1},
        {"blocked": True, "reviewed": True},
    ):
        with pytest.raises(ValueError):

            @runbound.tool(**kwargs)
            def issue_refund(user): ...


# --- fail-open at the seam ---------------------------------------------------


def test_a_fold_that_cannot_be_built_leaves_the_configured_policy_enforcing(
    monkeypatch, caplog
):
    runbound.init(tool_policy={"deny": ["wire_money"]})

    @runbound.tool(max_calls=1)
    def issue_refund(user): ...

    def explode(*args, **kwargs):
        raise RuntimeError("builder is broken")

    monkeypatch.setattr("runbound.engine.from_decorators", explode)
    api._ENGINE._local_key = None
    with caplog.at_level(logging.WARNING, logger="runbound"):
        folded = local_policy()

    assert folded is api._ENGINE.config.tool_policy
    assert folded.deny == ["wire_money"]
    assert any("decorator" in r.getMessage() for r in caplog.records)


def test_an_unusable_configured_policy_still_lets_the_decorators_enforce(caplog):
    runbound.init()
    api._ENGINE.config.tool_policy = object()

    @runbound.tool(blocked=True)
    def send_email(to): ...

    with caplog.at_level(logging.WARNING, logger="runbound"):
        folded = local_policy()
    assert folded.deny == ["send_email"]


def test_a_registry_that_cannot_be_read_enforces_the_configured_policy(monkeypatch):
    runbound.init(tool_policy={"deny": ["wire_money"]})

    def explode():
        raise RuntimeError("registry is broken")

    monkeypatch.setattr(policy_mod, "ORIGIN_ORG", "org")  # no-op, keeps the import used
    monkeypatch.setattr("runbound._coverage.tool_rules", explode)
    monkeypatch.setattr("runbound._coverage.tool_rules_version", explode)
    api._ENGINE._local_key = None
    assert local_policy().deny == ["wire_money"]
