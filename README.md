# runbound

**Runtime controls for autonomous AI agents.** runbound runs inside your
agent's process and stops runaway cost, loops, tool abuse and provider
failures deterministically, by counting, timing and hashing, never by asking a
model whether something looks wrong. It reads no prompts and no replies, it
puts no gateway in your hot path, and it never speaks to your users: when it
stops something it hands control back to you and you answer in your own voice.

This is for the platform team that owns AI agents in production, the people
who get paged when one runs away, not the people who wrote its prompt.

One agent burned **$2,847 in four hours** on a refactoring loop while every
monitoring dashboard stayed green. Nobody was watching it at 3am, and nothing
in the stack had the authority to pull the plug. runbound is the layer with
the authority to pull the plug, and the proof that it did — the whole story is
in [Why this exists](docs/concepts/why.md).

## Install

```bash
pip install runbound
```

There are no runtime dependencies — stdlib only. LangChain support is the one
extra: `pip install "runbound[langchain]"`.

## Quick start

Guard every model call in the process:

```python
import runbound
runbound.init(budget_usd=5.0, on_anomaly="raise")   # everything below is now guarded
```

Scope every control to one unit of work:

```python
with runbound.session(f"run:{run_id}"):
    answer = client.chat.completions.create(...)
```

Refuse an action before its function body runs:

```python
@runbound.tool(max_calls=1)            # the rule lives on the tool, in the same diff
def issue_refund(user: str, amount: float): ...
```

The full five-minute version — wiring all three sensors, checking what is
actually guarded, catching the trip, and the offline demos that need no API
key — is in [Getting started](docs/getting-started.md).

## Three levels of protection

You choose how far in to go. Each level is a small, separate step, and most of
the value arrives at the first one. The full text is in
[Three levels of protection](docs/concepts/three-levels.md).

**Level 1: guard the model calls.** Two lines. `runbound.init()` wraps the
OpenAI and Anthropic clients for you (or you call `runbound.wrap(client)` on
any client with that shape). From then on every model call is counted, timed
and priced, and you get: dollar and token budgets, per-call caps, velocity
limits, step and wall-clock limits, the spike ladder, error-storm detection
with a circuit per provider, and refusal profiles that carry your own status
and sentence. All of that is what the free SDK does on its own, with no
network call and no account. Nothing is decorated, no policy is written. See
[What it controls](docs/concepts/what-it-controls.md).

**Level 2: one session per run.** One line per unit of work. Wrap each run in
`runbound.session(key)` — the key is any id you already have: a run id, a job,
a tenant, a customer — and every control above becomes per key instead of per
process: this run's budget, this run's ladder, this run stopped, every other
caller untouched. See [Runs keyed by any id](docs/guides/runs.md).

**Level 3: refuse actions before they run.** Only for the tools that matter.
Decorate the two or three functions that are irreversible or reach the outside
world with `@runbound.tool`, write a policy (deny this tool, at most once per
run, needs approval, or a predicate of your own over the arguments, run in
your process), and the call is refused *before the function body runs*. The
rules are yours; runbound enforces and records them, it never judges the
action. See [Action policy](docs/guides/policy.md).

Nothing here protects against a *bad answer*: hallucinations, prompt injection
and output quality are out of scope by design. "The agent said something
wrong" is a different product. "The agent would not stop" is this one.

## What it never sees

- **Prompts and model replies are never read, stored, or sent** — except the
  one narrow case of `estimate_tokens=True`, which counts characters when the
  server reported no usage.
- **Tool arguments are sha256-hashed before storage, salted per process.** The
  loop detector compares digests of `(tool_name, args, sorted kwargs)` mixed
  with a random salt generated once at import, never the arguments themselves.
- **Raw arguments never leave your process.** They are not stored, not logged,
  and not included in any alert payload.
- **A session key reaches a connected plane as a hash.** The plane gets
  `sha256(key)` and nothing else, unless you opt in with
  `send_session_keys=True`.
- **Failures are recorded as the exception's class name.** Only the
  exception's class name, never its message, is what reaches the plane.
- **No network calls except the ones you configure.** No telemetry, no
  phone-home, no hosted backend you did not point us at. With no `token` and
  no `control_plane_url` set, runbound opens no sockets at all.

The full accounting — every number, where it comes from, and what goes blind
without each sensor — is in
[What the SDK actually sees](docs/concepts/what-it-sees.md) and
[Privacy](docs/reference/privacy.md).

## Documentation

The manual lives in [`docs/`](docs/README.md); it is also published, with
executed snippets, at [runbound.co/docs](https://runbound.co/docs).

- **[Getting started](docs/getting-started.md)** — install, wire the three
  sensors around one agent run, and check what is actually guarded.
- **[Concepts](docs/README.md#concepts)** — the three levels, why this exists,
  how it works, what it controls, and exactly what the SDK sees and never
  sees.
- **[Guides](docs/README.md#guides)** — keyed runs, fleet mode, spike
  detection, action policy, self-hosted models, LangChain, async and
  streaming.
- **[Reference](docs/README.md#reference)** — every configuration field, the
  detectors, what happens when something trips, the circuit breaker, limits,
  the compatibility matrix, privacy, and the guarantees.

[Roadmap](docs/roadmap.md) is what is shipped, what is still open, and what is
explicitly out of scope.

## Getting help

**Something not working, or a question?** Open an issue at
[github.com/runboundai/runbound/issues](https://github.com/runboundai/runbound/issues),
or email **runboundai@gmail.com** if it is not something you can post in
public.

The two things that let us answer on the first reply instead of the third:

```python
import runbound
print(runbound.__version__)
print(runbound.coverage())   # after init() and your first guarded call
```

`coverage()` is plain numbers and names — which provider classes were
patched, how many calls and tools runbound saw, which imported providers
nothing is guarding, and any silent-zero warning — so it shows at a glance
whether runbound can see your traffic at all, which is the most common
cause of "it isn't catching anything". It lists your decorated tools'
names; remove any you would rather not post. Please also say your Python
version, your `openai` / `anthropic` versions, and whether you run with a
control plane (`token` / `control_plane_url`) or locally.

**Never paste** an API key, a control-plane token, a prompt, a reply, or a
real session key into an issue.

**Security issues go to email, not to an issue** — see
[SECURITY.md](SECURITY.md) for what to send and the disclosure window.

## License

MIT — see [LICENSE](LICENSE). Release history is in
[CHANGELOG.md](CHANGELOG.md). The promises above, stated as bounds with the
tests that assert them (and where nothing does yet), are in
[INVARIANTS.md](INVARIANTS.md).
