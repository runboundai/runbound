# Boundaries

[← Docs](../README.md)

runbound decides what an agent may consume, how far it may run, and what it
may do — never what the agent says, or what a model answered. That is not an
oversight. It is the one constraint everything else in this SDK is built on:

> **Every core runtime decision is made from non-content signals** — counts,
> hashes, timings, prices, and the capability classes a tool declares. No
> prompt, reply, or tool result is ever read to decide anything. See
> [Content independence](../../INVARIANTS.md#content-independence).

Below is what that constraint rules out, on purpose, and what each exclusion
costs you and buys you in return.

## No prompt or reply inspection

runbound never reads what an agent said or what a model answered. A detector
sees counts, timings and prices; it has no field to put message text in even
if it wanted one — see [What the SDK actually sees](what-it-sees.md).

**Costs you:** runbound will not catch a hallucination, a bad or unsafe
answer, or an attempt to manipulate the model through its input. "The agent
said something wrong" is not a question this SDK answers.

**Buys you:** a control that cannot be argued with. A budget, a step count or
a declared capability class means the same thing in every language, in every
prompt format, and under every attempt to phrase around it — there is no
wording that makes a dollar not a dollar. It also means the control adds no
latency, no cost and no failure mode of its own: there is nothing to read, so
there is nothing to get wrong reading it.

**Use instead:** a content-safety or output-evaluation tool, run alongside
runbound, for the "was this answer any good" question — a different question
from "is this run inside its bounds."

## No response rewriting

runbound never edits, retries with a different prompt, or substitutes an
answer. A refusal stops execution and raises; it never quietly hands the
agent something else instead.

**Costs you:** a refused call is a stop, not a fix. Nothing here "cleans up"
a bad response for you.

**Buys you:** what your agent decided stays exactly what it decided. A
silently rewritten answer is a bug report waiting to happen — the log says
one thing, the response the caller received says another. Every response
either came from the provider untouched or was refused before it went out;
there is no third case to debug.

**Use instead:** your own application code, deciding on its own terms what to
do with a response it doesn't like.

## No routing, fallback or model swapping

runbound opens a circuit and can narrow a run's posture. It never decides
which provider, endpoint or model handles a call.

**Costs you:** runbound will not fail a request over to a cheaper or faster
model, and will not retry a failed call against a different endpoint for you.

**Buys you:** your code stays the one place that decides where traffic goes.
A circuit breaker that also rerouted traffic would be making a business
decision — which provider, at what price, with what latency — that belongs
to the caller, not to a safety layer sitting in front of it. Keeping the two
separate means adding runbound to an agent never changes what provider a
request actually reaches.

**Use instead:** a gateway, load balancer or router in your own stack,
downstream of runbound's circuit state if you want it informed by that
signal.

## No LLM judge on the blocking path

No model call ever decides whether another call, or an action, is admitted.
Every admission check is a comparison over plain numbers, run before the call
it gates — see [Authorization before
reservation](../../INVARIANTS.md#authorization-before-reservation).

**Costs you:** runbound cannot make a semantic judgment call — "does this
refund request look reasonable" is outside what it will ever decide.

**Buys you:** an admission check that is fast, deterministic and never itself
the reason a call fails. A judge model on the blocking path adds its own
latency, its own cost, its own outage, and its own non-determinism to the
exact moment you need a control to be reliable — the same call could be
allowed one run and refused the next, for a reason nobody can reproduce.
Runbound's checks give the same answer every time, from the same numbers.

**Use instead:** an evaluation or review process that runs off the blocking
path — asynchronously, on a sample, or in a human queue — for judgment calls
that genuinely need one.

## No policy files

There is no YAML, no DSL and no CLI for expressing what an agent may do.
Class rules and tool policy are written where the agent's other logic
already lives: in code, on `init()` and `@runbound.tool` — see [Action
policy](../guides/policy.md).

**Costs you:** no declarative rules language to hand a non-engineer, and no
local policy-editing UI.

**Buys you:** a policy that is type-checked, testable and reviewed the same
way the rest of the agent's code is — a typo in a YAML key fails silently at
3am; a typo in a Python keyword argument fails at import time, in your own
tests. It also means there is no second file format, and no second parser, to
secure and keep in sync with the code it is supposed to govern. A connected
control plane can still deliver policy centrally, but it arrives the same
way local policy does — merged, never replacing what the code already
states, and only ever tightening it.

**Use instead:** write the rule where the tool is declared. If policy needs
to be authored outside the codebase, keep that authoring tool's output data
your own code merges in — never a file runbound itself parses and trusts.

---

None of the above is missing. Each is a boundary this SDK holds on purpose,
because holding it is what makes the rest of what runbound does —
deterministic, explainable, and safe to run on the same thread as the agent
it guards — true.
