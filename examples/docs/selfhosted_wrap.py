"""Guarding your own GPU box the same way as a hosted API. Shown on: Self-hosted."""

import _offline

REQUIRES = ("openai", "httpx")

transport = _offline.patch_openai([_offline.chat_completion(model="llama-3.3-70b")])

# docs: selfhosted-wrap
import openai, runbound

runbound.init(max_inflight_calls=8, max_total_tokens=500_000)
client = runbound.wrap(
    openai.OpenAI(base_url="http://gpu-box:8000/v1", api_key="x")
)
client.chat.completions.create(
    model="llama-3.3-70b", messages=[{"role": "user", "content": "hi"}]
)
# /docs

assert transport.calls == 1
assert runbound.current_session().turns == 1
assert runbound.circuit_state("openai@gpu-box:8000") == "closed"
