# Attach: the action API

[← Docs](../README.md) · [Which door?](../which-door.md)

The action API answers one question over HTTP, with no SDK in the process: *may
this action run?* A tool server, a voice agent's backend or a no-code flow asks
before it acts and says what happened after. The answer is the same Decision a
`@runbound.tool` gets from the SDK: it is refused by the same budgets, postures
and halts, in the same words. It does not run the action and cannot stop one you
did not ask about.

`$RUNBOUND_URL` is where your gateway is reachable and `$APP_TOKEN` is an
application token (`rb_gw_...`), the same token the model doors use. Get one from
the console's onboarding step 2 (see [Attach: the gateway](attach-gateway.md#get-a-token)).

## Admit, then report

```bash
curl -s -X POST $RUNBOUND_URL/g/$APP_TOKEN/actions/admit \
  -H "Content-Type: application/json" \
  -H "X-Runbound-Caller: alice" \
  -d '{"action": "issue_refund", "class": "financial", "args": {"order": "o-1", "amount": 25}}'
```

```json
{"decision_id": "7f3c...", "verdict": "allow", "boundary": null, "level": null,
 "reason": null, "run_id": "...", "class_source": "request", "hold": "..."}
```

Run the action only on `"verdict": "allow"`. When it has finished, report what
happened with the `decision_id`:

```bash
curl -s -X POST $RUNBOUND_URL/g/$APP_TOKEN/actions/$DECISION_ID/report \
  -H "Content-Type: application/json" \
  -d '{"outcome": "executed", "duration_ms": 120}'
```

```json
{"decision_id": "7f3c...", "settled": true, "counted": true}
```

A tool server is two calls around the work:

```python
decision = post("/actions/admit", {"action": name, "class": "financial", "args": args})
if decision["verdict"] == "allow":
    outcome = run(name, args)                       # your code, your side effects
    post(f"/actions/{decision['decision_id']}/report", {"outcome": outcome})
```

*Not executed in CI: a sketch; `post` is your HTTP client.* If your proxy logs
URLs, send the token as `Authorization: Bearer rb_gw_...` to `/g/actions/admit`
and `/g/actions/<decision_id>/report` instead of putting it in the path.

## The request

| Field | |
|---|---|
| `action` | the action's name, 1 to 200 characters. Required |
| `class` | one of `read`, `write`, `external`, `financial`, `destructive`, `privileged`, or a list of them. The application's own `action_classes` map, if it names this action, replaces whatever you send |
| `args` | the arguments. They are hashed the moment they arrive and never stored or logged; only the digest is kept |
| `idempotency_key` | a string that makes a retry safe (below) |
| `cost_usd`, `tokens` | your estimate, held against the budgets until you report |
| `acknowledge_duplicate` | `true` to run an action held as a possible duplicate, once you have checked |
| `caller`, `run` | who and which run, as headers `X-Runbound-Caller` and `X-Runbound-Run` do on the model doors |

## The Decision, field by field

| Field | Meaning |
|---|---|
| `decision_id` | this decision's id; send it to `/report` |
| `verdict` | `allow`: run it. `deny`: do not |
| `boundary` | which limit refused: `money`, `tokens`, `halt`, `latch`, `posture` or `duplicate`; `null` on an allow |
| `level` | where that limit sits: `key`, `run` or `fleet`; `null` on an allow |
| `reason` | the refusal in words |
| `run_id` | the run the action belongs to |
| `class_source` | where the class came from: `request`, `app_map` or `none` |
| `hold` | the amount held against your budgets until you report |
| `posture` | the caller's posture, when it is narrowed |

A refusal also carries the `x-runbound-verdict`, `x-runbound-boundary` and
`x-runbound-level` headers and `x-should-retry: false`.

| Status | Meaning |
|---|---|
| 200 | allowed |
| 402 | a budget |
| 403 | the kill switch, a latch, or a posture that denies this class (`restricted` allows `read` and `write` and refuses the rest; `stopped` refuses all) |
| 409 | a duplicate, below |
| 400, 422 | the request is not a valid action |
| 401 | the token is unknown or revoked |
| 503 | the store is unreachable and this action fails closed |

## Retries and duplicates

A tool server that times out will retry, and a retried refund is a second
refund. The door guards effects (every class but `read`) against a repeat of the
same action with the same arguments by the same caller inside the duplicate
window (10 minutes by default, set on the Gateway apps page):

| Situation | Answer |
|---|---|
| The first call is still in flight | 409, `held: true`, "in flight" |
| It failed or timed out, and you send the same call again | 409, `held: true`, `retry_with: "idempotency_key"`. Send `acknowledge_duplicate: true` if you have checked it did not run |
| It succeeded, and you send it again with no key | allowed: two refunds of $25 are two refunds |
| Same `idempotency_key`, it succeeded | 409, `held: false`, `outcome: "executed"`: it already ran, do not run it again |
| Same `idempotency_key`, it failed or timed out | allowed, with `retry_of` naming the earlier decision |

So: always send an `idempotency_key` for an action that must not run twice. A key
whose first call was never reported (the server crashed) is retried once its hold
has expired, and held until then.

## If the store is down

Actions of the classes `financial`, `destructive` and `privileged` always fail
closed: 503 `store_unavailable`, because the duplicate check cannot run. Other
actions follow the application's `fail_mode`: with `open` they are allowed, with
`"degraded": true` and `"duplicate_check": "skipped"` in the answer and nothing
recorded.

## Report

`outcome` is `executed`, `failed` or `timed_out`; `duration_ms`, `cost_usd` and
`tokens` are optional. A report settles the hold once: `executed` charges your
`cost_usd` (else the amount you held), `failed` and `timed_out` charge nothing
unless you state a cost. A second report answers `settled: false,
"reason": "already_reported"`; an unknown or expired decision is 404; a report
after the hold's ttl succeeds with `"late": true`. Unreported holds expire on
their own (`action_hold_ttl_s`, 2 minutes by default).

## What it cannot see

Only what you ask about. An action you never admit is invisible, and the API
never learns whether the action ran except from your report.

## Verify

The first record to look for is an `action_call` for your application, and it
appears **when you report, not when you admit**: an admit alone files nothing,
and a refused admit files a refusal instead. Onboarding step 2's Action API tab
turns green on the first report. After one admit and one report the check is green; a refused admit shows on the
console's Refused actions page.

*Not executed in this repository's CI.* The control plane's test suite sends the
curl commands on this page to a real gateway and checks the statuses and fields
described here.
