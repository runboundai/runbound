# Handling refusals

[← Docs](../README.md)

The first thing a real application meets is a denied model call in the
middle of its own business logic. runbound's answer is deliberate: an
explicit, structured, retry-aware dependency failure — never a fake success,
never a swallowed error, never a business decision made for you, and never
something a generic retry loop turns into the storm this SDK exists to stop.
**Runbound controls execution without taking ownership of business logic.**
What the caller does with a refusal — apologize, queue the job for later,
fall back to a cheaper model, page a human — is always your call.

## Never a fake success

The one temptation to resist is catching the exception and returning
something that *looks* like a normal reply. A refusal is news: the run is
over budget, out of steps, narrowed to a posture that cannot act, or blocked
by your own policy. Papering over it with a made-up "assistant" message
hides that from whoever is downstream — a user who never learns their
request failed, a log that never shows the stop, a caller who retries the
"successful" call forever because nothing told it otherwise. Handle the
refusal; do not disguise it as an answer.

## The typed refusal

`runbound.GuardrailTripped` is the exception every refusal raises.
`runbound.ExecutionRefused` is its public name — the identical class object,
so every handler already written as `except GuardrailTripped:` keeps
working forever, and every new one may spell either name. It is never one of
`openai`'s or `anthropic`'s own exceptions, so a provider's own retry layer,
or a bare `except openai.APIError:`, never mistakes a refusal for its own
kind of failure:

```python
import runbound
from runbound import ExecutionRefused

try:
    response = client.chat.completions.create(model="gpt-4o", messages=messages)
except ExecutionRefused as exc:
    log.warning(
        "runbound refused a call: reason=%s boundary=%s provider_called=%s",
        exc.reason, exc.boundary, exc.provider_called,
    )
    raise
```

Every refusal exposes:

- **`reason`** — a stable code from a closed set (`budget`, `tokens`,
  `steps`, `time`, `posture`, `policy`, `approval`, `circuit`,
  `concurrency`, `blast_radius`, `halt`, `plane`, `loop`, `error_storm`,
  `spike`). Switch on this, not on the message — the message may be
  reworded, the reason never is.
- **`boundary`** — which dimension of the execution envelope was hit
  (`"money"`, `"steps"`, `"posture"`, …), or `None` for a refusal that
  carries no `Decision` at all.
- **`decision`** — the full `Decision` object: `verdict`, `kind`,
  `boundary`, `level`, `evaluation` (whichever of `limit`, `used`,
  `reserved`, `estimate`, `remaining` apply, plus `provider_called`).
- **`provider_called`** — was the provider actually reached before this
  refusal? `False` for every admission (pre-call) refusal; `True` only for a
  budget crossing discovered after the call returned — the call happened,
  it is billed, and its result is withheld.
- **`scope`** — `{"level": ..., "key_hash": str | None}`: which scope decided,
  and a salted hash of the session's key, never the key itself. `level` is one of
  `"session"` (this session's own safe mode, or a limit that is the session's),
  `"process"` (the whole process: `runbound.enter_safe_mode`, a provider's open
  circuit, a capability rule given to `init()`, the in-flight cap), `"fleet"` (a
  posture the control plane states, a Narrow halt, a decision the plane relayed),
  and, for a budget, `"run"` or `"key"` (the per-run or per-key limit that was
  tighter). A refusal because the run is stopped, or because a tool's class is
  denied by a posture, names the scope of the posture that did it.

  The same refusal through the model gateway says the gateway's own scope in
  `x-runbound-level` (`fleet` = the whole application, `key` = one caller, `run`
  = one run). Where the two meet:

  | SDK `level` | What decided | Gateway `level` for the same rule |
  | --- | --- | --- |
  | `session` | one session's own posture or limit | `key` (a session is one key's run of calls) |
  | `key` | the key's own budget | `key` |
  | `run` | the run's own budget | `run` |
  | `process` | this process: `runbound.enter_safe_mode`, a provider's circuit, a capability rule, the in-flight cap | no equivalent: the gateway's circuit is per application, so `fleet` |
  | `fleet` | the control plane: a posture it states, a Narrow or Stop halt, a relayed decision | `fleet` |
