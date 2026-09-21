---
name: Feature request
about: Propose a control, a knob, or a change to how runbound behaves
title: ""
labels: enhancement
---

**Before you paste anything below: never paste an API key, a token, a
prompt, a reply, or a real session key into this issue.** Redact or replace
them — issues here are public.

## The problem

What is your agent doing that runbound cannot currently control or explain?
A real (redacted) scenario is more useful than an abstract one.

## What you have tried

Which existing knobs, if any, come close — `init()` keywords, `@runbound.tool`
policy, a control plane setting — and where they fall short.

## What you are proposing

The shape of the change, if you have one in mind. It does not need to be a
finished API; a sketch is enough to start the conversation.

## Why it belongs here, not in your own code

runbound draws a deliberate line around what it will and will not do — see
[Boundaries](docs/concepts/boundaries.md). If your proposal reads content
(a prompt, a reply, a tool result) to decide anything, say so up front; that
is the kind of request most likely to be declined, and it saves everyone
time to know early.

## Environment (if relevant)

- runbound version: `python -c "import runbound; print(runbound.__version__)"`
- Is a control plane connected?
