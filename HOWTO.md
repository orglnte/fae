# Your first FAE experiment

*From an empty directory to a sealed green cell, in about twenty minutes.*

An FAE experiment runs coding agents against a task, judges every attempt
with a verifier you write, and records how many attempts each agent needed.
The unit of work is a **cell**: one `(model, variant, condition, task, rep)`
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
name and knows nothing about its vocabulary: arms, stages, metrics, the
lot, come from your definition.

The experiment we are about to write is called **shout**: the agent must
make a program that reads one line from stdin and prints it upper-cased.
Its tree:

```
shout/
├── __init__.py                 # the definition: what the engine reads
├── task/
│   ├── T1.PROMPT.md            # the agent's brief (becomes TODO.md)
│   └── skeleton/
│       └── README.md           # every variant's common files
├── variants/
│   ├── __init__.py             # VARIANTS = (Python,)
│   └── python/
│       ├── __init__.py         # class Python(Variant)
│       └── seed/
│           ├── overlay/shout.py                 # the stub the agent starts from
│           ├── T1.python.project_layout.md      # what the agent may change
│           ├── any.python.api.md                # the contract (base doc)
│           ├── any.python.apidocs.api.md        # the contract, "apidocs" condition
│           └── reference/overlay/shout.py       # the known-good answer
└── verifier/
    ├── __init__.py             # class ShoutVerifier(Verifier)
    └── Dockerfile              # the environment the verifier runs in
```

Three things map onto three interfaces:

| Directory | Interface | Answers |
|---|---|---|
| `__init__.py` | `Definition` | which variants, which docs, which gate, which verifier |
| `variants/<tech>/` | `Variant` | what the agent authors, what it is handed, what substrate a cell needs |
| `verifier/` | `Verifier` | how one attempt is judged, in what environment |

Create the skeleton:

```sh
mkdir -p shout/task/skeleton shout/variants/python/seed/overlay \
         shout/variants/python/seed/reference/overlay shout/verifier
```

## 3. Write the definition

`shout/__init__.py` is what the engine loads. Keep it light: the variants
and the verifier are returned by functions so importing the definition does
not pull docker-touching code in before the config has been read.

```python
"""shout — read one line, print it upper-cased. The smallest experiment."""
from fae.cell.experiment import Gate

NAME = "shout"


def variant_classes():
    from .variants import VARIANTS
    return VARIANTS


# (arm, condition) -> (the doc file the agent is handed, its minimum line count).
# The engine checks the file exists and is at least that long before a cell starts.
SEED_DOCS = {("python", "apidocs"): ("any.python.apidocs.api.md", 5)}

# One arrangement per attempt. Gate(("A", "B", "C")) would run the verifier
# three times per attempt, and green means all three passed.
GATE = Gate()


def verifier_class():
    from .verifier import ShoutVerifier
    return ShoutVerifier
```

Everything except `variant_classes` is optional and has an engine default.
The full list is the docstring of `fae/cell/experiment.py`; the ones you
will meet later are `MATRIX` (derived from each variant's `CONDITIONS` when
absent), `CONFIG` (machine-local keys your experiment needs from
`fae.toml`), `taint_rules` and `verbs`.

A **condition** is the information axis of the study: the same task under
different documentation. `apidocs` is the only one here. A **variant** (in
the paper, an arm) is the design alternative: `python` here, three
languages in the calculator.

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

A variant answers three questions: what does the agent author, what is it
handed, and what does a cell of this variant need from the host. It also
declares the image its program is built and run in, because the verifier
will run the agent's program only inside a container of that image.

`shout/variants/__init__.py`:

```python
from .python import Python

VARIANTS = (Python,)
```

`shout/variants/python/__init__.py`:

```python
"""A Python program, run by the image's interpreter inside a throwaway container."""
import subprocess

from fae.cell.variants.base import Variant, daemon_answers

PREFIX = "fae-shout-"


class Python(Variant):
    ARM = "python"
    TECH = "python"
    CONDITIONS = ("apidocs",)
    # The authorable surface: (exact files, directory prefixes) the agent may
    # write. Every other seeded file is restored before a verdict.
    AUTHORING_SURFACE = (("shout.py",), ())

    IMAGE = "python:3.12.3-slim"      # where the agent's program runs
    RUN = ("python3", "shout.py")
    # {kind: name prefix} of what a cell provisions, so the reaper can find
    # a container a dead cell left behind.
    SUBSTRATE_PREFIXES = {"container": PREFIX}

    @classmethod
    def container_name(cls, cid):
        return f"{PREFIX}{cid}"

    @classmethod
    def substrate_identities(cls, cid):
        return [("container", cls.container_name(cid))]

    def substrate_ok(self):
        """Can this host carry a cell of this variant at all? False halts the
        cell before an attempt is spent; the agent never sees a rig fault as
        its own failure."""
        if subprocess.run(["docker", "info"], capture_output=True).returncode:
            self.log("HALT[substrate]: docker unreachable")
            return False
        if subprocess.run(["docker", "image", "inspect", self.IMAGE],
                          capture_output=True).returncode:
            self.log(f"HALT[substrate]: image {self.IMAGE} not present "
                     f"(docker pull {self.IMAGE})")
            return False
        return True

    def substrate_alive(self):
        """Does the substrate answer right now? Asked before every arrangement
        and again after a charged fail: a substrate that died under the
        measurement voids the arrangement instead of scoring the agent."""
        return daemon_answers(["docker", "version"])
```

