# Examples

Three runnable scripts. Each one runs with no account and no plane; the parts that need a control
plane say what they print instead. Run them from `runbound-sdk/`.

| Script | Shows | Needs |
|---|---|---|
| [`tool_rules.py`](tool_rules.py) | `@runbound.tool` with code rules (`constraint=`, `max_calls=`), and a console row that tightens them | nothing; a plane for the row |
| [`deferred_approval.py`](deferred_approval.py) | `approval="required", mode="deferred"`, the seal (`RUNBOUND_APPROVAL_SECRET`), `on_deferred_result`, `runbound.current_request()` | a plane to ask a person |
| [`langchain_otel.py`](langchain_otel.py) | the LangChain callback handler, and one OpenTelemetry span per tool call | `pip install --pre "runbound[langchain,otel]"` |

## Without a plane

```bash
python examples/tool_rules.py          # ok, refused policy, ok, ok, refused policy
python examples/deferred_approval.py   # refused approval_unavailable
python examples/langchain_otel.py      # a lookup, a refusal, two spans
```

## Against a plane

The SDK reads `RUNBOUND_PLANE_URL` and `RUNBOUND_TOKEN` (the service's key). The console row and the
approval are set by a person in the console; the admin API calls below do the same from a shell, with
`ADMIN_KEY` a key the plane accepts on its admin routes. They are not part of the examples.

```bash
export RUNBOUND_PLANE_URL=http://localhost:8000 RUNBOUND_TOKEN=rb_...

# tool_rules.py: a row for issue_credit, capped at 1 call per session, then rolled out
curl -s -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"service": "credits", "tool": "issue_credit", "max_calls_per_session": 1}' \
  $RUNBOUND_PLANE_URL/v1/admin/tool-policies
curl -s -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"state": "enforced"}' $RUNBOUND_PLANE_URL/v1/admin/tool-policies/credits/issue_credit/rollout
python examples/tool_rules.py          # the third call is now refused: session_max_calls

# deferred_approval.py: run it, then approve the pending request from a second shell
RUNBOUND_APPROVAL_SECRET=change-me python examples/deferred_approval.py
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "$RUNBOUND_PLANE_URL/v1/admin/approvals?status=pending&service=refunds"
curl -s -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"reason": "ok"}' $RUNBOUND_PLANE_URL/v1/admin/approvals/<id>/approve
```

`tests/test_examples.py` runs all three offline; `tests/integration/test_examples_plane.py`
(marker `plane`) runs them against a throwaway plane and checks what they print.
