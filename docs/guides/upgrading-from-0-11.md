# Upgrading from 0.11.x

[← Docs](../README.md)

**Console tool rules are enforced only by runbound 1.x.** A worker running 0.x (0.11.2 is what
`pip install runbound` installs today, because 1.0 is a pre-release) ignores them. It keeps only
the rules in your code and the older organisation-wide policy document, and it does not tell you it
is doing so: a rule you set on a tool in the console (blocked, a session cap, an approval, a risk
class, a fail mode, a quota) has no effect on that worker.

To get console rules, move the worker to 1.x:

```bash
pip install --pre "runbound>=1.0.0a2"
```

What changed between the two lines, and how to move each 0.11.x call, is in the
[migration table in the changelog](../../CHANGELOG.md#migrating-from-011x). The short version:
1.x guards the tools an agent calls (`@runbound.tool`), not the model client, so the model-call
features of 0.11.x are gone.

**What the console will show.** Once the console's tool detail view ships (it is under way and is
not released yet), a service with a 0.x worker will say that worker "cannot enforce" console rules,
next to the rules it cannot honour. Until then the console shows no such warning, so check the
version your workers report (`runbound.__version__`, and the worker list in the console) before you
rely on a console rule.

A mixed fleet is not safe: a call that lands on a 0.x worker skips every console rule, even while the 1.x workers beside it enforce them.