- **`refusal`** — the customer-facing status and sentence you configured
  (see [the reactions reference](../reference/reactions.md#what-the-caller-sees--your-words-your-status)),
  unaffected by anything on this page.

See [the reactions reference](../reference/reactions.md#the-refusal-table)
for the full table of every refusal, whether the provider was called, and
whether it is retryable.

## `retryable`: the one question a retry loop actually needs answered

Most refusals cannot be fixed by waiting and asking again — a budget that is
spent stays spent, a policy that denies a tool keeps denying it, a run whose
own shape tripped a loop detector will trip it again on the same shape.
Three reasons are different: `circuit` and `concurrency` are the dependency
itself saying "not right now" (a provider outage, a slot that will free up),
and `plane` is a degraded link to the control plane that may already have
recovered by your next request. `exc.retryable` — and the free-standing
`runbound.is_retryable(exc)`, for a predicate that also works on exceptions
runbound never raised (`False` for those) — answers exactly that question,
and `exc.retry_after` gives a number of seconds where one is known
(`CircuitOpen`'s own decaying snapshot; a heuristic poll interval for
`concurrency`, since an in-flight slot has no real cooldown to report).

```python
import time
import runbound

def call_with_retry(fn, *args, max_attempts=3, **kwargs):
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not runbound.is_retryable(exc) or attempt == max_attempts - 1:
                raise
            time.sleep(getattr(exc, "retry_after", None) or 1.0)
```

A retry loop written this way makes exactly one attempt on a budget
refusal — `is_retryable` says no, and the loop stops immediately. A loop
that skips the predicate entirely and retries on bare `Exception` is *still*
bounded: runbound's own loop detector, watching only the repeated identical
call, latches the session within `loop_threshold` attempts, turning every
attempt after that into an instant, free refusal instead of an unbounded
stream of real ones — the careless case is degraded, never unbounded.

## FastAPI

```python
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from runbound import ExecutionRefused

app = FastAPI()

@app.exception_handler(ExecutionRefused)
def handle_refusal(request, exc: ExecutionRefused):
    r = exc.refusal
    return JSONResponse(
        status_code=r.status,
        headers=r.headers,
        content={**r.body(), "reply": r.message, "retryable": exc.retryable},
    )
```

## Flask

```python
from flask import jsonify
from runbound import ExecutionRefused

@app.errorhandler(ExecutionRefused)
def handle_refusal(exc: ExecutionRefused):
    r = exc.refusal
    resp = jsonify({**r.body(), "reply": r.message, "retryable": exc.retryable})
    resp.status_code = r.status
    resp.headers.update(r.headers)
    return resp
```

## A background job

A queued job has no HTTP caller waiting, so the shape of a good handler is
different: decide whether to re-queue (only when `retryable`), and always
record *why* the job did not run — a job silently dropped, with no trace of
the refusal that stopped it, is exactly the swallowed error this contract
exists to prevent.

```python
from runbound import ExecutionRefused

def run_job(job):
    try:
        process(job)
    except ExecutionRefused as exc:
        job.record_failure(reason=exc.reason, message=str(exc))
        if exc.retryable:
            job.requeue(delay_seconds=exc.retry_after or 30.0)
        else:
            job.mark_failed()  # a human, or a different job, decides next
```

## The streaming case

A refusal raised at the door — before the request goes out — behaves no
differently for a streamed call than for any other: `client.chat.
completions.create(..., stream=True)` raises before it ever returns an
iterator, so the same `except ExecutionRefused:` block that wraps an
ordinary call also wraps a streamed one, with nothing extra to write:

```python
from runbound import ExecutionRefused

def stream_reply(client, messages):
    try:
        stream = client.chat.completions.create(
            model="gpt-4o", messages=messages, stream=True
        )
    except ExecutionRefused as exc:
        yield refusal_event(exc)
        return
    for chunk in stream:
        yield chunk_event(chunk)
```

A refusal discovered *after* streaming has already started — the one case
where `provider_called` is `True` — is different in kind: the call already
happened and part of an answer may already have reached your own code, so
runbound does not (and cannot) retroactively unsend it. Check
`exc.provider_called` if your own handling needs to tell "refused before a
token went out" from "refused after usage was known, with a partial answer
already in hand" apart.
