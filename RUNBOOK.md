# FAE operator runbook

Running an experiment's fleet: prerequisites, the `cli.py` verbs, the
backlog, the suites, scoring. What FAE is and a first run: [`README.md`](README.md).

An experiment's `cli.py` puts the FAE checkout on `PYTHONPATH` and runs the
engine with the experiment's repository as the root, so its `fae.toml`,
`experiment/` and workspaces are its own (`[paths] experiment_dir` in
`fae.toml`, default `experiment`). Cell anatomy, the ledger, locks and
verdict rules: [`AGENTS.md`](AGENTS.md).

---

## 1. Prerequisites (install yourself — this repo installs nothing)

1. **Python 3.11+** with `typer` and `ujson`, and a **docker daemon**. That
   is the engine's whole host footprint: the verifier runs in a container
   of an image the experiment declares, and the judged program inside the
   variant's substrate; the agent runs in the engine's base image plus the
   layer the experiment declares. Whatever else an experiment needs on the
   host is its definition's to declare and `experiment substrate` to report.
   New experiment: [`HOWTO.md`](HOWTO.md).
2. Agent CLIs you plan to run: `claude` (logged in), `agy`, and/or `opencode`.
   For opencode the operator writes the API key **themselves** to
   `.agent-home/.opencode/opencode.key` (chmod 600, gitignored; holds
   the opencode Go key — `openrouter.key` is the legacy name and still works).
   Never commit keys; `fae.toml` is
   gitignored — never put secrets in it either (the agent authenticates
   through `.agent-home`, not a config value).
3. From the experiment root (the directory holding the experiment; `REPO_ROOT` in the environment overrides), `python3 cli.py experiment init [--experiment DIR]` writes `fae.toml` (every key at its default, pointed at `DIR`); set the
   machine-local rig paths under `[paths]`. The engine is the `fae` package
   at this root: `python3 cli.py …` runs it uninstalled, and `pip install -e .`
   gives the same verbs as `fae …`.

Then let the preflight localize anything missing:

```sh
python3 cli.py experiment check      # the definition, every seed, each arm's substrate (--walk: step by step)
python3 cli.py experiment substrate  # [ok]/[MISS] per arm: its daemon, tools and images
python3 cli.py experiment smoke      # one reference cell per arm, one arrangement (--full-gate: the whole gate)
```

---

## 2. Driving the experiment — `cli.py` (operator surface)

`cli.py` is the single entry point, six noun groups:

```sh
python3 cli.py cell spawn|pause|resume|stop|tail|log|seal|reverify …   # ONE cell
python3 cli.py conduct run|pause|resume|stop|diagnose|reconcile|queue-add …
python3 cli.py fleet-status                              # read-only table
python3 cli.py results score|grade|validate|aggregate …
python3 cli.py experiment init|check|substrate|smoke|prepare|verb …   # the experiment this root runs; verb: its own commands
python3 cli.py rig selftest|trace-reset|agent-image|zombies …          # the harness itself
python3 cli.py tools run <name> [args]                   # one instrument standalone: the engine's, a contrib block's, the experiment's
```

**The rule**: a verb acts directly iff it touches exactly one cell; anything
matching more goes through `conduct` — one spawner, one cap owner. `cell
resume` respawns its one cell directly (cap-checked, `--force` overrides);
`conduct resume {all|MODEL…}` never spawns — it unparks lanes, lifts pause
locks and requeues interrupted cells at the FRONT, and a running conduct
admits them under its caps.

**The backlog is a directory tree, one file per spec** — pending in
`.orch/queue/<model>/`, claimed by the lane's cell in `.orch/running/<model>/`,
terminal in `.orch/done/`, and shelved (never deleted) in `.orch/backups/`.
Each transition is a single atomic rename, so an interrupted scheduler can
neither lose nor duplicate work. **One live cell per non-empty lane** is the
invariant: `--per-model` (1) enforces it, `-n` is the global ceiling and
should equal the lane count (conduct warns when it does not).

Everyday loop:

```sh
python3 cli.py conduct queue-add MODEL --to-rep N --combo t·v  # fill backlog
python3 cli.py conduct run        # THE scheduler AND supervisor, FOREGROUND:
                                  # global cap 7, 1 live cell per model,
                                  # starved-lanes-first round-robin; every
                                  # --supervise-interval (300s) it repairs
                                  # crashed/hung cells (requeue-front — its
                                  # own admission respawns them), validates
                                  # DONE cells, reaps zombies. Ctrl-C
                                  # detaches — live cells keep running;
                                  # admission AND supervision stop until it
                                  # is run again. The narration is the monitor.
python3 cli.py conduct diagnose   # READ-ONLY: supervision dry run + zombies
                                  # + per-lane admission preview, no waiting
python3 cli.py fleet-status       # fleet table; footer shows conduct: UP
python3 cli.py results score      # validate -> per-cell score.json -> table
```

