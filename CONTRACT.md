# The enforcement floor

Draft, R0 (tool rules). Not part of the public mirror until Apollo says so. Source: `docs/product/tool-rules-tpd.md`
Draft 5.3 §4d.

An SDK version that the tool-rules contract covers must meet four lines. Their ids and wording are fixed (they are
quoted verbatim by the capability matrix):

* **F1** enforces halt
* **F2** refuses governed calls under a policy it cannot interpret safely
* **F3** executes no governed call before its first successful handshake or sealed-cache load
* **F4** reports its policy_features on hello

Reporting an incompatibility is not preventing execution: for a version that does not meet a line, the plane cannot
guarantee enforcement through the protocol, and the deployment needs an upgrade, isolation or an external admission
mechanism.

## Where each version stands

Each cell is **MEETS**, **MEETS WITH CONDITION** or **DOES NOT MEET**, and cites the file and line (at that tag) or the
test that proves it. Probe output is `tests/contract/floor_probe.py` run on a `git archive` of the tag; the tests are on
this branch and run against main. "Governed call" is read here as a call to a tool the plane has a rule for.

Sources: `v0.11.2` = f391e3c, `v1.0.0a1` = b347ac8 (built, never published), `v1.0.0a2` = dff8d82 (the local tag in this
repo), main = TR2-O2 (branches `tr2-sdk-floor` then `tr2-sdk-lines`, base 051efc38).

| | F1 | F2 | F3 | F4 |
|---|---|---|---|---|
| **0.11.2** | **MEETS WITH CONDITION:** enforced only inside a `runbound.session(key)` block and only with `on_halt="raise"` (the default, `config.py:501`); a bare `@tool` call outside any session block is not stopped, and `"warn"` lets even a session call through (probe `L1`: bare RAN, in-session REFUSED with `raise`, in-session RAN with `warn`). The halt is read at the session door (`api.py:1066-1082`, `engine.py:1737`). | **DOES NOT MEET.** An org policy that cannot be merged is dropped and the local policy alone applies (`engine.py:2441-2446`); probe `L2`: the same document that refuses the tool when every key is known lets it run with one unknown key. There are no per-tool rows in this version. | **DOES NOT MEET.** There is no gate and no cache (no `plane_refusal`, no `cache.py`); probe `L3`: a destructive and an unclassified tool run before any hello. | **DOES NOT MEET.** `git grep policy_features v0.11.2` finds nothing; the hello payload (`shared.py:1799`) has no such field. |
| **1.0.0a1** | **MEETS WITH CONDITION:** the default `on_halt="raise"` (`config.py:81`) refuses every tool (`gates.py:26-55`, called at `engine.py:115`); `on_halt="warn"` logs it and lets the call through (`gates.py:43-50`; probe `L1`). | **DOES NOT MEET.** A malformed row is skipped, so its tool runs unruled (`policy_rows.py:85`); a row key the SDK does not know is ignored (`policy_rows.py:63-85`); an org document that cannot be merged is dropped (`engine.py:294`). Probe `L2`: all three run the tool, where the well-formed controls refuse it. | **MEETS WITH CONDITION:** financial, destructive, privileged and unclassified tools are refused until the first hello or a cache confirmed within 15 min (`shared.py:237-258`, `cache.py:46`, `hooks.py:365`, door at `gates.py:59-67`). read, write and external tools run on code rules alone (probe `L3`). | **DOES NOT MEET.** No `policy_features` anywhere in the tree; the hello (`shared.py:470`) sends `sdk_version` and `policy_version_seen`. |
| **1.0.0a2** | **MEETS WITH CONDITION:** same as a1 (`config.py:82`, `gates.py:43`, `engine.py:130`). | **DOES NOT MEET:** same as a1 (`policy_rows.py:85`, `engine.py:309`; probe `L2`). | **MEETS WITH CONDITION:** same as a1 (`shared.py:235`, `cache.py:46`, `engine.py:133`; probe `L3`). | **DOES NOT MEET** (`shared.py:477`). |
| **main** (TR2-O2) | **MEETS.** A served halt refuses every tool at the tool boundary whatever `on_halt` says (`gates.py` `halt`). Tests: `O-FLR-1a` and `O-FLR-1` (`on_halt="warn"`) pass; `tests/test_wall_sdk.py::test_on_halt_warn_logs_and_still_refuses_the_tool`. | **MEETS.** A row the SDK cannot read, a row with a key it does not know, or a body the plane's validator would refuse, refuses its own tool with `policy_unsupported`; other tools are unaffected (`policy_rows.parse`, `_row_check.py`). Tests: `O-FLR-2`, `O-FLR-2b`, `O-FLR-3`, `O-FLR-3a`, `tests/contract/test_plane_rejected_rows.py` (the 21 bodies once O-LEG-ROW-600..620), `tests/contract/test_row_check_matches_plane.py`. The hello reports the list as `policy_unsupported` (`tests/contract/test_lines_wire.py`). | **MEETS.** Until its first answered hello or sealed-cache load a worker runs no governed call: `read`, `write` and `external` refuse `policy_unavailable`, the consequential classes and unclassified `plane_unavailable` (`gates.py` `plane`). Tests: `O-FLR-4` (x3), `O-FLR-4a`, `O-FLR-4b`. | **MEETS.** Every hello carries `policy_features` (`lines`, `halt_tools_v1`) and `policy_unsupported`, and the reported set is the implemented set (`runbound/lines.py` `FEATURES`). Tests: `tests/contract/test_lines_wire.py`. |

