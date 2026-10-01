# FAE — a Framework for Agentic-authoring Evaluations

FAE is a framework for measuring how well coding agents author against a
given surface: a design pattern, a framework, a library, a language, an API
style, an infrastructure technology. An experiment built on it hands several
agents the same task, the same verifier and the same attempt budget, changes
only the surface they author against and what they are told about it, and
records what happens.

This document is the design as it stands. [`AGENTS.md`](AGENTS.md) states
the invariants the code must keep, [`RUNBOOK.md`](RUNBOOK.md) is the operator
runbook, and [`HOWTO.md`](HOWTO.md) builds an experiment from nothing.

## 1. What is measured

The unit is a **cell**: one model, one arm, one condition, one task and one
repetition, run in a workspace of its own. A cell is a sequence of attempts
under a fixed budget. Each attempt has three steps:

1. **Author.** The agent edits the workspace inside a sealed container.
2. **Restore.** The engine puts back every file the agent was not allowed
   to author, so the judged tree is the seed plus the agent's authored
   surface and nothing else.
3. **Judge.** A deterministic verifier deploys and exercises the result and
   returns a verdict.

The cell ends **green** (a verdict passed), **failed** (the budget ran out)
or **revoked** (the operator withdrew it).

A cell yields:

1. **Attempts to green**, the primary measure. Across repetitions it is a
   distribution per (model, arm, condition), read as a reliability curve.
2. **The authored surface**: the lines, files and languages the agent wrote,
   counted from content, not from file names.