**conduct pause vs conduct stop**: `conduct pause all` is the graceful stop —
stops conduct, pauses every cell cooperatively, waits (the FP-edit window);
`conduct pause MODEL…` parks+pauses those lanes (`--admission-only` parks
only; a parked lane shows `[PAUSED]` in status). `conduct stop {all|MODEL…}`
is the hard stop — loops TERMed mid-attempt, containers removed, the scope's
queues backed up then cleared; `all` also TERMs conduct. Both are resumable
via `conduct resume`. The terminal per-cell verdict is `cell stop --cancel`
(DONE·cancelled, never comes back); a plain `cell stop` is a resumable halt.
There is no separate supervisor process any more: `conduct run` is the one
controller and the one spawner (`cli.py conduct reconcile` remains as the
one-shot engine verb; its `--watch` is gone by design).

**Limit walls**: a quota-walled cell (WAITING·limit) is stood down
cooperatively by the supervision sweep — its arm lock and work slot are
freed, its spec requeued at the lane front, and the lane cools until the
provider's reset hint (or 3h, `LIMIT_COOLDOWN_S`). Status shows
`[LIMIT until hh:mmZ]`; conduct retries at expiry and re-arms the cooldown
if the wall persists. Conduct also prints a liveness tick every ~10 polls
and, on a green, the attempt/gate detail (`GREEN cid (attempt 4/4, gate
6/6)`).

Cells live under `workspaces.nosync/<cell_id>/` (gitignored): `PROMPT.md`,
`artifacts/`, `iterations.log` (the ledger — `fae/ledger.py` is the only
parser), `metrics.json`, `score.json`, `validation.json`, per-attempt logs.

---

## 2b. The suites

```sh
python3 -m unittest discover -s tests    # the engine, on its own fixture experiment (tests/fixture_experiment)
```

An experiment's own suite lives in its repo. The engine suite runs against
`tests/fixture_experiment/` (two variants, one lock, six arrangements, a
verifier that judges a text file), so a change there is a change to what
every experiment gets.

Beside a live fleet, run it under `DOCKER_HOST=tcp://127.0.0.1:1`: the
docker-backed tests skip instead of touching the host daemon.

## 3. Scoring and results

```sh
python3 cli.py results score          # all DONE cells; in-process, cached
python3 fae/scoring/score_cell.py <cell>  # one cell, standalone
```

`score` validates first (VALID/TAINTED — TAINTED is raised to the operator,
never auto-requeued), scores each cell into `<cell>/score.json` (per-cell
`score-cache.json` makes warm sweeps incremental), then prints the aggregate
table and writes `workspaces.nosync/results.csv` + `results.json`
(schema: `fae/scoring/SCHEMA.md`).

`results grade` runs the experiment's defect taxonomy through the judge model
twice per cell (`--judge-model` is required, no default); the taxonomy, the
evidence extractor and the reply merger are the experiment's.

---

## Repo map

```
README.md            what FAE is, a first run
RUNBOOK.md           this runbook
AGENTS.md            rig contract: cells, ledger, locks, verdicts, delete rules,
                     and §0 the engine/experiment boundary
cli.py               operator CLI (noun groups); the only entry point
fae/driver/              the orchestrator's library: spawn/queues/conduct/
                     reconcile/validate/score/rig, one module per concern
fae/cell/           the Python cell driver: attempt loop, the Variant
                     interface + registry, verify, config, the contract base,
                     contrib/ and substrate/ blocks
fae/                the engine package: cli.py, driver/, cell/, scoring/,
                     ledger.py (the one parser), mutex.py (the one flock),
                     agent-container/
fae/scoring/             score_cell.py, aggregate.py, surface_filter.py,
                     grader_agreement.py, SCHEMA.md
tests/               the engine's unittest suite (locks, queues, scoring), on
                     tests/fixture_experiment/
.tla/                TLA+ model of the run lifecycle (tla_verify replays
                     ledgers/transitions against it; see AGENTS.md)
(an experiment's root holds its own fae.toml, experiment/ and
workspaces.nosync/ — per-cell workspaces + the .orch/ control plane)
```