## Floor gap, closed: a read error that erased the rows (register C12)

Against F2 ("never resolves to allow"), in 1.0.0a1, 1.0.0a2 and main up to the C12 hotfix: when the plane could not read
its ledger it answered `/v1/policy` with `rows: []` and `rows_hash: ""`, and `RemoteState._fetch_policy` replaced every
held row with nothing and wrote that to the cache, so every console rule was dropped and the drop survived a restart.
Fixed in the SDK: a `rows_hash` of `""` is not an answer, so nothing is applied and no cache is written (tests `O-FLR-5`,
`O-FLR-5a-d`, now passing; the legacy twin `O-LEG-FLR-1` is retired). 1.0.0a2 and earlier still have the gap, and a plane
that serves the marker to such a worker still erases its rows. 0.11.2 has no rows to lose.

## The policy states and the validity window (TR2-O1)

Every decision is made in one of three states of the policy the worker holds (TPD §5.7), a function of `now` alone: **fresh** (a hello
answered within the last two poll intervals, at least 30 s), **cached** (the policy held was sealed within the validity window) and
**unavailable** (no policy, or the one held is older than the window). The **validity window is 24 hours** (`cache.STALE_AFTER_S`).

Which rule a service gets follows the service (Apollo's ruling on TR2-O1, TPD §5.5 "existing services keep today's behaviour until an operator
adopts defaults"):

* A service **without enforced service defaults** (every service until TR2-O3 and an operator's adoption) keeps today's per-class cache trust,
  unchanged (`consequential_grace_s`, the 15-minute split; the legacy fixtures O-LEG-STA stay as captured). Three tightenings stand on top: no
  governed call before a first handshake or sealed-cache load (F3); a **bare `write`** counts as unclassified for fail mode (K8), so it refuses
  `plane_unavailable` when the state is *unavailable*; a tool the *expired* policy blocked or asked a person about refuses
  `policy_expired_restricted` (K7).
* The **single window** (fresh and cached enforce the sealed policy in full and fail mode applies only when unavailable) applies only to a sealed
  policy that carries enforced service defaults (`defaults_v1`, TR2-O3). From TR2-O3 the twelve O-STA-ENG cases that expect it pass with enforced defaults
  (`test_the_engine_does_what_the_state_contract_says_with_enforced_defaults`).

Tests: `tests/test_cache.py`, `tests/contract/test_states_engine.py`, `tests/chaos/test_flapping_plane.py`.

## Conditions, operators and repetition (TR3-O)

`runbound/_rules/conditions.py` implements every operator of TPD §5.2, three-valued: `tests/contract/test_operators.py` (about 100 cases, ids O-OP-n,
also written to `fixtures/operators.json` for the plane's vendored copy). An unknown never reads as false (K1 is unchanged in the evaluator). On the
call path a row with a condition is decided anew for each call (`policy_rows.resolve_call`) from the call's final arguments and the context; tests:
`tests/test_conditions.py`.

## How to read the conditions

* **F1.** From TR2-O1 on, `on_halt` changes nothing: a halt refuses every tool. For earlier SDKs a deployment meets the floor only if it
  leaves `on_halt` at its default `"raise"` (and, on 0.11.2, only for calls made inside a `runbound.session(...)` block).
* **F3.** From TR2-O1 on the gate covers every class. Before that it covered only the classes that fail closed.
* **F4.** No SDK before TR2-O2 reports `policy_features`. For the plane, that makes every one of them an
  *unverified* worker (TPD §4d), never a supported one.

## Reproducing

```
git archive <tag-or-sha> runbound-sdk/runbound | tar -x -C /tmp/probe-<tag>
python runbound-sdk/tests/contract/floor_probe.py /tmp/probe-<tag>/runbound-sdk
python -m pytest runbound-sdk/tests/contract/test_enforcement_floor.py --runxfail   # the failures are the findings
```
