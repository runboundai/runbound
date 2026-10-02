"""When the model or provider behind a call changes, events() says so once. Shown on: What changed."""

# docs: runtime-change
import runbound

runbound.init()

runbound.record_call("gpt-4o", 100, 50, provider="openai@default")    # the first model seen: nothing to change from
runbound.record_call("gpt-4o", 100, 50, provider="openai@default")    # the same again: still no change
runbound.record_call("gpt-4.1", 100, 50, provider="openai@default")   # a different model: one record

for change in runbound.events():
    if change["kind"] == "runtime_change":
        print(change["what"], change["from"], "->", change["to"])
# /docs

changes = [e for e in runbound.events() if e["kind"] == "runtime_change"]
assert len(changes) == 1, changes
assert (changes[0]["what"], changes[0]["from"], changes[0]["to"]) == ("model", "gpt-4o", "gpt-4.1")
assert set(changes[0]) == {"kind", "at", "what", "from", "to"}
