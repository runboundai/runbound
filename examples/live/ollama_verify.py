#!/usr/bin/env python3
"""Thirteen runbound scenarios against a REAL local model, for free.

    ollama serve
    ollama pull qwen2.5:1.5b
    .venv/bin/pip install openai
    .venv/bin/python examples/live/ollama_verify.py

Every other demo in this repo hands runbound numbers it invented. This one
does not: a real ``openai.OpenAI`` client points at Ollama's OpenAI-compatible
endpoint, a real 1.5B model answers, and every token count, duration and
model-requested tool call runbound reacts to was measured off the wire. The
model is local, so the whole run costs nothing and takes about two minutes.

Each scenario re-runs ``runbound.init()`` — which reconfigures the SDK and
starts a fresh session — describes what it is about to prove, and reports one
of three results:

    PASS   the guard did the deterministic thing it promises
    INFO   it ran, but the outcome depends on the model's own behavior
    FAIL   the guard did not do what it promises  (exit code 1)

A scenario that raises is a FAIL and never stops the run. The scorecard is
printed at the end and written to ``examples/live/last_report.md``.
"""

import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    import openai
except ImportError:  # the one dependency this harness has
    print(
        "This harness needs the real openai SDK (runbound itself needs nothing):\n"
        "    .venv/bin/pip install openai",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

try:  # only used to work around an SDK default that fails on some machines
    import httpx
except ImportError:  # pragma: no cover - the default client is fine without it
    httpx = None

import runbound
from runbound import GuardrailTripped, PolicyViolation
from runbound.exceptions import CircuitOpen

BASE_URL = os.environ.get("RUNBOUND_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("RUNBOUND_MODEL", "qwen2.5:1.5b")
API_KEY = os.environ.get("RUNBOUND_API_KEY", "ollama")

#: A port nothing listens on: the provider that is "down" in scenario 7.
DEAD_URL = "http://localhost:9/v1"

def endpoint_label(base_url: str, shape: str = "openai") -> str:
    """The circuit label runbound keys a client at ``base_url`` under.

    Worked out here from the URL alone — netloc, lowercased — so the scenario
    checks runbound's label against the endpoint rather than against
    runbound's own idea of it.
    """
    return f"{shape}@{urllib.parse.urlsplit(base_url).netloc.lower()}"


REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_report.md")

#: Short, fixed prompts. A 1.5B model on a laptop is the budget here.
TINY = "Reply with exactly: OK"
TINY_TOKENS = 5

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"


# --- the harness ------------------------------------------------------------


@dataclass
class Result:
    """One scenario's verdict, and the line that justifies it."""

    name: str
    proves: str
    outcome: str = FAIL
    evidence: str = "did not run"
    notices: list = field(default_factory=list)


class Notices(logging.Handler):
    """Collects runbound's own log lines so a scenario can assert on them."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def clear(self) -> None:
        self.lines.clear()

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]


NOTICES = Notices()


def install_log_capture() -> None:
    """Route runbound's logger into :data:`NOTICES` and nowhere else."""
    log = logging.getLogger("runbound")
    log.handlers.clear()
    log.addHandler(NOTICES)
    log.setLevel(logging.INFO)
    log.propagate = False


_FALLBACK_REPORTED = False


def build_client(base_url: str, timeout: float, max_retries: int = 0):
    """A real ``openai.OpenAI`` pointed at ``base_url``.

    openai 3.x builds its default HTTP client on httpx2, whose macOS trust
    store shim fails outright on some systems. That is nothing to do with
    runbound, so rather than being unrunnable there the harness falls back to
    an explicit httpx client — the SDK's own documented escape hatch — and says
    so once.
    """
    global _FALLBACK_REPORTED
    kwargs = {
        "api_key": API_KEY,
        "base_url": base_url,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    try:
        return openai.OpenAI(**kwargs)
    except Exception as exc:
        if httpx is None:
            raise
        if not _FALLBACK_REPORTED:
            _FALLBACK_REPORTED = True
            print(
                f"  note: the openai SDK's default HTTP client would not build "
                f"({type(exc).__name__}); using an explicit httpx client instead"
            )
        return openai.OpenAI(http_client=httpx.Client(timeout=timeout), **kwargs)


def guarded(base_url: str = BASE_URL, timeout: float = 180.0, max_retries: int = 0):
    """A wrapped client. Wrapping patches in place and returns the same object."""
    return runbound.wrap(build_client(base_url, timeout, max_retries))


def ask(client, prompt: str, max_tokens: int, **kwargs):
    """One chat completion. Deterministic where the model allows it."""
    return client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0,
        **kwargs,
    )


def tokens_used() -> int:
    """Total tokens on the session work is being accounted to right now."""
    state = runbound.current_session()
    return 0 if state is None else state.total_tokens


def run(result: Result, scenario) -> Result:
    """Run one scenario, turning anything it raises into a FAIL."""
    NOTICES.clear()
    print(f"\n{result.name}")
    print(f"  {result.proves}")
    started = time.monotonic()
    try:
        scenario(result)
    except Exception as exc:
        result.outcome = FAIL
        result.evidence = f"raised {type(exc).__name__}: {exc}"
        traceback.print_exc(limit=3)
    result.notices = list(NOTICES.lines)
    print(f"  {result.outcome}  {result.evidence}  [{time.monotonic() - started:.1f}s]")
    return result


# --- the scenarios ----------------------------------------------------------


def token_budget(result: Result) -> None:
    """A session-wide token budget stops the run once it is spent."""
    runbound.init(max_total_tokens=300, on_anomaly="raise")
    client = guarded()
    attempts = 15  # ~36 tokens a call, so the wall lands well inside this
    for call in range(1, attempts + 1):
        try:
            ask(client, TINY, TINY_TOKENS)
        except GuardrailTripped as tripped:
            total = tripped.anomaly.details.get("total_tokens")
            result.evidence = (
                f"call {call} refused: detector={tripped.anomaly.detector!r}, "
                f"{total} tokens used against a limit of 300"
            )
            good = tripped.anomaly.detector == "budget" and total > 300
            result.outcome = PASS if good else FAIL
            return
    result.evidence = f"{attempts} calls made, {tokens_used()} tokens, never tripped"
    result.outcome = FAIL


def per_call_output_cap(result: Result) -> None:
    """A per-call output cap needs no baseline: one call over it is enough."""
    runbound.init(max_tokens_out_per_call=40, on_anomaly="raise")
    client = guarded()
    try:
        response = ask(client, "Write a 200 word story about a lighthouse.", 200)
    except GuardrailTripped as tripped:
        details = tripped.anomaly.details
        result.evidence = (
            f"detector={tripped.anomaly.detector!r}, cap={details.get('cap')}, "
            f"{details.get('value'):.0f} output tokens on one call"
        )
        result.outcome = (
            PASS
            if tripped.anomaly.detector == "spike" and details.get("cap") == 40.0
            else FAIL
        )
        return
    result.evidence = f"not tripped; the model produced {response.usage.completion_tokens} tokens"
    result.outcome = FAIL


def spike_watch(result: Result) -> None:
    """A behavior change is watched and reported — and served anyway."""
    runbound.init(on_anomaly="raise", spike_warmup_calls=4)
    client = guarded()
    normal = []
    for _ in range(6):
        started = time.monotonic()
        ask(client, TINY, TINY_TOKENS)
        normal.append(time.monotonic() - started)

    started = time.monotonic()
    response = ask(client, "Write 300 words about the sea.", 400)
    odd = time.monotonic() - started

    watching = NOTICES.matching("(watching)")
    measured = (
        f"normal call {min(normal):.2f}-{max(normal):.2f}s, "
        f"the odd one {odd:.1f}s / {response.usage.completion_tokens} output tokens"
    )
    if watching:
        notice = watching[0].replace("[runbound] ", "")
        result.outcome = PASS
        result.evidence = f"{measured}; runbound logged {notice!r}, and served it"
        return
    result.outcome = INFO
    result.evidence = f"{measured}; no watch notice — the odd call was not odd enough"


#: The nudge that makes a small model keep asking for the same tool. Without
#: it, qwen2.5:1.5b gives up after one "unknown" and answers in prose.
RETRY_NUDGE = "The tool returned unknown. Call get_weather for Paris again."

WEATHER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather in a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def tool_conversation() -> list:
    """The opening messages of a weather agent that is about to loop."""
    return [{"role": "user", "content": "Weather in Paris? Use get_weather."}]


def assistant_turn(calls) -> dict:
    """The assistant message that carries the tool calls back to the model."""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in calls
        ],
    }


