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

The unit is a **cell**: one model, one variant, one task and one
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
   distribution per (model, variant), read as a reliability curve.
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

- **Variant** — one complete set of what the agent is given and how its
  work is judged: the code it starts from, what it may write, what it
  reads, its tools, how the verifier runs what it wrote, and the infra
  around both. Experimental design calls this a *treatment* (a
  *condition* in psychology, an *arm* in clinical trials); "variant"
  because more readers know the word. A variant is one file,
  `<experiment>/variants/<id>.toml` (§5), and the file is the whole of it:
  two variants differ exactly where their files differ.
- **Factors** — what a variant is a level of, as data in its file (for
  example `tech`, `access`, `docs`). The engine pools nothing by them;
  results group by variant and by each factor (§9).
- **Task** — the skeleton every variant's template starts from, and the
  prompt. It is versioned together with the verifier, since the two are
  correlated by construction.
- **Arrangement** and **gate** — the verifier judges an attempt under
  several arrangements (for example, orderings of load events); the gate
  passes only if all of them pass. How many, and whether the first one
  rotates with the attempt number, is the experiment's choice.
- **Verdict** — pass or fail, the stage that failed, and whether the attempt
  is charged. An uncharged failure is the rig's, and the attempt is refunded.
- **Infra** — what exists around a variant's program, an object of its
  own (an `Infra` class the variant file names, instantiated per cell with
  the variant), described by four independent properties:
  1. the *cell infra*, provisioned for the cell's lifetime;
  2. *access*, whether the agent's container can reach it
     (`access_infra` in the file);
  3. the *cap*, how many live infra of that kind the host carries at
     once (the variant's lock);
  4. the *verify infra*, provisioned per arrangement inside the
     verifier's container.
  Variants that share an infra class share its code; what one variant
  sets differently (its access, its parameters) the class reads from the
  variant.
- **Retired variant** — a variant that still names existing cells but is
  never scheduled again (`retired = true`).

## 4. Layers

1. **The engine** (`fae/`) owns everything that is not an experiment's
   choice: cells and their lifecycle, the ledger, the locks, the scheduler
   and supervisor (`conduct`), restore-and-judge, the gate loop, the seal,
   the fingerprint, the image builder, the agent CLIs, grading, the taint
   framework, and conformance of the orchestration to its formal model. It
   names no experiment.
2. **Contrib blocks** (`fae/cell/contrib/`, `fae/cell/infra/`) are
   mechanisms without policy that an experiment may use: a load-shape law
   for a resource scaled 0↔1 under load (judged relative to the store's own
   saturation, with closed-loop blips), Docker-in-Docker and kind-cluster
   infra, and the secure runner every judged program runs in.
3. **Experiments** each live in a repository of their own beside the
   engine. An experiment's `cli.py` imports the engine from its checkout
   (`$FAE_DIR`, else `../fae`) and makes its own repository the root, so its
   configuration, workspaces and lock plane are its own. The smallest is
   [fae-authoring-a-calculator](https://github.com/orglnte/fae-authoring-a-calculator).

The host needs Python and a Docker daemon. Everything the agent authors and
everything the verifier runs executes in containers.

## 5. The experiment definition

An experiment's `__init__.py` is loaded by path as the package
`experiment`. Its variants are the files beside it, one per variant:

```toml
# experiment/variants/<id>.toml — the id is the file's stem
label = "..."                       # what reports show; the id by default
retired = false
factors = { ... }                   # what this variant is a level of

[authoring]                         # what the agent gets
template = ["task/skeleton", ...]   # directories merged into the workspace, in order
surface = { files = [...], prefixes = [...] }   # what it may write, within the template
tools = "..."                       # its image layer, over the agents' base
access_infra = false                # its container reaches the cell's infra
[authoring.inputs]                  # workspace path = source file it reads
"TODO.md" = "task/T1.PROMPT.md"

[verify]                            # how the result is judged
image_dir = "..."                   # the verify container's layer
reference = "..."                   # the known answer, for smoke
[verify.run]                        # how the verifier runs the artifacts
command = [...]
serves = 8080                       # kept running on the cell's network; else run once
image = "..."                       # or image_dir; none: the verify image
build = [...]

[infra]                             # optional: the default checks docker and the run image
class = "module:Class"              # an Infra subclass of the experiment package
lock = "..."                        # held for the cell's life; lock_slots its default cap
params = { ... }                    # the class's own settings
```

Paths are relative to the experiment directory. A key the engine does not
know is refused, so a typo cannot drop a declaration; an input the template
also provides is refused, so the agent's input has one source. Seeding is
the template directories merged in order plus each input copied to its
path, recorded in the manifest the surface is checked against. Everything
an agent of a variant reads is therefore named in that variant's file.

`__init__.py` declares the rest:

1. **Gate** (`GATE`): the arrangements and the sentence the retry prompt
   carries.
2. **Verifier** (`verifier_class()`, §6).
3. **Configuration** (`CONFIG`): the machine-local keys it reads from
   `fae.toml`, with their defaults. Host paths, caps, load tuning and model
   tags are machine-local and stay out of version control; the defaults do
   not.
4. **Fingerprint trees** (`fingerprint_trees`): source outside the
   experiment directory that a verdict depends on, such as an SDK.
5. **Taint rules, reporting and verbs**: how a rig fault shows in this
   experiment's evidence, the reference workspace the grader compares
   against, how model ids pool into scoreboard rows, the experiment's own
   reading of the results table, and its reference cell and self-test.
6. **The agents' base image** (`Dockerfile.agent-base` at the root): the
   engine's clients and what every variant's agent needs.

Python exists in an experiment only for its infra classes (one per kind of
infra; the methods: `cell_setup`/`cell_teardown`, `verify_setup`/
`verify_teardown`, `ok`, `alive`, and the names it provisions, `PREFIXES`
and `identities`) and for its verifier.

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
   every variant: the arrangements, the load profile, the law, the end-to-end
   checks, which failures are the rig's, which contract breaks stand a cell
   down, the metrics, and the image it runs in. It runs the artifacts as
   the variant's `[verify.run]` says (`secrunner.for_variant`): it starts
   them, holds their readiness and stops them before the infra's teardown.
   It reaches a variant's infra only through the infra class.
3. **Each infra class's verify hooks and contract** decide what world an
   arrangement runs in (`verify_setup` / `verify_teardown`), and check the
   promises the variant's docs make. They hold no lock and give no verdict.

The test of the split: changing the law touches only (2); adding a variant
is a file, and a new kind of infra touches only (3); changing how verifiers
run or refund touches only (1).

**The fingerprint** records provenance: this verdict came from exactly this
task, verifier, variants and engine. It hashes the content and path of every
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
orphaned infra by the names the infra classes and verifiers declare.

Every lock is a `flock(2)` on a file held by the driver's own descriptor, so
recovery after any crash is the kernel's. The orchestration's state machine
is specified in `.tla/Runs.tla`; every live transition is logged and can be
replayed against the specification. A wiped workspace retires its cell id in
that log, so a reused id is a new cell rather than a contradiction.

## 9. Reporting

`cli.py results score` validates every finished cell, writes its
`score.json`, and prints the ranked table: per (model, variant), the green
rate, attempts to green, authored lines and error rates, with significance
against a baseline; the aggregate also groups by each factor the variant
files declare. It can cut the table to one driver and compare it with its
predecessor. The engine's table names no experiment's variants or metrics;
the experiment's `report_summary` adds its own reading.
Grading is a separate, explicit step with a named judge model.

## 10. Trust and limits

1. **The verifier is trusted.** It runs with the operator's uid and access
   to the Docker daemon, so that it can provision infra. Its container
   bounds what it leaves behind, not what it may do. Only the agent is
   sealed.
2. **One host is one rig.** The verify lock serialises measurements across
   the whole fleet on a machine. Concurrency is bounded by the work slots and
   the infra caps; changing either changes the circumstances every cell is
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

1. **Reference solutions.** Every variant of the example experiments carries a
   known-good answer; `cli.py experiment smoke` runs each through the full
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
   an infra that dies under the measurement are refunded; a verify the
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
   verifier, variants and engine that produced it (§6), every cell records its
   driver (`IMPL`), and every attempt records the agent client and version
   it ran with (the ledger's AGENT line). Client versions follow upstream
   and are not part of the fingerprint.

**Not validated yet.**

8. **Run-to-run variance.** Cells are repeated (reps), but no test-retest
   study of the same model and variant, run at different times, is reported
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
