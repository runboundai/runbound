# Why this exists

[← Docs](../README.md)

One agent burned **$2,847 in four hours** on a refactoring loop while every
monitoring dashboard stayed green. A four-agent loop ran for **11 days and cost
$47,000**; a budget alert fired on day 9, two days too late. In both cases the
telemetry worked. Nobody was watching it at 3am, and nothing in the stack had
the authority to pull the plug. The same failure arrives at higher volume when
one service serves many callers: a chatbot free-rider works out that your
support assistant will answer anything and spends your API key on their
homework, or a provider update flips the model into thinking mode and a
two-second answer becomes a seventy-second one across every run you serve.
Nothing errors. Nothing pages.

runbound is the layer with the authority to pull the plug, and the proof
that it did. It raises on the agent's own thread, in the middle of the loop,
rather than reporting it in the morning. Every reaction is a choice you made in
plain configuration; every refusal is a row in a ledger that holds the tool
name and the rule, never the arguments.

Try it offline, no API key needed:

```
.venv/bin/python examples/runaway_demo.py         # 10 seconds, no API key needed
.venv/bin/python examples/chatbot_abuse_demo.py   # the free-rider story, offline
.venv/bin/python examples/policy_demo.py          # actions refused before they ran
```

---
