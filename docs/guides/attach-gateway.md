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
`/g/<token>/openai/v1/responses` and `/g/<token>/anthropic/v1/messages`, plus
`/g/<token>/anthropic/v1/messages/count_tokens`, which is counted toward nothing
(below). Your
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

## The doors

| Path under `/g/<token>` | Admitted, held, billed and filed? |
|---|---|
| `/openai/v1/chat/completions` | yes |
| `/openai/v1/responses` | yes |
| `/anthropic/v1/messages` | yes |
| `/anthropic/v1/messages/count_tokens` | **counted toward nothing** |

`count_tokens` is the Anthropic SDK's call for counting a request's tokens
without running it, so an Anthropic client pointed at the gateway does not get a
404 on it. It is relayed to the application's Anthropic upstream and answered as
the provider answers. The token is resolved, the upstream is checked against the
egress list and an open Anthropic circuit answers 503, with the same 401, 404 and
502 answers as the other doors; but nothing is admitted, held, billed or filed,
no budget or halt applies to it, and the body is relayed without being read.

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

Every response for a resolved application also carries two integers,
`x-runbound-controls-applied` and `x-runbound-signals-applied`: the id of the
newest Controls row in effect for that application, as admission and as the
signals side hold it. They are **an id, not a version** (versions are numbered
per scope; ids only climb): after a Controls write returns its `id`, the write
is live once both headers are at least that number. `0` means no Controls row
exists. A request with an unknown token carries neither.

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

## Realtime

A voice agent on OpenAI's Realtime API keeps its client and changes the address. The
Realtime door is a WebSocket on the same application token:

```
wss://$RUNBOUND_HOST/g/$APP_TOKEN/openai/v1/realtime?model=gpt-realtime
```

Name the model in the URL, as the provider's own URL does. The provider key goes in as
it always did (an `Authorization` header, or, for a browser, the
`openai-insecure-api-key.<key>` subprotocol); the gateway passes it on and never reads,
stores or logs it. The caller is the `X-Runbound-Caller` header, or `?runbound_caller=`
where a browser cannot set a header, else `default`. Each socket is a run: every
connection answers with `x-runbound-run` and `x-runbound-decision-id`, and
`X-Runbound-Run` names one yourself.

```python
import asyncio
import json
import os

from websockets.asyncio.client import connect


async def main():
    host, token = os.environ["RUNBOUND_HOST"], os.environ["APP_TOKEN"]
    url = f"wss://{host}/g/{token}/openai/v1/realtime?model=gpt-realtime"
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    async with connect(url, additional_headers=headers) as ws:
        await ws.send(json.dumps({"type": "response.create"}))
        async for raw in ws:
            event = json.loads(raw)
            print(event["type"])
            if event["type"] in ("response.done", "error"):
                break


asyncio.run(main())
```

**At the handshake** the gateway decides as it does for a model call: the token, the
application's upstream, the caller, a halt, a latch, the posture, a spent budget, an open
circuit. A refusal is an ordinary HTTP answer before the upgrade, with the same statuses,
codes and headers as the table above (a client that cannot read a status, a browser's
WebSocket, can have the application answer by accepting the socket, sending one `error`
event and closing 1008 with `runbound_refusal:<code>` instead; set it per application
through the admin API, `realtime_refusal: "close"`, the default being `"http"`). The
handshake holds nothing and costs nothing.

**Then each response is its own decision.** A `response.create` is admitted before it is
forwarded; a response the provider starts on its own (server voice detection) is admitted
when it arrives. Budgets, postures, a halt or a revoked token are read again for every
response, so a change takes effect at the next one and a lift serves the same socket
again. A response that is admitted runs to `response.done` byte for byte: the gateway
never cancels it, and a budget spent or a halt set in the middle of it does not cut it.

A refusal is one `error` event on the socket, which stays open:

