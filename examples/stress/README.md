# Stress harness — a guarded chatbot, under attack

A real HTTP chatbot (FastAPI) with runbound around its model call, and a
stdlib-only driver that plays four end-users against it: a normal one, an
abuser, a forgiven abuser, and a genuine user asking a genuinely hard
question.

Two files, no fixtures, no mocks of runbound: `attack.py` talks to `app.py`
over the wire and asserts what an operator would see.

```
examples/stress/app.py       the chatbot: one /chat endpoint, one session block
examples/stress/attack.py    the driver: four acts, PASS/FAIL, exit 1 on any FAIL
```

The integration under test is two lines of `app.py`:

```python
with runbound.session(f"user:{user_id}", tags={"app": "stress"}) as state:
    reply = client.chat.completions.create(model="gpt-4o", messages=[...])
```

---

## Run it (FAKE mode — no key, no network, no cost)

Two terminals, from the repo root:

```bash
# terminal 1
pip install fastapi uvicorn
python -m examples.stress.app

# terminal 2
python -m examples.stress.attack
```

The driver prints each act and ends with `4/4 acts passed`. Watch terminal 1
while ACT 4 runs — runbound's watch notices appear there, prefixed
`[runbound]`. The whole run takes about 15 seconds.

Environment knobs on the server:

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUNBOUND_MODE` | `raise` | `on_anomaly` — `raise` stops a session, `warn` only logs |
| `RUNBOUND_BUDGET_USD` | `0.12` | per-end-user cap, the wall ACT 2 hits |
| `RUNBOUND_ON_SPIKE` | `notify` | `notify` watches a behavior change; `trip` stops the session; `limit` climbs the abuse ladder |
| `RUNBOUND_WORKERS` | `1` | uvicorn worker processes (see the limitation below) |

`STRESS_URL` points the driver at a server somewhere else (default
`http://127.0.0.1:8008`).

## Run it for real (a few cents)

```bash
pip install fastapi uvicorn openai
export OPENAI_API_KEY=sk-...
python -m examples.stress.app        # prints "REAL mode"
python -m examples.stress.attack
```

Same code path — `runbound.wrap()` takes the real `openai.OpenAI()` client
instead of the built-in fake. Expect **well under $1**: the driver sends 27
messages, most of them a sentence, and the abuser's eight 6,000-character
dumps are the expensive part (~1,500 input tokens each). The budget wall caps
what any one user can spend at `RUNBOUND_BUDGET_USD`, so the abuser's bill
stops climbing whatever the model does.

Two acts read differently against the real API: gpt-4o answers a 6,000
character dump with a few hundred tokens rather than the fake's 2,000, so
mallory may need the full eight turns to hit the cap (lower
`RUNBOUND_BUDGET_USD` to `0.03` to see it sooner), and grace's "hard
question" only spikes if the model actually takes much longer on it. The
PASS/FAIL lines are tuned for FAKE mode.

---

## What each act proves

**ACT 1 — alice, six ordinary questions.** Six 200s, spend around $0.009.
The point is the absence of anything: a normal user is never touched, and the
guard costs her nothing.

**ACT 2 — mallory pastes a 6,000-character document eight times.** Her spend
climbs $0.024 a turn until it passes the cap, and turn 6 comes back `429`
with `detector: "budget"`. Then she keeps typing: five more messages, all
`429`, all in **~1.5 ms against ~56 ms for a served turn**. That gap is the
product — under `on_anomaly="raise"` runbound refuses a latched session *at
the door*, so a blocked user costs zero model calls from their next message
on. The driver asserts the latched replies are at least twice as fast as a
served one, which they cannot be if a model call happened.

**ACT 3 — support lets her back in.** `/admin/status` lists her as tripped,
`POST /admin/clear/mallory` calls `runbound.clear()`, and her next message
is served with spend restarted at zero. Forgiveness is the business's call,
and it is one call.

