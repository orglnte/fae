# FAE — a Framework for Agentic-authoring Evaluations

FAE is a framework for comparing design approaches by how well coding
agents build with them.

Take one task and two or more ways to do it: two tools, two libraries, two
architectures. An FAE experiment gives each way to the same coding agents
(Claude, Gemini, DeepSeek, …), lets each agent try up to ten times, checks
every attempt with the same automated verifier, and reports which way the
agents get working, in how many attempts, in how much time, and with how much
code.

```
  the task + one approach's docs
               │
               ▼
  ┌───────────────────────────┐   the agent's files   ┌──────────────────────────┐
  │ coding agent              │ ────────────────────▶ │ verifier                 │
  │ sealed container:         │                       │ deploys the files, runs  │
  │ no network, no host,      │ ◀──────────────────── │ the checks: green or not │
  │ its approach's tools only │     why it failed     └──────────────────────────┘
  └───────────────────────────┘
       repeat until green, at most 10 attempts; every step goes in the ledger
```

One run of that loop is a **cell**: one model, one variant (one approach,
with one set of docs), one repetition. An experiment is many cells, and its
result is a table, one row per model and variant. This one is from the calculator example below:
Claude Sonnet writing it in Zig, and a scripted agent that fails once on
purpose:

```
MODEL            | VARIANT                  | REPS | E2E   | GREEN | ITG mn/avg/mx  | MIN mn/avg/mx     | MIN/ATT | SLoC avg -mn/+mx
sonnet-5         | zig                      | 1    | -     | 100%  | 1 / 1.0 / 1    | 6 / 6.3 / 6       | 6.3     |   47   -0/+0
testagent        | python                   | 1    | -     | 100%  | 2 / 2.0 / 2    | 0 / 0.0 / 0       | 0.0     |   12   -0/+0
```

`GREEN` is the share of cells the agents got working, `ITG` the attempts it
took (iterations to green), `SLoC` the lines they wrote. `MIN` is the agent's
authoring time in minutes up to green, summed over its attempts, and `MIN/ATT`
the mean per attempt; each cell's `score.json` keeps the seconds of every
attempt. `E2E` is the share of the verifier's end-to-end checks passed, `-`
when the verifier reports none (the calculator's does not). Until `results
grade` has judged the cells, `results score` warns that the graded metrics (consistency defects) are missing; the
counted ones above do not need it.

## Try it in five minutes

You need Python 3.11+ with `typer` and `ujson`, and docker. Nothing else is
installed on your machine: everything an agent writes, and everything the
verifier runs, runs in containers.

```sh
git clone https://github.com/orglnte/fae
git clone https://github.com/orglnte/fae-authoring-a-calculator
cd fae-authoring-a-calculator
python3 cli.py experiment init       # writes fae.toml, the machine-local config, then offers the walk
python3 cli.py experiment check      # everything in place? (--walk: step by step, with the fixes)
python3 cli.py experiment smoke      # one cell per language, the known answer in place of an agent
```

```
=== SMOKE SUMMARY ===
  ok   python         GREEN at attempt 1
  ok   brainfuck      GREEN at attempt 1
  ok   zig            GREEN at attempt 1
  PIPELINE OK on every variant.
```

That ran the whole pipeline (a sealed workspace, the verifier in its
container, the ledger) with no agent and no tokens. Now a real agent: build
the agent image once, log Claude in inside it, and start one cell.

```sh
bash ../fae/fae/agent-container/build.sh
docker run -it --rm -v "$PWD/.agent-home/.claude:/home/node/.claude" fae-agent:latest claude auth login
python3 cli.py cell spawn sonnet zig --rep 1
python3 cli.py fleet-status   # its phase, its attempt, its verdict
python3 cli.py results score  # the table
```

The cell's workspace is `workspaces.nosync/sonnet_high_zig_T1_r1/`:
what the agent wrote (`artifacts/`), each attempt's transcript, the
verifier's log, and `iterations.log`, the ledger.

## What an experiment is

An experiment is a repository beside FAE. The calculator is the smallest:

```
experiment/
├── __init__.py     the definition: the approaches, the scenarios, the verifier
├── task/           the brief, and the files every approach starts from
├── variants/       one directory per approach: what the agent may write, its docs,
│                   a known-good answer, the image it runs in
└── verifier/       how an attempt is judged, and the image the judging runs in
```

Three interfaces, one per part: the **definition** says what there is, a
**variant** says what the agent writes and what it is given, the
**verifier** turns one attempt into a verdict. [HOWTO.md](HOWTO.md) builds
one from an empty directory.

## A bigger example: Terraform vs Pulumi

[fae-terraform-vs-pulumi](https://github.com/orglnte/fae-terraform-vs-pulumi)
asks a question teams face now: when agents write the infrastructure code,
do they get it right more easily in HCL or in Python?

Agents write the same stack with each tool: an app behind nginx, Postgres
and, in prod, Redis, on a Docker daemon that belongs to the cell. Each
attempt is deployed twice, as prod and as dev, and checked for what each
tool typically gets wrong:

| check | catches |
|---|---|
| 3 replicas answer through nginx, an item round-trips | a stack that does not work |
| only nginx publishes a port; the stores share no network with it | a stack that is not private |
| the database password is generated and in none of the agent's files | a hard-coded secret |
| a second plan with the same inputs is empty | code that is not idempotent |
| 3 → 5 replicas keeps the same database and its rows | a change that recreates the database |
| destroy leaves nothing | leaks |

```sh
cd ../fae-terraform-vs-pulumi
python3 cli.py experiment init && python3 cli.py experiment check
python3 cli.py experiment smoke --full-gate                    # both tools' known answers, both scenarios
python3 cli.py conduct queue-add sonnet --matrix --reps 5
python3 cli.py conduct run -n 1                         # the scheduler; Ctrl-C detaches
python3 cli.py results score
```

## Why the numbers can be compared

1. **Same everything but the approach.** One brief, one verifier, one
   attempt budget, one model per row.
2. **Sealed agents.** The agent works in a container with no network, sees
   only its workspace, and its image holds its own approach's tools and no
   other's.
3. **Rig faults are refunded.** A verifier that crashes, a daemon that does
   not answer, a provider's rate limit: the attempt is not charged to the
   agent, and it runs again.
4. **Verdicts are pinned.** Every verdict records a fingerprint of the task,
   the verifier and the engine that produced it, so a change to any of them
   is visible.

## Agents

A model tag (`sonnet`, `haiku`, `opus`, `fable`, `gemini`, `g38f`, `dsv4f`, …)
names a CLI and a model id; the list is `[models]` in `fae.toml`. Each agent
signs in through `.agent-home/` in the experiment's root, never through a
config value, and each cell gets a fresh copy of it.

| CLI | what `.agent-home/` must hold |
|---|---|
| `claude` | `.claude/.credentials.json`: `claude auth login` inside the agent image, as above |
| `opencode` | `.opencode/opencode.key`: the API key, written by you (`chmod 600`) |
| `agy` | `.gemini/`: a signed-in agy home. Sign in once inside the agent image with that directory mounted at `/home/node/.gemini` (agy in Docker). |

`.agent-home/` and `fae.toml` are gitignored. Keep secrets out of `fae.toml`.

## Where to go next

- [HOWTO.md](HOWTO.md): your own experiment, from an empty directory.
- [RUNBOOK.md](RUNBOOK.md): running a fleet (the scheduler, the backlog,
  pausing, scoring).
- [DESIGN-fae.md](DESIGN-fae.md): what is measured and why.
- [AGENTS.md](AGENTS.md): the invariants the code keeps.
