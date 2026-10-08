"""The operator's command line: grouped Typer verbs, their printing in
_render.py.

DESIGN: this is a CLI LAYER, not the orchestrator. A command acting on the
run calls the Conduct (fae/conduct/); one acting on cells selects
them and asks each Cell; one on the work list asks the Queues. What stays here
is selection and printing. The verbs on the experiment as a whole (init,
smoke, prepare, verb; the rig's trace-reset) are its Experiment's
(fae/experiment/), `experiment check` and `experiment infra` included.

GROUPS
  experiment  the experiment this root runs: set it up (init, check, infra,
           smoke, prepare), run it (run, pause, resume, stop, status,
           diagnose, repair), and its own commands (verb)
  queue    the work list, pending specs only: add, list, cancel
  cell     exactly ONE named cell: spawn, pause, resume, stop, tail, log,
           seal, reverify
  results  what the experiment produced, and whether to trust it: score,
           validate, aggregate
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

The cell process (fae/cell) imports `fae.experiment.cell_id` and reads the
experiment definition directly; nothing shells out to a hidden subcommand.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import typer

ROOT = Path(__file__).resolve().parents[2]
# Path-loaded by file (tests do), this package's checkout is not on sys.path:
# the insert makes `fae` importable either way.
sys.path.insert(0, str(ROOT))

import fae.experiment  # noqa: E402
from fae import experiment as _experiment  # noqa: E402
from fae.cell.cell import Busy  # noqa: E402
from fae.cli import _render  # noqa: E402
from fae import conduct  # noqa: E402


def _ns(**kw):
    """The namespace the verbs' functions read. Defaults mirror argparse's."""
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


# --- the verbs on cells and specs: selection and printing ---------------------
# Each takes the namespace its command builds. Acting is the owners': a Cell
# for one cell, the Queues for the work list, the Conduct for the run.

def _selectors(args):
    """The verbs take `selectors` (several); a lone `selector` still works."""
    sels = getattr(args, "selectors", None)
    return list(sels) if sels is not None else [args.selector]


def _one_cell(verb, sels, n, queued=None):
    """Exactly ONE cell per cell verb: anything matching more goes through
    the run's bulk verb, so there is one bulk path and one cap owner."""
    if n + (queued or 0) > 1:
        sys.exit(f"`{verb}` acts on exactly ONE cell; {' '.join(sels)!r} matches "
                 + (str(n) if queued is None else f"{n} cell(s) + {queued} queued spec(s)")
                 + f" — bulk {verb} goes through: experiment {verb} AGENT...|all")


def spawn(args):
    """Start one cell now, without slots: refused while the run is up unless
    --dangerously-ignore-slots."""
    try:
        reps = [int(r.strip()) for r in str(args.rep).split(',')]
    except ValueError:
        sys.exit("invalid --rep format. Must be an integer or comma-separated integers.")
    if len(reps) != 1:
        sys.exit("`spawn` starts exactly ONE cell (operator rule, 2026-08-12); "
                 "for several reps enqueue them (top-up / queue add) and let "
                 "`experiment run` admit under its caps")
    refusal = conduct.Conduct().refusal_while_up(
        "spawn", getattr(args, "dangerously_ignore_slots", False))
    if refusal:
        sys.exit(refusal)
    rep = reps[0]
    # smoke= as prepare passes it: a SMOKE=1 spawn computing the unsmoke cid would
    # guard one identity while the cell process runs under another
    cid = _experiment.cell_id(args.agent, args.variant, rep, args.task,
                         smoke=bool(os.environ.get("SMOKE")))
    live = conduct.loop_parents()
    if cid in live:
        print(f"refusing: loop already running for {cid} (pid {live[cid]}) — "
              f"two loops on one workspace corrupt its logs")
        return
    if (fae.experiment.exp().workspace.path / cid).is_dir() and not args.fresh:
        c = fae.experiment.exp().cell(cid)
        L = c.read_ledger()
        if L["verdict"] in ("green", "failed", "revoked"):
            at = f" @{L['green_at']}" if L["verdict"] == "green" else f" @{L['att']}"
            print(f"skipping {cid}: already DONE·{L['verdict']}{at} — use --fresh to force a new run")
            return
        if c.cancelled:
            print(f"skipping {cid}: already DONE·cancelled — use --fresh to force a new run")
            return
    cell = fae.experiment.exp().cell(cid, args.task, args.variant, rep, agent=args.agent)
    if not cell.ready_image():
        sys.exit(f"refusing: the agent image for {args.variant} could not be built "
                 f"(the lines above say why)")
    warning = cell.shared_resource_warning()
    if warning:
        print(warning)
    cell.prestart_clean()
    cell.prepare(fresh=args.fresh)
    if args.agent == "human":
        # the human pseudo-agent is interactive (the driver pauses on a tty each
        # attempt): the command goes to the person's own terminal
        print(f"HUMAN cell {cid} — run this in YOUR terminal (tmux for long sessions):\n")
        print(f"  cd {fae.experiment.exp().root} && {cell.IGNORE_SLOTS_ENV}=1 AGENT=human "
              f"python3 -m fae.cell {args.task} {args.variant} {rep}\n")
        print(f"Each attempt: edit workspaces*/{cid}/artifacts, press ENTER to "
              f"verify (q to stop).")
        return
    if conduct.Conduct().launch(cell, args.agent, what="spawn") is None:
        print(f"spawned {cid}")


