**Before you open this: never paste an API key, a token, a prompt, a reply,
or a real session key anywhere in this pull request** — the title, the
description, a commit message, or a diff. Redact or replace them; pull
requests here are public. See [CONTRIBUTING.md](CONTRIBUTING.md) for how a
change here actually lands (this repository is a release mirror).

## What this changes, and why

Describe the problem, not just the fix. What did not work, or what could
not be expressed, before this change?

## What you considered and rejected, if anything

Especially for a new control or a change in behavior — what alternative
shape did you rule out, and why?

## Tests

- [ ] A failing test came first (this project is developed test-first; see
      [CONTRIBUTING.md](CONTRIBUTING.md#making-a-change)).
- [ ] `python -m pytest -q tests` passes locally.
- [ ] Fail-open still holds for any new code path that can raise — see
      [INVARIANTS.md](INVARIANTS.md).

## Checklist

- [ ] No new runtime dependency (stdlib only; see
      [CONTRIBUTING.md](CONTRIBUTING.md#what-we-will-and-will-not-accept)).
- [ ] No content — a prompt, a reply, or a raw tool argument — is read to
      decide anything (see [Boundaries](docs/concepts/boundaries.md)).
- [ ] Docs updated if this changes a public keyword, an exception, or a
      guarantee.
- [ ] No API key, token, prompt, reply, or real session key appears
      anywhere in this diff.