Two things worth knowing:

- `substrate_alive` has no useful default: the base answer is "dead", and
  a variant that does not override it is refused at preflight
  (`HALT[substrate]: … declares no substrate_alive probe`) before an
  attempt is spent. Every variant declares its own probe.
- `author_setup`/`author_teardown` (what the agent needs while it authors:
  a sandbox cluster, a docker-in-docker sidecar) and
  `verify_setup`/`verify_teardown` (what one arrangement runs on, brought
  up fresh) are no-ops by default. A program that runs in one container
  needs neither.

Now the seed: what the agent is handed on top of the skeleton.

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

`shout/variants/python/seed/any.python.api.md` and, identical for now,
`any.python.apidocs.api.md`:

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

The doc grammar is `<task|any>.<tech>[.<condition>].api.md` and
`<task>.<tech|arm>.project_layout.md`: the engine copies the prompt to
`TODO.md`, the layout to `docs/project.md` and the api doc (the condition's
when one exists, else the base) to `docs/<tech>.md` in the agent's
workspace. That is the whole seal on what an agent can read. Two arms of
one tech that must be told different things set `DOCS` on their variant
class: their api doc is then `any.<DOCS>[.<condition>].api.md`, while the
overlay, the layout fallback and `docs/<tech>.md` stay the tech's.

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

`shout/verifier/__init__.py`. The engine's `substrate/sandbox` block does
the container work: `sandbox.run` is one `docker run --rm` of an image over
a directory, with no network, a memory and pid ceiling, stdin in and
stdout out, and it removes the container on a timeout; `sandbox.fresh_copy`
is the verifier's own copy of the artifacts, so nothing writes into the
judged tree.

```python
"""The shout verifier: run the program over a fixed table of lines inside
the variant's image; green iff every answer matches."""
import time
from pathlib import Path

from fae.cell import experiment as _experiment
from fae.cell.substrate import sandbox
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
    out, err, rc, error = sandbox.run(variant.IMAGE, variant.container_name(cid),
                                      workdir, variant.RUN, stdin=line + "\n",
                                      timeout_s=TIMEOUT_S)
    if error:
        return None, error
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

        workdir = sandbox.fresh_copy(artifacts, out)
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
python3 cli.py rig init --experiment shout     # refuses to overwrite an existing one
```

which sets `[paths] experiment_dir = "shout"` and renders the rest for
that experiment (its lock caps, its declared config keys). If the file
already exists, edit that key. `EXPERIMENT_DIR=shout` in the environment
overrides the file on any single command; the rest of this howto passes
it so the commands are self-contained whatever the file says.

Pull the runtime image and run the preflight. The preflight asks every
variant's `substrate_ok()` and builds the verifier image, so the first
cell does not pay for the build under a lock:

```sh
docker pull python:3.12.3-slim
EXPERIMENT_DIR=shout python3 cli.py rig substrate
```

## 8. Run a cell with no agent

Before spending tokens on an agent, prove the rig judges the reference
correctly. `--stub DIR` replaces the agent with "copy DIR over the
artifacts", one attempt, the full gate:

```sh
EXPERIMENT_DIR=shout MODEL=stub WORKSPACES_DIR=/tmp/shout-ws \
  python3 -m fae.cell T1 python apidocs 1 \
  --stub shout/variants/python/seed/reference/overlay
```

The positional arguments are `TASK VARIANT CONDITION [REP]`; `MODEL` and
`EFFORT` come from the environment and name the cell:
`stub_high_python_apidocs_T1_r1`. `WORKSPACES_DIR` keeps this dry run out
of the real workspace root (default `workspaces.nosync/`).

The exit code is the first thing to read:

| Exit | Meaning |
|---|---|
| 0 | the cell ended with a verdict, green or failed |
| 45 | `HALT[substrate]`: the host could not carry the cell; nothing charged |
| 46 | the workspace is already sealed; a finished cell is read-only |

