#!/usr/bin/env python3
"""A deliberately runaway agent, caught twice, entirely offline.

No network, no API key, no `openai` package: the "client" below is a plain
object shaped like `openai.OpenAI`, which is all `runbound.wrap()` looks for.

    .venv/bin/python examples/runaway_demo.py

Two scenarios, both the shape of real incidents: an agent repeating one edit
forever, and an agent whose context grows until the bill does.
"""

from types import SimpleNamespace
from typing import Any, Callable

import runbound
from runbound import GuardrailTripped

MODEL = "gpt-4o"  # priced in runbound/pricing.py
COMPLETION_TOKENS = 700
CONTEXT_START = 90_000  # a codebase re-sent on every turn
CONTEXT_GROWTH = 20_000
CONTEXT_MAX = 128_000
SECONDS_PER_ITERATION = 20.0  # one model call plus one tool round trip
UNWATCHED_HOURS = 8.0  # "I started it and went to bed"


class _Completions:
    """Stands in for `client.chat.completions`; the context grows every call."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        prompt = min(CONTEXT_START + CONTEXT_GROWTH * (self.calls - 1), CONTEXT_MAX)
        usage = SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=COMPLETION_TOKENS
        )
        return SimpleNamespace(model=self.model, usage=usage)


def fake_openai_client() -> SimpleNamespace:
    """An object shaped like `openai.OpenAI`. Shape is all runbound needs."""
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(MODEL)))


@runbound.tool
def refactor_file(path: str, instruction: str) -> str:
    print(f"           tool ran: refactor_file({path!r})")
    return "patched"


def _looping_args(iteration: int) -> dict:
    """The runaway: the agent asks for the exact same edit every time."""
    return {"path": "src/app.py", "instruction": "fix the failing test"}


def _distinct_args(iteration: int) -> dict:
    """No repetition here - only the context, and the bill, keep growing."""
    return {"path": f"src/module_{iteration}.py", "instruction": "add type hints"}


def _spent() -> float:
    session = runbound.current_session()
    return session.total_cost_usd if session else 0.0


def _steps() -> int:
    session = runbound.current_session()
    return session.step_count if session else 0


def run_agent(
    client: Any, max_iterations: int, args_for: Callable[[int], dict]
) -> tuple[int, Any]:
    """Drive the agent until it finishes or runbound stops it.

    Returns the iteration it reached and the anomaly that stopped it (``None``
    if it ran to completion, which for a runaway means runbound missed).
    """
    for iteration in range(1, max_iterations + 1):
        try:
            client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": "keep going"}]
            )
            print(f"  iter {iteration:>2}   model call    spend so far ${_spent():.3f}")
            refactor_file(**args_for(iteration))
        except GuardrailTripped as tripped:
            print(f"  iter {iteration:>2}   STOPPED       {tripped}")
            return iteration, tripped.anomaly
    return max_iterations, None


def summarize(iteration: int, anomaly: Any) -> None:
    """Print what tripped, what it cost, and what it would have cost."""
    spent = _spent()
    per_second = spent / (iteration * SECONDS_PER_ITERATION)
    projected = per_second * UNWATCHED_HOURS * 3600
    print()
    print(f"  tripped detector    : {anomaly.detector}")
    print(f"  stopped at          : iteration {iteration} "
          f"(runbound step {_steps()}; one step per recorded event)")
    print(f"  spent when stopped  : ${spent:,.2f}")
    print(f"  same burn rate, {UNWATCHED_HOURS:.0f}h unwatched: ~${projected:,.0f}")
    print(f"  (extrapolation only: {SECONDS_PER_ITERATION:.0f}s per iteration at the "
          "rate measured above)")
    print()


def scenario_loop() -> None:
    print("=" * 72)
    print("SCENARIO 1  the refactoring loop: same tool, same arguments, forever")
    print("=" * 72)
    print("  runbound.init(loop_threshold=3, on_anomaly='raise')")
    runbound.init(loop_threshold=3, on_anomaly="raise")
    client = runbound.wrap(fake_openai_client())
    summarize(*run_agent(client, 25, _looping_args))


def scenario_budget() -> None:
    print("=" * 72)
    print("SCENARIO 2  the spend spiral: every call is new, every call is bigger")
    print("=" * 72)
    print("  runbound.init(budget_usd=2.00, on_anomaly='raise')")
    runbound.init(budget_usd=2.00, on_anomaly="raise")
    client = runbound.wrap(fake_openai_client())
    summarize(*run_agent(client, 25, _distinct_args))


def main() -> int:
    print()
    scenario_loop()
    runbound.reset()  # end the run: counters to zero, detectors re-armed
    scenario_budget()
    print("Both runs were stopped by plain counting and hashing - no LLM, no")
    print("network, no dashboard to watch. Wire it into your agent in 3 lines;")
    print("see README.md.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