**ACT 4 — grace asks five ordinary questions, then two hard ones.** The hard
ones take 60x her session's normal time and 26x its normal output. runbound
notices — the server log shows `Unusual call for session 'user:grace'
(watching)` and then `confirmed, notifying only` — and **she is served
anyway**. That is the default and the deliberate one:

- **spike** = a behavior change. It NOTIFIES. Her question changed, not her
  intent, and one odd model call must never take down a chatbot.
- **budget** = money actually spent. That is the wall, and it is what stopped
  mallory.

To see the other policy, restart the server with
`RUNBOUND_ON_SPIKE=trip` and re-run the driver: grace's second hard
question comes back `429` with `detector: "spike"`. ACT 4 reports FAIL then,
because it asserts the default. Opting in is a decision, not a setting you
trip over.

There is a third policy between those two. Restart the server with
`RUNBOUND_ON_SPIKE=limit` and re-run the driver to watch the **abuse
ladder** instead: `app.py` reads that value straight into
`runbound.init(on_spike=...)`, and `on_trip` is already the `"latch"` the
ladder needs. Grace is then *limited* rather than blocked — the server log
shows `session limited: ... 5 more abnormal calls close this session`, and
both her hard questions are still served, because two spikes do not exhaust
the default `spike_limit_calls=5` allowance. ACT 4 keeps passing. To see the
rest of the ladder — the closed session, the 300-second cooldown, the strikes
and the final block — run `python examples/ladder_demo.py`, which drives one
key all the way down it offline against a fake clock.

Throughout, the 429 body carries `blocked`, `detector`, runbound's
`message`, and a `reply` field holding **our** sentence for the end-user.
runbound stops the session and hands control straight back; it never writes
a word of the bot's copy.

---

## The multi-worker limitation, demonstrated

runbound's state lives in the process. Run the server with two workers and
the per-user budget is enforced once *per worker*:

```bash
RUNBOUND_WORKERS=2 python -m examples.stress.app
python -m examples.stress.attack
```

ACT 2 then FAILs, on purpose, and the printout shows why:

```
turn 3  200  spend $0.0712
turn 4  200  spend $0.0238     <- a different worker, spend starts over
...
turn 8  200  spend $0.1187     <- never blocked; 8/8 dumps served
```

Mallory's requests round-robin between two processes, each with its own
`SessionState` for `user:mallory`, so her effective cap is **2 x
`RUNBOUND_BUDGET_USD`** — N workers, N times the budget. The same is true
of the notices: grace's spike is reported once per worker, because each one
learned its own baseline from its own half of her traffic. The latch is
per-worker too, which is why the 5 follow-up messages in ACT 2 are refused
only by the worker she tripped.

This is the honest boundary of the free SDK: a single process gets a hard
wall, a fleet gets a wall per replica. Shared state across workers is what
the hosted backend is for. Until then, the mitigations are: run the guard in
one process per user-facing service, or set the per-worker budget to
`cap / worker_count`.

---

## Honest caveats

- **FAKE mode's timings are simulated with real sleeps.** runbound reads
  the real clock, so the harness may not lie to it: a "thinking" call sleeps
  3.0 seconds and an ordinary one 0.05. Nothing is patched inside
  runbound — the durations it sees are durations that actually elapsed.
- **FAKE mode's token counts are invented**, and deliberately: ~150 output
  tokens for a normal reply, 2,000 for a document dump, 4,000 (of which
  3,600 reasoning) for a thinking-mode answer, and input tokens estimated at
  four characters per token. Real traffic will not produce these exact
  numbers.
- **Spend figures come from runbound's price table**, not from a bill. They
  are gpt-4o list prices applied to the token counts above, which is exactly
  how runbound prices real calls too — the arithmetic is the same, only the
  inputs are simulated.
- **The default `$0.12` budget is a demo number**, chosen so grace's two
  thinking-mode answers (~$0.088 at gpt-4o rates) fit under the cap while
  mallory's dumps blow through it in six turns. Pick your own.
- **The driver asserts against FAKE-mode behavior.** Under REAL mode treat
  the PASS/FAIL lines as a rough guide and read the numbers instead.
