# Security policy

## Reporting a vulnerability

Please report security issues privately, not as a public GitHub issue: email
**runboundai@gmail.com** with a description of the issue and, if you
have one, a minimal reproduction. That address is a stand-in until a
dedicated security contact exists; it is read by the same people either way.
We will acknowledge your report within a few business days and keep you
updated as we work on a fix.

We follow a **90-day disclosure window**: we ask that you give us 90 days
from the initial report to investigate and ship a fix before any public
disclosure, and we will tell you as soon as a fix is available so that
window can close early where it makes sense for everyone.

## Supported versions

| Version | Supported |
|---|---|
| Latest published release | Yes — security fixes are made against it |
| Anything older | No — there is no long-term support branch today |

`python -c "import runbound; print(runbound.__version__)"` prints the
version you have. If you are not on the latest release, upgrade before
filing anything that is not itself a security report.

## What "secure" means for this SDK

runbound runs inside your own process, on your own thread, and never sends
your prompts, replies, or tool arguments anywhere. Its telemetry — used only
when you connect a control plane — is deliberately content-minimizing: hashes
and counts, never content. Concretely, what leaves your process is tool
names, model names, call timing, counts (tokens, steps, calls), provider
error *classes* (never error text), and a salted, per-process hash of a
tool call's arguments used only to spot a repeat inside that one process. A
vulnerability that would make any of this reveal more than that — a prompt,
a reply, a raw tool argument, or a raw session key with `send_session_keys`
left at its default of `False` — is exactly the kind of report we want; see
the README's "What the SDK actually sees" and "Privacy" sections for the
full, checkable list of what is and is not read, stored or sent.

## Threat model

This section states what the SDK's own code actually does, not an
aspiration. Where a claim below matters to your own security review, the
module named after it is the place to check it yourself.

### What runbound trusts

The host process, by design — runbound runs on the same thread as the agent
it guards, with no sandbox between them, because a sandbox boundary there
would be a second thing that could fail open or closed independently of the
agent it is meant to protect. It does not trust the network: every call to
a connected control plane is wrapped so that a slow, wrong, or hostile
reply degrades to local enforcement rather than blocking or crashing the
host (`runbound/shared.py`'s `RemoteState`). It does not trust its own
detectors, pricing tables, or hashing to be bug-free either — every
internal failure is caught and logged rather than allowed to take the
host down; see [Guarantees and
limitations](docs/reference/guarantees.md#guarantees-and-limitations).

### What it hashes, and with what salt

Two different digests exist, for two different purposes, and they are not
interchangeable:

- **A session key** (`runbound.key_hash`, `runbound/plane_types.py`) is a
  plain SHA-256 hex digest of the key string. Not salted — it needs to be
  the same value on every worker in your fleet so a control plane can
  recognize the same key twice, which a per-process salt would prevent. It
  is a one-way digest, not encryption: do not treat it as safe to publish
  even though it hides the literal key value, and prefer a key you would not
  mind appearing as a hash in a log (a stable opaque id, not an email
  address) if that matters to you.
- **A tool call's arguments** (`_args_hash` in `runbound/api.py`) are hashed
  with a random salt generated once per process (`_HASH_SALT`) and mixed
  into the digest before it is taken. This is deliberate: it makes the
  digest an equality check for "the same call repeated inside this one
  process," and nothing more — the same call hashes differently on two
  workers, and differently again after a restart, so a leaked digest cannot
  be used to correlate calls across your fleet, and the raw arguments
  themselves never leave the process either way. See [Salted hashes and
  correlation](INVARIANTS.md#salted-hashes-and-correlation).

### What leaves the process, and only when

With no `token` and no `control_plane_url` set — the default — `plane_mode`
is `"off"` and nothing about your agent's execution leaves the process,
ever (`runbound/config.py`'s `_normalize_connection`). Connecting requires
one of two explicit choices: a `token` (hosted) or a `control_plane_url`
(self-hosted, with `token` required unless the url itself needs no auth).
Once connected, what can leave is: heartbeat metadata (service and worker
identity, SDK version, policy/controls versions seen, circuit states,
active session counts, coverage, whether this worker can enforce a fleet
stop), session entry/exit deltas keyed by the session's `key_hash` (never
the raw key, unless you set `send_session_keys=True`), and, while
`export_events` is on (the default once connected), the event stream
described above — counts, hashes, prices, never content. Turning off
`export_events` stops the event stream specifically; turning off the
connection entirely (`plane_mode="off"`) stops all of it.

### How plane webhooks are signed, and how a reader verifies them

A control plane's outbound webhook delivery is HMAC-SHA256 signed. The
header value is **`"sha256=" + HMAC-SHA256(secret, "<timestamp>." + <raw
body bytes>).hexdigest()`** — the `sha256=` prefix is part of the value, not
a label around it, so a hand-rolled verifier that computes the bare hex
digest and compares it directly will reject every genuine delivery.
`runbound.alerts.verify_webhook_signature` (also
`runbound.verify_webhook_signature`) is the public helper a receiver calls
to check one instead of reimplementing the scheme:

```python
import runbound

if not runbound.verify_webhook_signature(
    secret=WEBHOOK_SECRET,
    timestamp=request.headers["X-Runbound-Timestamp"],
    body_bytes=request.get_data(),   # raw bytes, not the re-serialized JSON
    signature=request.headers["X-Runbound-Signature"],   # the full "sha256=..." value
):
    return "bad signature", 400
```

It compares in constant time (`hmac.compare_digest`) and rejects a
timestamp more than `WEBHOOK_TOLERANCE_SECONDS` (300 seconds, five minutes)
from the receiver's clock, in either direction, before it even compares the
signature, so a captured delivery cannot be replayed later once its
timestamp has aged out. A verifier written against this page without the
helper needs both facts to work: the `sha256=` prefix, and the 300-second
window. Verify the raw body bytes exactly as received — re-serializing the
parsed JSON will not reproduce the bytes the signature was computed over,
and the check will fail even on a genuine delivery.

### What a compromised plane could, and could not, make a worker do

Local and central configuration merge **tighten-only** — one function, the
same rule everywhere a plane directive reaches a worker (`runbound/
controls_merge.py`, `runbound/posture.py`'s `tighten`). That rule is the
whole answer here: a control plane, compromised or merely misconfigured,
can only make a worker's runtime narrower than the code already configured
it to be, never wider.

**It could:** narrow a worker's posture, including down to `stopped`;
issue a fleet-wide halt (`Stop` or `Narrow`); deliver `Controls` that
tighten a budget, a step count, or a capability rule further than the code
states; and, in the worst case, deny service to your own fleet by narrowing
or halting it for no good reason — a denial-of-service against your own
agents is the ceiling of what a hostile plane reply can do. That ceiling is
itself bounded: `stale_halt="release"` (the default) drops an enforced halt
60 seconds after the last successful contact, so a plane that goes quiet —
compromised, crashed, or merely disconnected — cannot hold your fleet
stopped forever unless you explicitly chose `stale_halt="hold"` and accepted
that trade.

**It could not:** loosen a local budget, capability rule, or any other
control the code already set tighter — the merge has no code path for that
direction; make the SDK read, log, or forward a prompt, a reply, or a raw
tool argument — the SDK has no such field to fill in even under a plane's
instruction, since no core decision path is wired to content at all (see
[Boundaries](docs/concepts/boundaries.md)); or impersonate your own workers
to a third party — a worker only ever calls out to the plane url and token
your own configuration named, never the reverse.