def drive_tool_loop(client, dispatch, rounds: int = 8) -> tuple[int, list]:
    """Let the model drive a function-calling loop; return where it stopped.

    ``dispatch(name, arguments)`` is the agent's own tool call — decorated or
    not — and its return value goes back to the model as the tool result.
    Returns ``(rounds_completed, requests)``; a :class:`GuardrailTripped`
    raised by runbound propagates, which is the outcome being tested.
    """
    messages = tool_conversation()
    requests = []
    for completed in range(rounds):
        response = client.chat.completions.create(
            model=MODEL, messages=messages, tools=WEATHER_TOOLS, max_tokens=80, temperature=0
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            return completed, requests
        messages.append(assistant_turn(calls))
        for call in calls:
            requests.append((call.function.name, call.function.arguments))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": dispatch(call.function.name, call.function.arguments),
                }
            )
        messages.append({"role": "user", "content": RETRY_NUDGE})
    return rounds, requests


def decorated_tool_loop(result: Result) -> None:
    """A model looping on one tool, with the agent's tool decorated."""
    runbound.init(on_anomaly="raise")
    client = guarded()

    @runbound.tool
    def get_weather(city: str) -> str:
        return "unknown, call again"

    def dispatch(name, arguments):
        return get_weather(**json.loads(arguments))

    try:
        rounds, requests = drive_tool_loop(client, dispatch)
    except GuardrailTripped as tripped:
        executed = runbound.tool_calls().get("get_weather", 0)
        result.evidence = (
            f"detector={tripped.anomaly.detector!r} after "
            f"{tripped.anomaly.details.get('count')} identical requests; "
            f"the decorated tool ran {executed}x and never a third time"
        )
        result.outcome = PASS if tripped.anomaly.detector == "loop" else FAIL
        return
    result.outcome = INFO
    result.evidence = (
        f"the model stopped asking after {rounds} rounds ({len(requests)} requests); "
        "a 1.5B model does not always loop"
    )


