# Your first FAE experiment

*From an empty directory to a sealed green cell, in about twenty minutes.*

An FAE experiment runs coding agents against a task, judges every attempt
with a verifier you write, and records how many attempts each agent needed.
The unit of work is a **cell**: one `(agent, variant, task, rep)`
run with its own workspace, its own attempt budget (10) and its own ledger.
A fleet of cells under one scheduler is an experiment.

This howto builds the smallest experiment that exercises every interface:
one task, one variant, one verifier. When you are done you will have run a
cell three ways: with no agent at all, with a scripted agent, and with a
real one. The finished reference is
[fae-authoring-a-calculator](https://github.com/orglnte/fae-authoring-a-calculator), which does the same
thing in three languages; keep it open beside you.

## Contents

1. [Before you start](#1-before-you-start)
2. [How an experiment is laid out](#2-how-an-experiment-is-laid-out)
3. [Write the definition](#3-write-the-definition)
4. [Write the task](#4-write-the-task)
5. [Write the variant](#5-write-the-variant)
6. [Write the verifier](#6-write-the-verifier)
7. [Point the engine at it](#7-point-the-engine-at-it)
8. [Run a cell with no agent](#8-run-a-cell-with-no-agent)
9. [Run a cell with the scripted agent](#9-run-a-cell-with-the-scripted-agent)
10. [Run a cell with a real agent](#10-run-a-cell-with-a-real-agent)
11. [Run the fleet](#11-run-the-fleet)
12. [Reading what came out](#12-reading-what-came-out)
13. [Where to go next](#13-where-to-go-next)
14. [Interface cheat sheet](#14-interface-cheat-sheet)

---

## 1. Before you start

You need:

- **Python 3.11+** and the two runtime packages the engine imports:

  ```sh
  python3 -m pip install typer ujson
  ```

  or, from the FAE checkout, `pip install -e .` to also get the `fae`
  command. Everything below uses `python3 cli.py …`, which works uninstalled.

- **A docker daemon** you can talk to. Nothing an agent writes ever runs on
  your machine: the verifier runs in a container, and it runs the agent's
  program in another container. The host needs Python and docker, nothing
  else.

- **Optionally, a coding-agent CLI** (`claude`, `agy` or `opencode`) logged
  in, for section 10. Sections 8 and 9 need no agent.

Clone the repo and work from its root. The engine is the `fae/` package;
`cli.py` beside it is the operator front-end.

```sh
git clone <fae repo> fae && cd fae
```

## 2. How an experiment is laid out

**One root runs one experiment.** The engine finds the experiment through
one setting, `EXPERIMENT_DIR` (the environment, else `[paths]
experiment_dir` in `fae.toml`, default `experiment`), and loads that
directory as a Python package by path. It never imports your experiment by
name and knows nothing about its vocabulary: variants, stages, metrics, the
lot, come from your definition and its variant files.

The experiment we are about to write is called **shout**: the agent must
make a program that reads one line from stdin and prints it upper-cased.
Its tree:

```
shout/
├── __init__.py                 # the definition: the gate, the verifier
├── task/
│   ├── T1.PROMPT.md            # the agent's brief (becomes TODO.md)
│   └── skeleton/
│       └── README.md           # every variant's common files
├── variants/
│   ├── python.toml             # the variant: what the agent gets, how it is judged
│   └── python/
│       └── seed/               # the files python.toml names
│           ├── overlay/shout.py                 # the stub the agent starts from
│           ├── T1.python.project_layout.md      # what the agent may change
│           ├── any.python.api.md                # the contract
│           └── reference/overlay/shout.py       # the known-good answer
└── verifier/
    ├── __init__.py             # class ShoutVerifier(Verifier)
    └── Dockerfile              # the environment the verifier runs in
```

Three things map onto three interfaces:

| Path | Interface | Answers |
|---|---|---|
| `__init__.py` | `Definition` | which gate, which verifier, which config |
| `variants/<id>.toml` | a variant | what the agent starts from, may write, reads, runs with; how its program is run; its infra |
| `verifier/` | `Verifier` | how one attempt is judged, in what environment |

A variant that needs infra beyond a container of an image (a sidecar, a
cluster) names an infra class, `[infra] class = "module:Class"`, Python in
your package (§13). Shout needs none.

Create the skeleton:

```sh
mkdir -p shout/task/skeleton shout/variants/python/seed/overlay \
         shout/variants/python/seed/reference/overlay shout/verifier
```

## 3. Write the definition

`shout/__init__.py` is what the engine loads. Keep it light: the verifier
is returned by a function so importing the definition does not pull
docker-touching code in before the config has been read.

```python
"""shout — read one line, print it upper-cased. The smallest experiment."""
from fae.experiment import Gate

NAME = "shout"

# One arrangement per attempt. Gate(("A", "B", "C")) would run the verifier
# three times per attempt, and green means all three passed.
GATE = Gate()


def verifier_class():
    from .verifier import ShoutVerifier
    return ShoutVerifier
```

Everything here is optional and has an engine default except
`verifier_class` (a cell cannot verify without one). The full list is the
docstring of `fae/experiment.py`; the ones you will meet later are
`CONFIG` (machine-local keys your experiment needs from `fae.toml`),
`taint_rules` and `verbs`. The variants are not declared here: they are
the files in `variants/`.

A **variant** is one complete set of what the agent is given and how its
work is judged; experimental design calls it a treatment. `python` here,
three languages in the calculator. Two variants that differ only in the
docs the agent reads are two files that differ in one input.

## 4. Write the task

The task is shared by every variant: the skeleton every workspace starts
from, and the prompt.

`shout/task/skeleton/README.md`:

```markdown
# shout

Reads one line from stdin and prints it upper-cased, followed by a newline.
See `TODO.md` for the task and `docs/` for the contract.
```

`shout/task/T1.PROMPT.md`:

```markdown
# TODO — make shout work

The program in this workspace is a stub. Implement it so that it reads one
line from standard input and prints the same line upper-cased. Read
`docs/project.md` for what you may change and `docs/python.md` for the
contract the verifier holds you to.

**Aim to get it right in as few submissions as possible.** Each submission
runs the verifier; treat it as a costly deployment, not a free retry.
```

The task id `T1` is part of every cell id and of every file name below; an
experiment can carry several tasks side by side.

## 5. Write the variant

A variant is a file. It answers what the agent starts from and may write,
what it reads, and how the verifier runs what it wrote.

`shout/variants/python.toml` (the id is the file's stem, `python`):

```toml
[authoring]
template = ["task/skeleton", "variants/python/seed/overlay"]
surface = { files = ["shout.py"] }      # the agent may write this; the rest is restored

[authoring.inputs]
"TODO.md" = "task/T1.PROMPT.md"
"docs/project.md" = "variants/python/seed/T1.python.project_layout.md"
"docs/python.md" = "variants/python/seed/any.python.api.md"

[verify]
reference = "variants/python/seed/reference/overlay"

[verify.run]
image = "python:3.12.3-slim"            # where the agent's program runs
command = ["python3", "shout.py"]
```

What each part does:

- `[authoring] template`: directories merged into the agent's workspace, in
  order, a later one winning on a shared path. `surface`: the exact files
  (`files`) and directory prefixes (`prefixes`) the agent may write;
  every other seeded file is restored before a verdict. Required: a cell
  of a variant without one is refused.
- `[authoring.inputs]`: what the agent reads, each source file copied to
  its workspace path. That is the whole seal on what an agent can read: an
  input the template also provides is refused, so every file has one
  source. The prompt tells the agent to read `TODO.md`.
- `[verify] reference`: the known answer, laid over the template for a
  reference (smoke) cell.
- `[verify.run]`: how the verifier runs the artifacts
  (`secrunner.for_variant`): `command` in `image` (a pinned tag, or
  `image_dir`, a directory with a Dockerfile the engine builds and tags by
  content; none: the verify image), `build` first when the program needs
  compiling. A program that `serves` a port is kept running on the cell's
  network; any other runs once, with no network.

A key the engine does not know is refused, so a typo cannot drop a
declaration. A file with no `[infra]` gets the engine's default infra:
before every attempt the docker daemon answers and the run image is
present (`docker pull` it, or built from `image_dir`), else the cell halts
before an attempt is spent; before every arrangement, and after a charged
fail, the daemon still answers, else the arrangement is void rather than
scored against the agent.

Now the files it names: what the agent is handed on top of the skeleton.

`shout/variants/python/seed/overlay/shout.py`, the stub:

```python
"""shout: read one line from stdin, print it upper-cased."""
import sys


def shout(line: str) -> str:
    raise NotImplementedError("TODO.md")


if __name__ == "__main__":
    print(shout(sys.stdin.readline()))
```

`shout/variants/python/seed/T1.python.project_layout.md`:

```markdown
# Project layout — what is yours to change

- `shout.py` — the only file you author. Everything else is fixed and is
  restored before every verification.
- `README.md`, `docs/` — read-only.

The verifier runs `python3 shout.py` with one line on stdin.
```

`shout/variants/python/seed/any.python.api.md`:

```markdown
# The contract

The program reads ONE line from standard input and prints it upper-cased,
followed by a newline, exiting 0. Trailing whitespace on the input is
dropped.

| stdin        | stdout       |
|--------------|--------------|
| `hello`      | `HELLO`      |
| `Hello, fae`| `HELLO, FAE`|
| `42`         | `42`         |
```

The names are yours: the file says where each one goes.

`shout/variants/python/seed/reference/overlay/shout.py`, the known-good
answer. It is what you verify the rig with before any agent runs, and what
the scripted agent writes when its plan says "green":

```python
"""shout: read one line from stdin, print it upper-cased."""
import sys


def shout(line: str) -> str:
    return line.rstrip().upper()


if __name__ == "__main__":
    print(shout(sys.stdin.readline()))
```

## 6. Write the verifier

The verifier is the whole judgment. The engine hands it a `Ctx` (where the
workspace is, which variant, which arrangement) and reads back one
`Verdict`. It runs **out of process and out of the host**: in a container
of an image you declare, with the workspace mounted at its own path and the
docker socket available, so it can run the agent's program in a sibling
container and nothing untrusted touches the machine.

`shout/verifier/Dockerfile`, the environment the verifier itself runs in:
the engine's Python plus a docker client. Every version pinned.

```dockerfile
FROM python:3.12.3-slim
ARG DOCKER_CLI_VERSION=27.1.1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && arch="$(dpkg --print-architecture)"; case "$arch" in \
        amd64) dc=x86_64 ;; arm64) dc=aarch64 ;; *) dc="$arch" ;; esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dc}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
```

The engine tags the image by the content of this directory
(`fae-shout-verifier:<sha12>`), builds it when missing and never rebuilds
it otherwise. Change a pin, get a new image.

`shout/verifier/__init__.py`. `secrunner.for_variant` does the container
work from the variant's `[verify.run]` (a container of its image over a
directory, no network, memory, pid and CPU ceilings, removed when the
program ends or times out); `secrunner.fresh_copy` is the verifier's own
copy of the artifacts, so nothing writes into the judged tree.

```python
"""The shout verifier: run the program over a fixed table of lines inside
the variant's image; green iff every answer matches."""
import time
from pathlib import Path

from fae.cell import experiment as _experiment
from fae.cell.infra import secrunner
from fae.cell.verify import Verdict, Verifier

CASES = (
    ("hello", "HELLO"),
    ("Hello, fae", "HELLO, FAE"),
    ("42", "42"),
    ("mixed Case line", "MIXED CASE LINE"),
)
TIMEOUT_S = 20


def run_case(variant, cid, workdir, line):
    """(stdout, error) of one container run with `line` on stdin."""
    rc, out, err = secrunner.for_variant(variant, cid, workdir).run(
        TIMEOUT_S, stdin=line + "\n", split=True)
    if rc is None:
        return None, err          # it never started, or it timed out
    if rc != 0:
        return None, f"exit {rc}: {err.strip()[-200:]}"
    return out.strip(), None


class ShoutVerifier(Verifier):
    IMAGE_DIR = Path(__file__).parent     # the Dockerfile above
    FILES = ("verify.log",)               # archived per arrangement

    def verify(self, ctx):
        t0 = time.time()
        artifacts, out = Path(ctx.artifacts), Path(ctx.out)
        variant = _experiment.current().variant(ctx.variant)

        def done(ok, stage, why, passed=0):
            return Verdict(ok=ok, stage=stage, why=why,
                           metrics={"cases": len(CASES), "passed": passed},
                           seconds=time.time() - t0)

        workdir = secrunner.fresh_copy(artifacts, out)
        passed, first_why = 0, ""
        with (out / "verify.log").open("w") as log:
            if not (workdir / "shout.py").is_file():
                log.write("FAIL[deploy]: no shout.py in the workspace\n")
                return done(False, "deploy", "no shout.py")
            for line, want in CASES:
                got, err = run_case(variant, ctx.cid, workdir, line)
                ok = err is None and got == want
                passed += ok
                log.write(f"{'ok  ' if ok else 'FAIL'} {line!r} -> {got!r}"
                          f"{'' if ok else f' (want {want!r}{'; ' + err if err else ''})'}\n")
                if not ok and not first_why:
                    first_why = f"{line!r} -> {got if err is None else err}, want {want!r}"
            log.write(f"cases: {passed}/{len(CASES)}\n")
        green = passed == len(CASES)
        return done(green, "" if green else "cases", first_why, passed)
```

What a `Verdict` carries, and what the engine does with each field:

| Field | Meaning |
|---|---|
| `ok` | green or not |
| `stage` | where it failed (`deploy`, `cases`, …): your vocabulary, recorded in the ledger |
| `why` | one line for the ledger and the agent's feedback |
| `charge` | **False = a rig fault.** The attempt is refunded, the agent's edits discarded, the same attempt retried. Default True. |
| `stand_down` | a non-empty tuple pauses the cell for the operator (a broken promise in the environment) |
| `metrics` | written as `metrics.json` in the workspace |
| `files` | archived under `arrangements/NN-a<attempt>-<label>-<end state>/` with the class's `FEEDBACK_LOGS`, this run's `verifier.log` and a `verdict.json` (falls back to the class's `FILES`) |

The one rule: a failure of the **rig** (docker died, the image vanished, a
timeout in your own tooling) answers `charge=False`; a failure of the
**program** is charged. Crashes, hangs past the timeout and empty answers
from the verifier are `charge=False` automatically.

Every run is archived, whatever its end state: `green`, `charged`,
`refunded`, or `interrupted` (a verify that never returned, found at the
next one). The next attempt's feedback is the last `charged` run's, and the
logs mounted at `/feedback/` are the class's `FEEDBACK_LOGS` from it
(default `verify.log`, `deploy.log`).

## 7. Point the engine at it

`fae.toml` at the root is the machine-local config. Write it pointed at
the experiment:

```sh
python3 cli.py experiment init --experiment shout     # refuses to overwrite an existing one
```

which sets `[paths] experiment_dir = "shout"` and renders the rest for
that experiment (its lock caps, its declared config keys). If the file
already exists, edit that key. `EXPERIMENT_DIR=shout` in the environment
overrides the file on any single command; the rest of this howto passes
it so the commands are self-contained whatever the file says.

Pull the runtime image, then check that everything is in place:

```sh
docker pull python:3.12.3-slim
EXPERIMENT_DIR=shout python3 cli.py experiment check --walk
```

`experiment init` offers the same walk when it has written the file. The
check goes in the order of this howto: the host, the config and the
definition, each variant file (its authoring surface, its liveness probe,
its template and inputs), every active variant seeded into a throwaway workspace
root by the same `prepare()` a real cell runs, the engine's and the corpus's
invariants (the engine's own functions, a shell that names cells as the
scheduler does, the experiment's own checks, finished cells whose record
disagrees with their state), the docker daemon, each variant's infra
preflight (`ok()`) with the verifier image built, so the first cell does not
pay for the build under a lock, the agents' images (built when missing; a
CLI behind upstream is noted), and no leftovers of dead cells (`experiment
repair` reaps them). `--walk` explains each step before running it and, on
a failure, names the fix and waits for you to retry. Without `--walk` it
prints a checklist and exits 1 on any failure; `--static` skips the docker
steps while you are still writing the definition; `--smoke` adds section 8's
reference run; `--tla-trace` replays the fleet's recorded transitions against
the TLA+ model of the cell lifecycle.

## 8. Run a cell with no agent

Before spending tokens on an agent, prove the rig judges the reference
correctly. `--stub DIR` replaces the agent with "copy DIR over the
artifacts", one attempt, the full gate:

```sh
EXPERIMENT_DIR=shout AGENT=stub WORKSPACES_DIR=/tmp/shout-ws \
  python3 -m fae.cell T1 python 1 \
  --stub shout/variants/python/seed/reference/overlay
```

The positional arguments are `TASK VARIANT [REP]`; `AGENT` and `EFFORT`
come from the environment and name the cell: `stub_high_python_T1_r1`. `WORKSPACES_DIR` keeps this dry run out
of the real workspace root (default `workspaces.nosync/`).

The exit code is the first thing to read:

| Exit | Meaning |
|---|---|
| 0 | the cell ended with a verdict, green or failed |
| 45 | `HALT[infra]`: the host could not carry the cell; nothing charged |
| 46 | the workspace is already sealed; a finished cell is read-only |

Then the workspace, `/tmp/shout-ws/stub_high_python_T1_r1/`:

```
artifacts/            the workspace the agent (here: the stub) saw and edited
iterations.log        THE ledger: one TAB-separated event per line
metrics.json          the last verify's numbers (your Verdict.metrics)
verify.log            your verifier's log
verifier.log          the engine's log of running your verifier
arrangements/01-a1-seed-green/  every verify run: its logs and verdict.json
hooks.log             what the variant's infra logged (its checks)
.sealed               written on green or on a spent budget; the cell is done
```

Look for the green line in the ledger:

```sh
grep -P '\tITER\t' /tmp/shout-ws/stub_high_python_T1_r1/iterations.log
```

Now prove a wrong answer is a **charged** fail, not a rig fault:

```sh
mkdir -p /tmp/broken && printf 'print(input())\n' > /tmp/broken/shout.py
EXPERIMENT_DIR=shout AGENT=stub WORKSPACES_DIR=/tmp/shout-ws2 \
  python3 -m fae.cell T1 python 1 --stub /tmp/broken
```

The ledger should carry `ITER failed … stage=cases`, `metrics.json` should
say one case passed (the digits), and there must be no `HALT` line. If
you see `stage=verifier-image`, `stage=verifier` or a `HALT`, the rig is
wrong, not the program; `verifier.log` says why.

## 9. Run a cell with the scripted agent

`testagent` is a coding agent that follows a script. It runs in the same
sealed container, with the same two mounts and the same invocation as a
real agent, and it audits what it was handed, so a bug in how the engine
starts an agent shows up here instead of after a night of tokens.

Build the agent base image once (it holds the three real clients too; a
variant whose file names `[authoring] tools` gets its own layer over it,
built by the engine when a cell of that variant is started, and by the run
at its preflight):

```sh
bash fae/agent-container/build.sh
```

Then run a cell whose agent fails twice and solves on the third attempt:

```sh
EXPERIMENT_DIR=shout AGENT=testagent TESTAGENT_PLAN=fail,fail,green \
  WORKSPACES_DIR=/tmp/shout-ws3 \
  python3 -m fae.cell T1 python 1
```

The ledger now shows three attempts: two `ITER failed`, then `ITER green`,
and the retry prompt for attempts 2 and 3 carried the previous attempt's
`verify.log` under `/feedback`. Other plans: `green` (solve at once),
`fail` (spend the whole budget), `noedit`, `crash`, `silent`, `limit`; the
grammar is at the top of `fae/testagent.py`. The agent's own findings
about its sandbox are in the cell's `.agent-testagent/findings.json`.

## 10. Run a cell with a real agent

The agents an experiment compares are its own, in git:
`<experiment>/agents.toml`, one `[agents.<tag>]` per agent with its `cli`
(`claude`, `agy`, `opencode`), its `model` and optionally its `effort`. The
tag names every cell id. Each agent authenticates through a credentials
home on this machine, never through a config value: `fae.toml`
`[agents.<tag>] home`, else the CLI's default (`.agent-home/.claude`,
`.agent-home/.gemini`, `.agent-home/.opencode`). Log the CLI in once and the
engine stages a fresh per-cell copy before every attempt. `README.md` has
the per-CLI details.

```toml
# shout/agents.toml
[agents.sonnet]
cli = "claude"
model = "claude-sonnet-5"
```

Start one cell in the background and watch it:

```sh
EXPERIMENT_DIR=shout python3 cli.py cell spawn sonnet python --rep 1
EXPERIMENT_DIR=shout python3 cli.py experiment status
EXPERIMENT_DIR=shout python3 cli.py cell tail sonnet_high_python_T1_r1
```

`experiment status` shows the cell's phase (`agent`, `verify`, a waiting phase),
its attempt count against the budget and its gate progress. When it seals,
the same files as in section 8 are under
`workspaces.nosync/sonnet_high_python_T1_r1/`, plus one log per
attempt with the agent's full transcript.

## 11. Run the fleet

An experiment is many cells, not one. `experiment run` is the one scheduler and
the one supervisor: you fill a backlog, it admits cells under its caps,
repairs crashed ones, validates finished ones.

```sh
# three reps of every active variant, for two agents
EXPERIMENT_DIR=shout python3 cli.py queue add sonnet --matrix --reps 3
EXPERIMENT_DIR=shout python3 cli.py queue add haiku  --matrix --reps 3

# the scheduler, in the foreground; Ctrl-C detaches, cells keep running
EXPERIMENT_DIR=shout python3 cli.py experiment run -n 2 --per-agent 1
```

`-n` is the global cap and should equal the number of lanes; `--per-agent`
keeps one live cell per agent so lanes are comparable. In another
terminal:

```sh
EXPERIMENT_DIR=shout python3 cli.py experiment status          # the table
EXPERIMENT_DIR=shout python3 cli.py results run-report    # what this run produced so far
EXPERIMENT_DIR=shout python3 cli.py results score         # validate -> score.json -> table
```

The backlog is a directory tree, one file per spec, moved by atomic rename
between `queue/`, `running/` and `done/` under `workspaces.nosync/.queues/`,
so an interrupted scheduler neither loses nor duplicates work.

## 12. Reading what came out

The ledger, `iterations.log`, is the file of record; `fae/cell/ledger.py` is
its only parser. Each attempt is one `ITER` line:

```
2026-09-16T10:02:11Z	ITER	failed	attempt=1 stage=cases verify_s=4 tree=9f1c0b2a7d3e shape-gate=seed
2026-09-16T10:04:40Z	ITER	green	attempt=2 verify_s=4 tree=1b7e44c0aa19
```

`tree=` is the git tree hash of the artifacts the verdict was judged on;
`shape-gate=` names the arrangement that failed; a verifier whose metrics
carry an `e2e_pass`/`e2e_total` pair also gets `e2e=<pass>/<total>`. The
`why` of a failed verdict is in `verify.log` and in the next attempt's
feedback.

and a cell ends in one of:

| Ending | Meaning |
|---|---|
| `ITER green` + `.sealed verdict=green` | the agent solved it; attempts-to-green is the attempt number |
| `.sealed` after 10 attempts, no green | budget spent; a failure, comparable across cells |
| `HALT` | the rig could not carry the cell; nothing charged; the operator investigates |
| `ALERT` | something the operator must read (an infra fault, a stand-down) |

`metrics.json` holds only the last verify's numbers and never decides
doneness. `results score` reads the ledger, validates each cell against the
engine's taint rules (and yours, if the definition declares
`taint_rules`), and writes `score.json` beside it. `results aggregate`
turns a directory of scored cells into the results table.

## 13. Where to go next

- **A gate of several arrangements.** `GATE = Gate(("A", "B", "C"),
  rotate=True)` runs your verifier three times per attempt with
  `ctx.arrangement` set; green means all three passed. `rotate` moves the
  first arrangement with the attempt number so an agent never sees the
  same first timeline twice; `feedback_note` is the sentence the retry
  prompt carries when one fails.
- **Several variants.** One file each. Variants that read different docs
  differ in one `[authoring.inputs]` line; `factors = { docs = "..." }`
  says what each is a level of, and the results table groups by it.
- **An infra of its own.** `[infra] class = "infra:Sidecar"` names a
  subclass of `fae.cell.infra.base.Infra` in your package, instantiated per
  cell with the variant (`self.variant`: its file's data). It writes
  `ok()` (preflight; False halts before an attempt) and `alive()`
  (liveness; **must be declared**, or the cell halts at preflight), and
  any of:
  - `cell_setup()` / `cell_teardown()`, what exists for the cell's life (a
    sandbox cluster, its own docker daemon). `cell_setup` returns the extra
    docker arguments for the agent's container when the file says
    `access_infra = true`; the driver calls both, and teardown runs on
    every path.
  - `verify_setup(ctx, env)` / `verify_teardown(ctx, env)`, the world one
    arrangement runs in, brought up fresh. Your verifier calls them inside
    the verify container, over the daemon's socket, and runs the
    artifacts on that world from `[verify.run]` in between; it stops them
    before `verify_teardown`.
  - `PREFIXES` and `identities(cid)`, the names of what it provisions, so
    the reaper can find what a dead cell left.

  Variants that differ only in what the agent may reach share one class:
  it reads `self.variant.ACCESS_INFRA` and `self.variant.PARAMS`.
- **An exclusive resource.** `[infra] lock = "gpu"` and `lock_slots = 1`
  serialise a variant's cells fleet-wide; the cap is `[slots] arm_gpu` in
  `fae.toml`.
- **Tools your verifier needs.** Put them in the verifier's Dockerfile.
  A variant that needs more (a compiler, a load generator) names its own
  layer, `[verify] image_dir`, a Dockerfile built `FROM $BASE`, the
  verifier's image.
- **Taint rules, reporting.** `taint_rules`, `POOLED_MODELS` on the
  definition; see
  `experiment/__init__.py` for a full-size example and `AGENTS.md` for
  what each invariant protects.

## 14. Interface cheat sheet

`python3 cli.py experiment check --static` checks a definition and its
variant files against all of the below, without docker.

**Definition** (`<EXPERIMENT_DIR>/__init__.py`, read by `fae/experiment.py`):

| Name | Required | Default |
|---|---|---|
| `verifier_class()` | yes (a cell cannot verify without one) | |
| `NAME` | no | the directory name |
| `GATE` | no | `Gate()`: one arrangement |
| `CONFIG` | no | `{}` |
| `fingerprint_trees(conf)` | no | `[]` |
| `taint_rules`, `report_text`, `POOLED_MODELS`, `verbs()` | no | none |

**Variant file** (`<EXPERIMENT_DIR>/variants/<id>.toml`, read by
`fae/cell/variants/files.py`; paths relative to the experiment directory,
unknown keys refused):

| Key | Purpose |
|---|---|
| `label`, `retired`, `factors` | what reports show; never scheduled again; what it is a level of |
| `[authoring] template` | directories merged into the workspace, in order |
| `[authoring] surface` | `{ files, prefixes }` the agent may write; required |
| `[authoring] tools` | the agent's image layer, over the base |
| `[authoring] access_infra` | the agent's container reaches the cell's infra |
| `[authoring.inputs]` | workspace path = the source file the agent reads |
| `[verify] image_dir` | the verify container's layer, over the verifier's image |
| `[verify] reference` | the known answer, for a reference cell |
| `[verify.run] command, serves, image, image_dir, build` | how the verifier runs the artifacts |
| `[infra] class` | `module:Class`, an `Infra` subclass; none: the engine's default |
| `[infra] lock, lock_slots, params` | an exclusive lock held for the cell's life; the class's settings |

**Infra** (`fae/cell/infra/base.py`, instantiated with the variant and the cell):

| Member | Purpose |
|---|---|
| `ok()` | preflight; False halts before an attempt |
| `alive()` | liveness before/after each arrangement; **must be declared**, or the cell halts at preflight |
| `cell_setup()` / `cell_teardown()` | the cell-lifetime infra, driver-run |
| `verify_setup(ctx, env)` / `verify_teardown(ctx, env)` | the per-arrangement infra, verifier-run |
| `PREFIXES`, `identities(cid)`, `stray()`, `sweep()` | what a reaper may find and remove |
| `image_context(conf)`, `agent_image_context(conf)` | sources staged beside the variant's verify and agent Dockerfiles |

**Verifier** (`fae/cell/verify.py`):

| Member | Purpose |
|---|---|
| `IMAGE_DIR` | **required**: the Dockerfile of the environment it runs in |
| `verify(ctx) -> Verdict` | the whole judgment |
| `FILES` | archived per arrangement when the Verdict names none |
| `RUN_LOG` | where the artifacts' runner's output is kept when it is stopped |
| `EXCLUSIVE` | a lock the engine holds around every run |
| `PREFIXES`, `identities(cid)` | what a verify provisions, for the reaper |
| `MEASURED_STAGES` | charged fails at these stages are voided when the infra is found dead afterwards |

**Ctx** fields: `root`, `experiment_dir`, `workspace`, `artifacts`, `out`,
`cid`, `task`, `variant`, `arrangement`, `expected_fp`, `mode`
(`cell` | `reverify` | `exp1`).

**Cell id**: `<agent>_<effort>[_smoke]_<variant>_<task>_r<rep>`, encoded in
one place (`fae/experiment.py`: `cell_id`, `parse_cell_id`).

---

### Status notes for reviewers

This document describes the interface as it stands. For a variant whose
infra is more than one container, the engine's infra blocks
(`fae/cell/infra/`: `dind`, `kind`, `secrunner`) are what its infra
class composes.
