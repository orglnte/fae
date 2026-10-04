# tests/ — unit suite for the orchestrator's Python

Run from the repo root:

    python3 -m unittest discover -s tests -v

Stdlib `unittest`, deliberately: `pytest` is not installed on this host and
installing it is plan-gated. Nothing here needs a third-party runner.

## Safety rule — these tests never touch the live fleet

The current Experiment (`fae.shared.current()`) names the workspace
(`workspaces.nosync/`) and the scheduling plane (`.queues/`, `.conduct/`,
`.locks/`, `transitions.log`; `fae/plane.py`), and the fleet reads those paths
while it runs. A test that wrote a pause file or a queue line
under them would act on live cells.

So: importing `runs` is safe (import is side-effect free — path construction,
no writes), but **any test that
exercises a function which writes must redirect the target into a
`TemporaryDirectory` first**, by passing an explicit path or by making a temp tree the current
Experiment's (`_ctx.use_workspace` / `at_workspace` / `patch_plane`, or
`OrchTmpCase`). No test in this suite reads or writes the real
workspace tree.

## What is covered

| file | variant |
|---|---|
| `test_cell_id.py` | `cell_id` / `parse_cell_id` — the id format, round-trips, rejection of retired arm tokens |
| `test_seed_doc.py` | `EXPECTED_SEED_DOC` / `resolve_seed_doc` — the study's independent variable |
| `test_mount_logic.py` | `mount_counts_seen`, `never_mounted`, `mounts_matched` — the taint discriminators |
| `test_ledger.py` | `fae/cell/ledger.py` `parse` / `hist` — the single verdict derivation |
| `test_helpers.py` | `_tail_hist`, `never_started`, `parse_reset` |
| `test_fs_lock.py` | `fs_lock` — the filesystem mutex, including the stale-holder steal path |

Not covered (needs docker, live processes, or a real workspace tree):
`cell_state`, `render`, `find_zombies`, `reconcile`, `worker`, the `exp1`
shape gate. Those are exercised by `cli.py experiment check` instead.

## Known failures

None. Every test passes as of 2026-08-24.

Tests that pin an accepted defect rather than a fixed one say so in their name
and docstring (e.g. `test_empty_effort_id_is_unparseable_BY_DECISION`): the
behaviour is known, guarded elsewhere, and deliberately not changed.

## Tests must never start a cell

`cli.resume()`, `Conduct.respawn()` and `Conduct.launch()` Popen `python3 -m
fae.cell`. A test that reaches them launches real, paid agent runs against
whatever workspace tree it is pointed at — this has happened once during
development. Fixtures that touch those paths stub `Conduct.respawn` and
`Conduct._spawn`, and `Cell.prestart_clean` (it removes the cell's agent
container).

Patch `TRANSITIONS_LOG` too, not just `WS` and the plane folders: `common.cell`
hands Cell the module-level path, so an unpatched test writes fabricated
transitions into the live conformance log that `experiment check --tla-trace` replays against
`.tla/Runs.tla`.