def pause(args):
    """Queue a pause of ONE cell for the run to act on. Returns the cid, or None."""
    sels = _selectors(args)
    cids = fae.experiment.exp().workspace.select(*sels)
    if not cids:
        print(f"no cells match {' '.join(sels)!r}")
        return None
    _one_cell("pause", sels, len(cids))
    conduct.queues().request(cids[0], "pause", reason=args.reason, who="operator")
    print(f"pause requested [{args.reason}] for {cids[0]}")
    return cids[0]


def stop_cells(args):
    """Queue a stop of ONE cell for the run to act on (Conduct.stop_cell).
    Resumable — PAUSED·stopped — unless --cancel, the terminal verdict. A spec
    with no workspace is the queue's alone and is shelved here. Returns the
    cids queued."""
    sels = _selectors(args)
    cids = fae.experiment.exp().workspace.select(*sels)
    # specs with no workspace are invisible to the selection; a stop that
    # ignored them would leave the cell to be admitted later
    q_only = [c for c in fae.experiment.exp().workspace.queued_cells(*sels) if c not in cids]
    if not cids and not q_only:
        print(f"no cells match {' '.join(sels)!r}")
        return None
    _one_cell("stop", sels, len(cids), len(q_only))
    cancel = getattr(args, "cancel", False)
    if getattr(args, "dry_run", False):
        verb = "cancel" if cancel else "stop"
        for cid in cids:
            st = conduct.cell_state(fae.experiment.exp().workspace.path / cid, conduct.loop_pids(), conduct.containers())
            print(f"would {verb} {cid}" + (f" ({st['state']}·{st['why']})" if st else ""))
        for cid in q_only:
            print(f"would drop queued spec {cid} (no workspace; backed up)")
        print(f"— {len(cids)} cell(s), {len(q_only)} queued spec(s). Nothing done (--dry-run).")
        return None
    # never cancel a finished verdict: a stop halts runs, it does not relabel data
    done = [c for c in cids
            if (st := conduct.cell_state(fae.experiment.exp().workspace.path / c, {}, set())) and st["state"] == "DONE"]
    cids = [c for c in cids if c not in set(done)]
    for c in done:
        print(f"  {c}: already DONE — left untouched")
    if not cids and not q_only:
        print("nothing to stop (all matches are DONE)")
        return None
    qs = conduct.queues()
    n = sum(qs.shelve_cell(c, "cancelled" if cancel else "stopped") for c in q_only)
    if n:
        print(f"  {n} spec(s) out of the backlog (restore from .queues/backups/)")
    for cid in cids:
        qs.request(cid, "stop", cancel=cancel)
        print(f"{'cancel' if cancel else 'stop'} requested for {cid}")
    return cids


