# Runs keyed by any id

[← Docs](../README.md)

One process usually does more than one thing at a time, and "the process spent
too much" is rarely the useful sentence. `runbound.session(key)` scopes every
control above to whatever id you pass it — an agent run, a background job, a
tenant, a customer — so counters, baselines and trips are per key:

```python
with runbound.session(f"run:{run_id}", tags={"service": "refunds-agent"}):
    response = client.chat.completions.create(model="gpt-4o", messages=messages)
```

## Key and run are two different things

One word, "session", used to mean both, which is why `budget_usd` — a
number stated once, in code, that never resets on its own — read as "per
run" to anyone who had not read the source. It is neither; it is the
**key**'s:

- A **key** is an identity that can live for months: a user, a tenant, a
  customer, a support ticket that reopens. It is whatever string you pass
  to `runbound.session(key)`, and the same key is the same `SessionState`
  every time you pass it again — that is what lets a budget, a learned
  spike baseline or a strike count survive from request to request.
- A **run** is one `runbound.session(key)` block: one agent turn, one job,
  one conversation exchange. It starts the moment you enter the block and
  ends the moment you leave it.

Two independent pairs of controls follow that split:

| | Key (identity) | Run (one block) |
|---|---|---|
| Money | `budget_usd`, optionally windowed by `budget_window` | `run_budget_usd` |
| Tokens | `max_total_tokens` | `run_max_total_tokens` |
| Time | `max_session_lifetime_seconds` | `max_session_seconds` |

`run_budget_usd` and `run_max_total_tokens` reset to fresh, full numbers on
every `session(key)` entry — the same way `max_session_seconds`'s own clock
already does. `budget_usd` and `max_total_tokens` keep accumulating across
every run this key has ever made, exactly as before, *unless* you set
`budget_window`: `"hour"`, `"day"`, `"month"` (a calendar boundary in UTC)
or a plain number of seconds (a rolling window) — the window governs every
cumulative number on the key, resetting them together the moment it rolls
over. Leaving `budget_window` unset is a real, permanent choice for a key
meant to track a lifetime total (a project budget, say); it is also an easy
thing to leave unset by accident on a key meant to represent a person or a
tenant, so a keyed session that has lived past 24 hours with a real
`budget_usd` and no `budget_window` logs one warning, once, and changes
nothing about what runs — set `budget_window` if that is not what you meant.
In memory only: a process restart starts every window fresh, the same as
every other counter this SDK holds.

A call is checked against **both** budgets at once, and refused by
whichever is tighter — `Decision.level` on the refusal says which,
`"run"` or `"key"`, so you always know which number to raise:

```python
runbound.init(
    run_budget_usd=5.0,          # a fresh $5 every session() entry
    budget_usd=20.0,             # this key's own running total
    budget_window="day",         # ...reset at UTC midnight
)

with runbound.session("user:8842"):
    response = client.chat.completions.create(model="gpt-4o", messages=messages)
```

Four runs at $5 each spend the key's daily $20 in full; a fifth run that
same day starts with its own fresh $5 of run budget, but is refused at the
door — `exc.decision.level == "key"` — because the key itself has nothing
left until the next UTC day. `runbound.budget("user:8842")` reads the key's
own numbers (`limit`, `spent`, `remaining`, `window`, `resets_at`); a run's
own remaining is `runbound.envelope()["budget"]["remaining"]`\* inside the
block — the tighter of the two, exactly what the door would check next.

\* `envelope()`'s `budget` object reports the *effective* remaining (the
smaller of the run's and the key's), not the run's own number in isolation
— read `exc.decision.evaluation["limit"]`/`["remaining"]` on an actual
refusal for the specific number that bound it.

- **The key is yours and opaque to us.** runbound stores it, names it in
  its own anomaly messages, and never interprets it. Send a run id, a job id, a tenant id, a
  customer id, a hash — identity stays the business's. `tags` is a small dict
  of labels that rides along with it.
