# Attach: the gateway

[← Docs](../README.md) · [Which door?](../which-door.md)

The gateway is a service between your app and the model provider. Point your
client's base URL at it and every call is admitted against your budgets, priced,
recorded, and refused when a limit says so. You change one environment variable;
your code, your SDK and your provider key stay as they are.

Below, `$RUNBOUND_URL` is where your gateway is reachable and `$APP_TOKEN` is an
application token (`rb_gw_...`). Both come from whoever runs your Runbound plane.

## Get a token

In the console, onboarding step 2, Gateway tab: name an application, give the
upstream base URL for each provider you use (OpenAI, Anthropic, or both), choose
its fail mode, and create it. The token is shown once, with a copy button; it is
never readable again, only rotated. The Gateway apps page lists your
applications, edits their settings, and rotates or revokes tokens. A token is
all a caller needs: it names the application, so it must be kept like a key.

## Point your client at it

```bash
# OpenAI, and anything that speaks its API
export OPENAI_BASE_URL=$RUNBOUND_URL/g/$APP_TOKEN/openai/v1

# Anthropic
export ANTHROPIC_BASE_URL=$RUNBOUND_URL/g/$APP_TOKEN/anthropic
```

The doors are `/g/<token>/openai/v1/chat/completions`,
`/g/<token>/openai/v1/responses` and `/g/<token>/anthropic/v1/messages`. Your
provider key goes in the request as it always did; the gateway forwards it and
never reads it, and returns the provider's answer, status and headers unchanged.
Send one request:

```bash
curl -s $RUNBOUND_URL/g/$APP_TOKEN/openai/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Runbound-Caller: alice" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}'
```

## Who is calling

A budget that follows a person needs to know the person. The gateway reads, in
order:

| | Caller | Run |
|---|---|---|
| Header | `X-Runbound-Caller` | `X-Runbound-Run` |
| OpenAI body | `user`, then `safety_identifier` | |
| Anthropic body | `metadata.user_id` | |
| Otherwise | `default` | a rolling run |

The caller is hashed (sha256, unsalted, the same hash the SDK uses for a
session key), so one person is one caller across the SDK and the gateway, and
only the digest is stored. Names longer than 512 characters are cut.

## What a refusal looks like

A refusal is an error in the provider's own shape, so your client's error handling
sees an ordinary API error:

```json
{"error": {"type": "runbound_refusal", "code": "budget_exceeded",
           "message": "caller usd budget: spent $4.9990, held $0.0200, limit $5.0000"}}
```

(On Anthropic: `{"type": "error", "error": {"type": "runbound_refusal", "message": "..."}}`.)
Every response, a refusal or not, carries `x-runbound-decision-id` and
`x-runbound-run`. A refusal also carries `x-runbound-verdict` (`deny`),
`x-runbound-boundary` (`money`, `tokens`, `halt`, `latch`, `posture` or
`circuit`), `x-runbound-level` (`fleet`, `key` or `run`) and `x-should-retry:
false`, so a provider SDK does not retry it. Repeating the call cannot help.

| Status | Code | Meaning |
|---|---|---|
| 402 | `budget_exceeded`, `unpriced_model` | a dollar or token budget; a model with no known price under a dollar budget |
| 403 | `halted`, `latched`, `blocked`, `posture_stopped` | the kill switch, a latch on this caller, or a stopped posture |
| 503 | `provider_circuit_open` | this provider and model is failing; has `Retry-After` |
| 503 | `runbound_store_unavailable` | the store is down and this application fails closed |
| 401 | `authentication_error` | the token is unknown or revoked |
| 404 | | the application has no upstream for that provider |
| 502, 504 | | the upstream could not be reached, or timed out (the call is released, nothing is charged) |

Any other answer is the provider's own, passed through. (The [action
API](attach-action-api.md) adds a 409 for a duplicate action; model calls have none.)

## If the store is down

The gateway admits a call against a shared store. `fail_mode` is your choice per
application: **closed** (the default) refuses with 503 while the store is
unreachable; **open** lets model calls through, recorded as admitted without a
check. Actions of the classes financial, destructive and privileged fail closed
whatever the mode says (see the action API).

## Streams

`stream: true` works on all three doors and the bytes reach your client unchanged.
To count a stream the gateway needs its usage, so on OpenAI chat it asks the
provider to include it and removes that addition before your client sees the
answer; on the other doors the usage is already in the stream. The call is
priced when the stream ends. The gateway never cuts a stream: a budget a stream
uses up refuses the next call, not the one in flight. A client that disconnects
does not stop the accounting, within a bounded drain.

## What it cannot see

It sees model calls that come through the base URL, with their model, tokens and
cost, and nothing else: never a prompt or a reply, which are forwarded and not
kept. It cannot see a tool your agent runs inside your app, a call that does not
use the base URL, or anything the provider does not report. To govern tools, use
the SDK's `@runbound.tool` or the [action API](attach-action-api.md).

## Verify

Send the request above, then open the console. The first record of a gateway
call is an `llm_call` for your application's name: onboarding step 2's Gateway
tab turns green when it arrives, and the application shows a session for your
caller on the Runs page. If nothing arrives, check the base URL ends
`/openai/v1` (OpenAI) or `/anthropic` (Anthropic), that the token matches, and
read the response headers: `x-runbound-decision-id` is on every answer.

*Not executed in this repository's CI.* The control plane's test suite sends the
commands on this page to a real gateway and checks the statuses and headers
described here.
