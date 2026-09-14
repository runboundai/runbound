"""One session per unit of work, keyed by an id you already have. Shown on: Level 2, Runs and keys."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion()])

# docs: session-per-run
import runbound
from openai import OpenAI

runbound.init()
client = OpenAI()

run_id = "8842"
with runbound.session(f"run:{run_id}", tags={"service": "refunds-agent"}):
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
# /docs

with runbound.session(f"run:{run_id}") as keyed:
    pass
default_session = runbound.current_session()

assert keyed.turns == 1, "the keyed session should have seen one model turn"
assert default_session.turns == 0, "the default session should have seen nothing"
