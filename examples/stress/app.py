#!/usr/bin/env python3
"""A chatbot server, guarded per end-user, for `attack.py` to shoot at.

    .venv/bin/python -m examples.stress.app        # then, elsewhere:
    .venv/bin/python -m examples.stress.attack

One HTTP handler, one model call, one `runbound.session()` block keyed by the
end-user id the request carries. That is the whole integration: a chatbot
wrapper adds two lines and gets per-user budgets, per-user baselines, and a
latch that costs a blocked user zero model calls from their next message on.

FAKE mode (no `OPENAI_API_KEY`) serves every request from `FakeOpenAI` below —
an object shaped like `openai.OpenAI` that sleeps instead of calling anyone.
The sleeps are real, because runbound times calls with the real clock and
this harness may not lie to it: 0.05s for an ordinary reply, 3s for a
"thinking" one. Set `OPENAI_API_KEY` (and `pip install openai`) to run the
same code against the real API for a few cents.

Environment:
    RUNBOUND_MODE            "raise" (default) | "warn"    -> on_anomaly
    RUNBOUND_BUDGET_USD      per-user cap in dollars, default 0.12
    RUNBOUND_ON_SPIKE        "notify" (default) | "trip"
    RUNBOUND_WORKERS         uvicorn worker processes, default 1
    RUNBOUND_REFUSAL_STATUS  the status a stopped session answers with, default 429
    RUNBOUND_REFUSAL_MESSAGE the sentence that goes with it, default the busy
                               line below — this harness has no control plane,
                               so it sets its own local refusal profile the
                               same way any single-process customer would
"""

import logging
import os
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

try:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError:  # the harness is the only thing here with dependencies
    print("This example needs a web server:\n\n    pip install fastapi uvicorn\n")
    sys.exit(1)

import runbound
from runbound import GuardrailTripped

HOST = "127.0.0.1"
PORT = 8008
APP_PATH = "examples.stress.app:app"

MODEL = "gpt-4o"  # a priced model, so FAKE mode still reports real dollars

#: What the fake backend does with a message.
NORMAL_SECONDS = 0.05
NORMAL_OUTPUT_TOKENS = 150
THINKING_MARKER = "[THINK]"
THINKING_SECONDS = 3.0
THINKING_OUTPUT_TOKENS = 4_000
THINKING_REASONING_TOKENS = 3_600
LONG_MESSAGE_CHARS = 2_000  # past this, the "user" is dumping a document
LONG_OUTPUT_TOKENS = 2_000  # ...and gets a long answer back
CHARS_PER_TOKEN = 4  # the usual rough tokenizer estimate
MIN_PROMPT_TOKENS = 16

#: Our own words for a stopped session, and the default status they go with.
#: runbound never writes this sentence — it stops the session and hands
#: control straight back to us; what the end-user reads is always the
#: business's copy, never the SDK's. With no control plane of its own, this
#: harness sets that copy locally via `runbound.init(refusals=...)` below —
#: same mechanism a fleet customer uses, just configured instead of pushed.
DEFAULT_REFUSAL_STATUS = 429
DEFAULT_REFUSAL_MESSAGE = "You've reached today's assistant limit — a human will follow up."


# --- the fake backend -------------------------------------------------------


class _FakeCompletions:
    """`client.chat.completions`, minus the network.

    Sleeps for as long as it claims the call took, because runbound reads
    the real clock: a simulated 3s thinking call has to cost 3 real seconds
    for the spike baseline to see anything.
    """

    def create(self, **kwargs: Any) -> SimpleNamespace:
        prompt = str(kwargs["messages"][-1]["content"])
        seconds, tokens_out, reasoning = _fake_reply_shape(prompt)
        time.sleep(seconds)
        usage = SimpleNamespace(
            prompt_tokens=max(MIN_PROMPT_TOKENS, len(prompt) // CHARS_PER_TOKEN),
            completion_tokens=tokens_out,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        )
        message = SimpleNamespace(content=f"(fake answer, {tokens_out} tokens)")
        return SimpleNamespace(
            model=kwargs.get("model", MODEL),
            usage=usage,
            choices=[SimpleNamespace(message=message)],
        )


def _fake_reply_shape(prompt: str) -> tuple[float, int, int]:
    """`(seconds, output tokens, reasoning tokens)` this prompt would produce.

    Three behaviors worth demonstrating: an ordinary turn, the thinking-mode
    turn a hard question triggers, and the document dump an abuser pastes in
    to use the bot as a free LLM.
    """
    if THINKING_MARKER in prompt:
        return THINKING_SECONDS, THINKING_OUTPUT_TOKENS, THINKING_REASONING_TOKENS
    if len(prompt) > LONG_MESSAGE_CHARS:
        return NORMAL_SECONDS, LONG_OUTPUT_TOKENS, 0
    return NORMAL_SECONDS, NORMAL_OUTPUT_TOKENS, 0


class FakeOpenAI:
    """An OpenAI-shaped client. runbound recognizes shape, never type."""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions())


# --- wiring -----------------------------------------------------------------