Then the workspace, `/tmp/shout-ws/stub_high_python_apidocs_T1_r1/`:

```
artifacts/            the workspace the agent (here: the stub) saw and edited
iterations.log        THE ledger: one TAB-separated event per line
metrics.json          the last verify's numbers (your Verdict.metrics)
verify.log            your verifier's log
verifier.log          the engine's log of running your verifier
arrangements/01-a1-seed-green/  every verify run: its logs and verdict.json
hooks.log             what the variant logged (substrate checks)
.sealed               written on green or on a spent budget; the cell is done
```

Look for the green line in the ledger:

```sh
grep -P '\tITER\t' /tmp/shout-ws/stub_high_python_apidocs_T1_r1/iterations.log
```

Now prove a wrong answer is a **charged** fail, not a rig fault:

```sh
mkdir -p /tmp/broken && printf 'print(input())\n' > /tmp/broken/shout.py
EXPERIMENT_DIR=shout MODEL=stub WORKSPACES_DIR=/tmp/shout-ws2 \
  python3 -m fae.cell T1 python apidocs 1 --stub /tmp/broken
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

Build the agent base image once (it holds the three real clients too;
an experiment that declares `AGENT_IMAGE_DIR` gets its own layer over it,
built by the engine — `python3 cli.py rig agent-image --rebuild`):

```sh
bash fae/agent-container/build.sh
```

Then run a cell whose agent fails twice and solves on the third attempt:

```sh
EXPERIMENT_DIR=shout MODEL=testagent TESTAGENT_PLAN=fail,fail,green \
  WORKSPACES_DIR=/tmp/shout-ws3 \
  python3 -m fae.cell T1 python apidocs 1
```

The ledger now shows three attempts: two `ITER failed`, then `ITER green`,
and the retry prompt for attempts 2 and 3 carried the previous attempt's
`verify.log` under `/feedback`. Other plans: `green` (solve at once),
`fail` (spend the whole budget), `noedit`, `crash`, `silent`, `limit`; the
grammar is at the top of `fae/testagent.py`. The agent's own findings
about its sandbox are in the cell's `.agent-testagent/findings.json`.

## 10. Run a cell with a real agent

The model tag names the agent CLI and the model id, in `fae.toml`
`[models]`; `sonnet`, `opus`, `haiku`, `fable` map to the `claude` CLI.
The agent authenticates through the credentials home in `[paths]
agent_home` (`.agent-home` by default), never through a config value: log
the CLI in on the host and the engine stages a fresh per-cell copy before
every attempt. `README.md` §1 has the per-CLI details.

Start one cell in the background and watch it:

```sh
EXPERIMENT_DIR=shout python3 cli.py cell spawn sonnet python apidocs --rep 1
EXPERIMENT_DIR=shout python3 cli.py fleet-status
EXPERIMENT_DIR=shout python3 cli.py cell tail sonnet_high_python_apidocs_T1_r1
```

`fleet-status` shows the cell's phase (`agent`, `verify`, a waiting phase),
its attempt count against the budget and its gate progress. When it seals,
the same files as in section 8 are under
`workspaces.nosync/sonnet_high_python_apidocs_T1_r1/`, plus one log per
attempt with the agent's full transcript.

## 11. Run the fleet

An experiment is a matrix, not a cell. `conduct` is the one scheduler and
the one supervisor: you fill a backlog, it admits cells under its caps,
repairs crashed ones, validates finished ones.

```sh
# three reps of every (variant, condition) in the matrix, for two models
EXPERIMENT_DIR=shout python3 cli.py conduct queue-add sonnet --matrix --reps 3
EXPERIMENT_DIR=shout python3 cli.py conduct queue-add haiku  --matrix --reps 3