def undecorated_tool_loop(result: Result) -> None:
    """The same loop with nothing decorated: the wrapper sees the request."""
    runbound.init(on_anomaly="raise")
    client = guarded()
    executed = []

    def dispatch(name, arguments):
        executed.append((name, arguments))  # dispatched by hand, runbound-blind
        return "unknown, call again"

    try:
        rounds, requests = drive_tool_loop(client, dispatch)
    except GuardrailTripped as tripped:
        result.evidence = (
            f"detector={tripped.anomaly.detector!r}, message "
            f"{tripped.anomaly.message[:70]!r}; the agent had dispatched "
            f"{len(executed)} of them by hand"
        )
        result.outcome = (
            PASS
            if tripped.anomaly.detector == "loop"
            and "model requested" in tripped.anomaly.message
            else FAIL
        )
        return
    result.outcome = INFO
    result.evidence = f"the model stopped asking after {rounds} rounds ({len(requests)} requests)"


def policy_deny(result: Result) -> None:
    """A denied action is refused before the function body runs."""
    runbound.init(tool_policy={"deny": ["delete_account"]}, on_anomaly="raise")
    ran = []

    @runbound.tool
    def delete_account(user: str) -> str:
        ran.append(user)
        return "deleted"

    try:
        delete_account("user:1")
    except PolicyViolation as refused:
        result.evidence = (
            f"rule={refused.violation.rule!r}, tool={refused.violation.tool!r}, "
            f"the body ran {len(ran)} times"
        )
        result.outcome = PASS if refused.violation.rule == "deny" and not ran else FAIL
        return
    result.evidence = f"not refused; the body ran {len(ran)} times"
    result.outcome = FAIL


