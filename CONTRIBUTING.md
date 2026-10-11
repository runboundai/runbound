# Contributing

Thanks for taking the time to look at runbound's source.

## This repository is a release mirror

Development happens in a private monorepo that also holds Runbound AI's
control plane and its test suites, which exercise the SDK at the same
commit. This public repository is a **generated, squashed mirror** of the
SDK half only, published on each release — it is not the repository commits
are made against directly.

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
- a minimal reproduction, ideally one that runs offline: with no
  `control_plane_url` (and no `RUNBOUND_PLANE_URL`), `runbound.init()` makes no
  control-plane call

Security issues go to the address in [SECURITY.md](SECURITY.md), not to an issue.

## Running the tests

The suite runs on Python 3.10, 3.12 and 3.14 (`requires-python = ">=3.10"`):

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q tests
```

The OpenTelemetry tests skip unless the `otel` extra is installed
(`pip install -e ".[test,otel]"`); the LangChain tests use a fake
`langchain_core` and need nothing extra.

Tests marked `plane` start a real control-plane process on a throwaway SQLite
file (see `tests/integration/test_real_plane.py`). They skip unless three
variables name it:

```bash
RUNBOUND_PLANE_PYTHON=/path/to/plane/venv/bin/python \
RUNBOUND_PLANE_DIR=/path/to/plane/checkout \
RUNBOUND_PLANE_APP=<module>:<factory> \
    .venv/bin/python -m pytest -q -m plane tests/integration
```

`RUNBOUND_PLANE_PYTHON` is an interpreter that can import the plane's app and
`uvicorn`; it lives in its own environment, not the one under test.

Working in the monorepo, also run the public-tree lint from the repository
root; it must report 0 violations:

```bash
python3 scripts/lint_public_tree.py runbound-sdk/
```

## Making a change

1. Fork this repository and create a branch off `main`.
2. Write a failing test first, then the minimum code to make it pass. A
   change with no test is unlikely to be accepted.
3. **Spot-break each new test**: break the line it protects, watch the test
   fail, restore the line. A test that cannot fail proves nothing.
4. Run the full suite, then open the pull request with a description of
   *why*, not just *what*: the problem it solves, and what you considered and
   rejected.

## Rules of the tree

- **Stdlib only at runtime.** `dependencies = []` in `pyproject.toml` stays
  empty: the SDK runs inside your production agent, and every dependency it
  added would be yours too. The extras are exactly `otel`, `langchain` and
  `test`, and the runtime imports `opentelemetry` lazily. A change that needs a
  new import from PyPI to work will not be merged; open an issue first if you
  think a case truly needs one.
- **Observers fail open; built-in checks fail closed.** Anything that only
  watches a call (the event exporter, the span emitter, the `on_event`
  callback) catches its own errors and never changes what the call returns.
  A built-in check that raises (argument validation, tool policy, capability
  rules) refuses the call for every class, and redaction that raises
  withholds the output. User hooks follow the tool's class: a failing hook
  refuses a `financial`, `destructive`, `privileged` or unclassified tool, and
  is logged and skipped for `read`, `write` and `external`. See
  [INVARIANTS.md](INVARIANTS.md) and
  [Guarantees](docs/reference/guarantees.md).
- **No new global state.** Do not add module-level mutable state; keep it on
  the engine, the session or the plane link, so `runbound.reset()` still puts
  the process back where it started.
- **Every behaviour change updates [API.md](API.md) and
  [CHANGELOG.md](CHANGELOG.md), and gets a test.** `tests/test_public_api.py`
  checks API.md against `runbound.__all__`.
- **The wire protocol needs extra care.** `POST /v1/hello`, `GET /v1/policy`,
  `POST /v1/events`, the approval routes (`/v1/approvals`,
  `/v1/approvals/{id}`, `/v1/approvals/{id}/report`) and
  `POST /v1/quota/take` can be implemented by a third party's server too;
  changing them silently breaks anyone who did. Nothing new may carry an
  argument, a result or exception text without a line in
  [SECURITY.md](SECURITY.md) saying so.

## Docs conventions

- Plain, precise and short. Show the refusal reasons and defaults that really
  exist; verify each claim against the code or a test, or leave it out.
- `tests/test_docs_banned_words.py` keeps the vocabulary of the removed 0.x
  product out of README.md, `docs/` and `examples/`. Only CHANGELOG.md may
  name what was removed.
- Every `python` code block in the docs must parse, and use only names that
  exist: `runbound.<name>` must be in `runbound.__all__` or be a documented
  submodule attribute.
- Link only to pages that exist, with relative paths.

## License

By contributing, you agree that your contribution is licensed under this
project's [MIT license](LICENSE).
