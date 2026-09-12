# Security policy

## Reporting a vulnerability

Please report security issues privately, not as a public GitHub issue: email
**runboundai@gmail.com** with a description of the issue and, if you
have one, a minimal reproduction. We will acknowledge your report within a
few business days and keep you updated as we work on a fix.

We follow a **90-day disclosure window**: we ask that you give us 90 days
from the initial report to investigate and ship a fix before any public
disclosure, and we will tell you as soon as a fix is available so that
window can close early where it makes sense for everyone.

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

## Supported versions

Security fixes are made against the latest published release. There is no
long-term support branch for older versions today.