class _Notices(logging.Handler):
    """Prints runbound's notices to the server log, where operators look."""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message.startswith("[runbound]"):
            message = f"[runbound] {message}"
        print(message, flush=True)


def _build_client() -> Any:
    """The real OpenAI client when a key is set, else the fake one — wrapped."""
    if os.getenv("OPENAI_API_KEY"):
        try:
            import openai
        except ImportError:
            print("OPENAI_API_KEY is set but `openai` is not installed; "
                  "serving FAKE replies. pip install openai to use the real API.")
        else:
            print(f"REAL mode: calling {MODEL} through the OpenAI API.")
            return runbound.wrap(openai.OpenAI())
    print(f"FAKE mode: no API key, no network — replies are simulated ({MODEL} prices).")
    return runbound.wrap(FakeOpenAI())


def _refusals_profile() -> dict:
    """This harness's local refusal profile, read from the environment.

    No control plane here to push a profile from, so the same status and
    message a customer would `PUT` onto one are instead read from env vars —
    configuration, not code, is still what decides them.
    """
    status = DEFAULT_REFUSAL_STATUS
    raw_status = os.getenv("RUNBOUND_REFUSAL_STATUS")
    if raw_status:
        try:
            status = int(raw_status)
        except ValueError:
            pass
    message = os.getenv("RUNBOUND_REFUSAL_MESSAGE") or DEFAULT_REFUSAL_MESSAGE
    return {"default": {"status": status, "message": message}}


def _configure() -> None:
    """One `init()` per worker process, from the environment."""
    runbound.init(
        on_anomaly=os.getenv("RUNBOUND_MODE", "raise"),
        budget_usd=float(os.getenv("RUNBOUND_BUDGET_USD", "0.12")),
        on_spike=os.getenv("RUNBOUND_ON_SPIKE", "notify"),
        refusals=_refusals_profile(),
    )


def _install_notices() -> None:
    """Print runbound's warnings to stdout, once per process."""
    logger = logging.getLogger("runbound")
    logger.setLevel(logging.WARNING)
    if not any(isinstance(handler, _Notices) for handler in logger.handlers):
        logger.addHandler(_Notices())


_install_notices()
_configure()
CLIENT = _build_client()

app = FastAPI(title="runbound stress harness")

#: Every end-user this worker has served, so /admin/status has a roll to call.
_SEEN: set[str] = set()
_SEEN_LOCK = threading.Lock()


class ChatRequest(BaseModel):
    user_id: str
    message: str


def _seen(user_id: str) -> None:
    with _SEEN_LOCK:
        _SEEN.add(user_id)


def _key(user_id: str) -> str:
    """The session key: ours, opaque to runbound, stable per end-user."""
    return f"user:{user_id}"


def _answer(message: str) -> str:
    """Ask the model and read the reply text out of whatever came back."""
    response = CLIENT.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": message}]
    )
    try:
        return response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return "(no content)"


# --- endpoints --------------------------------------------------------------


@app.post("/chat")
def chat(request: ChatRequest) -> Any:
    """One end-user turn, accounted to that end-user's own session.

    The `session()` block is the entire integration. Under
    `on_anomaly="raise"` a user who already tripped is refused *at the door*,
    before the model call — so the 429 below costs nothing to serve.
    """
    _seen(request.user_id)
    try:
        with runbound.session(_key(request.user_id), tags={"app": "stress"}) as state:
            reply = _answer(request.message)
            spend = state.total_cost_usd if state is not None else 0.0
        return {"reply": reply, "spend_usd": round(spend, 6)}
    except GuardrailTripped as tripped:
        return _blocked_response(tripped)


def _blocked_response(tripped: GuardrailTripped) -> JSONResponse:
    """The response runbound's own refusal profile asks for: our status,
    our sentence, never invented here — see `_refusals_profile()`."""
    refusal = tripped.refusal
    body = {**refusal.body(), "reply": refusal.message}
    return JSONResponse(status_code=refusal.status, headers=refusal.headers, content=body)


@app.get("/admin/status")
def status() -> dict:
    """Who this worker has served, and which of them are latched."""
    with _SEEN_LOCK:
        users = sorted(_SEEN)
    tripped = {}
    for user_id in users:
        anomaly = runbound.is_tripped(_key(user_id))
        if anomaly is not None:
            tripped[user_id] = anomaly.detector
    return {"users": users, "tripped": tripped}


@app.post("/admin/clear/{user_id}")
def clear(user_id: str) -> dict:
    """Explicit forgiveness: fresh budget, fresh baseline, detectors re-armed."""
    runbound.clear(_key(user_id))
    _seen(user_id)
    return {"cleared": user_id}


def main() -> int:
    workers = max(1, int(os.getenv("RUNBOUND_WORKERS", "1")))
    print(f"serving on http://{HOST}:{PORT} with {workers} worker(s)")
    if workers > 1:
        print("NOTE: each worker is its own process with its own runbound "
              "state, so a per-user budget is enforced once per worker.")
    # Several workers means several processes, and uvicorn can only start
    # those from an import string; one worker serves this process's own app.
    target = app if workers == 1 else APP_PATH
    uvicorn.run(target, host=HOST, port=PORT, workers=workers, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