3. **Graded defects**, when a grading pass is run: codes from the
   experiment's taxonomy, assigned twice per cell by a named judge model and
   reported with their agreement (Cohen's κ).

## 2. Why the numbers can be trusted

1. **The agent is sealed.** It sees the workspace, its own credentials and,
   from the second attempt on, the logs of its own judged run. It sees
   nothing of the engine, the verifier or other cells.
2. **The verifier is deterministic and pinned.** It runs in a declared
   image, so every host judges in the same environment.
3. **Rig faults are refunded, authoring failures are charged.** Every
   verdict says which it is. A refunded attempt also discards its edits, so
   the retry repeats the same experiment rather than continuing a different
   one.
4. **Every verdict is tied to a fingerprint** of what judged it (§6). A
   change between a cell's start and a verify voids that verify.
5. **One file of record.** The ledger is append-only and parsed by one
   module; every derived view is recomputed from it.
6. **Provenance is recorded.** Each cell records the driver that ran it
   (`IMPL`), so populations run by different drivers can be compared rather
   than silently pooled.

## 3. Vocabulary

- **Arm** — one way of authoring the task, declared as a variant class: the
  docs the agent is given, the substrate it may touch, the tooling it
  targets. Arms of one **tech** share a substrate, an image and a contract;
  they may still be told different things (their docs name, §4).
- **Condition** — what an arm's agent is told, on an information ladder
  (for example: source only, API docs, a how-to). The matrix says which
  conditions each arm runs under.
- **Task** — the skeleton the agent starts from, the prompt, and the docs
  per arm and condition. It is versioned together with the verifier, since
  the two are correlated by construction.
- **Arrangement** and **gate** — the verifier judges an attempt under
  several arrangements (for example, orderings of load events); the gate
  passes only if all of them pass. How many, and whether the first one
  rotates with the attempt number, is the experiment's choice.
- **Verdict** — pass or fail, the stage that failed, and whether the attempt
  is charged. An uncharged failure is the rig's, and the attempt is refunded.
- **Substrate** — what an arm runs on, described by four independent
  properties:
  1. the *authoring substrate*, provisioned for the cell's lifetime;
  2. *access*, whether the agent's container can reach it;
  3. the *cap*, how many live substrates of that kind the host carries at
     once (the arm lock);
  4. the *verify substrate*, provisioned per arrangement by the verifier.
- **Retired arm** — an arm that still names existing cells but is never
  scheduled again.

## 4. Layers

1. **The engine** (`fae/`) owns everything that is not an experiment's
   choice: cells and their lifecycle, the ledger, the locks, the scheduler
   and supervisor (`conduct`), restore-and-judge, the gate loop, the seal,
   the fingerprint, the image builder, the agent CLIs, grading, the taint
   framework, and conformance of the orchestration to its formal model. It
   names no experiment.
2. **Contrib blocks** (`fae/cell/contrib/`, `fae/cell/substrate/`) are
   mechanisms without policy that an experiment may use: a load-shape law
   for a resource scaled 0↔1 under load (judged relative to the store's own
   saturation, with closed-loop blips), and Docker-in-Docker, kind-cluster
   and sandbox substrates.
3. **Experiments** each live in a repository of their own beside the
   engine. An experiment's `cli.py` imports the engine from its checkout
   (`$FAE_DIR`, else `../fae`) and makes its own repository the root, so its
   configuration, workspaces and lock plane are its own. The smallest is
   [fae-authoring-a-calculator](https://github.com/orglnte/fae-authoring-a-calculator).

The host needs Python and a Docker daemon. Everything the agent authors and
everything the verifier runs executes in containers.

## 5. The experiment definition

An experiment's `__init__.py` is loaded by path as the package
`experiment`. It declares:

1. **Arms** (`variant_classes()`): per arm its name, tech, conditions, lock
   and cap, the files the agent may author, its authoring and verify
   substrate, preflight and liveness probes, the names of what it provisions
   (so the reaper can find leftovers), an optional image layer, and
   optionally a docs name (`DOCS`) when arms of one tech are told different
   things: the api doc is then `any.<DOCS>[.<condition>].api.md`.
2. **Matrix, retired arms and seed docs** (`MATRIX`, `RETIRED`,
   `SEED_DOCS`): which conditions each arm runs; the arms kept only for
   their existing cells; and the doc each (arm, condition) must receive,
   with a floor on its size, so a missing or truncated doc stops the cell
   instead of falling back silently.
3. **Gate** (`GATE`): the arrangements and the sentence the retry prompt
   carries.
4. **Verifier** (`verifier_class()`, §6).
5. **Configuration** (`CONFIG`): the machine-local keys it reads from
   `fae.toml`, with their defaults. Host paths, caps, load tuning and model
   tags are machine-local and stay out of version control; the defaults do
   not.
6. **Fingerprint trees** (`fingerprint_trees`): source outside the
   experiment directory that a verdict depends on, such as an SDK.
7. **Taint rules, reporting and verbs**: how a rig fault shows in this
   experiment's evidence, the reference workspace the grader compares
   against, how model ids pool into scoreboard rows, the experiment's own
   reading of the results table, and its reference cell and self-test.
8. **The agent image layer**: the tools its agents need on top of the
   engine's clients.

## 6. Verification and the fingerprint

Verification is split across three owners, and each question has exactly
one:

1. **The engine** decides how a verdict is obtained and recorded, never
   what it means: the wire (a context file in, a verdict out), the verifier
   container as a session (a timeout, crash or silence is a refund, and the
   container is always removed), the declared lock, the fingerprint
   re-check, persistence, the ledger, the gate loop, refunds and stand-downs.
   It knows no stage name and no technology.
2. **The experiment's verifier** decides what green means, the same way for
   every arm: the arrangements, the load profile, the law, the end-to-end
   checks, which failures are the rig's, which contract breaks stand a cell
   down, the metrics, and the image it runs in. It reaches an arm only
   through the arm's class.
3. **Each arm's verify hooks and contract** decide how that arm's artifacts
   are brought up in the substrate the verifier measures, and check the
   promises the arm's docs make. They hold no lock and give no verdict.

The test of the split: changing the law touches only (2); adding an arm
touches only (3); changing how verifiers run or refund touches only (1).

**The fingerprint** records provenance: this verdict came from exactly this
task, verifier, arms and engine. It hashes the content and path of every
file in the experiment tree, the declared fingerprint trees and the engine's
cell package. It is pinned when a cell's driver starts, and a mismatch at a
verify voids that verify. It hashes content rather than git objects, so an
uncommitted edit changes it too, and an empty tree or a missing declared
file is fatal rather than a quietly smaller surface. Hence the **drain
rule**: the experiment tree and the engine's cell package change only in a
drain window, with no cell running (`conduct pause all`, edit, `conduct
resume all`); a resumed cell pins the new fingerprint.

## 7. Agents

An agent is a command-line client run in the sealed container — `claude`,
`opencode` or `agy` — plus a scripted `testagent` that exercises a cell
without a model. `fae.toml` maps each lane tag to a client and the model id
it receives; the tag is part of every cell id, so lanes write to disjoint
workspaces. Credentials are restaged from the configured agent home before
every attempt. The vocabulary of provider faults (rate and quota walls with
their reset hints, authentication walls, transient errors) decides whether a
failed run is retried, refunded, or stood down with its lane cooled.

The agent image is the engine's base (the clients, git, Python) plus the
experiment's layer. The base keeps the clients at their upstream versions,
since providers gate new models on a minimum client version, and the layer
is rebuilt whenever its content or the base changes.

## 8. Orchestration

`conduct` is the only scheduler and the only supervisor. The backlog is a
directory tree with one file per spec, moved from queue to running to done
by atomic renames. Conduct admits specs round-robin across model lanes under
a global cap and a per-lane cap; stands cells down on provider walls and
cools their lane; holds budget lanes near a weekly usage cap; repairs
crashed or hung cells by requeuing them; validates finished cells; and reaps
orphaned substrate by the names the arms and verifiers declare.

Every lock is a `flock(2)` on a file held by the driver's own descriptor, so
recovery after any crash is the kernel's. The orchestration's state machine
is specified in `.tla/Runs.tla`; every live transition is logged and can be
replayed against the specification. A wiped workspace retires its cell id in
that log, so a reused id is a new cell rather than a contradiction.

## 9. Reporting

`cli.py results score` validates every finished cell, writes its
`score.json`, and prints the ranked table: per (model, arm, condition), the
green rate, attempts to green, authored lines and error rates, with
significance against a baseline. It can cut the table to one driver and
compare it with its predecessor. The engine's table names no experiment's
arms or metrics; the experiment's `report_summary` adds its own reading.
Grading is a separate, explicit step with a named judge model.

## 10. Trust and limits

1. **The verifier is trusted.** It runs with the operator's uid and access
   to the Docker daemon, so that it can provision substrate. Its container
   bounds what it leaves behind, not what it may do. Only the agent is
   sealed.
2. **One host is one rig.** The verify lock serialises measurements across
   the whole fleet on a machine. Concurrency is bounded by the work slots and
   the arm caps; changing either changes the conditions every cell is
   measured under.
3. **Host sleep and host speed are rig faults, not verdicts.** Supervision
   excludes host sleep from every age it judges, a verify the host slept
   through is voided, and a load shape is tuned to each host's store before
   scored cells run.

## 11. Validation

What the repository can show about the method, and what it cannot yet.
Each check below names where to run or read it.

**Validated: the rig judges a known answer as green and a known wrong
answer as charged.**

1. **Reference solutions.** Every arm of the example experiments carries a
   known-good answer; `cli.py rig smoke` runs each through the full
   pipeline (sealed workspace, the verifier's container, the ledger) with
   no agent, and `--full-gate` runs every arrangement. A reference that is
   not green is a rig fault to fix before any scored cell runs.
2. **Negative controls.** A deliberately wrong answer must end as a charged
   failure, never as a refund or a halt (HOWTO.md §8, the `--stub` step).
   For load-judged experiments, a load profile whose top is below the
   host's knee must end in `FAIL[load-shape]`, refunded (AGENTS.md, tuning
   the load shape to a host). The fae-terraform-vs-pulumi example records,
   in its first commit, six broken solutions that each fail at the check
   written for them.
3. **Rig faults are separated from authoring failures.** A verifier that
   times out, crashes or never starts, a provider's rate or quota wall, and
   a substrate that dies under the measurement are refunded; a verify the
   host slept through is voided. AGENTS.md states each rule, and the ledger
   records each case as what it is.
4. **The authoring surface.** Before every verify, a changed seeded file is
   restored from the seed and a file outside the surface is moved out of the
   tree; the seed record sits outside the agent's mount, so the agent cannot
   rewrite what it is checked against (`fae/cell/surface.py`,
   `tests/test_surface.py`).

**Validated: the engine behaves as specified.**

5. **State machine.** `.tla/Runs.tla` specifies the orchestration; `cli.py
   rig selftest` replays the logged live transitions against it when
   `tla_verify` is available, and says so when it is not.
6. **Tests.** `tests/` is the engine's suite. `pyproject.toml` configures
   `mutmut` over `fae/` and `cli.py`; no mutation score is published.
7. **Provenance.** Every verdict carries the fingerprint of the task,
   verifier, arms and engine that produced it (§6), every cell records its
   driver (`IMPL`), and every attempt records the agent client and version
   it ran with (the ledger's AGENT line). Client versions follow upstream
   and are not part of the fingerprint.

**Not validated yet.**

8. **Run-to-run variance.** Cells are repeated (reps), but no test-retest
   study of the same model and arm, run at different times, is reported
   with the framework. How much of a difference between two rows is noise
   is left to each experiment's statistics.
9. **Grader reliability.** `fae/scoring/grader_agreement.py` computes
   Cohen's kappa between two independent judge runs over the same cells. No
   result is reported in the repository, and agreement between the LLM
   judge and human graders has not been measured. The counted metrics
   (green, attempts, minutes, lines) do not depend on a judge.
10. **External validity.** That attempts and minutes to green on these
    tasks predict how hard an approach is for agents on real work is an
    assumption of each experiment, not something the framework establishes.
