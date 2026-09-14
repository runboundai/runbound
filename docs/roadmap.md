# Roadmap

[← Docs](README.md)

Shipped since:
[fleet mode](guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane) — a
shared budget, a shared latch, shared strikes, org-wide action policy with
dry-run rollout, fleet-wide provider circuits and a kill switch.

Still open:

- Cross-instance **spike baselines**, so a session's normal survives a restart
  and is shared between workers rather than re-learned per replica
- A shared **`max_calls` tally**, so "once per session" is once for the fleet
  and not once per worker
- Fleet-wide **fan-out counters and in-flight caps**, so a concurrency cap is
  the cluster's rather than each worker's
- Native wrappers for non-OpenAI-shaped SDKs (TGI, Bedrock, Vertex)
- OpenTelemetry export

Explicitly out of scope: hallucination scoring, answer-quality judgement,
prompt-injection blocking, and anything that needs an LLM to decide whether to
page you.

---
