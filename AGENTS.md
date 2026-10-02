# AGENTS.md — FAE invariants

FAE is a Framework for Agentic-authoring Evaluations. An experiment built
on it runs coding agents against a task under a fixed attempt budget, judges
every attempt with a deterministic verifier, and records how many attempts each agent needed, how
much it authored and, optionally, graded defects. [`README.md`](README.md) is
what FAE is and a first run, [`RUNBOOK.md`](RUNBOOK.md) the operator runbook, [`DESIGN-fae.md`](DESIGN-fae.md) the design,
[`HOWTO.md`](HOWTO.md) the tutorial for a new experiment. This file records
what is load-bearing but not obvious from reading the code, each with the
failure it prevents.

**Maintenance contract: this file is updated in the same commit as the
change it describes.** A rule that lives only in someone's memory is lost,
and a reader reconstructing what a run measured needs the contract, not just
the source. Section 9 lists which change touches which section.

## How to author a new experiment

Follow [`HOWTO.md`](HOWTO.md): it builds one from an empty directory, and
[fae-authoring-a-calculator](https://github.com/orglnte/fae-authoring-a-calculator) is the finished reference. The steps, and where each
one's rules live:

1. Make the experiment its own repo beside this checkout, with a `cli.py`
   that imports this engine (§0). The engine never names the experiment; the
   definition declares everything the engine reads.
2. Write the definition, `<experiment>/__init__.py` (HOWTO §3): variants,
   matrix, seed docs, gate, config keys, fingerprint trees, verifier, verbs.
3. Write the task, the skeleton and the prompt (HOWTO §4). What the agent
   may write is the Variant's `AUTHORING_SURFACE`; everything else is healed before
   a verdict (§4).
4. Write the variants (HOWTO §5): the calls in §4's table — `author_*` on
   the host, `verify_*` inside the verify container — and their image layers.
5. Write the verifier (HOWTO §6): its image, the `Verdict` it returns and
   what refunds (§5, §7). A resource scaled 0↔1 under load composes the
   contrib law (§7).
6. Point the engine at it (HOWTO §7), then run one cell three ways: no
   agent, the scripted agent, a real one (HOWTO §8–10).
7. If the verifier measures under load, tune the load shape to the host and
   pass the references' full gate before any scored cell (§7).
8. Run the fleet (HOWTO §11) and read what came out (HOWTO §12). The ledger
   decides (§2); a cell that reached a verdict is sealed (§8).

---

## 0. Two roots: the engine and the experiment

This repo is the engine: the `fae/` package (`cli`, `driver/`, `cell/`,
`scoring/`, `mutex`, `ledger`) behind a root `cli.py` shim, its suite
`tests/` (run on `tests/fixture_experiment/`) and the TLA+ model `.tla/`.
An experiment is its own repo beside it. Its
`cli.py` imports this checkout — `$FAE_DIR`, else the sibling `../fae` —
puts it on `PYTHONPATH` for every process the engine starts, and sets the
ROOT to that repo. Nothing is installed, so an engine fix is an edit here.

The ROOT (`REPO_ROOT`, else the working directory; `fae/paths.py`) holds
what belongs to one experiment: `fae.toml`, the experiment directory
(`EXPERIMENT_DIR`: the environment, else `[paths] experiment_dir`, default
`experiment`), the workspaces and the lock plane. One root is one
experiment. The engine finds ITSELF from its own files (`paths.ENGINE`),
never from the root: its instruments, the testagent, the agent-container
context, the `.tla` model, and the cell package the fingerprint guards. The
verifier container mounts the root and the engine's tree at their own paths
and puts both on its `PYTHONPATH`.

The engine never imports the experiment by name. `fae/cell/experiment.py`
loads `<EXPERIMENT_DIR>/__init__.py` BY PATH as the package `experiment`
(one process, one experiment — a second definition is refused; tests call
`unload()`), and every reader goes through the `Definition` it returns: the
variant registry (`variant_classes()`, a function, because importing the
variants pulls the substrate in and the config reads the definition first),
`MATRIX`, `SEED_DOCS`, `GATE`, `CONFIG`, `fingerprint_trees`,
`verifier_class()`, `verbs()`, `taint_rules`, `reference_workspace`,
`POOLED_MODELS`, `report_summary`, and the agent image's layer.
`tests/test_experiment_definition.py` pins that no module under `cli.py` or
`fae/` names the package. The engine keeps no arm, stage or model name of
any experiment.

**What the agent wrote never executes on the host, and neither does the
verifier.** The verifier builds and runs the artifacts inside the variant's
substrate (a container, a dind sidecar, a kind cluster), so a judged program
cannot touch the rig; and the verifier itself runs in a container of an image
the experiment declares (`Verifier.IMAGE_DIR`, a Dockerfile directory with
every version pinned; a Variant may layer its own `IMAGE_DIR` on it), built
by the engine when missing and tagged by its content (`fae/cell/image.py`).
Every host judges in the same environment, and the host needs only python and
a docker daemon. There is no host path for the verifier: a verifier without
an image is `stage=verifier-image`, a void.

Reusable verifier blocks are contrib — engine-owned mechanisms with no
policy: `fae/cell/contrib/elastic_resource/` (the load-shape law for a
resource scaled 0↔1: `load_shape`, `trace`, `k6`, `law`; §7) and the
substrate blocks under `fae/cell/substrate/` (`dind`, `kind`, `sandbox`).
An experiment composes them with its own parameters. What a rig fault looks
like in an experiment's evidence is its `taint_rules`, run by
`fae/driver/validate.py` beside the engine's own rules.

## 1. The unit of work is a cell

A **cell** is one `(model, variant, condition, task, rep)` run, identified by
a `cell_id` and owning one workspace under `<root>/workspaces.nosync/<cell_id>/`
— or under another WORKSPACE ROOT named by `WORKSPACES_DIR`
(`ws-test.nosync` holds harness-validation and smoke cells). Only cell
PLACEMENT follows that root: the lock plane (`<root>/workspaces.nosync/.orch`
— verify lock, slots, transitions.log, the queue) serializes the one
physical rig and is shared by every workspace root, and each root's
`safe_wipe` boundary stops at that root. A cell runs up to `ATTEMPT_BUDGET`
(10) attempts; each attempt is author → restore-and-judge → verify. The cell
ends green, failed, or revoked.

`cell_id` is encoded in ONE place, `fae/driver/common.py`'s `cell_id()`;
the driver's entry point and the prepare import it — two implementations
kept in sync by hand drift, and a drifted id writes to one workspace and is
read from another. `parse_cell_id` is positional
(`<model>_<effort>[_smoke]_<arm>_<condition>_<task>_r<rep>`: the arm is
whatever lies between the effort and the last three tokens, since it may
carry an underscore) and accepts only an arm the loaded experiment declares.
`tests/test_cell_id.py` pins the wiring.

**Code names and data names.** In the code the design alternative is the
Variant and the information axis is the condition. The words recorded in data
— `cell.env`'s `TREATMENT=`, `metrics.json`'s `treatment`, the cell id's
slot, the ledger, `score.json` — keep their names, so every sealed cell reads
as it was written. Do not rename a recorded field.

The matrix is the experiment's `MATRIX` (`common.definition().matrix`); a
combination that exists as a workspace but not in the matrix is legacy, and
`spawn-matrix` does not re-run it.

## 2. The ledger is the file of record

`<workspace>/iterations.log` — TAB-separated, one event per line.
**`fae/ledger.py` is the only parser.** Two parsers disagreeing is how a
revoked green scores as green, a mid-gate resume mints an ungated green, or
a stranded reverify leaves the population.

- **`metrics.json` never decides doneness.** It holds only the last single
  verify's numbers, so a loop killed mid-gate leaves a green `metrics.json`
  on a cell that is not green. `ITER green` is written only after the FULL
  gate passes, and is the one sufficient condition.
- **`fae/metrics.py` is the one reader of `metrics.json`** for everything
  that computes with it (validation, scoring). It turns a count a verifier
  wrote as text (`"28114"`) into a number: a string there crashes the rules
  that divide it and drops the cell from the table's load-error rate, and a
  sealed cell's file cannot be rewritten.
- **Trailing non-attempt events never un-finish a cell.** A
  `SHAPE`/`REVERIFY`/`PAUSED` line after an `END` does not reopen it; the
  last `END`/`HALT` stays authoritative.
- **`revoked` is not `failed`.** A cell whose green the gate later revoked
  solved the task once but is not arrangement-robust; merging it with
  never-solved-it destroys the finding.
- **Ledger rewrites are operator-run only.** No automated repair.
- **The host is the ledger's only writer.** A verify never opens
  `iterations.log`: it records its events with `verify.record_event` in its
  own directory, and after the child exits the host appends them, under the
  cell's id and with the time they happened, and only the kinds in
  `verify.LEDGER_EVENTS` (ALERT, VERIFY_READY). A verify cannot write an
  ITER or END line.

## 3. Locks — what each one actually protects

Every lock is **`flock(2)` on a file**, implemented once in
**`fae/mutex.py`**; `fae/cell` (Arena, `Cell.acquire_slots`,
`Cell.verify_lock_acquire`, `Cell.exclusive_acquire`) and
`fae/driver/common.py`'s `fs_lock` are its holders. Do not add a second
mutex implementation.

**A lock is a FILE, held by an open fd.** The kernel releases it when the
holding process dies by any means — SIGKILL, OOM, panic, host sleep — so
nothing on disk is ever judged stale: no steal, no adoption, no settle
window. **The driver owns the fd and takes the slots itself:** `Arena` opens
every candidate lock file and `Cell.acquire_slots` flocks the work slot, then
the arm slot, in the driver's own process, the holder for the cell's whole
life. Provisioning never touches the arena.

Two rules break mutual exclusion **silently** if violated:

1. **Never let a process that outlives its driver inherit a lock fd.** A
   flock is freed when the LAST fd on it closes, so an inherited fd keeps a
   dead holder's lock alive. `subprocess` closes fds by default; nothing in
   `fae/cell` writes `pass_fds` or `close_fds=False`, and a daemon that
   outlives its setup says `close_fds=True` out loud.
2. **Never unlink a lock file.** Unlink-and-recreate puts two holders on two
   inodes with no error anywhere. The lock FILE is permanent; only the
   `<lock>.holder` sidecar may be removed.

`tests/test_lock_fd_discipline.py` pins both rules by source text;
`tests/test_mutex_kernel.py` pins the mechanism, including the negative
control that an inherited fd DOES pin a lock.

**Both acquire queues stand down on an operator pause**
(`mutex.pause_requested`, checked while queued and again after winning): an
unbounded queue that cannot honour a pause waits forever holding what it
already took. Pause checks inside the mutex use `.paused` EXISTENCE, as the
driver does — never `fae/driver/state.py`'s `pause_lock`, which parses the
first token and calls an empty `.paused` "not paused".

**The `<lock>.holder` sidecar is DISPLAY ONLY.** "Is it held?" is a
question for the kernel (`mutex.probe_held`); no code path branches on the
sidecar.

**A live holder's lock is never taken; a wedged holder is ended by
supervision.** `ARM_HELD_ALERT_S` alerts and then stands the cell down; age
alone never acts — overaged AND heartbeat-stalled does. Conduct is the only
actor that ends a cell, and `_teardown_cell` the only way it does so,
because a kill that does not also tear down frees the arm slot while the
substrate it capped still runs. A kill signals the driver's whole session,
not its pid. After the kill the arm's `verify_teardown` runs for the cell's
last arrangement in a fresh container of the variant's image
(`fae/cell/verify.py: run_teardown`): what the verify provisioned outside
itself does not die with its container. `cli.py cell stop` does the same.

**`conduct` refuses to start if `.orch`'s filesystem does not ENFORCE
flock** (`mutex.fs_enforces_flock`). A filesystem can accept flock without
enforcing it, which would turn every cap into a no-op that reports success;
the probe proves exclusion across a real second process. Probe by hand:
`python3 fae/mutex.py fscheck <root>/workspaces.nosync/.orch`.

| Lock | Scope | Held for | Protects |
|---|---|---|---|
| loop lock | per cell | cell lifetime | two loops on one workspace corrupt its logs |
| **verify lock** | **global, per machine** | one verify | the shared measurement surface: one load test at a time, fleet-wide |
| **arm lock** | per variant `LOCK`, N-ary; declared by variants whose agents hold a live substrate | **cell lifetime, setup→teardown** | host contention: another live substrate distorts load-test timing |
| work slot | global semaphore, `WORK_SLOTS` | cell lifetime | total concurrent cells |
| exclusive lock | global, the name a verifier declares in `EXCLUSIVE` (none: no lock) | one arrangement, on the cell's own fd around the verifier | whatever singleton substrate a verifier declares |

**Lock ordering is slot ≺ arm**, globally consistent, so deadlock-free; it
also keeps the scarce arm held only while the cell works. Release is the
reverse.

**Teardown is mandatory and covers setup.** `Cell.run` tears the variant
down on its unconditional path whether setup finished or not; `cell stop`,
conduct's kill fallback and the zombie reaper call the same
`author_teardown()` in-process. Variants hold no rollback of their own:
teardown is idempotent and derivable from the cid alone. A non-zero setup
writes an `ALERT SETUP-FAILED` ledger line.

- **The arm lock is far coarser than the verify lock.** It spans every
  attempt and the agent's thinking time, so an access arm's concurrency is
  the arm lock's doing, and changing a cap changes the host-load regime
  every cell is measured under. Caps are `[slots] arm_<lock>` in `fae.toml`
  (`ARM_SLOTS_<LOCK>` in the environment on top); a lock the config does not
  name caps at the variant's `LOCK_SLOTS`. A verifier may refuse to judge on
  an overloaded host and say why, as a void.
- **Admission is `conduct`'s job; `WORK_SLOTS` is the backstop.**
  `cli.py conduct run` (`fae/driver/conduct.py`) is the ONE scheduler and
  the ONE controller: it admits queued specs up to a global cap (`-n`) and
  `--per-model`, round-robin with starved lanes first, and every
  `--supervise-interval` it repairs crashed or hung cells by requeuing them at
  the lane front (its own admission respawns them — one spawner), validates
  DONE cells, and reaps zombies on a second consecutive sighting. A cell whose
  validation raises is reported once (an ALERT line) and left unvalidated,
  retried every pass: one cell's evidence never stops supervision of the
  fleet. It runs in
  the foreground; Ctrl-C stops admission and supervision while live cells
  keep running. `conduct diagnose` is the read-only preview; `conduct
  reconcile` the one-shot engine verb. Nothing else reaps: the fleet console
  lists zombies and touches none, a spawn clears only its own cid, and
  `cli.py rig zombies --reap` is the operator's deliberate verb.
- **A reaper's predicates name every holder shape.** A cell loop, `exp1`,
  `reverify` and `smoke` all hold the verify lock in-process around a
  verifier child that inherits no fd (`_VERIFY_HOLDER_ARGV`,
  `fae/driver/zombies.py`); a predicate that knows only the loops reads a
  live verify as a leak. The verify container `fae-verify-<cid>` and the
  cell network `fae-net-<cid>` are the engine's substrate
  (`fae/cell/image.py` names them; `Variant.network_up` creates the network
  first); what a verifier provisions for a cell is declared on the verifier
  (`SUBSTRATE_PREFIXES`, `substrate_identities`). All are reaped like a
  variant's substrate, a network only past the long grace.
- **Walls and stand-downs.** A cell waiting on a provider limit is stood
  down (pause `limit-wall`, by=conduct), requeued at the front, and its lane
  cools (`.orch/cooldown.<model>`: the provider's reset hint, or 3 h);
  expiry lifts the wall and retries. The other stand-downs conduct writes
  (arm-stuck, phase-stalled-*, verify-wedged, silent-hang) are lifted after
  `STANDDOWN_COOL_S`, each lift spending one `MAX_RESPAWNS` repair, flagged
  when spent. **A repair, substrate void or contract hit on a scored cell is
  investigated at its first occurrence** — `MAX_RESPAWNS` bounds a loop, it
  is not an investigation budget. The wall wording, the reset hint and the
  driver's retry decision come from `fae/cell/faults.py` alone. Every
  supervisory age excludes host sleep (`.orch/host_sleep.json`), so a
  suspended laptop does not read as a stall.
- **The weekly cap.** The claude lanes share one weekly cap: conduct reads
  the CLI's seven-day `rate_limit_event` from the attempt logs
  (`.orch/weekly.json`), parks the lanes listed in `BUDGET_LANES` (lane
  tags, default `fable,opus`) once utilization reaches `BUDGET_HOLD_AT`
  (0.75) with an ALERT line, and releases them within `BUDGET_RELEASE_H`
  (24 h) of the reset. A lane tag is covered only when it is listed.
  `conduct-pause <model> --admission-only` parks a lane by hand;
  `conduct-resume` reverses it.
- **A spec is a FILE and its state is the directory it sits in.**
  `.orch/queue/<model>/<seq>.<cid>.json` pending (lane order is the sequence
  number), `.orch/running/<model>/` claimed, `.orch/done/<model>/` terminal,
  `.orch/backups/` taken out of play by an operator verb. Every transition is
  one `rename(2)`, so a conduct killed at any instant can neither lose nor
  duplicate a spec. Conduct manages exactly the CLAIMED set — supervision
  never invents a claim — and ADOPTS a live cell with no claim at start, or
  its lane would read as free and run a second cell.
- **A spawn's exit code decides whether a crash spends repair budget.**
  `LOCK_EXIT` (43: another loop owns the workspace) is never charged; the
  claim stands for the next pass. Anything else conduct does not recognise
  is `GENERIC_CRASH_EXIT` (47) and is charged, like a substrate HALT. The
  loop-lock refusal raises `Halt(msg, LOCK_EXIT)` explicitly, so a
  deterministic host fault never shares the benign race's number and
  respawns forever uncounted.
- **One live cell per non-empty lane.** `--per-model` (1) enforces it; `-n`
  is the global ceiling and should equal the lane count (conduct warns). A
  lane that needs more concurrency takes `--per-model-override MODEL=N`,
  scoped to that lane. Arm serialization is not an admission concern: a cell
  parks on the arm lock in-cell and takes the arm as soon as it frees.
- **The exactly-1 rule: a cell verb acts directly iff it matches ONE cell;
  bulk goes through conduct.** `cell resume CID` lifts locks and respawns
  directly, cap-deferred (`--force` overrides). `conduct-resume
  {all|MODEL…}` never spawns: it unparks lanes, lifts pause locks and
  requeues interrupted cells at the front for conduct to admit under its
  caps. A blanket resume leaves roster/manual pauses and cancelled cells
  alone; naming the model lifts them.
- **Every lock that can block declares a heartbeat phase** (`hb_phase`), or
  a queued cell displays whatever phase preceded it. `WAIT_PHASES` in
  `fae/driver/state.py` is the set rendered as `WAITING`.
- **Liveness is shown, never inferred from silence.** The LOOP declares
  itself: `.loop` is rewritten every `HB_TICK` (30 s) by a ticker that dies
  with its loop, so a phase that blocks for an hour still reads young. The
  AGENT is measured from outside: supervision samples each agent container's
  received bytes and compares across sweeps. A cell is killed as hung only
  after two consecutive sweeps with no received bytes and no output growth.
  An empty agent log means the first model turn has not returned: unknown,
  and unknown never kills.
- **A phase is a place a cell can WAIT or WORK, never a label for a step.**
  There are seven: `setup`, `slot-wait`, `arm-lock`, `agent`, `limit`,
  `verify-lock`, `verify`. A phase for a step nothing waits on buys nothing
  and costs a state everything downstream must interpret.

## 4. Variants and substrate

The interface is `fae/cell/variants/base.py` (`Variant`); the classes are
the experiment's, registered by arm on first use (§0). Each declares `ARM`,
`TECH` (its skeleton overlay and api-doc name), `VARIANTS`, `LOCK` (the arm
lock its cells hold for their lifetime, or none), `LOCK_SLOTS` and
`AUTHORING_SURFACE` (required; the engine has no default, and the preflight
refuses a variant without it) — the exact files and directory prefixes the
agent may write;
`fae/cell/surface.py` heals everything else before a verdict: a changed
seeded file is restored from the seed, and a file that is neither seeded nor
authorable is moved to `<ws>/.out-of-surface/attempt-N/` (kept, never
deleted); the rig's own `.git`/`.gitignore` and tool caches are left alone.
Both are named in the ledger (`HEAL`) and in the next prompt. The seed record,
`<ws>/.skeleton_manifest`, sits beside `artifacts/`, not in it: the agent's
container mounts only `artifacts/`, so it cannot rewrite the record it is
checked against. It answers:

| Call | Called by | Purpose |
|---|---|---|
| `substrate_ok()` | `Cell.substrate_ok()` before every attempt; `cli.py experiment substrate` | preflight, including whatever daemon the variant needs (the engine probes nothing itself); HALT, no attempt burned, if the environment is wrong |
| `substrate_alive()` | `Cell.verify` before every arrangement and after a charged fail | the substrate answers RIGHT NOW (a hard connect failure is "no", a slow daemon is alive). Dead before → the arrangement is void without a deploy; dead after a charged fail at one of the verifier's `MEASURED_STAGES` (None: any) → the same void, stage `substrate`, refunded. The base answer is False, and a variant that never overrides it is refused at preflight (`liveness_declared`): with the base answer every arrangement would be void forever |
| `sweep()` | `cli.py experiment substrate` | remove stale substrate left by dead cells |
| `author_setup()` | `Cell.run` / `Cell.reverify` / `cli.py experiment exp1`, from the driver on the host | what the agent needs while it authors, kept for every attempt, on the cell network; returns the agent's docker args |
| `author_teardown()` | the same, on their unconditional path; `cell stop`, the kill fallback, the reaper | tear it down, idempotently |
| `verify_setup(ctx, env)` | the experiment's Verifier, inside the verify container over the daemon's socket | what one arrangement of the judged artifacts runs on, fresh every time; raise on a rig fault (refunded), raise a rejection once the substrate is up and the artifacts fail on it (charged); return what later stages need |
| `verify_teardown(ctx, env)` | the same, whether `verify_setup` finished or not; `run_teardown` after a kill | the world reset after the arrangement, derivable from the ctx alone |
| `tool(argv, env, network)` | the variant's own `author_*` pair | a tool the host does not carry, run in a throwaway container of the variant's image over the daemon's socket |
| `IMAGE_DIR`, `image_context(conf)` | `fae/cell/image.py` | the variant's layer over the verifier's image (`FROM $BASE`), versions pinned; none = the verifier's image as is |
| `substrate_identities(cid)` | `fae/driver/zombies.py` | the names of what a cell of this arm provisions, from the one formula the provisioner uses |
| `SUBSTRATE_PREFIXES`, `stray(live, workspaces)` | `fae/driver/zombies.py` | what a reaper may DISCOVER: name prefixes to scan by, and any substrate no name carries |

Who provisions what is a matter of PHASE: the authoring substrate is the
Variant's `author_*` pair (the driver, on the host), the verify substrate
its `verify_*` pair (the Verifier, in the container), the instruments of the
measurement the Verifier's own. Everything a cell provisions sits on the
cell's network `fae-net-<cid>` and is addressed by NAME inside it; nothing
the verify uses is a host port.

## 5. The verifier: out of process, in a declared image

The engine hands the experiment's Verifier one `ctx.json` (workspace,
arrangement, variant, the fingerprint pinned at cell start) in `docker run
--rm` of the variant's image (`fae/cell/verify.py: run_verifier`: the root,
the engine's tree and the workspace mounted at their own paths, the cell
network, the daemon's socket, the driver's environment minus the host's own
and anything naming a secret) and reads one `Verdict` back. `ctx.out` is the
verify's own directory, `<ws>/.verify-out`: after the child exits the host
copies the verifier's declared outputs (`FILES`, `FEEDBACK_LOGS`) up into
the workspace, never over a host-owned name and never through a symlink;
`verifier.log`, `metrics.json` and `arrangements/` are the host's:

