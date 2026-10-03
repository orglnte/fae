# What the scoring writes

`fae/scoring/score_cell.py` writes one record per sealed cell,
`<workspaces>/<cell_id>/score.json`; `fae/scoring/aggregate.py` reads every
record and writes `<workspaces>/results.csv` (one row per cell) and
`<workspaces>/results.json` (the rows plus a summary block). All three are
outputs, gitignored, and re-runnable: a record older than any of its inputs
makes `aggregate` refuse rather than publish a stale table.

## Per-cell record (`<cell_id>/score.json`)

| field | type | meaning |
|-------|------|---------|
| `cell_id` | str | `<agent>_<effort>[_smoke]_<variant>_<task>_r<rep>` |
| `model` | str | the provider's model id the agent ran (`cell.env` `AGENT_MODEL`) |
| `task` / `variant` / `repeat` | str/int | the cell's coordinates, from `cell.env` |
| `factors` | obj | the factors the variant is a level of, from its file (`{}` when it declares none) |
| `impl` | str | which cell driver ran it |
| `attempt_budget` | int/null | the attempt budget the cell ran under (10, everywhere) |
| `deploy_ok` | bool/null | the bring-up brought the service up |
| `e2e_pass` / `e2e_total` / `e2e_green` | int/int/bool, null | the functional contract on the last verify |
| `load_errors` / `load_total` / `load_ran` / `k6_available` | int/int/bool/bool, null | the load test on the last verify; `k6_available` false means k6 was never consulted |
| `verify_stage_failed` | str/null | the verifier's stage of the last non-green verify (`deploy`, `e2e`, `k6`, `scaling`, …) or null |
| `iterations_to_green` | int/null | attempts to the first green, from the ledger; null when never green |
| `green` / `revoked` / `budget_exhausted` | bool | the terminal outcome the ledger records (`revoked`: a green the gate later revoked — never merged with never-green) |
| `author_surface` | obj | `{files, languages[], language_count, lines, sloc, total_files, total_languages, total_language_count, total_lines, per_file[]}` — the agent-authored delta over the seeded skeleton (`surface_filter.py` decides what counts) |

## `results.csv`

One row per cell, the columns of `CSV_COLUMNS` in `aggregate.py`:
`cell_id, model, task, variant, factors (k=v;k=v), impl, repeat,
first_pass_correct, correctness_tier, deploy_ok, e2e_pass, e2e_total,
e2e_green, load_errors, load_total, k6_available, verify_stage_failed,
iterations_to_green, green, revoked, budget_exhausted, agent_s_total,
agent_s_per_attempt, files, language_count, lines, sloc, total_lines, grader,
grading_missing`.

## `results.json`

`{ "cells": [ ...the rows... ], "summary": {...} }`. The `summary` block:

- `total_cells`, `cells_missing_grading`.
- `by_model_variant["<model> / <variant>"]` → the
  per-group metrics of `cell_metrics`: `n_cells`, `n_green`,
  `first_pass_correct_rate`, `e2e_green_rate`,
  `green_rate`, `revoked_rate`, `mean_e2e_pass_rate`,
  `load_error_rate_on_green`, `min/mean/max_iterations_to_green`,
  `budget_censored_rate`, `mean_files`, `mean_languages`, `mean_lines` (green
  cells) with `n_green_surface`, and the LoC spread. Model ids the
  definition pools (`POOLED_MODELS`) share one row.
- `baseline` → when the table is cut to one `--impl`, the other driver's
  cells under the same cuts as `delta_by_model_variant`; else null.
- **whatever the experiment's definition adds** (`report_summary`, given
  every cell, the engine's per-group metric function, its None-safe delta and
  the metric names): the experiment's own keys — differences between its
  variants, say — and its `notes` on how to read the table.

## How to read it

`green_rate` and `mean_iterations_to_green` with `budget_censored_rate`
carry the attempts-to-green story; `load_error_rate_on_green` is a parity check
measured on green builds only. Always report `n_cells` and
`cells_missing_grading`. Which gaps between variants matter is the experiment's
to say, in its notes.