```json
{"type":"error","event_id":"evt_rb_0f3a9c1d7e2b4a68","error":{"type":"runbound_refusal","code":"budget_exceeded","message":"app usd budget: spent $0.0080, held $0.0064, limit $0.0085","event_id":"evt_2","verdict":"deny","boundary":"money","level":"fleet"}}
```

The `error` carries the `event_id` of your `response.create` when the create had one, so you
know which request it answers (a create with no id gets no correlation); the event's own
top-level `event_id` is always the gateway's, beginning `evt_rb_`. A refused create is never
sent to the provider. If the provider had started the response
itself, the gateway cancels it upstream and drops its events before any reached you, then
sends the same event. Each response is priced when it ends, from the usage the provider
reports (text, audio and image tokens at their own rates), and filed as one call.

**A caller whose posture is `restricted`** has its responses held whole. The gateway
withholds a response from `response.created` to `response.done`, then judges the tools it
asked for against the posture. If every tool is allowed, the held events are released in
order, unchanged. If one is denied, or has no class, the whole response is replaced by one
`error` event, `posture_restricted`, naming the tool and its class (the event's own `event_id`
is the gateway's, beginning `evt_rb_`); the provider ran and
is billed, and the refusal is filed beside the call.

```json
{"type":"error","event_id":"evt_rb_0f3a9c1d7e2b4a68","error":{"type":"runbound_refusal","code":"posture_restricted","message":"the model asked for the tool 'issue_refund' (financial); posture 'restricted' denies its financial capability (plane: restricted)","verdict":"deny","boundary":"posture","level":"key","tool":"issue_refund","class":"financial"}}
```

A restricted caller therefore hears a response only when it is complete, and a response
longer than `GATEWAY_REALTIME_HOLD_MAX_BYTES` (16 MiB by default) is refused whole
(`runbound_response_too_large`) rather than released unchecked. A caller under a full
posture is never held.

**How a session ends.** A session never closes with a response open. If your client
leaves mid-response, the gateway keeps reading until the response is done so its real
usage is counted, within the same bounded drain as a stream. If the provider closes or
drops the session, your client gets the provider's close code where one can be sent,
otherwise 1011. A session lasts at most an hour (`GATEWAY_REALTIME_MAX_SESSION_S`; a
running response finishes first, then 1000); a socket turned away for a halt, a stopped
posture or a revoked token is closed 1008 `runbound_refusal:<code>` after a minute of
silence, so its reconnect meets the handshake refusal; a frame over
`GATEWAY_MAX_BODY_BYTES` ends the session 1009. Open sessions are capped per process and
per application; past a cap the handshake is a 503 `runbound_realtime_capacity`.

**What it cannot see.** A copy of each event is read for its ids, its usage numbers and
the names of tools; the audio and the words are forwarded and never kept. It cannot judge
what the model says or hears, and a tool your agent runs inside your own process is the
SDK's or the action API's to govern.

**Running it.** Never run the gateway at DEBUG: the WebSocket libraries print every
handshake header and frame at that level, and the headers carry your provider key. The
gateway keeps its own loggers quiet and a test checks that no key reaches its logs or its
store, but a library's DEBUG output is outside that. A reverse proxy in front of it must
not log the `Sec-Websocket-Protocol` header either, which is where a browser's key
travels: tell it to drop that header from its access log and its error log, and spell
the name as `Sec-Websocket-Protocol`, the way the gateway's own Caddyfile does, and check
that your proxy's matching really drops it (a log filter that misses the header leaves the
key in the log).

## What it cannot see

It sees model calls that come through the base URL, with their model, tokens and
cost: never a prompt or a reply, which are forwarded and not stored, logged or sent
on. What it reads of a request is the model, the `stream` flag, the output cap it states
(`max_tokens` or `max_output_tokens`) and the caller's identity fields (`user`,
`safety_identifier`, `metadata.user_id`), and it counts the
body's characters in transit to estimate tokens. For a caller whose posture is narrowed
it also reads the *names* of the tools a reply asks for (never their arguments), and
holds a streamed reply whole until it has judged them. It cannot see a tool your agent runs inside your app, a call that does not
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
