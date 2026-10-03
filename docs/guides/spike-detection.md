# Progressive degradation

[← Docs](../README.md)

This is not a headline "abuse score" — runbound never scores a session, and
never reads what it said: the detector sees durations and token counts only. It is a behavioral watch: does this session's
model calls still look like themselves? The spike detector below is the
free, local, on-by-default version of that question, and
[the abuse ladder](#many-callers-behind-one-service-the-abuse-ladder)
(`on_spike="limit"`) is
what answers it by degrees — notify, then narrow, then close — rather than
slamming a door on the first thing that looks different. The ladder is the
**automatic driver of postures**: a confirmed spike is the one thing in this
SDK that moves a session's posture on its own, with no policy write and no
operator in the loop, because "this session stopped behaving like itself" is
the one signal that has to react before anyone can read a dashboard.

## Spike detection (free and local)

Every other detector needs a number from you; this one learns one instead —
zero-config: `runbound.init()` with no arguments already watches every
model call (`spike_detection` defaults `True`). A connected plane's own
Controls can only tighten what you configured here (a stricter mode, a
longer cooldown, a lower confirmation count — never the reverse), or state
one from scratch where you leave a knob at its default. Everything below
describes what runs locally, with no account at all; see
[Free SDK, connected plane](../concepts/free-and-connected.md) for what a
connected plane additionally coordinates.

It keeps a sliding window of that session's last `spike_window` (50) model
calls. Once there are `spike_warmup_calls` (4) of them, the **median** duration
and the median output work become "this session's normal" — a median, so one bad
call in the history cannot drag the baseline up with it. Output work is the
provider's **completion-token count, which already includes reasoning
(thinking) tokens**, so a model that quietly starts thinking is measured on
what it actually generated. The reasoning share is carried alongside in the
event so you can see the split; it is never added on top, which would make a
cap fire at half the number you set.

A call is abnormal when it exceeds `spike_factor` (10) times that median **and**
rises above it by an absolute floor — `spike_min_duration_s` (2s) for duration,
`spike_min_output_tokens` (500) for output work — so ordinary answer-length
variance on a fast bot (0.3s lookups, one 1.5s answer) never counts as a spike:

- **First abnormal call → a `warn` anomaly.** A quiet notice, logged and
  alerted, that **never stops the agent, whatever `on_anomaly` says**. Models
  are noisy; one slow call is not an incident.
- **`spike_confirm` (2) of the trailing 5 calls abnormal → a `critical`
  anomaly.** Under the default mode (`on_spike="notify"`) this is **still
  only logged and alerted** — a model legitimately thinking hard about a
  hard question is not an incident either, and a wrong stop on a real user
  is worse than the spend. Set `on_spike="trip"` if you want a confirmed
  spike to follow your `on_anomaly` (raise, callback) and stop that
  session, or
  [`on_spike="limit"`](#many-callers-behind-one-service-the-abuse-ladder)
  for the middle setting — sustained spiking earns a session limit before
  anything closes.
  Each phase is reported once per session, and a window that returns to normal
  simply goes quiet.

**A spike is a behaviour-change signal, not proof of abuse.** All the
detector knows is that this session's calls stopped looking like this
session's own recent calls — which is also what a model update, a new
thinking mode, a longer document and one genuinely hard question look like.
That is why the first one never stops anything, why the default reaction is
to notify, and why the ladder below applies pressure by degrees instead of
slamming a door. Read a spike as "something changed here, go look", never as
a verdict about the caller.

**The held baseline: a run of spikes never becomes the session's "normal".**
The baseline is the median of the session's own recent calls, and a spike lands
in that history like any other call — so a sustained run of them would drag the
median up until the spiking read as ordinary. It does not: while the trailing
window still contains an abnormal call, the baseline is **frozen** at the
snapshot taken on the last call before the abnormal run began. Live tracking
resumes once five ordinary calls in a row have pushed the abnormal ones out of
the window, so a session whose traffic genuinely changes still re-learns. This
holds in every `on_spike` mode — it is a correctness property, not a policy.

Spikes are the early-warning system; the hard walls are the deterministic
detectors — a session that keeps burning money still hits `budget_usd`, and
an explicit `max_call_seconds` / `max_tokens_out_per_call` cap always reacts
through `on_anomaly` (you set that number on purpose). Your observers — and,
once a plane is connected, the plane itself — hear about both phases, each
exactly once per session: the watching notice and the confirmation.

The anomaly names the session and the numbers behind it:

```
Spike confirmed for session 'user:5310': call took 74.0s, this session's normal is 2.0s
```

```python
anomaly.details
# {'session_id': '91dd75886877', 'key': 'user:5310',
#  'tags': {'service': 'support-bot'}, 'metric': 'duration',
#  'value': 74.0, 'median': 2.0, 'factor': 10.0, 'confirmed': True,
#  'baseline_source': 'local'}
```

`metric` is `"duration"` or `"output_tokens"` — whichever moved further from
that session's normal. `baseline_source` is `"local"` (this session's own
live or held median — the only value without a control plane), `"restored"`
(this key's own baseline, delivered by the plane because some worker already
learned it — see "Baselines that survive a restart" below) or `"peer"` (no
baseline for this key exists anywhere yet, so the service-wide median stood
in for it). Connected to a plane, `details` also carries `vs_service` — this
call's ratio to the service-wide median, whatever baseline actually caught
it — and the message names both ratios:

```
Spike confirmed for session 'user:5310': call took 74.0s, this session's
normal is 2.0s (8.0x its own normal, 6.0x this service's)
```

**Optional hard caps.** If you do know a number, there are three per-call
ceilings: `max_call_seconds` (wall time), `max_tokens_out_per_call` (completion
tokens, reasoning included) and `max_cost_per_call_usd` (estimated dollars for that one
call). All three are off by default and they override the learned path
entirely: breaching one is `critical` from the first call, with no warm-up and
no confirmation, and it reacts through `on_anomaly` whatever `on_spike` says —
a number you stated is not a behavioral spike. The anomaly comes from the
`spike` detector with `details["cap"]` set and `details["metric"]` naming which
ceiling went:

```
Per-call cap exceeded for session 'user:5310': call cost $1.2000, cap $0.5000
```

```python
runbound.init(on_anomaly="raise")                              # the learned path: on by default, free, local
runbound.init(on_anomaly="raise", max_call_seconds=120)        # a hard ceiling -- free, local, on regardless
runbound.init(on_anomaly="raise", max_cost_per_call_usd=0.50)  # or a dollar one
```

The learned path (the ratio judgement above, and the ladder below) is on by
default in this process, with no account at all — set `spike_detection=False`
to turn it off. A connected plane can only turn it back on if you disable
it, never the reverse. The three hard caps are unaffected either way: they
are numbers you stated, not a behavioral judgement, and always run.

The dollar cap is priced from the same static table `budget_usd` uses, so
under the default `on_unpriced_model="zero"` it reads `$0.00` for a model
runbound has no price for — on Ollama or a local vLLM, cap the tokens
instead, or set `on_unpriced_model="estimate"` / `"refuse"` (see the
[configuration reference](../reference/configuration.md#configuration-reference)).

### Baselines that survive a restart

Honest about the limits: **without a control plane, baselines live in this
process and reset when it restarts** — a fresh worker re-learns each session
over its first `spike_warmup_calls`, and two workers serving the same key do
not share what they have learned. Connected to a plane, they do: every exit
reports the session's own *held, trusted* baseline (never a live median
taken mid-spike — the same held-baseline correctness above holds here too),
and the plane hands it back to whichever worker asks next.

- **A worker that restarts mid-spike re-enters at the same rung.** Its next
  entry for that key restores the ladder's level and allowance exactly as
  the plane last knew them — never reset to level 0 (that would be
  forgiveness by redeploy) and never escalated either.
- **A brand-new key is judged against the service median from its first
  call**, rather than served a free warmup nobody else got —
  `baseline_source: "peer"` above. The service median is a median of every
  key's own baseline, one vote per key: a single high-volume caller cannot
  define what "normal" means for a key that has never called before.
- **This never changes what `on_spike` does.** A restored or peer baseline
  only changes what counts as abnormal; whether that abnormal call notifies,
  trips or climbs the ladder is `spike.mode`'s call alone, exactly as it
  always was. Under the default mode (`"notify"`), a peer-judged spike is
  still only ever logged and alerted.
- **Content independence holds throughout.** A baseline is a duration and an
  output-token count, keyed by the key's hash; nothing about what a call
  said or returned ever travels with it.

## Many callers behind one service (the abuse ladder)

When one service is shared — a hosted agent, an internal API, an assistant —
some keys behave and some do not, and a moderate spike should not slam the
door. A caller asking one genuinely hard question looks, for a moment, exactly
like a caller farming your service for free tokens, and cutting the first one
off is worse than paying for the second. `on_spike="limit"` is the middle
setting: **graduated pressure for behavior**, applied to one key's session, one
rung at a time. Money keeps its own hard wall underneath — `budget_usd` is
arithmetic and stops the session outright, ladder or no ladder.

| Level | What it means | What runbound does | What your app sees |
|---|---|---|---|
| **0** | Quiet | Watching, saying nothing | Nothing |
| **1** | Watching | First abnormal call: a `warn` anomaly, logged and alerted once | Nothing — the call is served |
| **2** | Limited | `spike_confirm` of the trailing 5 calls abnormal: the session gets an **allowance** of `spike_limit_calls` (5) further abnormal calls. Each one spends a unit, silently. Still a `warn` — nothing stops. If the window normalizes before the allowance runs out the session **heals** back to level 1, allowance forgotten (a later re-confirmation limits it again, and alerts again) | Nothing — every call is served |
| **3** | Closed | The allowance hits zero: a `critical` anomaly with `action="rollover"`. The session is closed, the key takes a **strike**, and its next session starts after a `spike_cooldown_seconds` (300s) cooldown with **half** the allowance (halved again per strike, floor 1) | `GuardrailTripped` on the closing call, then a `GuardrailTripped` at the door of each message during the cooldown — then served again |
| **4** | Blocked | After `spike_max_strikes` (3) rollovers the key is latched permanently, `action="blocked"`. No cooldown expires it | `GuardrailTripped` at the door, every time, until `runbound.clear(key)` |

**The rollover is invisible to your code.** There is no new exception, no new
call to make, and no state for you to hold: it is the same
`with runbound.session(key):` block, and the refusal during a cooldown is an
ordinary `GuardrailTripped` — the same one you already catch. The key quietly
moves to a fresh session between requests; `anomaly.details["action"]` is
`"rollover"` (or `"blocked"`) if you want to tell it from a budget trip.

```python
runbound.init(
    on_anomaly="raise",  # requires on_trip="latch" — the default
    on_spike="limit",
    spike_limit_calls=5,       # abnormal calls a limited session may still make
    spike_cooldown_seconds=300,  # how long a rolled-over key is refused at the door
    spike_max_strikes=3,       # rollovers before a permanent block
)
```

The ladder itself — its mode (`"limit"`), the abnormal-call allowance, the
cooldown and the strikes before a permanent block — is free and local, the
same as spike detection itself. A connected plane's own Controls can only
tighten these further (a shorter allowance, a longer cooldown, fewer
strikes), never loosen them.

**Telling the caller how long.** `runbound.session_status(key)` reads the
ladder without touching it (never creates a session, never counts as using
one), which is how you render "back in 5 minutes" instead of a bare error:

```python
runbound.session_status("user:5310")
# limited, four of five abnormal calls left:
# {'level': 2, 'strikes': 0, 'allowance_left': 4,
#  'cooldown_remaining_s': 0.0, 'tripped_by': None, 'generation': 0}

# rolled over, serving a cooldown:
# {'level': 0, 'strikes': 1, 'allowance_left': None,
#  'cooldown_remaining_s': 287.4, 'tripped_by': 'spike', 'generation': 1}
```

`runbound.clear(key)` is total forgiveness: the strikes go too, so a cleared
key starts again at the full allowance rather than one rollover from a block.

**The ladder explains itself.** `session_status(key)` also answers "why was this
key limited?" without anyone reading our code:

```python
runbound.session_status("user:5310")["why"]
# {'trigger': {'metric': 'duration', 'value': 41.2, 'median': 2.0,
#              'factor': 10.0, 'vs_service': 6.1, 'at_s_ago': 12.4},  # or None
#  'limited_at_s_ago': 12.4,      # 0.0 if it has never been limited
#  'allowance_start': 5,          # the limit's starting allowance, or None
#  'healed_times': 0,             # how many times level 2 healed back to 1
#  'closed_at_s_ago': 0.0,        # 0.0 if it has never been closed
#  'baseline': {'duration_s': 2.1, 'output_tokens': 118.0,
#               'source': 'local'},                              # or None
#  'service_baseline': {'duration_s': 2.4, 'output_tokens': 130.0}}  # or None

runbound.session_status("user:5310")["history"]
# [(0, 1, 40.2, 'first_abnormal'), (1, 2, 12.4, 'confirmed'), ...]
```

`trigger` is the abnormal call that last moved the level up — a call that only
spends allowance or heals does not replace it — and `baseline` is the held or
live median the detector is judging calls against, with its own `source`
(`"local"`, `"restored"` or `"peer"` — see above). `service_baseline` is the
plane's last-known service-wide median, `None` without a plane. `history` is
the last 10 level transitions, oldest first, as
`(level_from, level_to, at_s_ago, reason)` with `reason` one of
`first_abnormal`, `confirmed`, `healed`, `allowance_spent`, `rollover`,
`blocked` or `restored` (a worker restart re-entering the key at the rung the
plane last knew it on). Both survive a rollover:
the fresh session's entry is appended to what the closed one had earned, so the
story reads as one continuous climb rather than restarting at every strike.
`clear()` stamps a `cleared` transition onto the session it is forgetting, but
the key is gone from the registry by then, so it is visible only to a caller
holding its own reference to that session's state — never through
`session_status()`.

Three honest boundaries:

- **The ladder is for keyed sessions.** It ends in rolling a *key* over to a
  fresh session, and the unkeyed default session has no key to roll over — so
  outside a `session()` block, `"limit"` behaves exactly like `"trip"`.
- **A latch is what serves the cooldown, so nothing latches under
  `on_anomaly="warn"`** — a warn-mode session climbs to level 3, logs the
  closure, and keeps going. Use `on_trip="latch"` (the default) with
  `"raise"` or `"callback"` if you want the ladder to actually hold a door
  shut. `init()` rejects `on_spike="limit"` with an `on_trip` other than
  `"latch"`, but it cannot reject `limit` with `on_anomaly="warn"`, since the
  plane can turn `spike.mode: "limit"` on for a worker at any later heartbeat,
  well after `on_anomaly` was already set.
- **Without a control plane, strikes, cooldowns and the rung live in this
  process** like every other counter here: two workers count separately, and
  a restart forgets them. Connected to a plane, strikes, the level and the
  allowance all follow the key across workers and across a restart (see
  "Baselines that survive a restart" above) — `on_spike`'s own reaction
  never changes either way.

The whole climb, end to end and offline:
[`examples/ladder_demo.py`](../../examples/ladder_demo.py).

---
