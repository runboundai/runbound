# Contributing

Thanks for taking the time to look at runbound's source.

## This repository is a release mirror

Development happens in a private monorepo that also holds Runbound AI's
control plane and its test suites, which exercise the SDK at the same
commit. This public repository is a **generated, squashed mirror** of the
SDK half only, published on each release from an explicit allowlist —
it is not the repository commits are made against directly.

That means a pull request opened here cannot be merged with a normal
`git merge` or rebase: **we port it by hand** into the private monorepo,
run it against the full test suite (including the control plane's), and
push it back out in the next release. You will be credited for the change
in the commit that lands it and in the release notes. This adds a step for
us, not for you — open the PR the way you normally would, against this
repository, against `main`.

## Filing an issue

Bug reports and feature requests are welcome. For a bug, please include:

- the runbound version (`python -c "import runbound; print(runbound.__version__)"`)
- Python version and OS
- a minimal reproduction — ideally something that runs offline, the way
  `examples/runaway_demo.py` does

## Making a change

1. Fork this repository and create a branch off `main`.
2. Write a failing test first, then the minimum code to make it pass. This
   project is developed test-first; a change with no test is unlikely to be
   accepted.
3. Run the full test suite before opening a pull request:

   ```bash
   python -m venv .venv
   .venv/bin/pip install -e ".[test]"
   .venv/bin/python -m pytest -q tests
   ```

4. Open the pull request with a description of *why*, not just *what* —
   what problem it solves, and what you considered and rejected, if
   anything.

## What we will and will not accept

- **No new runtime dependencies, ever.** This SDK is stdlib-only by design —
  it runs inside your production agent, and every dependency it could add is
  a dependency your agent inherits. `pytest` and the optional
  `langchain-core` extra are the only exceptions, and both are already
  declared in `pyproject.toml`. A change that needs a new import from PyPI
  to work will not be merged; if you think a case truly needs one, open an
  issue to discuss it before writing the code.
- **Fail-open, always.** Code that guards a customer's agent must never be
  the reason that agent goes down — see `INVARIANTS.md` and the "Guarantees
  and limitations" section of the README for what that promise covers today.
- Changes to the wire protocol between the SDK and a control plane
  (`POST /v1/hello`, `/v1/enter`, `/v1/trip`, `/v1/events`, `GET /v1/policy`,
  `POST /v1/clear`) need extra scrutiny, since a third party's server can
  implement this protocol too — breaking it silently breaks anyone who did.

## Adding a provider

One five-function wrapper module per provider, never a universal parser. The
five are the ones `runbound/wrappers/anthropic_wrapper.py` exposes —
`matches`, `is_wrapped`, `install`, `read_usage` and `read_tool_requests` —
plus the small readers that hang off them (`read_reasoning`, `read_cached`,
`read_cache_write`, the stream chunk readers). Providers change independently,
so each gets its own module that knows where *that* provider puts `create` and
what *that* provider calls its usage fields; a shared parser would make one
provider's rename everybody's bug. The `openai` and `anthropic` packages are
never imported — detection is duck-typing alone, and the SDK stays
dependency-free.

A new provider arrives as a module **plus recorded fixtures**. The fixtures are
recorded from the real provider SDK and replayed through the wrapper in a
private conformance kit (`runbound-conformance/`) in the development monorepo,
before any cell in this README's compatibility matrix is filled in — a
checkmark in that table means a test proves it, and "the shape looks right" is
not a test. Please open an issue before starting a new provider so we can agree
the surface and get the fixtures recorded.

## License

By contributing, you agree that your contribution is licensed under this
project's [MIT license](LICENSE).
