"""The pre-hook chain: order, rewrite, and what a hook that raises does."""

from __future__ import annotations

import logging

import pytest

import runbound
from runbound import api, hooks
from runbound.hooks import allow, refuse, rewrite


@pytest.fixture(autouse=True)
def _clean():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_the_order_is_rewrite_validate_policy_capability_approval_gate():
    """RS-3 §4: user rewrite hooks, validate, policy, capability, approval, user gate hooks."""
    log = []

    def spy(call):
        log.append("pre")

    def constraint(call):
        log.append("policy")
        return True

    def approve(call):
        log.append("approval")
        return True

    def gate(call):
        log.append("gate")

    runbound.init(
        capabilities={"financial": "approve"},  # slot 4 refuses
        tool_policy=runbound.ToolPolicy(
            constraints={"pay": constraint}, require_approval=["pay"], approval_callback=approve
        ),
        pre_hooks=[spy],
        gate_hooks=[gate],
    )

    @runbound.tool(effects={"financial"})
    def pay(amount: float): ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        pay("ten")  # rewrite hooks ran, then validate refused: nothing after it runs
    assert caught.value.reason == "invalid_arguments" and log == ["pre"]

    log.clear()
    with pytest.raises(runbound.ExecutionRefused) as caught:
        pay(10.0)  # pre, policy rules, then the capability refuses; approval and gate never run
    assert caught.value.reason == "capability"
    assert log == ["pre", "policy"]


def test_gate_hooks_run_last_and_may_only_refuse():
    log = []
    runbound.init(
        tool_policy=runbound.ToolPolicy(require_approval=["pay"], approval_callback=lambda c: log.append("approval") or True),
        gate_hooks=[lambda call: log.append("gate")],
    )

    @runbound.tool(effects={"read"})
    def pay(amount: float):
        log.append("body")

    pay(1.0)
    assert log == ["approval", "gate", "body"]


def test_a_gate_hook_can_refuse_after_every_built_in_passed():
    runbound.init()

    @runbound.tool(effects={"read"}, gate_hooks=[lambda call: refuse("not on a Friday")])
    def deploy(env: str): ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        deploy("prod")
    assert caught.value.reason == "hook_refused" and "not on a Friday" in str(caught.value)


def test_a_gate_hook_cannot_rewrite():
    runbound.init(gate_hooks=[lambda call: rewrite(args=("other",))])

    @runbound.tool(effects={"read"})
    def look(x: str):
        return x

    assert look("kept") == "kept"  # read-only tool: the failed gate hook is logged and skipped

    @runbound.tool(effects={"financial"})
    def pay(x: str):
        return x

    with pytest.raises(runbound.ExecutionRefused) as caught:
        pay("kept")
    assert caught.value.reason == "hook_error"


def test_a_hook_that_outlives_the_timeout_counts_as_raising():
    import time

    runbound.init(pre_hooks=[lambda call: time.sleep(5)], hook_timeout_s=0.05)

    @runbound.tool(effects={"financial"})
    def pay(x: int): ...

    @runbound.tool(effects={"read"})
    def look(x: int):
        return x

    with pytest.raises(runbound.ExecutionRefused) as caught:
        pay(1)
    assert caught.value.reason == "hook_error"
    assert look(1) == 1


def test_a_hook_sees_the_callers_session():
    seen = []
    runbound.init(pre_hooks=[lambda call: seen.append(call.session_key)], hook_timeout_s=1.0)

    @runbound.tool(effects={"read"})
    def look(x: int): ...

    with runbound.session("user-7"):
        look(1)
    assert seen == ["user-7"]


def test_a_built_in_that_raises_refuses_even_a_read_tool(monkeypatch):
    runbound.init()

    @runbound.tool(effects={"read"})
    def look(x: int): ...

    def broken(self, args, kwargs):
        raise RuntimeError("validator bug")

    monkeypatch.setattr(hooks.Validator, "check", broken)
    with pytest.raises(runbound.ExecutionRefused) as caught:
        look(1)
    assert caught.value.reason == "hook_error"
    assert "validator bug" not in str(caught.value)


def test_approval_runs_last_after_the_capability_check():
    log = []

    def approve(call):
        log.append("approval")
        return True

    runbound.init(
        capabilities={"read": "allow"},
        tool_policy=runbound.ToolPolicy(require_approval=["pay"], approval_callback=approve),
    )

    @runbound.tool(effects={"read"})
    def pay(amount: float):
        log.append("body")

    pay(1.0)
    assert log == ["approval", "body"]


def test_a_rewrite_is_visible_to_policy():
    seen = []

    def cap_amount(call):
        return rewrite(kwargs={**call.kwargs, "amount": min(call.kwargs["amount"], 100.0)})

    def under_500(call):
        seen.append(call.kwargs["amount"])
        return call.kwargs["amount"] <= 500

    runbound.init(
        tool_policy=runbound.ToolPolicy(constraints={"refund": under_500}), pre_hooks=[cap_amount]
    )
    ran = []

    @runbound.tool(effects={"read"})
    def refund(amount: float):
        ran.append(amount)

    refund(amount=9000.0)  # would break the constraint; the rewrite brought it under
    assert seen == [100.0] and ran == [100.0]


