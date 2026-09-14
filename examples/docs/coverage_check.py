"""Checking that a client is actually guarded before trusting the silence. Shown on: Install, Production checklist."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion()])

# docs: coverage-check
import runbound
from openai import OpenAI

runbound.init(budget_usd=5.0)

client = OpenAI()          # built after init(): auto_wrap already guards it
client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

report = runbound.coverage()
runbound.assert_guarded()   # raises RuntimeError if a provider SDK is
                              # imported and no guarded call has been recorded
# /docs

assert report["guarded_calls"] == 1
assert any(label.startswith("openai") for label in report["auto_wrapped"]), report["auto_wrapped"]
assert transport.calls == 1
