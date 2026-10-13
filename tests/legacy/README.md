# Legacy row fixtures

Today's SDK decision for per-tool policy rows, frozen (R0 legacy fixtures, `@pytest.mark.legacy`).

- `legacy_script.py` defines every case once and captures it: `python -m tests.legacy.legacy_script --write`
  (from `runbound-sdk/`). Regenerating is a decision to justify in a commit message, never a way to make a red
  test green.
- `fixtures/legacy_rows.json` is the capture: per case its inputs and, per call, the decision
  (`allowed` / `refused` / `deferred`), reason, detector, rule, boundary, `would_refuse:*` tags, the approval
  request raised (mode, timeout) and the `/v1/quota/take` calls and lease units used.
- `test_legacy_rows.py` re-runs each case on today's engine and compares.

Every case runs the real engine (a real `RemoteState` holding the rows, the real hook chain, quota and
approval modules); only the plane's HTTP answers and the clocks are fake. Nothing sleeps.

## Ids

`O-LEG-ROW-<n>`, never reused.

| Range | Group | What |
|---|---|---|
| 1-99 | `field` | every row field alone |
| 100-199 | `pair` | every row field folded with a code rule, `constraint=`, `require_approval=`/`approval=`, `init(capabilities=...)` |
| 200-299 | `document` | reads the org policy document served as `policy`; tagged `retires-with-H6` |
| 300-399 | `concrete` | rows the SDK's own tests feed straight in |
| 400-599 | `concrete` | rows other suites write at a plane (served as the plane would serve them) |
| 600-649 | `concrete` | bodies the plane refuses (400), fed to the SDK as if served, tagged `plane-rejects` |
| 650-699 | `concrete` | served-shape rows a plane test uses directly |

The concrete ranges are filled in the order of the lists in `legacy_script.py`: add new rows at the end of a
list, never in the middle.

## Where every concrete row came from

Each concrete case carries its `sources` (file:line) in the fixture. By origin:

- **SDK unit tests** (`SDK_FED`, 300-330): `tests/test_policy_rows.py` (every `row(...)` it feeds `FakeShared`
  or `policy_rows.parse`, including the malformed ones at :239-240) and `tests/test_spans.py:409`.
- **SDK real-plane suite** (`PLANE_SEEDED`): `tests/integration/test_rows_quota_plane.py`,
  `tests/integration/test_row_waits_off_loop.py`, `tests/integration/test_examples_plane.py`, and the row
  `examples/README.md` sets for `examples/tool_rules.py`.
- **Demo fleet** (`PLANE_SEEDED`, `DEMO_DOCUMENTS`): the A7 show's quota row (dry run, then enforced) and the
  fleet scorecard's org document policies (`deny` lists, enforced and dry run; these are `retires-with-H6`).
- **Chaos harness** (`PLANE_SEEDED`): the quota row the chaos scenarios create (limit 100 and 1000). The SDK's
  own `tests/chaos` seeds no rows.
- **Cross-tree suite** (`PLANE_SEEDED`): the fold cross-check's three rows.
- **Plane suite, console backend suite, console e2e** (`PLANE_SUITES`, `PLANE_REJECTED`, `PLANE_LITERAL`): every
  row created, PATCHed, rolled out or seeded into the rows table, captured in its final state (`draft` and
  `deleted` rows are not served: the SDK sees no row). Identical rows are merged into one case listing every
  source.

The stack scorecard writes no per-tool row of its own: its A7 beat is the show's quota row above, and its
other policy writes are the document policies above.
