#!/usr/bin/env python3
"""Grouped Typer front-end for the driver — 

DESIGN: this is a CLI LAYER, not the orchestrator. Every command builds the
namespace the target fae/driver/*.py function already expects and calls it
directly — fae/driver/ is the library, this is its one client. That includes
fae/driver/rig.py (the experiment's verbs: init, infra, smoke, prepare,
verb; the rig's own: trace-reset), fae/driver/check.py (experiment check)
and the tail/log pair folded into fae/driver/ops.py.
No orchestration logic is duplicated here.

GROUPS
  experiment  the experiment this root runs: set it up (init, check, infra,
           smoke, prepare), run it (run, pause, resume, stop, status,
           diagnose, repair), and its own commands (verb)
  queue    the work list, pending specs only: add, list, cancel
  cell     exactly ONE named cell: spawn, pause, resume, stop, tail, log,
           seal, reverify
  results  what the experiment produced, and whether to trust it: score,
           grade, validate, aggregate
  rig      the harness itself, not the experiment: trace-reset, and tool
           (an instrument run standalone for debugging; the harness calls
           them in-process, verify.py, never through here)

OPTIONS
  experiment status  --watch N, --walls (the fleet table, live, or its walls)
  results score      --no-aggregate
  queue add          --matrix, --to-rep N; every active variant by default, --variant V
  experiment pause   --admission-only
  experiment resume  requeues, never spawns
  experiment stop    scoped: all | AGENT...

The driver (fae/cell) imports `driver.common.cell_id` and reads the
experiment definition directly; nothing shells out to a hidden
subcommand any more (the `_cell_id` and `_seed_doc` verbs went with the bash
callers that needed them).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import typer

ROOT = Path(__file__).resolve().parents[1]
# When run directly (`python3 cli.py ...`) Python already puts this file's
# directory on sys.path[0]; when path-loaded (tests exec this module by
# file), it does not — so fae/driver/ needs this insert to be importable either way.
sys.path.insert(0, str(ROOT))

# fae/driver/ is the library; this file is the client of it. No orchestration
# logic is duplicated here — every command builds the namespace the target
# fae/driver/*.py function already expects and calls straight through.
from fae.driver import ops, render, score, supervise, conduct, rig  # noqa: E402


def _ns(**kw):
    """The namespace fae/driver/*.py's functions read. Defaults mirror argparse's."""
    return SimpleNamespace(**kw)


app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="FAE. Groups: experiment, queue, cell, results, rig, tools.")
cell_app = typer.Typer(no_args_is_help=True, help="Act on exactly ONE named cell.")
queue_app = typer.Typer(no_args_is_help=True,
                        help="The work list: pending specs per agent lane.")
results_app = typer.Typer(no_args_is_help=True, help="What the run produced, and whether to trust it.")
experiment_app = typer.Typer(no_args_is_help=True,
                             help="The experiment this root runs: set it up, check it, "
                                  "run it, watch it.")
rig_app = typer.Typer(no_args_is_help=True, help="The harness itself, not the experiment.")

app.add_typer(experiment_app, name="experiment")
app.add_typer(cell_app, name="cell")
app.add_typer(queue_app, name="queue")
app.add_typer(results_app, name="results")
app.add_typer(rig_app, name="rig")

# A passthrough command hands every argument, --help included, to what it runs.
_PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True,
                "help_option_names": []}

SEL = "cid, agent name, or a whole `_`-separated token run. Anchored: `r1` "
SEL += "does not match `r10`."


# --- cell -------------------------------------------------------------------

@cell_app.command("spawn")
def cell_spawn(
    agent: str, variant: str,
    rep: str = typer.Option("1", "-r", "--rep", help="int, or comma-separated ints"),
    task: str = typer.Option("T1", "--task", help="task id: T<n>, one the experiment's task/ carries"),
    fresh: bool = typer.Option(False, "--fresh",
                              help="WIPES the workspace (safe_wipe) and restarts at attempt 1"),
):
    """Start one cell now, in parallel with whatever else is running."""
    ops.spawn(_ns(agent=agent, variant=variant, rep=rep,
                  task=task, fresh=fresh))


@cell_app.command("pause")
def cell_pause(selectors: list[str] = typer.Argument(..., help=SEL + " Must match ONE cell."),
               reason: str = typer.Option("manual", "--reason",
                                          help="recorded in .paused; roster/manual survive `experiment resume all`")):
    """Ask ONE cell to stop at its next safe point. Cooperative, not a signal.
    Bulk pause is `experiment pause`."""
    ops.pause(_ns(selectors=list(selectors), reason=reason))


@cell_app.command("resume")
def cell_resume(selectors: list[str] = typer.Argument(..., help=SEL + " Must match ONE cell."),
                force: bool = typer.Option(False, "--force",
                                           help="respawn even past the per-agent live-cell cap")):
    """Lift ONE cell's locks, clear its reconcile flag, respawn its loop.
    Direct spawn is safe at n=1; the respawn still defers at the per-agent
    cap (--force pushes past it). Bulk resume is `experiment resume`."""
    ops.resume(_ns(selectors=list(selectors), force=force))


@cell_app.command("stop")
def cell_stop(selectors: list[str] = typer.Argument(..., help=SEL + " Must match ONE cell."),
              cancel: bool = typer.Option(False, "--cancel",
                                          help="TERMINAL: write .cancelled — DONE·cancelled, never comes back"),
              dry_run: bool = typer.Option(False, "--dry-run",
                                           help="list what would be stopped and dropped, do nothing")):
    """Halt ONE cell NOW: loop killed, infra torn down, queued specs
    removed (backed up). Resumable — PAUSED·stopped — unless --cancel.
    Files are never touched. Bulk stop is `experiment stop`."""
    ops.stop_cells(_ns(selectors=list(selectors), cancel=cancel, dry_run=dry_run))


@cell_app.command("tail")
def cell_tail(cid: str, follow: bool = typer.Option(False, "-f", "--follow",
                                                   help="stream as it grows")):
    """The agent transcript of the latest attempt."""
    ops.tail(_ns(cell=cid, follow=follow))


@cell_app.command("log")
def cell_log(cid: str):
    """The run_cell console log."""
    ops.log(_ns(cell=cid))


@cell_app.command("seal")
def cell_seal(selector: str = typer.Argument("all", help=SEL),
              apply: bool = typer.Option(False, "--apply",
                                         help="write .sealed (dry by default)"),
              verbose: bool = typer.Option(False, "-v", "--verbose",
                                           help="also list cells left alone, and why")):
    """Make terminal cells read-only. No unseal: redo a cell by deleting and
    requeueing it."""
    ops.seal(_ns(selector=selector, apply=apply, verbose=verbose))


@cell_app.command("reverify")
def cell_reverify(selectors: Optional[list[str]] = typer.Argument(None, help=SEL),
                  all_: bool = typer.Option(False, "--all",
                                            help="accept a selector matching more than one cell")):
    """Re-run the shape gate on a finished cell WITHOUT touching its recorded
    result (writes under <ws>/reverify/<ts>/)."""
    ops.reverify(_ns(selectors=list(selectors) if selectors else ["all"], all=all_))


# --- queue: the work list ---------------------------------------------------

@queue_app.command("add")
def queue_add(
    agent: str,
    matrix: bool = typer.Option(False, "--matrix",
                                help="enqueue every active variant"),
    to_rep: Optional[int] = typer.Option(None, "--to-rep",
                                         help="fill missing reps up to N for the selected variants"),
    variant: list[str] = typer.Option([], "--variant",
                                      help="with --to-rep: this variant (repeatable; default: "
                                           "every active one)"),
    reps: int = typer.Option(3, "--reps", help="with --matrix: how many reps"),
    task: str = typer.Option("T1", "--task", help="task id: T<n>, one the experiment's task/ carries"),
    fresh: bool = typer.Option(False, "--fresh",
                              help="WIPES each workspace before running it"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="with --to-rep: print the plan, enqueue nothing"),
):
    """Add work to the queue.

    Enqueues rep-outer, so every variant advances together and a partial run
    still yields comparable n across them. Enqueue-only either way:
    admission happens in `experiment run`.
    """
    if not matrix and to_rep is None:
        raise typer.BadParameter("choose --matrix or --to-rep N")
    if to_rep is not None:
        ops.top_up(_ns(agent=agent, to_rep=to_rep, variants=list(variant),
                       task=task, dry_run=dry_run))
        return
    ops.spawn_matrix(_ns(agent=agent, reps=reps, task=task, fresh=fresh))


@queue_app.command("list")
def queue_list(agents: Optional[list[str]] = typer.Argument(None, help="agent lane(s); default: all"),
               done: bool = typer.Option(False, "--done", help="also list the terminal specs")):
    """The work list per agent lane: pending in admission order (a paused
    lane is marked), and what is running. Read-only."""
    render.queue_list(_ns(agents=list(agents or []), done=done))


@queue_app.command("cancel")
def queue_cancel(selectors: list[str] = typer.Argument(..., help=SEL + " `all`: every pending spec."),
                 dry_run: bool = typer.Option(False, "--dry-run",
                                              help="list what would be cancelled, move nothing")):
    """Take pending specs out of the queue before admission. They are moved
    aside (.queues/.to_be_deleted/<ts>/queue/), never deleted; running cells
    are not touched (that is `cell stop`)."""
    ops.queue_cancel(_ns(selectors=list(selectors), dry_run=dry_run))


# --- experiment: the run ----------------------------------------------------

SCOPE = "`all` or agent lane name(s)."


@experiment_app.command("run")
def experiment_run(limit: int = typer.Option(7, "-n", "--limit",
                                          help="global cap on live cells"),
                per_agent: int = typer.Option(1, "--per-agent",
                                              help="max live cells per agent"),
                per_agent_override: str = typer.Option("", "--per-agent-override",
                                                        help="AGENT=N[,AGENT=N...] — raise "
                                                             "the per-agent cap for named "
                                                             "lanes only; every other lane "
                                                             "keeps --per-agent"),
                interval: int = typer.Option(30, "--interval", help="poll seconds"),
                supervise_interval: int = typer.Option(300, "--supervise-interval",
                                                       help="seconds between supervision "
                                                            "sweeps (repair-requeue, DONE "
                                                            "validation, zombie reap); 0 off")):
    """THE scheduler AND supervisor — the only thing that turns queued specs
    into cells, and the only controller: every --supervise-interval it
    repairs crashed/hung cells (by REQUEUING them at the lane front — its
    own admission brings them back), validates DONE cells, and reaps
    zombies on a 2nd consecutive sighting.

    Foreground: run it and watch it. Ctrl-C detaches — live cells keep
    running; nothing new starts and nothing is supervised until it runs
    again.

    Raising --per-agent changes the host-load regime every lane is measured
    under, so it applies fleet-wide; --per-agent-override scopes a raise to
    named lanes only, leaving the rest at the comparable baseline.
    """
    overrides = {}
    for part in per_agent_override.split(","):
        part = part.strip()
        if not part:
            continue
        agent, _, n = part.partition("=")
        if not agent or not n.strip().isdigit():
            raise typer.BadParameter(
                f"--per-agent-override wants AGENT=N, got {part!r}")
        overrides[agent.strip()] = int(n)
    conduct.Conduct().run(_ns(limit=limit, per_agent=per_agent,
                        per_agent_override=overrides, interval=interval,
                        supervise_interval=supervise_interval))


@experiment_app.command("diagnose")
def experiment_diagnose():
    """READ-ONLY one-shot: what supervision would do (dry), current zombies
    (listed, not reaped), and the admission preview per lane — the run
    loop's judgment without waiting for the loop."""
    conduct.Conduct().diagnose(_ns())


@experiment_app.command("pause")
def experiment_pause(scope: list[str] = typer.Argument(..., help=SCOPE),
                  admission_only: bool = typer.Option(False, "--admission-only",
                                                      help="park the lane(s) only; running cells finish"),
                  interval: int = typer.Option(60, "-n", "--interval",
                                               help="poll seconds while waiting (`all`)"),
                  dry_run: bool = typer.Option(False, "--dry-run",
                                               help="show what would be paused/parked, do nothing")):
    """GRACEFUL bulk pause, everything preserved. `all`: stop the run, pause
    every cell, wait for zero loops — the FP-edit window. Agent names: park
    those lanes + pause their running cells, return immediately.
    Contrast: `experiment stop` kills NOW."""
    conduct.Conduct().pause(_ns(scope=list(scope), admission_only=admission_only,
                              interval=interval, dry_run=dry_run))


@experiment_app.command("resume")
def experiment_resume(scope: list[str] = typer.Argument(..., help=SCOPE)):
    """Bulk resume WITHOUT spawning: unpark lanes, lift pause locks, requeue
    interrupted cells at the FRONT of their lane. A live `experiment run`
    admits them under its caps — nothing starts while it is down. Blanket
    `all` leaves roster/manual pauses and cancelled cells alone."""
    conduct.Conduct().resume(_ns(scope=list(scope)))


@experiment_app.command("stop")
def experiment_stop(scope: list[str] = typer.Argument(..., help=SCOPE),
                 yes: bool = typer.Option(False, "--yes", "-y",
                                          help="skip the confirmation")):
    """HARD halt NOW, scoped: loops TERMed mid-attempt, agent containers
    removed; `all` also TERMs the run. Queues are NOT touched. Warns and asks
    to confirm first. Resumable (`experiment resume`); the terminal verdict is
    `cell stop --cancel`. For a graceful stop use `experiment pause`."""
    conduct.Conduct().stop(_ns(scope=list(scope), yes=yes))


@experiment_app.command("repair")
def experiment_repair(dry_run: bool = typer.Option(False, "--dry-run",
                                                    help="preview repairs, write nothing"),
                      only: str = typer.Option("", "--only",
                                               help="restrict the sweep to this cid substring")):
    """One-shot supervision sweep — repair-requeue, DONE validation, zombie
    reap. `experiment run` calls the same sweep every --supervise-interval;
    this is the one-shot equivalent."""
    conduct.Conduct().repair(_ns(dry_run=dry_run, only=only))


@experiment_app.command("status")
def experiment_status(
    flat: bool = typer.Option(False, "--flat", help="one row per cell"),
    running_only: bool = typer.Option(False, "--running", help="only live cells"),
    watch: Optional[int] = typer.Option(None, "-w", "--watch",
                                        help="refresh every N s"),
    walls: bool = typer.Option(False, "--walls",
                               help="surface limit/AUTH walls"),
):
    """The fleet table: every cell, its state and gate; --watch refreshes,
    --walls surfaces limit/AUTH walls."""
    if walls:
        render.monitor(_ns(interval=watch or 60))
    elif watch:
        render.watch(_ns(interval=watch, flat=flat, running_only=running_only))
    else:
        conduct.Conduct().status(_ns(flat=flat, running_only=running_only))


# --- results ----------------------------------------------------------------

@results_app.command("score")
def results_score(
    selector: Optional[str] = typer.Argument(None, help=SEL),
    no_aggregate: bool = typer.Option(False, "--no-aggregate",
                                      help="score cells without printing the table"),
    all_cells: bool = typer.Option(False, "--all-cells", help="per-cell rows"),
    allow_stale: bool = typer.Option(False, "--allow-stale",
                                     help="proceed even if a score.json is older than its inputs"),
    variant: Optional[str] = typer.Option(None, "--variant",
                                          help="restrict the scoreboard to one variant; "
                                               "prints a FILTERED banner with shown/total counts"),
    where: list[str] = typer.Option([], "--where",
                                    help="FACTOR=LEVEL: only the variants at that level "
                                         "(repeatable)"),
    impl: Optional[str] = typer.Option(None, "--impl",
                                       help="restrict to one cell driver (py|bash)"),
    sort_discrepancy: bool = typer.Option(False, "--sort-discrepancy",
                                          help="rank rows by how far --impl's baseline "
                                               "comparison diverged, biggest gap first "
                                               "(needs --impl; no-op without it)"),
    sort_significant: bool = typer.Option(False, "--sort-significant",
                                          help="rank rows by whether --impl's baseline "
                                               "comparison is significant (Fisher's p<.05), "
                                               "most significant first; combine with "
                                               "--sort-discrepancy for significant AND big "
                                               "first (needs --impl; no-op without it)"),
):
    """Score cells and print the metrics table (was score + aggregate)."""
    score.score(_ns(selector=selector, all_cells=all_cells,
                    allow_stale=allow_stale, no_aggregate=no_aggregate,
                    variant=variant, where=list(where), impl=impl,
                    sort_discrepancy=sort_discrepancy,
                    sort_significant=sort_significant))


@results_app.command("run-report")
def results_run_report(
    since: Optional[str] = typer.Option(None, "--since",
                                        help="ISO 8601 or epoch seconds; default = the "
                                             "current run's start (conduct.pid mtime)"),
):
    """What THIS run produced: completed cells by variant (green rate + mean
    iterations-to-green) and the cells still working."""
    from fae.scoring import run_report
    run_report.cli(since)


@results_app.command("grade")
def results_grade(
    judge_model: Optional[str] = typer.Option(None, "--judge-model",
                                              help="REQUIRED unless --no-judge; recorded per cell as grader_model ($JUDGE_MODEL)"),
    force: bool = typer.Option(False, "--force", help="redo every cell ($FORCE=1)"),
    no_judge: bool = typer.Option(False, "--no-judge",
                                  help="mechanical extraction only ($JUDGE=0)"),
    cells: Optional[list[str]] = typer.Option(None, "--cells",
                                              help="restrict the scan to these cell ids ($CELLS)"),
    limit: Optional[int] = typer.Option(None, "--limit", help="stop after N cells scanned ($LIMIT)"),
    cost_log: Optional[str] = typer.Option(None, "--cost-log", help="per-call usage TSV ($COST_LOG)"),
    grade_inflight: bool = typer.Option(False, "--grade-inflight",
                                        help="also grade non-terminal cells ($GRADE_INFLIGHT=1)"),
):
    """Defect scan: one defects.json per terminal cell, graded twice by the
    judge model; restartable per pass."""
    score.grade(_ns(judge_model=judge_model, force=force, no_judge=no_judge,
                    cells=list(cells) if cells else None, limit=limit,
                    cost_log=cost_log, grade_inflight=grade_inflight))


@results_app.command("validate")
def results_validate(selector: Optional[str] = typer.Argument(None, help=SEL)):
    """Post-DONE trust check: VALID or TAINTED, with reasons."""
    score.validate(_ns(selector=selector))


@results_app.command("aggregate")
def results_aggregate(
    variant: Optional[str] = typer.Option(None, "--variant",
                                          help="restrict the scoreboard to one variant"),
    where: list[str] = typer.Option([], "--where",
                                    help="FACTOR=LEVEL: only the variants at that level "
                                         "(repeatable)"),
    impl: Optional[str] = typer.Option(None, "--impl",
                                       help="restrict to one cell driver (py|bash)"),
    include_tainted: bool = typer.Option(False, "--include-tainted",
                                         help="keep TAINTED cells in the scoreboard"),
    tainted_cells_details: bool = typer.Option(False, "--tainted-cells-details",
                                               help="print only the TAINTED cells, then exit"),
    allow_stale: bool = typer.Option(False, "--allow-stale",
                                     help="proceed even if a score.json is older than its inputs"),
    sort_discrepancy: bool = typer.Option(False, "--sort-discrepancy",
                                          help="rank rows by how far --impl's baseline "
                                               "comparison diverged, biggest gap first "
                                               "(needs --impl; no-op without it)"),
    sort_significant: bool = typer.Option(False, "--sort-significant",
                                          help="rank rows by whether --impl's baseline "
                                               "comparison is significant (Fisher's p<.05), "
                                               "most significant first; combine with "
                                               "--sort-discrepancy for significant AND big "
                                               "first (needs --impl; no-op without it)"),
):
    """Print the scoreboard from existing score.json files, without rescoring
    (was `runs.py aggregate`; `results score` also runs this as its last
    step)."""
    score.aggregate(_ns(variant=variant, where=list(where), impl=impl,
                        include_tainted=include_tainted,
                        tainted_cells_details=tainted_cells_details, allow_stale=allow_stale,
                        sort_discrepancy=sort_discrepancy, sort_significant=sort_significant))


# --- experiment / rig ------------------------------------------------------

@experiment_app.command("init")
def experiment_init(experiment: str = typer.Option("", "--experiment",
                                            help="the experiment directory the file "
                                                 "points the engine at (relative to the "
                                                 "root or absolute); default `experiment`")):
    """Write fae.toml at the root with every key at its default; refuses to
    overwrite one that exists."""
    rig.init(_ns(experiment=experiment))


@experiment_app.command("check")
def experiment_check(walk: bool = typer.Option(False, "--walk",
                                               help="step by step: why each step matters, "
                                                    "then run it; on a failure, fix and retry"),
                     static: bool = typer.Option(False, "--static",
                                                 help="no docker: the definition, the variants "
                                                      "and the seeds only"),
                     smoke: bool = typer.Option(False, "--smoke",
                                                help="also run each variant's reference through "
                                                     "the gate (experiment smoke)"),
                     variants: str = typer.Option("", "--variants",
                                                  help="comma-separated (default: every active variant)"),
                     tla_trace: bool = typer.Option(False, "--tla-trace",
                                                    help="also replay the fleet's transitions against "
                                                         "the TLA+ model of the cell lifecycle"),
                     task: str = typer.Option("T1", "--task", help="the task the seeds are checked for")):
    """Whether this root's experiment is ready to run: the host, the config,
    the definition, each variant, every cell's seed, the invariants, the
    infra, the agents' images, no leftovers. Exit 1 on any failure; each
    names its fix."""
    from fae.driver import check
    check.main(_ns(walk=walk, static=static, smoke=smoke, tla_trace=tla_trace, variants=variants,
                   task=task))


@rig_app.command("trace-reset")
def rig_trace_reset(dry_run: bool = typer.Option(False, "--dry-run",
                                                 help="preview the EPOCH lines, write nothing")):
    """Archive transitions.log, restart it from a recorded EPOCH state."""
    rig.trace_reset(_ns(dry_run=dry_run))


@experiment_app.command("infra")
def experiment_infra():
    """Every variant's infra preflight (its infra's ok()) + a sweep
    of stale per-verify kind clusters. Creates nothing: each verify provisions
    its own infra."""
    rig.infra(_ns())


@experiment_app.command("smoke")
def experiment_smoke(variants: str = typer.Option("", "--variants",
                                              help="comma-separated (default: one per way "
                                                   "of being judged)"),
              only: str = typer.Option("", "--only", help="substring filter on the variant id"),
              rep: int = typer.Option(1, "--rep"),
              full_gate: bool = typer.Option(False, "--full-gate",
                                             help="every arrangement of the gate per variant "
                                                  "(default: the canonical one)")):
    """Pipeline check: one reference cell per variant through the driver
    (`-m fae.cell ... --stub`), in ws-test.nosync. Exit 0 iff all green."""
    rig.smoke(_ns(variants=variants, only=only, rep=rep, full_gate=full_gate))


@experiment_app.command("prepare")
def experiment_prepare(agent: str = typer.Option("", "--agent", help="lane name (default: $AGENT)"),
                reps: int = typer.Option(1, "--reps", help="reps per combination"),
                task: str = typer.Option("T1", "--task", help="task id: T<n>, one the experiment's task/ carries")):
    """Seed the matrix's workspaces WITHOUT launching anything
    (fae/cell/prepare.py, the driver's own prepare)."""
    rig.prepare(_ns(agent=agent, reps=reps, task=task))


@experiment_app.command("verb", context_settings=_PASSTHROUGH)
def experiment_verb(ctx: typer.Context):
    """verb [NAME [ARGS...]] — one of the experiment's own commands (its
    definition's commands()), ARGS passed through untouched; no NAME lists
    them."""
    args = list(ctx.args)
    raise SystemExit(rig.verb_cmd(args[0] if args else "", args[1:]) or 0)


# --- rig tool: an instrument, run standalone (debug/one-off) ----------------

def _instrument_dirs():
    """Where an instrument name resolves, in order: the engine's own, the
    contrib blocks, the experiment's."""
    from fae.driver import common
    from fae.cell.contrib import elastic_resource
    from fae import paths
    return [paths.ENGINE / "cell" / "instruments", elastic_resource.DIR,
            common.experiment_dir() / "instruments"]


@rig_app.command("tool", context_settings=_PASSTHROUGH)
def rig_tool(ctx: typer.Context):
    """tool NAME [ARGS...] — one instrument by name (e.g. resource_sampler, law,
    trace, k6, load_shape), forwarding ARGS untouched; the script owns its
    own argument parsing."""
    if not ctx.args:
        raise typer.BadParameter("rig tool NAME [ARGS...]")
    name, argv = ctx.args[0], ctx.args[1:]
    for base in _instrument_dirs():
        if (base / f"{name}.py").is_file():
            rc = subprocess.run([sys.executable, str(base / f"{name}.py"), *argv]).returncode
            raise typer.Exit(code=rc)
    raise typer.BadParameter(f"no instrument named {name!r} under "
                             + ", ".join(str(d) for d in _instrument_dirs()))



def main():
    app()


if __name__ == "__main__":
    main()
