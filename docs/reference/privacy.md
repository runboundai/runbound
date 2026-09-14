# Privacy

[← Docs](../README.md)

Telemetry here is **content-minimizing, not content-free** — say plainly what
it still reveals rather than call it "safe" and leave you to find out. What it
reveals: tool names, model names, call timing, counts (tokens, steps, calls),
estimated dollars, provider error classes, salted argument-equality hashes
(below), session-key hashes, your `tags`, detector messages with the key
redacted, and the [tool report](../guides/fleet-mode.md#what-we-send--hashes-and-counts-never-content)
(parameter names and annotations, module, a docstring's first line). What it
never reveals: prompts, replies, tool arguments themselves, or error text.

- **Tool arguments are sha256-hashed before storage, salted per process.** The
  loop detector compares digests of `(tool_name, args, sorted kwargs)` mixed
  with a random salt generated once at import, never the arguments themselves.
  Keyword order does not change the digest, but the salt does: the same call
  hashes differently in a different process, so `args_hash` is an equality
  token for spotting a repeat inside this process, not a stable fingerprint
  someone could use to correlate calls across your fleet from the hash alone.
- **Raw arguments never leave your process.** They are not stored, not logged,
  and not included in any alert payload. An [action
  policy](#action-policy--rules-for-what-your-agent-may-do) hands them to your
  own constraint and approval callbacks for the duration of that call and
  nothing more; a policy violation records the tool and the rule, never the
  arguments that broke it.
- **Session keys and tags are stored as you wrote them in your own process;
  a key reaches a connected plane as a hash, tags as written.** Detectors name the key untouched in
  their own local `message` and `details`, so a key session with an id you
  are willing to see in your own logs, not with an email address. The plane —
  hosted or self-hosted, the only destination the SDK sends to any more — gets
  `sha256(key)` and nothing else, unless you opt in with
  `send_session_keys=True`. By default the plane's own alert adapters (Slack,
  PagerDuty, a webhook) read only that hash back off the ledger too. Opt in
  and the raw key can reach a delivery — through your own `link_template`'s
  `{key}`, or a detector's own `message`/`details`, unredacted — because it
  is now sitting in the ledger for the plane to pass along exactly as told.
- **No network calls except the ones you configure.** No telemetry, no
  phone-home, no hosted backend you did not point us at. With no `token` and
  no `control_plane_url` set, runbound opens no sockets at all. With
  either, it sends hashes and counts on a fixed, published list of fields —
  [what we send](../guides/fleet-mode.md#what-we-send--hashes-and-counts-never-content) — and never
  prompts, replies, tool arguments or error messages. Slack, PagerDuty,
  Opsgenie and a signed webhook are the control plane's job now (0.3.0);
  the SDK never opens a socket to any of them itself.
- Exception messages from a failing tool are truncated to 500 characters and
  kept in-process for the event record; only the exception's class name, never
  its message, is what reaches the plane.

---
