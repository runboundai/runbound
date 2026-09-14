"""Telling a policy refusal apart from a runaway agent. Shown on: Level 3, Tools and policy."""

import runbound
from runbound import GuardrailTripped, PolicyViolation

runbound.init(on_anomaly="raise")

@runbound.tool(blocked=True)
def send_email(to: str):
    return "sent"


def run_agent() -> None:
    send_email(to="user@example.com")


policy_log: list = []
guardrail_log: list = []


class _Log:
    def warning(self, *args) -> None:
        policy_log.append(args)


log = _Log()


def ask_the_user_what_to_do() -> None:
    pass


def shut_down_cleanly(anomaly) -> None:
    guardrail_log.append(anomaly)


# docs: catch-policy-violation
try:
    run_agent()
except PolicyViolation as exc:
    # exc.violation.rule is "deny" | "allow" | "max_calls" | "constraint" | "approval"
    log.warning("refused %s (%s): %s", exc.violation.tool, exc.violation.rule, exc)
    ask_the_user_what_to_do()
except GuardrailTripped as exc:
    shut_down_cleanly(exc.anomaly)
# /docs

assert len(policy_log) == 1, policy_log
assert policy_log[0][2] == "deny"
assert guardrail_log == [], "a policy refusal must never be caught as a plain GuardrailTripped"