- **The same key is the same session.** Re-entering a key in a later request
  returns the state it already had, so budgets and learned baselines survive
  from request to request — a retried job, or a second turn of the same
  conversation. Tags given on a later entry are merged in.
- **Everything is per key**: step count, tokens, dollars, the loop window, and
  the spike baseline.
- **A trip stops that session only.** The key that spikes gets a
  `GuardrailTripped` on the call that crossed the line; every other run carries
  on, because detectors fire per session and the other sessions never tripped.
- **A tripped session stays tripped.** The wall is a latch, not a speed bump:
  once a session trips critically, every later call re-raises (or re-invokes
  your callback), and under `on_anomaly="raise"` even *entering*
  `session(key)` raises — so a blocked key costs you **zero** model calls
  from its next request on. Check without tripping via
  `runbound.is_tripped(key)`; forgive explicitly with
  `runbound.clear(key)` (fresh budget, fresh baseline, detectors re-armed).
- **The latch is permanent by default — and that is a choice you can change.**
  Out of the box a tripped session stays tripped until you `clear()` it or the
  process restarts. If a false-positive trip should heal on its own, **opt
  in** with `latch_ttl_seconds=3600`: the latch lifts that long after it was
  set, every detector is re-armed, and the session's next event is judged
  fresh — on the same cumulative counters. Re-admits, does not reset: a
  session still over budget re-trips immediately, with the same detector,
  which is the wall the setting promises. It is **not** a windowed budget
  that zeroes on a schedule — that is a different, unbuilt feature — and
  nothing expires unless you set this. `runbound.clear(key)` is the one call
  that actually zeroes the counters.
- **Key and tags reach the anomaly.** A spike anomaly carries them in
  `anomaly.details["key"]` / `["tags"]`, so your own handler and your own
  logs have them. What leaves the process is different: the key travels to
  the control plane as a **hash**, never in the clear, and the plane is what
  turns an anomaly into a Slack message or a page. This SDK has sent no
  alert of its own.
- Blocks nest, and the enclosing session is restored on exit. Outside any
  block, work is accounted to the default session `init()` created — nothing
  changes for a single-agent process.
- At most `max_sessions` keys are kept (default 10,000, least-recently-used
  evicted); re-entering an evicted key simply starts it over.
- **Inert before `init()`.** The block yields `None` and observes nothing,
  exactly like the rest of the SDK.

**The second example: a chatbot.** The same call, keyed by the person rather
than by the run — one process serving many people, each with their own budget,
baseline and latch, so the one who spikes is stopped and everybody else keeps
chatting. runbound never writes the reply: it stops the session and hands
control straight back to you.

```python
from runbound import GuardrailTripped

def handle(user_id: str, message: str) -> str:
    with runbound.session(f"user:{user_id}", tags={"plan": "free"}):
        try:
            return call_model(message)
        except GuardrailTripped as tripped:
            log.warning("runbound: %s", tripped.anomaly.message)
            return "You've reached today's assistant limit — a human will follow up."
```

`examples/chatbot_abuse_demo.py` runs that story offline: three people on their
own keyed sessions, one of whom pushes the model into thinking mode, with
`on_anomaly="raise"` as the only configuration — a quiet notice on the first
odd call, a stop on the second, and the other two left alone. For a dress
rehearsal over HTTP — a FastAPI service, a normal caller, a repeat-offender key
that gets latched and refused pre-spend, a support `clear()`, and a heavy but
genuine caller who is watched and never blocked — see
[`examples/stress/`](../../examples/stress/README.md). It runs with no API key and
also against a real one, and shows the multi-worker limitation honestly.

**Async tasks inherit the session; new threads do not.** The binding is a
`contextvars.ContextVar`, so an `asyncio` task copies the context it was created
in, while a thread starts with an empty one. Enter the block inside the worker:

```python
# asyncio: tasks created inside the block see it
with runbound.session(key):
    await asyncio.gather(step_one(), step_two())

# threads: the block goes inside the thread's own function
def worker(key: str) -> None:
    with runbound.session(key):
        do_work()
```

---
