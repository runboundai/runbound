---
name: Bug report
about: Something runbound does not match what it documents
title: ""
labels: bug
---

**Before you paste anything below: never paste an API key, a token, a
prompt, a reply, or a real session key into this issue.** Redact or
replace them — `sk-...` → `sk-REDACTED`, a real prompt → `<prompt
redacted>` — issues here are public.

## What happened

A clear description of what you expected and what you saw instead.

## Minimal reproduction

Ideally something that runs offline, the way `examples/runaway_demo.py`
does — no network, no API key, no account. If it needs a real provider
call, say so and trim it to the smallest snippet that still reproduces it.

```python
# your reproduction here
```

## Environment

- runbound version: `python -c "import runbound; print(runbound.__version__)"`
- Python version and OS:
- Provider SDK(s) and version(s) (e.g. `openai`, `anthropic`):
- Is a control plane connected? (`token` / `control_plane_url` set, or not)

## `coverage()` output

Paste the output of `runbound.coverage()` from the same process, if you can
reproduce this with it available — it tells us what runbound could actually
see, which is usually the first question we would ask.

```python
import json
print(json.dumps(runbound.coverage(), indent=2, sort_keys=True))
```

```
# paste the output here
```

## Anything else

Logs, stack traces, or anything else that helps — with the same redaction
rule as above.