def error_storm_circuit(result: Result) -> None:
    """A provider that is down stops being called at all."""
    runbound.init(
        on_provider_failure="open", circuit_failure_threshold=3, error_storm_limit=None
    )
    client = guarded(base_url=DEAD_URL, timeout=1.0)
    failures = 0
    for _ in range(3):
        try:
            ask(client, TINY, TINY_TOKENS)
        except openai.APIConnectionError:
            failures += 1

    state = runbound.circuit_state("openai")
    try:
        ask(client, TINY, TINY_TOKENS)
    except CircuitOpen as refused:
        result.evidence = (
            f"{failures} connection errors opened the circuit (state {state!r}); "
            f"the 4th call was refused for provider {refused.provider!r} "
            "without a request going out"
        )
        result.outcome = PASS if failures == 3 and state == "open" else FAIL
        return
    except openai.APIConnectionError:
        result.evidence = f"the 4th call still went out; circuit state {state!r}"
        result.outcome = FAIL
        return
    result.evidence = "the 4th call to a dead port somehow succeeded"
    result.outcome = FAIL


def wall_clock_timeout(result: Result) -> None:
    """A session that has been running too long is stopped, whatever it is doing."""
    runbound.init(max_session_seconds=0.5, on_anomaly="raise")
    client = guarded()
    try:
        with runbound.session("timeout-demo"):
            ask(client, TINY, TINY_TOKENS)
            time.sleep(0.7)
            ask(client, TINY, TINY_TOKENS)
    except GuardrailTripped as tripped:
        details = tripped.anomaly.details
        result.evidence = (
            f"detector={tripped.anomaly.detector!r} after "
            f"{details.get('elapsed_s', 0.0):.1f}s, limit {details.get('limit')}s"
        )
        result.outcome = PASS if tripped.anomaly.detector == "timeout" else FAIL
        return
    result.evidence = "the session ran past its wall clock and was not stopped"
    result.outcome = FAIL


def fanout_depth(result: Result) -> None:
    """Sessions nested deeper than the customer allowed are refused at the door."""
    runbound.init(max_session_depth=1)
    try:
        with runbound.session("agent:a"):
            with runbound.session("agent:b"):
                with runbound.session("agent:c"):
                    pass
    except GuardrailTripped as tripped:
        details = tripped.anomaly.details
        result.evidence = (
            f"detector={tripped.anomaly.detector!r}, rule={details.get('rule')!r}, "
            f"depth {details.get('count')} against a limit of {details.get('limit')}"
        )
        result.outcome = (
            PASS
            if tripped.anomaly.detector == "fanout" and details.get("rule") == "depth"
            else FAIL
        )
        return
    result.evidence = "three levels of nesting were allowed under max_session_depth=1"
    result.outcome = FAIL


ABUSER = "user:abuser"
LADDER_PROMPT = "Write 250 words about mountains."


def abuse_ladder(result: Result) -> None:
    """One end-user pushed down the ladder: notice, limit, closed session."""
    runbound.init(
        on_spike="limit",
        on_anomaly="raise",
        spike_warmup_calls=4,
        spike_confirm=2,
        spike_limit_calls=2,
        spike_min_output_tokens=60,
        spike_cooldown_seconds=5,
    )
    client = guarded()
    trail = []

    def turn(prompt: str, max_tokens: int) -> None:
        with runbound.session(ABUSER, tags={"app": "ollama-verify"}):
            ask(client, prompt, max_tokens)

    try:
        for _ in range(5):
            turn(TINY, TINY_TOKENS)
            trail.append(_rung())
        for _ in range(8):
            turn(LADDER_PROMPT, 300)
            trail.append(_rung())
    except GuardrailTripped as tripped:
        details = tripped.anomaly.details
        trail.append(_rung())
        result.evidence = (
            f"action={details.get('action')!r} at level {details.get('level')}, "
            f"strike {details.get('strikes')}; rungs {'>'.join(trail)}"
        )
        result.outcome = PASS if details.get("action") == "rollover" else INFO
        return
    result.outcome = INFO
    result.evidence = f"never closed; rungs {'>'.join(trail)}"


