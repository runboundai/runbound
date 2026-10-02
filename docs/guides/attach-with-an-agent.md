# Attach with an agent

[← Docs](../README.md)

A coding agent can attach runbound to your project for you. Paste the prompt below
into it. Every command in it is a real one: the SDK door installs with `pip`,
starts with one `init()` line, and proves itself with `python -m runbound check`,
which exits `0` only when something is guarded. The gateway door changes one
environment variable and is proved by a request.

## The prompt

````text
You are attaching runbound to this project. Do the steps in order and stop at the first
step that fails; say what failed. Never print, log or commit a token or an API key.

1. Find where the agent starts and which tools it can call (functions that act: refund,
   send, write, delete). Pick the SDK door if this is Python code you can edit, the
   gateway door if the app is not Python or cannot be edited.

SDK door
2. $ pip install runbound
3. Add one line before the first model call:  runbound.init(budget_usd=5.0, on_anomaly="raise")
4. For each tool that acts, decorate it:  @runbound.tool(effects={"financial"})
   Use the classes read, write, external, financial, destructive, privileged.
5. $ python -m runbound check <the agent's entry script>
   It must exit 0. If it exits 1 nothing is guarded: fix steps 3 and 4. If it exits 2
   the script could not be loaded: report the error.
6. $ python -m runbound check --json <the agent's entry script>
   Read "tools" and confirm every acting tool is listed with its class.

Gateway door
2. Ask the operator for the gateway URL and an application token (rb_gw_...).
3. Set the base URL for the provider the app uses, without writing the token to a file:
   OPENAI_BASE_URL=$RUNBOUND_URL/g/$APP_TOKEN/openai/v1
   ANTHROPIC_BASE_URL=$RUNBOUND_URL/g/$APP_TOKEN/anthropic
4. Send one request through the app, then confirm the console shows it (the first record is an llm_call).

Report which door you used, the output of the check command, and anything you could not do.
````

## What the agent will see

`python -m runbound check` prints one short report: the clients runbound wrapped,
the tools it guards with their classes, whether a plane is connected (its address,
never its key), the posture in force, the budgets, and the last ten events. With no
token it makes no network call. See [Getting
started](../getting-started.md) for the same report from inside your own code
(`runbound.check()`), and [Which door](../which-door.md) for choosing between the SDK
and the gateway.

The `--json` form has a stable shape, `schema` 1:

| Key | Meaning |
|---|---|
| `guarded` | `true` when a client is wrapped, a tool is decorated or a call was guarded |
| `initialized` | whether `runbound.init()` has run |
| `clients` | `wrapped_clients`, `auto_wrapped` (provider to call shapes), `providers_imported`, `providers_unguarded` |
| `tools` | each decorated tool: `name` and its capability `classes` (`[]` is unclassified) |
| `plane` | `mode` (`local`, `connected` or `degraded`), `url` (or `null`), and a one-line `detail` |
| `posture` | the posture in force, by name |
| `budgets` | only the limits `init()` was given |
| `guarded_calls`, `tool_calls_seen` | counters for this process |
| `events` | the last ten: `kind`, `ts` (seconds since the epoch) and a short `line` |
