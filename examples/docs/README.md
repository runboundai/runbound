# examples/docs/

Every code block on the sales pages' docs tab is one file in this directory,
not text typed by hand into a `.tsx` file. `tests/test_docs_examples.py`
runs every file here (as a subprocess, offline) as part of the normal SDK
test suite, so a snippet that no longer matches the SDK fails CI instead of
drifting silently. A companion generator script extracts each file's marked
region at build time and the docs tab renders that text verbatim.

## The marker format

```python
"""<One sentence: what the reader learns>. Shown on: <site path(s)>."""
<prelude: imports and offline setup the reader does not see>

# docs: <id>
<the code the site shows, verbatim, top-level, no leading indentation>
# /docs

<assertions: prove the documented behaviour happened>
```

- The filename stem, with `_` replaced by `-`, must equal `<id>`
  (`quickstart_init.py` → `quickstart-init`).
- Exactly one `# docs: <id>` line and one `# /docs` line; the id may carry a
  trailing `· needs token` for a snippet that only makes sense with a control
  plane configured.
- The region between the markers is copy-pasteable, real, top-level code —
  no `...` placeholders that would not run, no comments claiming an output
  the file does not also assert.
- Everything after `# /docs` proves the documented behaviour actually
  happened: an exception's type and its `.anomaly.detector` /
  `.violation.rule`, a number from `runbound.coverage()` /
  `session_status()` / `tool_calls()`, a WARNING captured with a
  `logging.Handler`, a callback having fired, a tool body having *not* run.
- `_offline.py` is shared scaffolding (an offline `openai.OpenAI()` /
  `openai.AsyncOpenAI()`, canned response bodies), not a snippet — the
  leading underscore excludes it from both the site's generator and the
  test runner's discovery.
- A file whose prelude needs an optional package declares it, e.g.
  `REQUIRES = ("openai", "httpx")`, read by the test via `ast` (never by
  importing the file) so it can skip cleanly, naming the package, when that
  package is not installed in the interpreter running the suite.

## Running one by hand

```bash
cd runbound-sdk
../.venv/bin/python examples/docs/quickstart_init.py
```

Every file exits `0` with no network and no API key. The full set runs as
part of the ordinary suite:

```bash
cd runbound-sdk
../.venv/bin/python -m pytest -q tests/test_docs_examples.py
```
