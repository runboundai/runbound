# Time and fan-out limits

[← Docs](../README.md)

Some runs are wrong in a way no single call shows. An agent stuck waiting on
something that will never arrive; a run that spawns sub-agents that spawn
sub-agents. Every individual number looks reasonable, and the bill does not.
These are the numbers you state about the *shape* of a run — all `None`, all
off, until you set them.

```python
runbound.init(
    max_session_seconds=900,     # a run may last 15 minutes
    max_active_sessions=50,      # 50 session() blocks open at once, process-wide
    max_session_depth=2,         # a top-level block plus two levels under it
    max_child_sessions=10,       # one session may open ten distinct children
    on_anomaly="raise",
)
```

**`max_session_seconds` and `max_session_lifetime_seconds` are both the
`timeout` detector**, reading two different clocks. Any event kind trips
either — a session is running whether it is calling a model, running a tool,
or failing — measured on the monotonic clock, never the system one, so a
clock change cannot fake either. Each fires once per session, `critical`,
and follows `on_anomaly` and `on_trip` exactly like `budget` does.

| Clock | Measures | Keyed session (`session(key)`) | Unkeyed (default) session |
|---|---|---|---|
| `max_session_seconds` (`details["scope"] == "run"`) | The current *run* | Reset on **every entry** of `session(key)` — a returning key's next request starts a fresh clock, not a continuation of its first one ever. | Never reset: the default session guards the whole process, so it *is* the run, from `init()` onward. |
| `max_session_lifetime_seconds` (`details["scope"] == "lifetime"`) | The session's whole existence | Set once, at the key's first-ever entry, and never reset — the old (pre-0.3.0) meaning of `max_session_seconds`, for a customer who wants it back. `None` (off) by default. | Identical to `max_session_seconds` here, since the default session is never re-entered — this knob exists for keyed sessions. |

A keyed session entered three times over 70 minutes, each block short, never
trips `max_session_seconds=3600` — the run clock resets each time. One block
that itself runs 3601 seconds does. `max_session_lifetime_seconds` trips on the
*sum* of the key's whole history instead, whenever you set it — independently
of whether the run clock ever trips.

**The three fan-out limits are enforced at the door.** Entering
`runbound.session(key)` records where the block sits — its depth, its parent,
and one more child on that parent — and then checks the three numbers *before
the body runs*:

| Rule | Counts | Refused when |
|---|---|---|
| `active` | `session()` blocks open right now, process-wide | entering would make more than `max_active_sessions` |
| `depth` | how many blocks enclose this one — a top-level block is depth **0** | this block's depth exceeds `max_session_depth`, so `2` permits a top-level block and two levels under it |
| `children` | distinct children one session has opened | the parent's child count exceeds `max_child_sessions` |

A refusal is a `GuardrailTripped` whose anomaly carries
`details["rule"]` (`"active"` / `"depth"` / `"children"`), `count`, `limit` and
the session `key`. Two properties are deliberate, and both are in
[the reactions table](reactions.md#what-happens-when-something-trips--every-choice-in-one-place):
it is enforced **regardless of `on_anomaly`** — like a per-call cap, this is a
number you stated, not something inferred — and it **latches nothing**, because
the thing that went wrong is the shape of the run, not this key. The next
block for the same key is judged on its own shape. Each rule pages once per
session, however often the agent walks back into it.

Lineage is recorded where a session is *born*: a key first entered under one
parent keeps that depth and that parent forever, so a shared helper session used
from everywhere cannot inflate anybody's child count, and re-entering the same
child counts one child however many times it is used.

```python
runbound.active_sessions()   # keyed session() blocks open right now
```

It counts blocks, not keys — the same key entered twice in two threads is two
pieces of work in flight — and the default session is never counted, so this
reads `0` in a program that uses no keys, and before `init()`.

Fail-open holds here too: anything that goes wrong deciding is logged and the
block is entered, exactly as it would have been with no limits configured.

---
