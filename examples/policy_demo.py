#!/usr/bin/env python3
"""A support agent that tries four things the business told it not to do.

    .venv/bin/python examples/policy_demo.py

No network, no API key, no packages. The tools below are ordinary functions
decorated with ``@runbound.tool``, which fires BEFORE the body runs — so a
refused call never happens at all, rather than being reported afterwards.

Nothing here is judged by a model. Every refusal is one of the business's own
rules, written down in a ``ToolPolicy`` and enforced deterministically at the
hook: a deny list, a per-session call cap, an argument constraint, and a human
approval gate.
"""

import logging

import runbound
from runbound import GuardrailTripped, PolicyViolation, ToolPolicy

KEY = "user:8842"
TAGS = {"plan": "free"}

APPROVED: set[str] = set()  # what the human on call has said yes to
RAN: list[str] = []  # tool bodies that actually executed


NOTICES: list[str] = []  # runbound's own log lines, shown under their call


class _Notices(logging.Handler):
    """Collects runbound's log lines so the story can print them in place."""

    def emit(self, record: logging.LogRecord) -> None:
        NOTICES.append(record.getMessage())


@runbound.tool
def send_email(to: str, body: str) -> str:
    RAN.append("send_email")
    return f"sent to {to}"


@runbound.tool
def issue_refund(order: str, amount: float) -> str:
    RAN.append("issue_refund")
    return f"refunded ${amount:.0f}"


@runbound.tool
def wire_money(account: str, amount: float) -> str:
    RAN.append("wire_money")
    return f"wired ${amount:.0f}"


@runbound.tool
def delete_account(user: str) -> str:
    RAN.append("delete_account")
    return f"deleted {user}"


def ask_a_human(call) -> bool:
    """The approval gate. A real one pages someone; this one has a list."""
    return call.name in APPROVED


def policy(on_violation: str) -> ToolPolicy:
    """The rules, stated once. A plain dict of these fields works identically."""
    return ToolPolicy(
        deny=["wire_money"],  # never, by anyone
        max_calls={"send_email": 1},  # one email per session
        constraints={  # small refunds only
            "issue_refund": lambda call: call.kwargs.get("amount", 0) <= 500
        },
        require_approval=["delete_account"],  # a human says yes first
        approval_callback=ask_a_human,
        on_violation=on_violation,
    )


PLAN = [
    (send_email, {"to": "ana@example.com", "body": "your refund is on its way"}),
    (send_email, {"to": "ana@example.com", "body": "...and a quick survey?"}),
    (issue_refund, {"order": "A-1001", "amount": 40}),
    (issue_refund, {"order": "A-1002", "amount": 900}),
    (wire_money, {"account": "ACME-99", "amount": 2500}),
    (delete_account, {"user": KEY}),
]


def attempt(fn, kwargs: dict) -> None:
    """One tool call, with the two trips told apart from each other."""
    shown = ", ".join(f"{k}={v!r}" for k, v in kwargs.items() if k != "body")
    label = f"{fn.__name__}({shown})"
    try:
        print(f"  ok       {label} -> {fn(**kwargs)}")
    except PolicyViolation as exc:  # the agent tried something it may not do
        print(f"  REFUSED  {label}   rule={exc.violation.rule!r}")
        print(f"           {exc}")
    except GuardrailTripped as exc:  # the agent ran away (loop, budget, steps)
        print(f"  STOPPED  {label}   runaway: {exc.anomaly.detector}")
    while NOTICES:
        print(f"           {NOTICES.pop(0)}")


def run_the_agent() -> None:
    """Everything this support agent tries in one session, in order."""
    for fn, kwargs in PLAN:
        attempt(fn, kwargs)


def main() -> int:
    logger = logging.getLogger("runbound")
    logger.setLevel(logging.INFO)
    logger.addHandler(_Notices())
    print("\n" + "=" * 74)
    print("Action policy: the business's own rules, enforced at the tool call")
    print("=" * 74)
    print("  deny wire_money | max 1 send_email | refunds <= $500 | "
          "delete_account needs a human")

    print("\nACT 1  on_violation='dry_run' — how you roll a policy out")
    runbound.init(tool_policy=policy("dry_run"))
    RAN.clear()
    with runbound.session(KEY, tags=TAGS):
        run_the_agent()
    print(f"  nothing was refused; all {len(RAN)} tools ran: {RAN}")
    print("  read those four lines for a week, then flip the switch.")

    print("\nACT 2  on_violation='block' — the same script, refused for real")
    print("  (on_anomaly is still the default 'warn': a policy is the")
    print("   customer's own rule, so it is enforced either way)")
    runbound.init(tool_policy=policy("block"))
    RAN.clear()
    with runbound.session(KEY, tags=TAGS):
        run_the_agent()
        print("\n  the on-call approves the deletion; the agent tries again:")
        APPROVED.add("delete_account")
        attempt(delete_account, {"user": KEY})
        print(f"\n  runbound.tool_calls({KEY!r}) = {runbound.tool_calls(KEY)}")
    print(f"  what actually ran: {RAN}")

    print("\nACT 3  on_violation='block_and_latch' — one denied tool ends the session")
    APPROVED.clear()
    runbound.init(on_anomaly="raise", tool_policy=policy("block_and_latch"))
    with runbound.session(KEY, tags=TAGS):
        attempt(wire_money, {"account": "ACME-99", "amount": 2500})
    latched = runbound.is_tripped(KEY)
    print(f"  is_tripped({KEY!r}).detector = {latched.detector!r}")
    try:
        with runbound.session(KEY, tags=TAGS):
            attempt(send_email, {"to": "ana@example.com", "body": "hello again"})
    except GuardrailTripped as exc:
        print(f"  the next request is refused at the door: {exc}")

    print("\n  deny        wire_money      — an action this agent may never take")
    print("  max_calls   send_email      — one per session; the second is refused")
    print("  constraint  issue_refund    — $40 went out, $900 did not")
    print("  approval    delete_account  — refused until a human said yes")
    print("\n  No model judged any of that. They were the business's own rules,")
    print("  enforced before the action happened — which is the only place a")
    print("  refusal is worth anything once an agent can spend money.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