def _rung() -> str:
    """This key's place on the ladder: level, allowance left, strikes.

    ``a-`` is an allowance of ``None`` — a session below the limit has one to
    spend but has not been given a number yet.
    """
    status = runbound.session_status(ABUSER)
    if status is None:
        return "?"
    allowance = status["allowance_left"]
    return f"L{status['level']}a{'-' if allowance is None else allowance}s{status['strikes']}"


def gpu_cost_budget(result: Result) -> None:
    """Priced GPU time makes budget_usd work on a model nobody sells."""
    runbound.init(
        budget_usd=0.3,
        on_anomaly="raise",
        # $1000 per 1M tokens = $1 per 1k: what a rented GPU-hour costs,
        # divided by the tokens it produces. Absurd here on purpose, so the
        # wall is reached in a handful of calls.
        custom_prices={MODEL: (1000.0, 1000.0)},
    )
    client = guarded()
    attempts = 15
    for call in range(1, attempts + 1):
        try:
            ask(client, TINY, TINY_TOKENS)
        except GuardrailTripped as tripped:
            details = tripped.anomaly.details
            spent = details.get("total_cost_usd", 0.0)
            result.evidence = (
                f"call {call} refused: detector={tripped.anomaly.detector!r}, "
                f"${spent:.3f} of GPU time against a budget of $0.30 "
                f"({details.get('limit_hit')})"
            )
            result.outcome = (
                PASS
                if tripped.anomaly.detector == "budget"
                and details.get("limit_hit") == "budget_usd"
                else FAIL
            )
            return
    state = runbound.current_session()
    result.evidence = (
        f"{attempts} calls, ${state.total_cost_usd:.3f} priced, never tripped"
    )
    result.outcome = FAIL


def inflight_cap(result: Result) -> None:
    """One GPU serves one call: the second is refused while the first streams."""
    runbound.init(max_inflight_calls=1)
    client = guarded()
    streaming = threading.Event()
    proceed = threading.Event()
    chunks = []
    failure = []

    def stream_slowly() -> None:
        """Hold one slot open: start a stream, then wait mid-flight."""
        try:
            stream = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": "Write 150 words about rain."}],
                max_tokens=200,
                temperature=0,
                stream=True,
            )
            for chunk in stream:
                chunks.append(chunk)
                if not streaming.is_set():
                    streaming.set()
                    proceed.wait(30)
        except Exception as exc:  # reported by the scenario, never raised here
            failure.append(exc)
        finally:
            streaming.set()

    worker = threading.Thread(target=stream_slowly, daemon=True)
    worker.start()
    try:
        if not streaming.wait(60) or failure:
            result.evidence = f"the streaming call never started ({failure})"
            result.outcome = FAIL
            return
        running = runbound.inflight_calls("openai")
        try:
            ask(client, TINY, TINY_TOKENS)
        except GuardrailTripped as refused:
            details = refused.anomaly.details
            result.evidence = (
                f"detector={refused.anomaly.detector!r}, provider "
                f"{details.get('provider')!r}: {details.get('count')} call in "
                f"flight against a limit of {details.get('limit')}, "
                f"{len(chunks)} chunks streamed so far"
            )
            result.outcome = (
                PASS
                if refused.anomaly.detector == "inflight" and running == 1
                else FAIL
            )
            return
        result.evidence = (
            f"the second call went out with {running} already in flight"
        )
        result.outcome = FAIL
    finally:
        proceed.set()
        worker.join(60)