def resume(args):
    """Make ONE cell run again, whatever stopped it: its pause lifted, its
    flag cleared, its respawn budget reset, and its loop respawned without
    slots when it has none (never fresh). A blanket selection leaves standing
    operator decisions (roster/manual pauses, a cancel) alone."""
    qs = conduct.queues()
    sels = _selectors(args)
    matches = fae.experiment.exp().workspace.select(*sels)
    _one_cell("resume", sels, len(matches))
    blanket = _experiment.is_cell_selector_blanket(sels)
    parents = conduct.loop_parents()
    run = conduct.Conduct()
    touched = 0
    for cid in matches:
        st = conduct.cell_state(fae.experiment.exp().workspace.path / cid, conduct.loop_pids(), conduct.containers())
        if st is None:
            continue
        c = fae.experiment.exp().cell(cid)
        if c.sealed:
            print(f"{cid}: SEALED — {c.seal_record().replace(chr(9), ' ') or 'sealed'}; "
                  f"not restartable")
        reason = c.pause_reason
        # before the DONE/loop-alive branch: a pause is cooperative, so "paused
        # but still alive" is the normal state for a long window
        if reason in ("roster", "manual") and blanket:
            continue
        if st["state"] == "DONE" or parents.get(cid):
            # no respawn, but a standing pause is still lifted (a pause-pending
            # loop would honour it after this resume) — except a cancel's
            acts = []
            if reason and reason != "killed":
                c.unpause()
                acts.append("pause lifted")
            if st["state"] == "DONE":
                agent = cid.split("_", 1)[0]
                for q in qs.lane_specs(agent):
                    if qs.spec_cid(q) == cid:
                        try:
                            qs.finish(agent, qs.claim(agent, q))
                        except FileExistsError:
                            qs.shelve(q, "done-duplicate")
                        acts.append("spec retired (cell is DONE)")
                        break
            if acts:
                print(f"  {cid}: {', '.join(acts)} (no respawn — "
                      f"{'done' if st['state'] == 'DONE' else 'loop alive'})")
                touched += 1
            continue
        if reason == "killed" and cid not in sels:
            continue      # killed cells come back only when named exactly
        acts = []
        if reason:
            c.unpause()
            acts.append("pause lifted")
        if c.unflag():
            acts.append("flag cleared")
        agent = cid.split("_", 1)[0]
        # the per-agent cap holds on resume too, unless --force
        if not getattr(args, "force", False):
            live_m = sum(1 for x in conduct.loop_parents() if x.startswith(agent + "_"))
            if live_m >= conduct.PER_AGENT_CAP:
                touched += 1
                acts.append(f"respawn DEFERRED — {agent} already has {live_m} live loop(s) "
                            f"(cap {conduct.PER_AGENT_CAP}; --force overrides; resume again later)")
                print(f"  {cid}: {', '.join(acts)}")
                continue
        if run.reset_respawn_budgets([cid]):
            acts.append("respawn budget reset")
        # claim before spawning: claim() is one rename and refuses an existing
        # claim, so the run cannot admit the same spec meanwhile
        claimed = None
        if not run.is_claimed(cid):
            for q in qs.lane_specs(agent):
                if qs.spec_cid(q) == cid:
                    try:
                        claimed = qs.claim(agent, q)
                        acts.append("spec claimed")
                    except (FileExistsError, OSError):
                        pass
                    break
        if run.respawn(st, dry=False,
                       ignore_slots=getattr(args, "dangerously_ignore_slots", False)):
            acts.append("respawned")
        elif claimed is not None:
            qs.release(agent, claimed, front=True)
            acts.append("spec returned to the lane front")
        touched += 1
        print(f"  {cid}: {', '.join(acts)}")
    if not touched:
        print(f"nothing to resume for {' '.join(sels)!r} (already running or done)")


SEALABLE = {"green": "green", "failed": "budget", "revoked": "revoked"}


