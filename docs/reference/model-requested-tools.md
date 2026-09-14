# Model-requested tool calls (loops without `@tool`)

[← Docs](../README.md)

`@runbound.tool` sees the calls your code runs. It does not see a model
asking for the same tool forever when you dispatch those calls by hand, through
a framework, or into a queue — and that loop costs exactly the same money.

So the wrappers read the tool calls a response **asks for**, on every path:

- **OpenAI chat** — `choices[].message.tool_calls[].function.{name, arguments}`
- **OpenAI Responses** — `output[]` items of type `function_call`
- **Anthropic** — `content[]` blocks of type `tool_use`
- **Streams** — assembled from the fragments as they arrive and reported once
  at stream end, best-effort; a shape runbound cannot read reports nothing.

Each one becomes a `tool_request` event, hashed from the tool name and its
**canonicalized** arguments — JSON re-encoded with sorted keys, so the same
call formatted two ways still hashes the same, and a streamed call hashes like
the non-streamed one it is a copy of. The arguments themselves are never
stored, logged or alerted; only the digest is.

Those hashes live in their own `"req:"` namespace, separate from executed
`@tool` calls: **three requests and three executions are two threes, not a
six.** The `loop` detector treats them exactly like executed calls otherwise,
so `loop_threshold` and `loop_window` mean the same thing for both.

**The trip comes out of `create()`, after the response returned.** That call
happened and was counted — we cannot un-send it. What the raise prevents is
your dispatch of the third identical call:

```python
try:
    response = client.chat.completions.create(model="gpt-4o", messages=msgs,
                                              tools=tools)
except GuardrailTripped as looping:
    print(looping.anomaly.message)
    # Loop detected: model requested tool 'search' repeated 3x in last 20 actions
    return give_up_gracefully()

for call in response.choices[0].message.tool_calls:   # never reached
    dispatch(call)
```

Requests are counted for loops only. They never feed an action policy's
`max_calls`, and `runbound.tool_calls()` still counts executions alone: what
the model *asked* for is not what your agent *did*.

---