# the scheduler, in the foreground; Ctrl-C detaches, cells keep running
EXPERIMENT_DIR=shout python3 cli.py conduct run -n 2 --per-model 1
```

`-n` is the global cap and should equal the number of lanes; `--per-model`
keeps one live cell per model so lanes are comparable. In another
terminal:

```sh
EXPERIMENT_DIR=shout python3 cli.py fleet-status          # the table
EXPERIMENT_DIR=shout python3 cli.py results run-report    # what this run produced so far
EXPERIMENT_DIR=shout python3 cli.py results score         # validate -> score.json -> table
```

The backlog is a directory tree, one file per spec, moved by atomic rename
between `queue/`, `running/` and `done/` under `workspaces.nosync/.orch/`,
so an interrupted scheduler neither loses nor duplicates work.

## 12. Reading what came out

The ledger, `iterations.log`, is the file of record; `fae/ledger.py` is
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
| `ALERT` | something the operator must read (a substrate fault, a stand-down) |

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
- **A substrate per verify.** Override `verify_setup(ctx)` /
  `verify_teardown(ctx)` on the variant when one arrangement needs a world
  brought up fresh (a cluster, a daemon, a stack). Your verifier calls them
  at its own point in the arrangement, inside the verify container, over
  the daemon's socket.
- **A substrate per cell.** Override `author_setup()` /
  `author_teardown()` when the agent needs something while it authors (a
  sandbox cluster, its own docker daemon). `author_setup` returns the extra
  docker arguments for the agent's container; the driver calls both, and
  teardown runs on every path.
- **An exclusive resource.** `LOCK = "gpu"` and `LOCK_SLOTS = 1` on a
  variant serialise its cells fleet-wide; the cap is `[slots] arm_gpu` in
  `fae.toml`.
- **Tools your verifier needs.** Put them in the verifier's Dockerfile.
  A variant that needs more (a compiler, a load generator) declares its own
  `IMAGE_DIR` and builds `FROM $BASE`, the verifier's image.
- **Several conditions.** Add to `CONDITIONS` and ship one
  `any.<tech>.<condition>.api.md` per condition; `SEED_DOCS` pins each.
- **Taint rules, grading, reporting.** `taint_rules`,
  `reference_workspace`, `POOLED_MODELS` on the definition; see
  `experiment/__init__.py` for a full-size example and `AGENTS.md` for
  what each invariant protects.

## 14. Interface cheat sheet

**Definition** (`<EXPERIMENT_DIR>/__init__.py`, read by `fae/cell/experiment.py`):

| Name | Required | Default |
|---|---|---|
| `variant_classes()` | yes | |
| `verifier_class()` | yes (a cell cannot verify without one) | |
| `NAME` | no | the directory name |
| `MATRIX` | no | `{arm: CONDITIONS}` from the variants |
| `SEED_DOCS` | no | `{}` |
| `GATE` | no | `Gate()`: one arrangement |
| `CONFIG` | no | `{}` |
| `fingerprint_trees(conf)` | no | `[]` |
| `taint_rules`, `report_text`, `reference_workspace`, `POOLED_MODELS`, `verbs()` | no | none |

**Variant** (`fae/cell/variants/base.py`):

| Member | Purpose |
|---|---|
| `ARM`, `TECH`, `CONDITIONS` | identity; the tech names the seed docs |
| `AUTHORING_SURFACE` | `(files, dir prefixes)` the agent may write; required, a cell of a variant without it is refused |
| `LOCK`, `LOCK_SLOTS` | an exclusive lock held for the cell's life |
| `IMAGE_DIR`, `image_context(conf)` | the variant's layer over the verifier's image |
| `SUBSTRATE_PREFIXES`, `substrate_identities(cid)`, `stray()`, `sweep()` | what a reaper may find and remove |
| `substrate_ok()` | preflight; False halts before an attempt |
| `substrate_alive()` | liveness before/after each arrangement; **must be declared**, or the cell halts at preflight |
| `author_setup()` / `author_teardown()` | the authoring substrate, driver-run |
| `verify_setup(ctx)` / `verify_teardown(ctx)` | the per-arrangement substrate, verifier-run |
| `seed_root()`, `verify_root()` | `seed/` and `verify/` beside the module unless `SEED`/`VERIFY` say otherwise |

**Verifier** (`fae/cell/verify.py`):

| Member | Purpose |
|---|---|
| `IMAGE_DIR` | **required**: the Dockerfile of the environment it runs in |
| `verify(ctx) -> Verdict` | the whole judgment |
| `FILES` | archived per arrangement when the Verdict names none |
| `EXCLUSIVE` | a lock the engine holds around every run |
| `SUBSTRATE_PREFIXES`, `substrate_identities(cid)` | what a verify provisions, for the reaper |
| `MEASURED_STAGES` | charged fails at these stages are voided when the substrate is found dead afterwards |

**Ctx** fields: `root`, `experiment_dir`, `workspace`, `artifacts`, `out`,
`cid`, `task`, `variant`, `arrangement`, `expected_fp`, `mode`
(`cell` | `reverify` | `exp1`).

**Cell id**: `<model>_<effort>_<arm>_<condition>_<task>_r<rep>`, encoded in
one place (`fae/driver/common.py`).

---

### Status notes for reviewers

This document describes the interface as it stands. For a variant whose
substrate is more than one container, the engine's substrate blocks
(`fae/cell/substrate/`: `dind`, `kind`, `sandbox`) are what its
`verify_setup`/`verify_teardown` pair composes.
