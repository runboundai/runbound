# Start here: which door

[← Docs](README.md)

Runbound reaches your agent three ways. They enforce the same budgets, the same
postures and the same refusals, and all three report to the same plane. Pick by where
your agent's decisions are made, not by taste.

| Your workload | The door | You change |
|---|---|---|
| Python code you own, calling a model or running tools | **The SDK** | `pip install runbound` and one `init()` line |
| An app in any language, or code you cannot edit, calling OpenAI or Anthropic | **The gateway** | one environment variable: the base URL |
| Tools that run somewhere Runbound's code is not: a webhook tool server, a voice agent's actions, a no-code flow | **The action API** | one HTTP call before the action, one after |

**The SDK** runs inside your process. It sees model calls, the tools you
decorate, and the arguments' digests, and it refuses before a call goes out or
a tool body runs. It is the deepest door and needs your Python. Start with
[Getting started](getting-started.md).

**The gateway** sits between your app and the provider. You point
`OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL` at it and every model call is admitted,
priced and recorded with no change to your code. It sees model calls and nothing
inside your process, so a tool your agent runs is invisible to it. See [Attach:
the gateway](guides/attach-gateway.md).

**The action API** asks one question, "may this action run?", for work that is
not a model call. Your tool server calls admit before it acts and report after,
and gets the same Decision the SDK gives a decorated tool. It does not run the
action for you and cannot stop one you did not ask about. See [Attach: the
action API](guides/attach-action-api.md).

You can combine them: a Python agent on the SDK, a second service in another
language on the gateway, and its webhook tools on the action API all report to
the same plane.