def seal(args):
    """Make terminal cells read-only (.sealed). Dry by default: sealing has
    no inverse, so writing the markers is an explicit act (--apply)."""
    loops, boxes, live = conduct.loop_pids(), conduct.containers(), conduct.loop_parents()
    todo, already, skipped = [], 0, {}
    for cid in fae.experiment.exp().workspace.select(args.selector):
        st = conduct.cell_state(fae.experiment.exp().workspace.path / cid, loops, boxes)
        if st is None:
            continue
        if fae.experiment.exp().cell(cid).sealed:
            already += 1
            continue
        why = st.get("why", "")
        if live.get(cid):
            # a verdict and a live loop at once: the loop is between its ITER
            # and its own seal; it seals itself
            skipped[cid] = "loop alive"
        elif st["state"] != "DONE" or why not in SEALABLE:
            skipped[cid] = why or st["state"].lower()
        else:
            todo.append((cid, SEALABLE[why], st["att"]))
    for cid, verdict, att in todo:
        if args.apply:
            try:
                fae.experiment.exp().cell(cid).seal(verdict, att, by="cli.py seal")
            except Busy:
                print(f"skipped       {cid}  (held by another process)")
                continue
        print(f"{'sealed' if args.apply else 'would seal'}  {cid}  {verdict} attempts={att}")
    if args.verbose:
        for cid, why in sorted(skipped.items()):
            print(f"skipped       {cid}  ({why})")
    print(f"\n{'sealed' if args.apply else 'would seal'} {len(todo)} cell(s); "
          f"{already} already sealed; {len(skipped)} not terminal"
          + ("" if args.apply else "  — re-run with --apply to write"))


def reverify(args):
    """Re-run the shape gate on finished cells without touching their result:
    everything lands under <ws>/reverify/<ts>/ (Cell.reverify)."""
    cids = fae.experiment.exp().workspace.select(*_selectors(args))
    if not cids:
        sys.exit("no cells match")
    if len(cids) > 1 and not args.all:
        sys.exit(f"{len(cids)} cells match — re-verify takes the rig for "
                 f"~15 minutes each. Name one, or pass --all.")
    pid = conduct.Conduct().pid()
    if pid:
        print(f"WARNING: conduct is running (pid {pid}); this re-verify queues for the "
              f"verify lock behind its cells and holds it for the whole gate", flush=True)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    for cid in cids:
        c = fae.experiment.exp().cell(cid)
        if not c.terminal:
            print(f"skipping {cid}: not finished ({c.verdict or 'open'}) — "
                  f"re-verify applies to a recorded result")
            continue
        print(f"reverify {cid} -> reverify/{stamp}/  (recorded verdict: {c.verdict})", flush=True)
        try:
            results = c.reverify(stamp=stamp)
        except Busy:
            print(f"  skipped: {cid} is held by another process")
            continue
        want = c.gate_def.arrangements_nr
        ok = all(r.green for r in results) and len(results) == want
        print(f"  {'UPHELD' if ok else 'DIFFERS'}: "
              f"{sum(r.green for r in results)}/{want} arrangement(s) green"
              + ("" if ok else f" — first failure: "
                               f"{results[-1].shape} ({results[-1].stage_failed})"))
        print(f"  evidence: {c.ws / 'reverify' / stamp}")


def tail(args):
    """The agent transcript of the cell's latest attempt."""
    logs = fae.experiment.exp().cell(args.cell).agent_logs()
    if not logs:
        sys.exit("no attempt logs")
    subprocess.run(["tail", *(["-f"] if args.follow else ["-n", "40"]), str(logs[-1])])


def log(args):
    """The cell's most recent story (Cell.console_log)."""
    c = fae.experiment.exp().cell(args.cell)
    path = c.console_log()
    if path is None:
        print(f"no log under {c.ws}")
        return
    print(f"== {path.name}", flush=True)
    subprocess.run(["tail", "-n", "60", str(path)])


def _variants(variants):
    """The variants named, each one of the experiment's; none named: every
    active one."""
    d = fae.experiment.exp().definition
    for v in variants:
        if v not in d.variants:
            sys.exit(f"unknown variant '{v}' — valid: {', '.join(d.active)}")
    return list(variants) or list(d.active)


