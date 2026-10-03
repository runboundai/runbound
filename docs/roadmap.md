# Roadmap

[← Docs](README.md)

Shipped since:
[fleet mode](guides/fleet-mode.md#fleet-mode--one-truth-across-all-your-workers-control-plane) — a
shared budget, a shared latch, shared strikes, org-wide action policy with
dry-run rollout, fleet-wide provider circuits and a kill switch.

Also shipped: **spike baselines and the abuse-ladder rung that survive a restart**. A connected plane
keeps each key's held baseline and rung and hands them back to any worker on the next entry, and a new
key is judged against the service-wide median from its first call.

Still open:

- A shared **`max_calls` tally**, so "once per session" is once for the fleet
  and not once per worker
- Fleet-wide **fan-out counters and in-flight caps**, so a concurrency cap is
  the cluster's rather than each worker's
- Native wrappers for non-OpenAI-shaped SDKs (TGI, Bedrock, Vertex)

Explicitly out of scope: hallucination scoring, answer-quality judgement,
prompt-injection blocking, and anything that needs an LLM to decide whether to
page you.

---
