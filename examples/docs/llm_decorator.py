"""Recording in-process inference runbound cannot see any other way. Shown on: Self-hosted."""

import runbound

runbound.init()

def call_local_model(prompt: str) -> str:
    return "answer: " + prompt

# docs: llm-decorator
@runbound.llm(model="llama-3.1-8b", provider="llama.cpp@local",
              tokens=lambda out: (len(out), len(out)))
def generate(prompt: str) -> str:
    return call_local_model(prompt)
# /docs

result = generate("hi")
assert result == "answer: hi"

session = runbound.current_session()
with session.lock:
    total_tokens = session.total_tokens
assert total_tokens == len(result) * 2, total_tokens


@runbound.llm(model="llama-3.1-8b", provider="llama.cpp@local")
def broken(prompt: str) -> str:
    raise ValueError("boom")


raised = None
try:
    broken("hi")
except ValueError as exc:
    raised = exc

assert raised is not None, "an exception from the local model must reach the caller untouched"
