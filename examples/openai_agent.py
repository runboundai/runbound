#!/usr/bin/env python3
"""A real OpenAI agent with runbound wired in, end to end.

    export OPENAI_API_KEY=sk-...
    .venv/bin/python examples/openai_agent.py

This makes real, billable API calls. The limits below are deliberately small so
the trips are easy to see; raise them for real work.

The agent loop is intentionally naive - it asks the same kind of question over
and over - because the point is what happens when it misbehaves, not how well
it reasons.
"""

import os
import sys

import runbound
from runbound import GuardrailTripped

MODEL = "gpt-4o-mini"
MAX_ITERATIONS = 20
QUESTION = "Name one open question about the Python packaging ecosystem."


@runbound.tool
def lookup(topic: str) -> str:
    """A stand-in for a real tool. Every call is hashed and counted.

    Only the sha256 of ``("lookup", args, kwargs)`` is stored, never ``topic``
    itself, so an agent that keeps looking up the same thing is caught without
    runbound ever holding the argument.
    """
    return f"(no results for {topic})"


def build_client():
    """Import the OpenAI SDK late, so the module stays import-safe without it."""
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("this example needs the OpenAI SDK: pip install openai")
    return runbound.wrap(OpenAI())


def run(client) -> None:
    """One agent run. Raises GuardrailTripped if runbound stops it."""
    messages = [{"role": "user", "content": QUESTION}]
    for iteration in range(1, MAX_ITERATIONS + 1):
        response = client.chat.completions.create(model=MODEL, messages=messages)
        answer = response.choices[0].message.content
        print(f"iteration {iteration}: {answer.strip()[:100]}")

        lookup(topic="python packaging")  # same argument every time: a loop
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": "Now name another one."})


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("set OPENAI_API_KEY to run this example")

    runbound.init(
        budget_usd=0.25,
        max_steps=30,
        tokens_per_minute_limit=200_000,
        on_anomaly="raise",
    )

    client = build_client()
    try:
        run(client)
    except GuardrailTripped as tripped:
        anomaly = tripped.anomaly
        print(f"\nrunbound stopped the agent [{anomaly.detector}]: {anomaly.message}")
        print(f"details: {anomaly.details}")
        shut_down_cleanly()
        return 1

    session = runbound.current_session()
    print(f"\nfinished cleanly: {session.step_count} steps, "
          f"{session.total_tokens} tokens, ${session.total_cost_usd:.4f}")
    return 0


def shut_down_cleanly() -> None:
    """Where you release whatever the agent was holding.

    Close connections, roll back partial work, flush logs. GuardrailTripped is
    an ordinary exception raised on the agent's own thread, so normal
    ``try/finally`` and context managers still run.
    """
    print("released resources; nothing left running")


if __name__ == "__main__":
    raise SystemExit(main())