- `ok`, `stage`;
- `charge` — False is a rig fault and the attempt is refunded; the engine
  keeps no table of void stages, the verifier decides;
- `stand_down` — the cell pauses for the operator;
- `metrics` (written as `metrics.json` by the engine), `files` (archived
  under `arrangements/`; the verifier's `FILES` when the Verdict names none).

A verifier that crashes, hangs past `VERIFIER_TIMEOUT_S` or answers nothing
is `charge=False` too, and the container is `docker rm -f`'d on every path,
so nothing it started outlives it — the container IS the session. It runs
under the CPU cap the verifier declares (`Verifier.CPUS`), so the load side
is the same size on every host; as the operator's uid, so what it writes is
the operator's; and with the daemon socket's group added
(`image.socket_group`), without which every docker call inside answers
"permission denied" and reads as a charged fail. A verifier that declares
`EXCLUSIVE` gets that named lock held on the cell's fd around the run. The
engine re-checks the fingerprint after the child returns and overrides any
verdict with a `harness-fp` void.

The verifier's image and a variant's layer are built by content
(`fae-<experiment>-<leaf>:<sha12>`), so an edit to an image context is a new
image and the same content is never rebuilt.

**What the verify container can write: its own directory, nothing else.**
`verify.verify_mounts` mounts the experiment, the engine, the root's files,
the verifier's `ROOT_READS` and each entry of the workspace read-only, and
`ctx.out` alone writable. No mount sits inside another: on Docker Desktop a
writable bind nested in a read-only one vanishes within a second of the
container's start. A verify sees no other cell's workspace. Behind the
mounts, the host compares the cell's record before and after every verify
(cell.env, the seed record, `.sealed`, the archive, the checkpoint refs,
the judged tree, and the ledger, which may only gain a supervisor's ALERT):
a change voids the verify (stage `integrity`, uncharged), writes
`ALERT INTEGRITY`, and stands the cell down; `results validate` taints it.

**What every verify must leave is the experiment's to declare.**
`Verifier.REQUIRED_OUTPUTS` names the files every verify of the experiment
writes, whatever its outcome; they are always copied up and archived. One
that a verify which ran did not write (absent from its own directory, or
left there by an earlier verify) is a rig defect, the same on every retry:
`ALERT RIG-OUTPUT`, uncharged, the cell stood down for the operator.
`results validate` taints a cell with an attempt never judged after such an
alert, and warns on one judged again once the rig was mended.

**The judged program never runs in the verify container.** A verifier that
measures a running program starts it in the cell's secure runner,
`fae-secrun-<cid>` (`fae/cell/substrate/secrunner.py`), from the verify image
(`FAE_VERIFY_IMAGE`): a fresh copy of the artifacts at `/workspace` and a
scratch dir at `/scratch` are its only mounts; no Docker socket; the
operator's uid with `HOME` and `USER` set, every capability dropped, no
privilege escalation, CPU/memory/pid/open-file ceilings; the networks the
verifier names, reached by its name. The verify container keeps the rig's
reach (the mounts, the socket, the lock plane) and runs only rig code. A
program run once and judged by its output (a CLI, a compiler) uses
`substrate/sandbox.py`, the same shape without a network.

## 6. The seal: what the agent can actually reach

The agent runs in its own container, capped at `AGENT_CPUS` cores (default
1, 0 lifts it): agents run beside the one verify the fleet measures at a
time, and an uncapped one (its own tests, a build) takes the cores the
measurement runs on. `CPUSET_MEASURED` / `CPUSET_AGENT` pin the measured
verify's containers and the agents to separate cores; both are empty (off)
by default. It mounts exactly:

```
-v "$art":/workspace                    # the artifacts dir, nothing else
-v "$AGENT_CLAUDE":/home/node/.claude   # creds only (per-cell, staged fresh)
-v "$ws/feedback":/feedback:ro          # attempt ≥2: the judged run's logs, read-only
<the variant's author_setup args>       # empty for a sealed variant
```

`/feedback` holds copies of the judged run's logs, staged by
`Cell._stage_feedback` when the retry prompt is built: without it the
service's own output never reaches the agent, and a fault it could fix in
one read costs it the budget. It lives beside `artifacts/`, never inside it —
the no-edit oracle is the git tree hash of `artifacts/`, and a log there
would make every attempt read as edited. The engine's and the experiment's
source trees are never mounted.

Every arm runs in an image of its own, two layers. The base: every model's
client (claude, opencode, agy), git, python3, built from the experiment
root's `Dockerfile.agent-base` (tagged `fae-<experiment>-agent-base:latest`)
or, when the experiment has none, the engine's `fae/agent-container/`
(`fae-agent:latest`). Over it, the arm's own layer: the variant's
`AGENT_IMAGE_DIR` and `agent_image_context`, one per arm family (TECH),
tagged `fae-<experiment>-agent-<tech>:latest` and rebuilt by
`fae/cell/image.py: for_agent` whenever its content, its staged sources or
the base's id is not what its `fae-content` label records. An agent sees
its own arm's tools and SDK and no other arm's: a layer shared across arms
would hand one arm's agent the other's interface to read. The cell resolves
its image from its variant (`Cell.agent_image`); env `AGENT_IMAGE` forces
one image on every arm and is for rig tests only. The base's
clients follow upstream: conduct runs `fae/driver/image.py:ensure_agent` at
preflight and before every admission (upstream versions cached 1 h in
`.orch/agent_image.json`), because a provider gates new models on a minimum
client and a stale client fails every cell of that model. A failed update at
preflight stops conduct; before an admission it is logged and the cell
starts on the image there is. An update pulls the base image through
Docker's credential helper, so a helper that hangs blocks every update.
Client versions are not part of the fingerprint; each attempt's AGENT ledger
line records the CLI that ran and its version in the image it ran in
(`client=claude:2.1.286`, `-` when unreadable), probed once per image id
and cached in `.orch/agent_clients.json`.

`$AGENT_CLAUDE` is restaged **before every attempt**: the CLI keeps
per-project memory and transcripts under `~/.claude/projects/<cwd>`, and
every agent runs in `/workspace`, so a shared mount would let each agent read
every prior agent's memory — cross-run leakage invisible in the results.

## 7. Verdicts are gated, and fast failures are not failures

- **The gate is the experiment's.** `GATE` names the arrangements every
  attempt must pass, whether the seeded one rotates with the attempt number,
  and the sentence the retry prompt carries; the engine reads its arity
  wherever a gate is counted (`ledger.parse`'s `gate_n`, the fleet's GATE
  column). `SHAPE_VARIATION=0` (or `SHAPE_GATE` ≠ `all`) runs the single seed
  arrangement.
- **The elastic-resource law** (`fae/cell/contrib/elastic_resource/`)
  judges a resource scaled 0↔1 under load relative to the store's knee, not
  to the generator's clock, so the verdict does not depend on how fast this
  host's store is. The spike is a RAMP from the baseline to a top; the law
  marks saturation at the first trace tick inside the spike whose p99
  reaches the saturation threshold, and the resource must be up within the
  mount deadline of THAT. A blip is the same ramp, CLOSED-LOOP: a second
  generator per blip window, on top of the baseline, that climbs until the
  first slow or queued request, then offers a fixed number of requests past
  that rate and aborts. On any host it turns back just past the knee with a
  queue of a known size the store drains alone, so a policy that reacts to a
  fixed count of slow requests sees that count with nothing sustained behind
  it. A spike that never saturates the store is `FAIL[load-shape]`, a VOID
  (`charge=False`, ledger `ALERT LOAD-SHAPE`): the ramp did not reach the
  host's knee. Every value — baseline, top, ramp, hold, thresholds, the
  excess, the deadline — is the experiment's `CONFIG`, tuned per host.
- **Tuning the load shape to a host.** Before scored cells, in order: the
  store's bench is above the verifier's floor; the spike saturates well
  inside the ramp and the resource mounts well inside the deadline
  (`trace.csv`, `sidecar.log`); every blip stops by its rule, its queue
  clears within the references' own decision window, and nothing mounts on
  it; the references pass the full gate on this host; and a negative control
  whose top is below the knee ends in `FAIL[load-shape]`, refunded. Two hosts
  tuned differently are the same experiment only if both pass all five. The
  experiment's AGENTS.md carries its numbers.
- **A substrate that dies under the measurement is a void, not a verdict.**
  A verify that reached `VERIFY_READY` and collapsed minutes later reads
  exactly like a build failure, so `Cell.verify` asks `substrate_alive()`
  before every arrangement and after a charged fail (§4).
- **`TRACE_MIN_VERIFY_S` (45 s).** A fail returned faster than this without
  reaching `VERIFY_READY` is a substrate HALT, no attempt burned — except
  `stage_failed=deploy` (the substrate came up and the agent's artifacts
  failed on it), a legitimate authoring failure that can finish in seconds.
  Anything else a bring-up raises is `nostart`, refunded. Transient API
  faults (`fae/cell/faults.py`, the one vocabulary the driver and conduct
  share) retry the same attempt; scoring them corrupts iterations-to-green.
  A run that changed nothing and whose transcript names a wall is a wall
  whatever its exit code; an auth or config wall halts the cell (42).
- **`FAIL[contract]` — the world did not match the docs.** After every
  bring-up and every teardown the variant's contract asks the world the
  promises its seed docs make. A broken promise after the bring-up VOIDS the
  arrangement (`stage=contract`, `charge=False`); after the teardown the
  verdict stands. Either way `stand_down` names the broken promises, the
  driver writes a ledger ALERT and pauses the cell (`contract by=driver`);
  conduct does not respawn it — the operator digs first.
- **A voided run is zeroed.** Every START restores the work tree to the one
  the last CHARGED verdict was judged on (the ledger's last `ITER … tree=`,
  or attempt 1's `pre tree`); the abandoned tree is checkpointed first and a
  `RESTORE` line names both. A void, a stand-down, a stop, a kill or a pause
  after the agent ran refunds the attempt AND discards its edits, so the
  retry is the same experiment. Feedback is written only at the charge, so a
  voided run tells the next attempt nothing.
- **`validate` ⇒ VALID/TAINTED** runs before scoring: the engine's rules and
  the experiment's `taint_rules`. TAINTED is raised to the operator and never
  auto-requeued. A taint rule must not flag the cells that fail hardest:
  load errors are the measured failure when the policy never scaled, and only
  unexplained errors taint. Parsers of recorded verdict text are pinned by
  selftest cases taken verbatim from real `metrics.json`.
- **TLA+ conformance.** `.tla/Runs.tla` plus `tla_verify` (`$FAE_TLA_VERIFY`,
  else `tla_verify` on `PATH`; with neither, selftest says so and skips
  the replay): `--trace` replays
  one cell's ledger per attempt at verify-end; `--live-trace` replays the
  global `.orch/transitions.log` against the spec (`cli.py rig selftest`). A
  fresh prepare that wipes a workspace logs `Retire <cid>`: the id then names
  a NEW cell, which the replay judges from Init as `<cid>#<n>` — without it
  the new cell's `Spawn` is judged against the old cell's verdict and every
  later event of the id cascades. `rig trace-reset` archives the log and
  records the observed state as `EPOCH` lines, the only way to start a replay
  anywhere but a cold fleet.
- **Authored surface is defined once**, in `fae/scoring/surface_filter.py`,
  and it is content-based, not name-based: a name list cannot catch a vendor
  tree an agent downloads at runtime, and binary bytes counted as lines swamp
  a group's mean.
- **The judge model is an explicit parameter.** `cli.py results grade`
  requires `--judge-model` (or `$JUDGE_MODEL`), with no default, so a corpus
  is never graded half by one model and half by another; it is recorded per
  cell (`defects.json`'s `grader_model`) and exported as a column.
- **Every cell is graded TWICE, and only when terminal.** One `results
  grade` makes two independent judge passes per cell, and
  `fae/scoring/grader_agreement.py` reports Cohen's κ with raw agreement: a
  judge-produced metric without a reliability figure is an opinion.
  Resumability is per pass. Grading an in-flight cell is wrong — its
  artifacts change under the judge; doneness comes from `fae/ledger.py`.
- **"Grading", not "coding".** Assigning taxonomy codes to observed defects
  is *grading*; "coding" is what the agent under study does. `code` as a
  FIELD (a taxonomy identifier) keeps its name.

## 8. Data-destruction rules

- **Two-stage delete only.** `safe_wipe` (`fae/cell/prepare.py`) MOVES a
  workspace to `.to_be_deleted/<ts>/`; real deletion needs operator review.
  It refuses anything that is not an absolute path to a cell directory
  directly under the workspace root, so an unset variable dies there instead
  of expanding into a tree wipe. **Never `rm -rf` a workspace.**
- **Never resume, spawn, or restart a cell without an explicit operator
  order.**
- **A cell that reached a verdict is SEALED and read-only.** `Cell.seal`
  writes `<ws>/.sealed` on green or on the budget spent, and `Cell.run`,
  `ops._spawn_detached`, `queue.enqueue` and any verify that would write into
  the workspace refuse it; exit code 46 means *finished*. Re-verification is
  `cli.py cell reverify`, which writes under `<ws>/reverify/<ts>/` and never
  appends to the ledger. Derived files (`validation.json`, `score*.json`) are
  not sealed: they are functions of the result and stay re-runnable when the
  rules improve. **There is no unseal** — redoing a cell is a fresh requeue,
  two-stage and auditable, and a `Retire` in the log (§7). `cli.py cell
  seal` is dry by default; `--apply` writes.
- **The attempt budget is 10, everywhere, always.** `ATTEMPT_BUDGET` is a
  module constant (`fae/driver/common.py`, `fae/cell/config.py`), recorded
  in `cell.env`; no flag, spec field or environment override. A per-cell
  budget makes two cells incomparable and gives sealing an exception.
- **`stop` is resumable; `--cancel` is the verdict, and a cancel is durable
  before it is thorough.** A plain `cell stop` halts now (SIGKILL, substrate
  teardown, queued specs backed up) and stays `PAUSED·stopped`. `cell stop
  --cancel` writes `.cancelled` FIRST, before the kill and the teardown, so
  an interruption in between cannot leave a cell that looks killed and is
  respawned. Trace mapping: plain stop = `Pause` + `Crash`; only `--cancel`
  emits `Kill`.
- **Never log a transition that did not happen.** A conformance log is only
  worth replaying if every line in it is real; an event emitted for a cell
  that was merely inspected desyncs the replay and cascades.
- **FP-guarded files** — every file of the experiment tree, the trees the
  experiment declares through `fingerprint_trees`, and every `.py` of the
  engine's own `fae/cell/` package (found from the engine, never under the
  root) — are hashed, content and path, into the fingerprint
  (`fae/cell/rig.py:fp()`) that pins a cell; a mismatch between a cell's
  start and any of its verifies voids that attempt. Edit them only with no
  cell running, apply multi-hunk patches whole, and run `cli.py rig selftest`
  after. An empty experiment tree or a missing declared file is FATAL, never
  a silently smaller surface. `fae/mutex.py`, `cli.py`, `fae/driver/*.py`
  and the root docs are not guarded.
- **A drain is only a window because it stops CONDUCT.** Pausing covers
  cells that have a workspace; the scheduler is free to pop a spec that has
  none. `conduct-pause all` stops conduct first; `conduct-pause M…` parks
  those lanes. Conduct does not come back with `conduct-resume` — restart it
  deliberately.
- **Stopping never destroys the backlog.** `conduct-stop` backs the queues
  (parked lanes included) up to `queue.<model>.stopped-<ts>.jsonl` before it
  clears them, and on `all` it stops conduct before touching them.
- **Selectors are anchored at token boundaries**, and a cell verb that
  matches more than one cell is an error (the exactly-1 rule, §3); `stop`
  keeps `--dry-run` for the preview.
- **Terminal cells are never paused**, so a fleet-wide pause never stops the
  validation of cells that finish meanwhile.
- **`--dry-run` must not write** — neither a flag file nor a transition.

## 9. Keeping this file honest

Update the relevant section **in the same commit** as the change:

| Change | Section |
|---|---|
| the engine/experiment boundary, `EXPERIMENT_DIR`, the ROOT, a cross-root import | 0 |
| the cell id, the workspace roots | 1 |
| how a verdict is derived or gated | 2, 7 |
| a lock, its scope, or its hold duration | 3 |
| the Variant interface, substrate or its phases | 4 |
| how the verifier runs, what refunds | 5 |
| what the agent container mounts or can reach | 6 |
| anything about deleting or restarting cells | 8 |

State each rule in the present tense, with the failure it prevents; how it
was learned belongs in the commit that introduced it. Rules that belong to
one experiment live in that experiment's AGENTS.md.