def test_a_repairing_rewrite_passes_validation_and_the_body_gets_the_repaired_arguments():
    """The pass side: a hook turns "5" into 5 and fills a missing required argument."""
    ran = []

    def repair(call):
        if call.name != "book":
            return None
        kwargs = dict(call.kwargs)
        if isinstance(kwargs.get("slots"), str):
            kwargs["slots"] = int(kwargs["slots"])
        kwargs.setdefault("room", "main")
        return rewrite(args=(), kwargs=kwargs)

    runbound.init(pre_hooks=[repair])

    @runbound.tool(effects={"write"})
    def book(slots: int, room: str):
        ran.append((slots, room))

    book(slots="5")  # "5" would be refused (and room is missing) without the rewrite
    assert ran == [(5, "main")]
    assert runbound.current_session().executed_actions == 1


def test_a_rewrite_is_revalidated_and_never_mutates_the_callers_arguments():
    caller_kwargs = {"amount": 1.0}

    def break_it(call):
        return rewrite(kwargs={"amount": "free"})

    runbound.init(pre_hooks=[break_it])

    @runbound.tool
    def refund(amount: float): ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        refund(**caller_kwargs)
    assert caught.value.reason == "invalid_arguments"
    assert caller_kwargs == {"amount": 1.0}


def test_init_hooks_run_before_a_tools_own_and_the_first_refuse_wins():
    log = []

    def first(call):
        log.append("init")

    def second(call):
        log.append("tool")
        return refuse("use a smaller page size")

    def third(call):
        log.append("never")

    runbound.init(pre_hooks=[first])

    @runbound.tool(effects={"read"}, pre_hooks=[second, third])
    def search(q: str): ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        search("x")
    assert log == ["init", "tool"]
    assert caught.value.reason == "hook_refused"
    assert "use a smaller page size" in str(caught.value)


def test_a_hook_returning_false_refuses():
    runbound.init(pre_hooks=[lambda call: False])

    @runbound.tool(effects={"read"})
    def search(q: str): ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        search("x")
    assert caught.value.reason == "hook_refused"


# --- a user hook that raises: refuse for consequential and unclassified, continue for read/write/external ---


def boom(call):
    raise RuntimeError("secret-internal-detail")


@pytest.mark.parametrize(
    "effects", [{"financial"}, {"destructive"}, {"privileged"}, {"read", "financial"}, None]
)
def test_a_raising_user_hook_refuses_a_consequential_or_unclassified_tool(effects):
    runbound.init(pre_hooks=[boom])
    ran = []

    @runbound.tool(effects=effects) if effects else runbound.tool
    def act(x: int):
        ran.append(x)

    with pytest.raises(runbound.ExecutionRefused) as caught:
        act(1)

    assert ran == []
    assert caught.value.reason == "hook_error"
    assert "secret-internal-detail" not in str(caught.value)  # the exception text never reaches the model


@pytest.mark.parametrize("effects", [{"read"}, {"write"}, {"external"}, {"read", "write", "external"}])
def test_a_raising_user_hook_is_logged_once_and_a_read_write_or_external_tool_continues(effects, caplog):
    runbound.init(pre_hooks=[boom])
    ran = []

    @runbound.tool(effects=effects)
    def act(x: int):
        ran.append(x)

    with caplog.at_level(logging.WARNING, logger="runbound"):
        act(1)
        act(2)

    assert ran == [1, 2]
    assert caplog.text.count("pre-hook 'boom' failed") == 1


def test_a_hook_returning_nonsense_is_a_hook_error():
    runbound.init(pre_hooks=[lambda call: 42])

    @runbound.tool(effects={"financial"})
    def pay(x: int): ...

    with pytest.raises(runbound.ExecutionRefused) as caught:
        pay(1)
    assert caught.value.reason == "hook_error"


def test_a_framework_adapter_cannot_apply_a_rewrite():
    from runbound import api as api_module

    runbound.init(pre_hooks=[lambda call: rewrite(args=("other",))])
    with pytest.raises(runbound.ExecutionRefused) as caught:
        api_module._admit_action("t", ("x",), {}, frozenset({"financial"}), True, allow_rewrite=False)
    assert caught.value.reason == "hook_error"


def test_pre_hooks_must_be_callables():
    with pytest.raises(ValueError):
        runbound.init(pre_hooks=["not callable"])
    with pytest.raises(ValueError):
        runbound.tool(pre_hooks=[1])
    with pytest.raises(ValueError):
        runbound.tool(gate_hooks=[1])
    with pytest.raises(ValueError):
        runbound.init(hook_timeout_s=0)


def test_an_allow_verdict_changes_nothing():
    runbound.init(pre_hooks=[lambda call: allow()])

    @runbound.tool(effects={"read"})
    def search(q: str):
        return q

    assert search("ok") == "ok"