def spawn_matrix(args):
    """Every active variant, --reps reps each, rep-outer."""
    if args.dry_run:
        would, asked = conduct.queues().enqueue_matrix(
            args.agent, args.task, fae.experiment.exp().definition.active, args.reps,
            fresh=args.fresh, dry_run=True)
        for cid in would:
            print(f"  would enqueue  {cid}")
        print(f"[dry-run] would enqueue {len(would)} spec(s) for {args.agent}"
              + (f"; {asked - len(would)} already pending" if len(would) < asked else ""))
        return
    n, asked = conduct.queues().enqueue_matrix(args.agent, args.task, fae.experiment.exp().definition.active,
                                              args.reps, fresh=args.fresh)
    print(f"enqueued {n} runs for {args.agent} — `experiment run` admits them "
          f"(start it if not running: python3 cli.py experiment run)"
          + (f"; {asked - n} already pending" if n < asked else ""))


def top_up(args):
    """Fill each selected variant's missing reps up to --to-rep (Queues.top_up).
    A rep counts as had when its cell RAN (a verdict, or an attempt in
    flight); a workspace prepared but never launched holds no attempt and
    would cap the variant below target. Nothing is started."""
    variants = _variants(getattr(args, "variants", []))
    have, unstarted = {}, {}
    for cid in fae.experiment.exp().workspace.select(args.agent):
        p = _experiment.parse_cell_id(cid)
        if p[0] != args.agent or p[2] != args.task:
            continue
        c = fae.experiment.exp().cell(cid)
        ran = c.read_ledger()["iters"] or c.heartbeat() is not None
        (have if ran else unstarted).setdefault(p[1], set()).add(int(p[3]))
    need, n = conduct.queues().top_up(args.agent, args.task, variants, args.to_rep, have,
                                     dry_run=args.dry_run)
    for v in variants:
        idle = sorted(unstarted.get(v, set()) & set(need[v]))
        print(f"{args.agent:7} {v:24} have={sorted(have.get(v, ()))} add={need[v]}"
              + (f"  (of which {idle} were prepared but never ran)" if idle else ""))
    todo = sum(len(r) for r in need.values())
    if not todo:
        print("nothing to add — every selected variant is at target or queued")
    elif args.dry_run:
        print(f"[dry-run] would enqueue {todo} spec(s) for {args.agent}")
    else:
        print(f"enqueued {n} spec(s) for {args.agent} (nothing started)")


def cancel_pending(args):
    """Take pending specs out of the queue before admission (Queues.cancel_pending):
    moved aside, never deleted; running and done specs are never touched."""
    hits = conduct.queues().cancel_pending(
        lambda cid: any(_experiment.matches(cid, s) for s in args.selectors), dry_run=args.dry_run)
    if not hits:
        print("cancel: no pending spec matches")
        return []
    qs = conduct.queues()
    for agent, p, dest in hits:
        if dest is None:
            print(f"  would cancel  {agent:10s} {qs.spec_cid(p)}")
        else:
            print(f"  cancelled     {agent:10s} {qs.spec_cid(p)}  -> {dest.parent}")
    return hits


# --- cell -------------------------------------------------------------------

@cell_app.command("spawn")
def cell_spawn(
    agent: str, variant: str,
    rep: str = typer.Option("1", "-r", "--rep", help="int, or comma-separated ints"),
    task: str = typer.Option("T1", "--task", help="task id: T<n>, one the experiment's task/ carries"),
    fresh: bool = typer.Option(False, "--fresh",
                              help="WIPES the workspace (safe_wipe) and restarts at attempt 1"),
    dangerously_ignore_slots: bool = typer.Option(
        False, "--dangerously-ignore-slots",
        help="start even while the run is up; the cell runs without slots, outside the cap"),
):
    """Start one cell now, without slots. Refuses while the run is up, which
    admits cells with their slots, unless --dangerously-ignore-slots."""
    spawn(_ns(agent=agent, variant=variant, rep=rep,
                  task=task, fresh=fresh, dangerously_ignore_slots=dangerously_ignore_slots))