def endpoint_circuits(result: Result) -> None:
    """A dead box opens its own circuit and no one else's."""
    runbound.init(
        on_provider_failure="open", circuit_failure_threshold=2, error_storm_limit=None
    )
    dead_label = endpoint_label(DEAD_URL)
    live_label = endpoint_label(BASE_URL)
    dead = guarded(base_url=DEAD_URL, timeout=1.0)
    live = guarded()

    failures = 0
    for _ in range(2):
        try:
            ask(dead, TINY, TINY_TOKENS)
        except openai.APIConnectionError:
            failures += 1

    dead_state = runbound.circuit_state(dead_label)
    live_state = runbound.circuit_state(live_label)
    shape_state = runbound.circuit_state("openai")
    try:
        served = ask(live, TINY, TINY_TOKENS).choices[0].message.content
    except CircuitOpen:
        result.evidence = (
            f"the healthy endpoint was refused: {dead_label} is {dead_state!r} "
            f"and took {live_label} with it"
        )
        result.outcome = FAIL
        return

    result.evidence = (
        f"{failures} failures put {dead_label} in {dead_state!r} while "
        f"{live_label} stayed {live_state!r} and answered {served.strip()[:12]!r} "
        f"(the 'openai' shape reads {shape_state!r}: the worst of the two)"
    )
    result.outcome = (
        PASS
        if dead_state == "open" and live_state == "closed" and shape_state == "open"
        else FAIL
    )


SCENARIOS = [
    (
        "1. token budget",
        "max_total_tokens=300 stops the session once the tokens are spent",
        token_budget,
    ),
    (
        "2. per-call output cap",
        "max_tokens_out_per_call=40 stops one oversized answer, with no baseline",
        per_call_output_cap,
    ),
    (
        "3. spike watch",
        "a call unlike this session's others is reported — and still served",
        spike_watch,
    ),
    (
        "4. tool loop, decorated",
        "the model loops on @runbound.tool get_weather and is stopped",
        decorated_tool_loop,
    ),
    (
        "5. tool loop, undecorated",
        "the same loop caught from the model's requests, with nothing decorated",
        undecorated_tool_loop,
    ),
    (
        "6. policy deny",
        "a denied tool is refused before its body runs",
        policy_deny,
    ),
    (
        "7. error storm + circuit",
        "a dead provider's circuit opens and the next call never goes out",
        error_storm_circuit,
    ),
    (
        "8. wall-clock timeout",
        "max_session_seconds=0.5 stops a session that has run too long",
        wall_clock_timeout,
    ),
    (
        "9. fan-out depth",
        "max_session_depth=1 refuses the third nested session at the door",
        fanout_depth,
    ),
    (
        "10. abuse ladder",
        'on_spike="limit" walks one key from watching to a closed session',
        abuse_ladder,
    ),
    (
        "11. GPU cost budget",
        "custom_prices for your own hardware makes budget_usd work locally",
        gpu_cost_budget,
    ),
    (
        "12. in-flight cap",
        "max_inflight_calls=1 refuses a second call while the first streams",
        inflight_cap,
    ),
    (
        "13. per-endpoint circuits",
        "a dead endpoint's circuit opens without touching the healthy one's",
        endpoint_circuits,
    ),
]


# --- reporting --------------------------------------------------------------


def scorecard(results: list, seconds: float) -> str:
    """The run as a markdown table, printed and written to disk."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "# runbound live verification",
        "",
        f"- model: `{MODEL}`",
        f"- endpoint: `{BASE_URL}`",
        f"- runbound: `{runbound.__version__}`",
        f"- run at: {stamp}",
        f"- total runtime: {seconds:.1f}s",
        "",
        "| Scenario | Result | Evidence |",
        "| --- | --- | --- |",
    ]
    for result in results:
        evidence = result.evidence.replace("|", "\\|")
        lines.append(f"| {result.name} | {result.outcome} | {evidence} |")
    counts = {name: sum(1 for r in results if r.outcome == name) for name in (PASS, INFO, FAIL)}
    lines += [
        "",
        f"**{counts[PASS]} PASS, {counts[INFO]} INFO, {counts[FAIL]} FAIL** "
        f"of {len(results)} scenarios.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    install_log_capture()
    print("runbound live verification")
    print(f"  model     {MODEL}")
    print(f"  endpoint  {BASE_URL}")
    started = time.monotonic()
    results = [
        run(Result(name, proves), scenario) for name, proves, scenario in SCENARIOS
    ]
    seconds = time.monotonic() - started

    report = scorecard(results, seconds)
    print("\n" + report)
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        handle.write(report)
    print(f"written to {REPORT_PATH}")
    return 1 if any(result.outcome == FAIL for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
