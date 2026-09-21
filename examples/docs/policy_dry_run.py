"""Rolling a policy out dry: log what it would have blocked, block nothing. Shown on: Level 3, Tools and policy."""

import logging

log_records: list = []


class _ListHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log_records.append(record.getMessage())


logging.getLogger("runbound").addHandler(_ListHandler())
logging.getLogger("runbound").setLevel(logging.WARNING)

import runbound

# docs: policy-dry-run
runbound.init(tool_policy={"deny": ["wire_money"], "on_violation": "dry_run"})

@runbound.tool
def wire_money(account: str, amount: float):
    return "wired"

result = wire_money(account="acct-1", amount=500)
# /docs

assert result == "wired", "dry_run must let the tool body run"
assert any("Policy dry-run: would block tool" in message for message in log_records), log_records