@cell_app.command("pause")
def cell_pause(selectors: list[str] = typer.Argument(..., help=SEL + " Must match ONE cell."),
               reason: str = typer.Option("manual", "--reason",
                                          help="recorded in .paused; roster/manual survive `experiment resume all`")):
    """Ask ONE cell to stop at its next safe point: the run signals its loop,
    which records the pause and stands down there. Bulk pause is
    `experiment pause`."""
    if pause(_ns(selectors=list(selectors), reason=reason)):
        conduct.Conduct().act_on_requests()


@cell_app.command("resume")
def cell_resume(selectors: list[str] = typer.Argument(..., help=SEL + " Must match ONE cell."),
                force: bool = typer.Option(False, "--force",
                                           help="respawn even past the per-agent live-cell cap"),
                dangerously_ignore_slots: bool = typer.Option(
                    False, "--dangerously-ignore-slots",
                    help="respawn even while the run is up; the cell runs without slots")):
    """Lift ONE cell's locks, clear its reconcile flag, respawn its loop
    without slots. The respawn defers at the per-agent cap (--force pushes
    past it) and refuses while the run is up unless --dangerously-ignore-slots.
    Bulk resume is `experiment resume`."""
    resume(_ns(selectors=list(selectors), force=force,
                   dangerously_ignore_slots=dangerously_ignore_slots))


@cell_app.command("stop")
def cell_stop(selectors: list[str] = typer.Argument(..., help=SEL + " Must match ONE cell."),
              cancel: bool = typer.Option(False, "--cancel",
                                          help="TERMINAL: write .cancelled — DONE·cancelled, never comes back"),
              dry_run: bool = typer.Option(False, "--dry-run",
                                           help="list what would be stopped and dropped, do nothing")):
    """Halt ONE cell NOW: the run SIGTERMs its loop (SIGKILL past a grace),
    tears its infra down and removes its queued specs (backed up).
    Resumable — PAUSED·stopped — unless --cancel. Files are never touched.
    Bulk stop is `experiment stop`."""
    if stop_cells(_ns(selectors=list(selectors), cancel=cancel, dry_run=dry_run)):
        conduct.Conduct().act_on_requests()


@cell_app.command("tail")
def cell_tail(cid: str, follow: bool = typer.Option(False, "-f", "--follow",
                                                   help="stream as it grows")):
    """The agent transcript of the latest attempt."""
    tail(_ns(cell=cid, follow=follow))


@cell_app.command("log")
def cell_log(cid: str):
    """The run_cell console log."""
    log(_ns(cell=cid))


@cell_app.command("seal")
def cell_seal(selector: str = typer.Argument("all", help=SEL),
              apply: bool = typer.Option(False, "--apply",
                                         help="write .sealed (dry by default)"),
              verbose: bool = typer.Option(False, "-v", "--verbose",
                                           help="also list cells left alone, and why")):
    """Make terminal cells read-only. No unseal: redo a cell by deleting and
    requeueing it."""
    seal(_ns(selector=selector, apply=apply, verbose=verbose))


@cell_app.command("reverify")
def cell_reverify(selectors: Optional[list[str]] = typer.Argument(None, help=SEL),
                  all_: bool = typer.Option(False, "--all",
                                            help="accept a selector matching more than one cell")):
    """Re-run the shape gate on a finished cell WITHOUT touching its recorded
    result (writes under <ws>/reverify/<ts>/)."""
    reverify(_ns(selectors=list(selectors) if selectors else ["all"], all=all_))


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
                                 help="print the plan, enqueue nothing"),
):
    """Add work to the queue.

    Enqueues rep-outer, so every variant advances together and a partial run
    still yields comparable n across them. Enqueue-only either way:
    admission happens in `experiment run`.
    """
    if not matrix and to_rep is None:
        raise typer.BadParameter("choose --matrix or --to-rep N")
    if to_rep is not None:
        top_up(_ns(agent=agent, to_rep=to_rep, variants=list(variant),
                       task=task, dry_run=dry_run))
        return
    spawn_matrix(_ns(agent=agent, reps=reps, task=task, fresh=fresh, dry_run=dry_run))


