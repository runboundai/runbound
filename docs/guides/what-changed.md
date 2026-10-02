# What changed

[← Docs](../README.md)

When a value your process runs on moves from one value to another, `events()`
gets one `runtime_change` record. It exists so that "the agent started behaving
differently on Tuesday" has an answer: the model or the provider behind a call
changed, or a control plane delivered a new policy or controls version.

```python
import runbound

runbound.init()

runbound.record_call("gpt-4o", 100, 50, provider="openai@default")    # the first model seen: nothing to change from
runbound.record_call("gpt-4o", 100, 50, provider="openai@default")    # the same again: still no change
runbound.record_call("gpt-4.1", 100, 50, provider="openai@default")   # a different model: one record

for change in runbound.events():
    if change["kind"] == "runtime_change":
        print(change["what"], change["from"], "->", change["to"])
```

It prints `model gpt-4o -> gpt-4.1`.

A record has exactly these fields:

| Field | Meaning |
|---|---|
| `kind` | `"runtime_change"` |
| `what` | `"model"`, `"provider"`, `"policy_version"` or `"controls_version"` |
| `from`, `to` | The previous and the new value |
| `at` | When it was seen (seconds, this process's clock) |

It is recorded only on a real change: never for the first value seen, and never
again while the value stays the same. It holds no prompt, no reply and no tool
argument. With a control plane connected the record is sent with your other
telemetry and follows `export_events`.

Posture changes are in the same list: a `posture` record says which posture it
came `from`, its `scope` (`"session"` or `"process"`), the ladder `level` when
the ladder moved it, and the session `key`. Read them together to see what moved
before a refusal. See [A runaway, start to finish](a-runaway.md).
