"""Many callers behind one service, each stopped on their own. Shown on: Level 2, Runs and keys."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion(content="ok")])

import logging

import runbound
from runbound import GuardrailTripped

log = logging.getLogger("app")
from openai import OpenAI

# One call costs ~$0.00075 (100 in, 50 out at gpt-4o rates); a $0.001 budget
# lets one caller through and trips on their second request.
runbound.init(budget_usd=0.001, on_anomaly="raise")
client = OpenAI()


def call_model(message: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": message}]
    )
    return response.choices[0].message.content


# docs: chatbot-handler
def handle(user_id: str, message: str) -> str:
    with runbound.session(f"user:{user_id}", tags={"plan": "free"}):
        try:
            return call_model(message)
        except GuardrailTripped as tripped:
            log.warning("runbound: %s", tripped.anomaly.message)
            return "You've reached today's assistant limit — a human will follow up."
# /docs

first = handle("alice", "hi")
assert first == "ok"

second = handle("alice", "hi")
assert second == "You've reached today's assistant limit — a human will follow up."

third = handle("bob", "hi")
assert third == "ok", "a different caller must still be served"