@queue_app.command("list")
def queue_list(agents: Optional[list[str]] = typer.Argument(None, help="agent lane(s); default: all"),
               done: bool = typer.Option(False, "--done", help="also list the terminal specs")):
    """The work list per agent lane: pending in admission order (a paused
    lane is marked), and what is running. Read-only."""
    _render.queue_list(_ns(agents=list(agents or []), done=done))


@queue_app.command("cancel")
def queue_cancel(selectors: list[str] = typer.Argument(..., help=SEL + " `all`: every pending spec."),
                 dry_run: bool = typer.Option(False, "--dry-run",
                                              help="list what would be cancelled, move nothing")):
    """Take pending specs out of the queue before admission. They are moved
    aside (.queues/.to_be_deleted/<ts>/queue/), never deleted; running cells
    are not touched (that is `cell stop`)."""
    cancel_pending(_ns(selectors=list(selectors), dry_run=dry_run))


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
                                                            "validation, zombie reap); 0 off"),
                exclude_agent: list[str] = typer.Option([], "--exclude-agent",
                                                        help="AGENT whose credential is not "
                                                             "checked (repeatable); its "
                                                             "cells halt at staging if it is "
                                                             "missing")):
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
    left = _credentials_left(exclude_agent)
    if left:
        for name, why in left.items():
            typer.echo(f"  {name}: {why}", err=True)
        typer.echo("not started: agent credentials missing — `python3 cli.py "
                   "experiment credentials AGENT`, or --exclude-agent AGENT", err=True)
        raise typer.Exit(1)
    conduct.Conduct().run(_ns(limit=limit, per_agent=per_agent,
                        per_agent_override=overrides, interval=interval,
                        supervise_interval=supervise_interval))


def _credentials():
    from fae.cell import agent_image
    from fae.experiment import config as _config, credentials
    exp = fae.experiment.exp()
    return credentials.of(exp.definition, exp.root, _config._toml(exp.root),
                          agent_image.base_tag(exp.definition, exp.root))


def _credentials_left(exclude):
    from fae.experiment import credentials
    try:
        return credentials.ensure(_credentials(), exclude=exclude)
    except ValueError as e:
        raise typer.BadParameter(str(e))


@experiment_app.command("credentials")
def experiment_credentials(agent: str,
                           check: bool = typer.Option(False, "--check",
                                                      help="set nothing up: prove the "
                                                           "credential (claude: one confined "
                                                           "agent start)")):
    """Set up AGENT's credential (an agent is a CLI: claude, agy, opencode;
    every model it runs shares it), then prove it. The secret is read from a
    hidden prompt and written owner-only into the agent's credentials home."""
    creds = _credentials()
    if agent not in creds:
        raise typer.BadParameter(f"no agent {agent!r}; the agents are {sorted(creds)}")
    cred = creds[agent]
    if not check:
        why = cred.setup()
        if why:
            typer.echo(f"{agent}: {why}", err=True)
            raise typer.Exit(1)
    exp = fae.experiment.exp()
    model = next((m["model"] for m in exp.definition.models.values() if m["agent"] == agent), "")
    log_dir = exp.root / "tmp"
    log_dir.mkdir(exist_ok=True)
    why = cred.proven(model, log_dir) if model else cred.missing()
    if why:
        typer.echo(f"{agent}: NOT ready — {why}", err=True)
        raise typer.Exit(1)
    print(f"{agent}: ready ({cred.home})")


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
        _render.monitor(_ns(interval=watch or 60))
    elif watch:
        _render.watch(_ns(interval=watch, flat=flat, running_only=running_only))
    else:
        _render.status(_ns(flat=flat, running_only=running_only))


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
    """Validate and score the finished cells (the selector's, else all), then
    print the metrics table."""
    _render.results_score(_ns(selector=selector, all_cells=all_cells,
                             allow_stale=allow_stale, no_aggregate=no_aggregate,
                             variant=variant, where=list(where), impl=impl,
                             sort_discrepancy=sort_discrepancy,
                             sort_significant=sort_significant))


@results_app.command("validate")
def results_validate(selector: Optional[str] = typer.Argument(None, help=SEL)):
    """Post-DONE trust check: VALID or TAINTED, with reasons."""
    _render.results_validate(_ns(selector=selector))


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
    (`results score` also runs this as its last step)."""
    _render.results_aggregate(_ns(variant=variant, where=list(where), impl=impl,
                                 include_tainted=include_tainted,
                                 tainted_cells_details=tainted_cells_details,
                                 allow_stale=allow_stale, sort_discrepancy=sort_discrepancy,
                                 sort_significant=sort_significant))


# --- experiment / rig ------------------------------------------------------

@experiment_app.command("init")
def experiment_init(experiment: str = typer.Option("", "--experiment",
                                            help="the experiment directory the file "
                                                 "points the engine at (relative to the "
                                                 "root or absolute); default `experiment`")):
    """Write fae.toml at the root with every key at its default; refuses to
    overwrite one that exists."""
    fae.experiment.exp().init(experiment)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("next: `python3 cli.py experiment check --walk`, step by step through "
              "what the experiment needs before a cell runs")
        return
    if input("Walk through the readiness check now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        sys.exit(fae.experiment.exp().check(walk=True))


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
    sys.exit(fae.experiment.exp().check(walk=walk, static=static, smoke=smoke,
                                         tla_trace=tla_trace, variants=variants, task=task))


@rig_app.command("trace-reset")
def rig_trace_reset(dry_run: bool = typer.Option(False, "--dry-run",
                                                 help="preview the EPOCH lines, write nothing")):
    """Re-anchor transitions.log: append the recorded state of every cell (EPOCH)."""
    fae.experiment.exp().reset_trace(dry_run=dry_run)


@experiment_app.command("infra")
def experiment_infra():
    """Every variant's infra preflight (its infra's ok()) + a sweep
    of stale per-verify kind clusters. Creates nothing: each verify provisions
    its own infra."""
    bad = fae.experiment.exp().infra()
    if bad:
        sys.exit(f"infra: {bad} variant(s) refused — see hooks.log lines above")


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
    (`-m fae.cell ... --stub`), in ws-smoke.nosync. Exit 0 iff all green."""
    fae.experiment.exp().smoke(variants=variants, only=only, rep=rep, full_gate=full_gate)


@experiment_app.command("prepare")
def experiment_prepare(agent: str = typer.Option("", "--agent", help="lane name (default: $AGENT)"),
                reps: int = typer.Option(1, "--reps", help="reps per combination"),
                task: str = typer.Option("T1", "--task", help="task id: T<n>, one the experiment's task/ carries")):
    """Seed the matrix's workspaces WITHOUT launching anything
    (fae/cell/prepare.py, the driver's own prepare)."""
    agent = agent or os.environ.get("AGENT")
    if not agent:
        sys.exit("prepare: --agent AGENT (or AGENT in the environment) is required")
    fae.experiment.exp().prepare(agent, reps, task, fresh=bool(os.environ.get("FRESH")))


@experiment_app.command("verb", context_settings=_PASSTHROUGH)
def experiment_verb(ctx: typer.Context):
    """verb [NAME [ARGS...]] — one of the experiment's own commands (its
    definition's commands()), ARGS passed through untouched; no NAME lists
    them."""
    args = list(ctx.args)
    raise SystemExit(fae.experiment.exp().verb(args[0] if args else "", args[1:]) or 0)


# --- rig tool: an instrument, run standalone (debug/one-off) ----------------

def _instrument_dirs():
    """Where an instrument name resolves, in order: the engine's own, the
    contrib blocks, the experiment's."""
    from fae.cell.contrib import elastic_resource
    from fae import paths
    return [paths.ENGINE / "cell" / "instruments", elastic_resource.DIR,
            fae.experiment.exp().definition.path / "instruments"]


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
