"""The operator's direct verbs on cells: spawn, pause, resume, seal, reverify,
stop, top-up, spawn-matrix — everything that acts on exactly ONE cell (or, for
top-up/spawn-matrix, hands new work to the backlog). Also owns respawn: the
mechanism `resume` and the not-yet-moved supervision sweep both use to bring a
crashed or interrupted cell back, since a respawn IS a spawn, budgeted.

Every launch this file can make funnels through _spawn_detached, the one
guard that stops a sealed/locked/faulted cell from starting at all.
"""
from __future__ import annotations

import collections
import os
import signal
import subprocess
import sys
import threading
import time
import ujson as json
from datetime import datetime, timezone

from fae.driver import common
from fae.driver import queue
from fae.driver import state
from fae.driver import zombies

def prestart_clean(cid):
    """Called before starting a cell's loop.

    Clears THIS cid's leftovers that would collide or lie — the agent
    container (a new attempt recreates it; a stale one shadows liveness) and a
    corpse heartbeat. NEVER the dind sidecar or the cell's cluster, whose
    reuse/resume is a designed path.

    Scoped to this cid on purpose. Fleet-wide reaping belongs to `conduct
    run`'s supervision sweep, which knows what it started and reaps on a
    second sighting; a spawn is not a controller."""
    if common.agent_container(cid) in state.containers() and cid not in state.loop_parents():
        subprocess.run(["docker", "rm", "-f", common.agent_container(cid)],
                       capture_output=True)
    ws = common.WS / cid
    if (ws / ".loop").exists() and state.heartbeat(ws) is None:
        (ws / ".loop").unlink(missing_ok=True)


SPAWN_PROBE_S = 2.0


def _crash_before_spawn(cid):
    """Record the Crash a dying loop could not record itself — NOW, not in
    five minutes.

    `_reconcile_dead_loop` already emits this, but only after MODEL_DEAD_GRACE
    (300s) of quiet, because a loop that is merely re-execing is briefly
    pidless and must not be declared dead. A respawn does not have that
    ambiguity: we are about to start a loop, so whoever held this cid before
    is gone, and if the last loop-affecting transition was not a clearing one
    then the previous loop ended without saying so.

    Skipping this is what produced the 2026-08-18 trace: a cell whose setup
    failed was readmitted every ~32s, far inside the grace, so four Spawns
    replayed as illegal and — worse — each left a slot held by a cell that had
    none. Those leaked slots eventually made AcquireSlot un-enabled for the
    whole fleet, and the replay was then judging a world that did not exist.
    """
    last = common._last_transitions().get(cid)
    if not last or last[0] in common.LOOP_CLEARED_BY:
        return False
    if state.loop_parents().get(cid):
        # A live loop is not a crashed one. The callers all check this first,
        # but declaring a running cell dead would desync every later event for
        # it — the exact failure this function exists to prevent — so it is
        # checked here too rather than trusted from outside.
        return False
    common._emit_transition("Crash", cid, "pre-spawn: previous loop ended silently")
    return True


def _spawn_detached(argv, env, cid, what="spawn"):
    """Launch a detached cell driver, then confirm it did not die on the spot.

    The driver refuses a run BEFORE it writes anything for a whole class of
    reasons — a sealed cell (46), a seed-doc mismatch or the loop lock already
    held (43), no credentials or a substrate fault (42 / 45).
    Both spawn paths sent stdout AND stderr to DEVNULL, so those messages went
    nowhere and the console printed "spawned <cid>" regardless. The worst case
    is the seed-doc FATAL: the guard that stops a cell being labelled with an
    information condition it never received fired into /dev/null while the
    operator was told the cell was up.

    Captures stderr, waits briefly (these refusals are immediate), and reports
    an early exit instead of success. Returns True only if the child is still
    alive after the probe.
    """
    # THE chokepoint: spawn, human-spawn and reconcile's respawn all launch a
    # cell through here, so one guard covers every launch this file can make.
    # run_cell.sh refuses a sealed cell too — this stops the process being
    # started at all, so a finished cell costs nothing to leave alone.
    if common.is_sealed(cid):
        print(f"refusing to {what} {cid}: SEALED — {common.seal_reason(cid)}")
        print("  a terminal result is read-only; delete and requeue to redo it")
        return common.SEAL_EXIT
    # Spawning a stopped cell IS the decision to run it, and the model's
    # Spawn is enabled only on intent='run'. Lifting the pause here keeps the
    # two stores of intent — the .paused marker and the trace — in step;
    # without it a stop/spawn pair leaves the marker to be wiped by a --fresh
    # prepare and no Resume is ever recorded, so every later event on the cell
    # replays as an illegal transition. _unpause is a no-op when no pause
    # exists, and refuses to lift a cancellation.
    state._unpause(cid)
    _crash_before_spawn(cid)
    common.ORCH.mkdir(parents=True, exist_ok=True)
    # The driver's stderr for its whole life, not just the probe window: a
    # crash hours later must still have somewhere to land, so this is never
    # unlinked. Under .orch rather than the workspace because a spawn precedes
    # prepare, and writing to <ws>/ would mint a workspace for a cell that
    # never started. Opened "w": one file per cid, so it cannot grow.
    errf = common.ORCH / f"cell.{cid}.err"
    with errf.open("w") as e:
        e.write(f"=== {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} "
                f"{what} {' '.join(str(a) for a in argv)}\n")
        e.flush()
        p = subprocess.Popen(argv, cwd=common.ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=e,
                             start_new_session=True)
    time.sleep(SPAWN_PROBE_S)
    if p.poll() is None:
        return None                     # alive; an int is the immediate exit code
    print(f"FAILED to {what} {cid}: the driver exited {p.returncode} immediately")
    try:
        for line in errf.read_text(errors="replace").strip().splitlines()[-6:]:
            print(f"  {line}")
    except OSError:
        pass
    print(f"  (stderr kept at {errf})")
    return p.returncode


def _cell_argv(task, treatment, condition, rep):
    return [sys.executable, "-m", "fae.cell", task, treatment,
            condition, str(rep)]


def _spawn_spec(model, spec, cid, what):
    """Start one cell from its spec. Returns the spawn rc (None = alive)."""
    prestart_clean(cid)
    env = dict(os.environ, MODEL=model, CONDITION=spec["condition"])
    if spec.get("fresh"):
        env["FRESH"] = "1"
    return _spawn_detached(
        _cell_argv(spec.get("task", "T1"),
                   spec["treatment"], spec["condition"], spec["rep"]),
        env, cid, what)


def _matches(cid, sel):
    """One selector language for pause / resume / kill.

    Matching is ANCHORED at a token boundary. The old rule ended in a bare
    `sel in c`, which subsumed the other two and made any short selector
    fleet-wide: every cid contains `T1` and `r1`, so `kill T1` or `kill r1`
    killed everything, with no confirmation and no dry-run. It also made a
    FULL cid an unsafe target for its own siblings — `..._r1` is a substring
    of `..._r10`, so killing rep 1 would take reps 10-19 with it once reps go
    past nine (they already have: reps 4-6 were queued and trimmed).

    Anchored means: the whole cid, or a run of complete `_`-separated tokens
    starting at a token boundary. `sonnet` still matches every sonnet cell,
    an arm's name still matches that arm, `..._r1` matches only rep 1.
    """
    if cid == sel:
        return True
    return f"_{cid}_".find(f"_{sel}_") >= 0


def select_cells(sel):
    """cids with a workspace matching the selector."""
    cids = sorted(p.name for p in common.WS.iterdir()
                  if p.is_dir() and common.parse_cell_id(p.name))
    if sel in ("all", "*"):
        return cids
    return [c for c in cids if _matches(c, sel)]


def select_cells_many(selectors):
    """Union of select_cells over several selectors, deduped, sorted."""
    out = set()
    for sel in selectors:
        out.update(select_cells(sel))
    return sorted(out)


def queued_cids(sel):
    """cids that exist ONLY as a pending spec — no workspace yet.

    select_cells enumerates workspaces, so every operator verb built on it was
    blind to work that had been enqueued but not started. That is what made
    `drain` unsafe: it paused the workspaces that existed, waited for their
    loops, and printed "safe to edit FP-guarded files" while the scheduler was
    still free to start a brand new cell against a half-edited tree."""
    out = []
    for d in queue.lane_dirs(include_parked=True):
        for p in queue._dir_specs(d):
            cid = queue.spec_cid(p)
            if (common.WS / cid).is_dir():
                continue                      # has a workspace: select_cells has it
            if sel in ("all", "*") or _matches(cid, sel):
                out.append(cid)
    return sorted(set(out))


def queued_cids_many(selectors):
    out = set()
    for sel in selectors:
        out.update(queued_cids(sel))
    return sorted(out)


def request_pause(cids, reason, who="operator"):
    """Write the per-cell stop request. COOPERATIVE: loops poll it at their own
    safe points (attempt boundary, arm-lock queue, retry sleep) and exit
    through their teardown trap.

    Nothing is signalled. The old pause SIGSTOPped loops, which is not a pause
    at all — a frozen loop still holds the per-arm lock, so pausing one model
    would deadlock that arm for every other model until a human noticed."""
    stamp = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    written = []
    for cid in cids:
        if state.pause_lock(cid):
            continue   # an existing lock is an operator decision — never
                       # overwrite its reason (a drain must not relabel a
                       # standing manual/roster pause, audit finding)
        # TERMINAL cells are not paused. There is no loop to stop, and a
        # .paused on a finished cell has a cost: reconcile skips any cell whose
        # intent is not "run", and that check sits ABOVE its
        # mandatory-at-DONE validation — so after a drain or a stop-all
        # (both of which pause "all"), reconcile silently stopped validating
        # the ENTIRE fleet until someone ran `resume all`. Cells going terminal
        # in that window got no validation.json, and cell_state's taint read
        # defaults to False, so they read as untainted while never having been
        # checked. stop_cells already refuses to cancel terminal cells for the
        # sibling reason; pause now matches.
        _st = state.cell_state(common.WS / cid, {}, set())
        if _st and _st["state"] == "DONE":
            continue
        state._pause_file(cid).write_text(f"{reason} by={who} at={stamp}\n")
        written.append(cid)
        # Kill routes through request_pause(reason="killed") — tag it
        # distinctly from a plain Pause so live-trace replay picks Kill(c),
        # not Pause(c), matching .tla/Runs.tla's action set.
        common._emit_transition("Kill" if reason == "killed" else "Pause", cid,
                          f"reason={reason} by={who}")
    return written


def _selector_list(args):
    """The verbs take `selectors` (nargs +); old notes/scripts may say
    `selector`. Accept either, always a list."""
    sels = getattr(args, "selectors", None)
    if sels is None:
        sels = [args.selector]
    return list(sels)


def _is_blanket(sels):
    """'resume all' semantics: standing operator decisions (roster/manual
    pauses, killed intent) survive a BLANKET resume but yield to a named one.
    The invocation is blanket only when it is exactly the wildcard — `resume
    all sonnet` names sonnet for every sonnet cell but the wildcard still
    covers the rest, and the guard exists for cells the operator did NOT
    name, so a list containing all/* is blanket."""
    return any(s in ("all", "*") for s in sels)


def pause(args):
    sels = _selector_list(args)
    cids = select_cells_many(sels)
    if not cids:
        print(f"no cells match {' '.join(sels)!r}"); return
    if len(cids) > 1:
        sys.exit(f"`pause` acts on exactly ONE cell; {' '.join(sels)!r} "
                 f"matches {len(cids)} — bulk pause goes through: "
                 f"conduct-pause MODEL...|all")
    request_pause(cids, args.reason)
    print(f"pause requested [{args.reason}] for {len(cids)} cell(s) — each loop "
          f"stops at its next safe point; workspaces are preserved")
    for cid in cids:
        print(f"  {cid}")


SEALABLE = {"green": "green", "failed": "budget", "revoked": "revoked"}


def seal(args):
    """Make terminal cells read-only (writes <ws>/.sealed).

    Terminal = the ledger reached a verdict. Everything still open, still
    running, or cancelled is left alone. Dry by default — sealing has no
    inverse, so writing 470 markers is an explicit act (--apply).
    """
    loops, boxes, live = state.loop_pids(), state.containers(), state.loop_parents()
    todo, already, skipped = [], 0, {}
    for cid in select_cells(args.selector):
        st = state.cell_state(common.WS / cid, loops, boxes)
        if st is None:
            continue
        if common.is_sealed(cid):
            already += 1
            continue
        why = st.get("why", "")
        if live.get(cid):
            # A verdict and a live loop at once means the loop is between its
            # ITER and its own seal; let it finish and seal itself.
            skipped[cid] = "loop alive"
        elif st["state"] != "DONE" or why not in SEALABLE:
            skipped[cid] = why or st["state"].lower()
        else:
            todo.append((cid, SEALABLE[why], st["att"]))
    for cid, verdict, att in todo:
        if args.apply:
            p = common.WS / cid / common.SEAL_MARKER
            p.write_text(f"sealed={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t"
                         f"verdict={verdict}\tattempts={att}\tby=cli.py seal\n")
            p.chmod(0o444)
        print(f"{'sealed' if args.apply else 'would seal'}  {cid}  "
              f"{verdict} attempts={att}")
    if args.verbose:
        for cid, why in sorted(skipped.items()):
            print(f"skipped       {cid}  ({why})")
    print(f"\n{'sealed' if args.apply else 'would seal'} {len(todo)} cell(s); "
          f"{already} already sealed; {len(skipped)} not terminal"
          + ("" if args.apply else "  — re-run with --apply to write"))


def reverify(args):
    """Re-run the shape gate on a finished cell WITHOUT touching its result.

    Everything lands under `<ws>/reverify/<ts>/`; iterations.log is not
    appended to, and metrics.json, trace.csv and verify.log keep the numbers
    the cell was judged on. Old and new sit side by side.

    This replaces harness/reverify_cell.sh, which ran the six arrangements in
    the cell's OWN workspace — truncating verify.log and k6.log and
    overwriting metrics.json and trace.csv, three of the four files that ARE
    the recorded result. That is why sealing had to refuse it outright.

    It is also the tool for iterating on the verify itself: change it,
    re-verify a sample, diff, repeat — impossible while the only way to re-run
    was to overwrite.
    """
    sys.path.insert(0, str(common.ROOT))
    from fae.cell import Cell             # noqa: E402  (heavy; only here)
    cids = select_cells_many(_selector_list(args))
    if not cids:
        sys.exit("no cells match")
    if len(cids) > 1 and not args.all:
        sys.exit(f"{len(cids)} cells match — re-verify takes the rig for "
                 f"~15 minutes each. Name one, or pass --all.")
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    for cid in cids:
        c = Cell(cid, workspaces=common.WS, root=common.ROOT)
        if not c.terminal:
            print(f"skipping {cid}: not finished ({c.verdict or 'open'}) — "
                  f"re-verify applies to a recorded result")
            continue
        print(f"reverify {cid} -> reverify/{stamp}/  "
              f"(recorded verdict: {c.verdict})", flush=True)
        results = c.reverify(stamp=stamp)
        want = c.gate_def.arity
        ok = all(r.green for r in results) and len(results) == want
        print(f"  {'UPHELD' if ok else 'DIFFERS'}: "
              f"{sum(r.green for r in results)}/{want} arrangement(s) green"
              + ("" if ok else f" — first failure: "
                               f"{results[-1].shape} ({results[-1].stage_failed})"))
        print(f"  evidence: {c.ws / 'reverify' / stamp}")


def resume(args):
    """Make the selected cells run again, whatever stopped them.

    THE human act for every stopped-cell condition: lifts pause locks, clears
    a reconcile human-flag, resets the crash-respawn budget, and respawns any
    cell that has no loop (resume, never --fresh). The respawn budget reset is
    deliberate: the flag means 'a human must look', and resume IS that human
    — the counter also used to be consumed by operator drains and stops that
    were indistinguishable from crashes, charging maintenance to the cell."""
    sels = _selector_list(args)
    matches = select_cells_many(sels)
    # Exactly-1 rule (operator, 2026-08-12): a direct spawn is safe at n=1 —
    # the resume-all burst started ~15 loops because each respawn raced the
    # stale loop_parents() view of the ones before it. Bulk resume requeues
    # instead and lets conduct admit under its caps.
    if len(matches) > 1:
        sys.exit(f"`resume` acts on exactly ONE cell; {' '.join(sels)!r} "
                 f"matches {len(matches)} — bulk resume goes through: "
                 f"conduct-resume MODEL...|all")
    blanket = _is_blanket(sels)
    parents = state.loop_parents()
    touched = 0
    for cid in matches:
        ws = common.WS / cid
        st = state.cell_state(ws, state.loop_pids(), state.containers())
        if st is None:
            continue
        if common.is_sealed(cid):
            # Sealed cells are DONE by construction, so the branch below
            # already declines to respawn them. Said out loud rather than
            # reporting a "resume" that only tidied bookkeeping — and the
            # bookkeeping still runs, since a stale lock on a finished cell
            # would keep rendering it as paused forever.
            print(f"{cid}: SEALED — {common.seal_reason(cid)}; not restartable")
        _reason = state.pause_lock(cid)
        if _reason in ("roster", "manual") and blanket:
            # Checked BEFORE the DONE/loop-alive branch below, not after.
            # Pause is cooperative: a loop keeps running until its next safe
            # point, which during an agent build is far away, so "paused but
            # still alive" is the NORMAL state for a long window — and that
            # branch lifted any non-killed lock, roster included. The guard
            # therefore missed exactly the cells it was written for: the ones
            # paused most recently. That is the 2026-07-24 resurrection
            # (a roster paused until Tuesday) coming back. Name the model or
            # the cid to lift these.
            continue
        if st["state"] == "DONE" or parents.get(cid):
            # DONE cells and live loops don't get respawned, but a standing
            # lock must still be lifted: a pause-pending loop would otherwise
            # honor it AFTER this resume reported success (audit finding), and
            # a DONE cell's stale lock would instantly pause any future re-run.
            # Exception: reason 'killed' is kill's durable intent — resume
            # must not soften it.
            done_acts = []
            if state.pause_lock(cid) and state.pause_lock(cid) != "killed":
                state._unpause(cid)
                done_acts.append("pause lifted")
            # A terminal cell's spec is finished work: retire it here rather
            # than leave the lane reporting it as backlog and naming it as
            # `next:` until supervision or an admission scan gets to it.
            if st["state"] == "DONE":
                model = cid.split("_", 1)[0]
                for q in queue.lane_specs(model):
                    if queue.spec_cid(q) == cid:
                        try:
                            queue.finish(model, queue.claim(model, q))
                        except FileExistsError:
                            queue.shelve(q, "done-duplicate")
                        done_acts.append("spec retired (cell is DONE)")
                        break
            if done_acts:
                print(f"  {cid}: {', '.join(done_acts)} (no respawn — "
                      f"{'done' if st['state'] == 'DONE' else 'loop alive'})")
                touched += 1
            continue
        reason = state.pause_lock(cid)
        if reason == "killed" and cid not in sels:
            continue      # killed cells come back only when named exactly
        if reason in ("roster", "manual") and blanket:
            # standing operator decisions survive a blanket resume: 'resume
            # all' is the routine drain-release step, and it once resurrected
            # a roster paused until Tuesday (2026-07-24). Name the model or
            # cell to lift these.
            continue
        acted = []
        if state.pause_lock(cid):
            state._unpause(cid); acted.append("pause lifted")
        if (ws / "reconcile.flagged").exists():
            (ws / "reconcile.flagged").unlink(); acted.append("flag cleared")
        # Per-model cap holds on resume too: locks are lifted above either
        # way, but the RESPAWN defers while the model already has a live
        # loop (this call's own respawns included), unless --force pushes
        # past it. Without this, resuming several recovered cells quietly
        # ran a model 2-wide against conduct's 1/model admission cap.
        model = cid.split("_", 1)[0]
        if not getattr(args, "force", False):
            live_m = sum(1 for c in state.loop_parents() if c.startswith(model + "_"))
            if live_m >= common.PER_MODEL_CAP:
                touched += 1
                acted.append(f"respawn DEFERRED — {model} already has "
                             f"{live_m} live loop(s) (cap {common.PER_MODEL_CAP}; "
                             f"--force overrides; resume again later)")
                print(f"  {cid}: {', '.join(acted)}")
                continue
        common.ORCH.mkdir(parents=True, exist_ok=True)
        with common.fs_lock(common.ORCH / "respawn-book.lock"):
            book = {}
            if RESPAWN_BOOK.exists():
                try:
                    book = json.loads(RESPAWN_BOOK.read_text())
                except json.JSONDecodeError:
                    book = {}
            if book.pop(cid, None) is not None:
                RESPAWN_BOOK.write_text(json.dumps(book)); acted.append("respawn budget reset")
        refresh_cell_creds(cid.split("_", 1)[0])
        # Claim BEFORE spawning. claim() is one rename(2) and refuses an
        # existing claim, so conduct cannot admit the same spec concurrently;
        # a spawn that never starts hands it straight back to the lane front.
        # Without this the loop runs while its spec still reads as backlog.
        claimed = None
        if not _claimed(cid):
            for q in queue.lane_specs(model):
                if queue.spec_cid(q) == cid:
                    try:
                        claimed = queue.claim(model, q)
                        acted.append("spec claimed")
                    except (FileExistsError, OSError):
                        pass
                    break
        if _respawn(st, dry=False):
            acted.append("respawned")
        elif claimed is not None:
            queue.release(model, claimed, front=True)
            acted.append("spec returned to the lane front")
        touched += 1
        print(f"  {cid}: {', '.join(acted)}")
    if not touched:
        print(f"nothing to resume for {' '.join(sels)!r} (already running or done)")


def _shelve_specs(cids, why="stopped"):
    """Take these cids' specs out of play so nothing re-admits a stopped cell.
    Specs are moved to backups/, never deleted — a mistaken stop is undone by
    moving the file back into its lane."""
    n = 0
    for cid in sorted(set(cids)):
        model = cid.split("_", 1)[0]
        for d in (queue.lane_dir(model), queue.lane_dir(model, parked=True), queue.rundir(model)):
            if not d.is_dir():
                continue
            for p in sorted(d.glob(f"*{cid}.json")):
                queue.shelve(p, why)
                n += 1
    if n:
        print(f"  {n} spec(s) out of the backlog (restore from .orch/backups/)")
    return n


STOP_GRACE_S = int(os.environ.get("STOP_GRACE_S", 120))   # cooperative window


TERM_GRACE_S = int(os.environ.get("TERM_GRACE_S", 30))    # after SIGTERM


TEARDOWN_TIMEOUT_S = int(os.environ.get("TEARDOWN_TIMEOUT_S", 240))


def _await_exit(pid, grace, poll=2):
    """True if pid is gone within grace.

    Module level so tests can replace it wholesale: patching os.kill with a
    bare Mock makes every pid look alive forever, which would burn the whole
    grace in a unit test.
    """
    end = time.time() + grace
    while time.time() < end:
        if not zombies._pid_alive(pid):
            return True
        time.sleep(poll)
    return not zombies._pid_alive(pid)


def _kill_group(pid, sig):
    """The driver starts its own session, and the clients it runs (the verify
    container's, the agent's) live in it: a signal to the pid alone leaves
    them running."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, sig)
        except (OSError, ProcessLookupError):
            pass


def _bringup_teardown(cid):
    """The variant's verify_teardown for the cell's last arrangement (the
    per-verify cluster, the daemon's leases, the sidecar's inner world) —
    what the verify's own `finally` would have run had it been allowed to
    finish. The verify container died with the kill; this runs in a fresh
    one of the same image (fae/cell/verify.py: run_teardown). Best effort."""
    from fae.cell import verify as _verify
    from fae.cell.variants import _ShimCell
    p = common.parse_cell_id(cid)
    if not p:
        return
    cls = common.definition().variant(p[1])
    ws = common.WS / cid
    if cls is None or not (ws / "artifacts").is_dir():
        return
    cell = _ShimCell(cid, ws, common.ROOT)
    cell.treatment = p[1]
    variant = cls(cell)
    ctx = _verify.Ctx(root=str(common.ROOT), experiment_dir=str(cell.conf.get("EXPERIMENT_DIR")),
                      workspace=str(ws), artifacts=str(ws / "artifacts"), out=str(ws),
                      cid=cid, task=p[3], variant=p[1])
    _verify.run_teardown(ctx, variant, timeout_s=TEARDOWN_TIMEOUT_S)


def _variant_teardown(treatment, cid, timeout=None):
    """The arm's teardown (fae/cell/variants.py <Arm>.teardown), run
    in-process through the cell it belongs to — the same call the driver's own
    `finally` makes. Best-effort and idempotent by the treatment's contract;
    an exception lands as an ALERT in the cell's ledger (Cell.teardown).

    `timeout`: run it on a thread and give up waiting after that many
    seconds. The thread is a daemon, so a docker call that hangs past it is
    abandoned rather than wedging conduct; it is reported, never hidden.
    """
    if not treatment:
        return

    def run():
        sys.path.insert(0, str(common.ROOT))
        from fae.cell import Cell
        c = Cell(cid, workspaces=common.WS, root=common.ROOT)
        c._env.setdefault("TREATMENT", treatment)
        try:
            c.teardown()
        except Exception as e:           # Cell.teardown already ALERTs; a
            common._rec_log(f"{cid} treatment teardown raised {type(e).__name__}: {e}")

    if timeout is None:
        run()
        return
    t = threading.Thread(target=run, daemon=True, name=f"teardown-{cid}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        common._rec_log(f"{cid} treatment teardown still running after {timeout}s "
                 f"— abandoned (inspect the {treatment} substrate by hand)")


def _teardown_cell(cid, treatment=None, *, reason, grace=STOP_GRACE_S,
                   unblock_agent=False, dry=False):
    """THE one way a cell dies by conduct's hand. Returns what it took:
    "cooperative" | "termed" | "killed" | "absent".

    Cooperative first, and the substrate is gone before this returns. The arm
    lock does not guard a process, it guards provisioned substrate — a kind
    cluster, a dind sidecar, a host daemon — and the kernel frees the slot
    the instant the owner dies. A kill that does not also tear down therefore
    hands the arm to a new cell while the old cluster is still running.
    """
    if treatment is None:
        st = state.cell_state(common.WS / cid, {}, set())
        treatment = (st or {}).get("treatment")
    pid = state.loop_parents().get(cid)
    if dry:
        return "dry"

    outcome = "absent"
    if pid:
        # .paused is the loop's own stand-down signal; it exits through
        # _cell_exit, the only path that tears down in the right order.
        request_pause([cid], reason, who="conduct")
        # A loop parked inside the agent command notices nothing until that
        # command returns. Removing the AGENT box (only that box — the sidecar
        # is the loop's to release) makes it return.
        if unblock_agent:
            subprocess.run(["docker", "rm", "-f", common.agent_container(cid)],
                           capture_output=True)
        if _await_exit(pid, grace):
            outcome = "cooperative"
        else:
            try:
                os.kill(pid, signal.SIGTERM)   # bash runs the EXIT trap on this
            except (OSError, ProcessLookupError):
                pass
            if _await_exit(pid, TERM_GRACE_S):
                outcome = "termed"
            else:
                _kill_group(pid, signal.SIGKILL)   # skips the trap: from here
                _bringup_teardown(cid)             # the substrate is ours
                outcome = "killed"

    # Always, and always AFTER the loop is dead: running it under a live loop
    # tears down substrate the loop is still using. Idempotent, so the
    # cooperative case re-runs a no-op.
    _variant_teardown(treatment, cid, timeout=TEARDOWN_TIMEOUT_S)
    subprocess.run(["docker", "rm", "-f", "-v", common.agent_container(cid),
                    *common.substrate_containers(treatment, cid)], capture_output=True)
    return outcome


def stop_cells(args):
    """Halt ONE cell NOW: TERM its loop, tear down its substrate, take its
    queued specs out of the backlog (backed up). Default is RESUMABLE —
    the cell reads PAUSED·stopped and `cell resume CID` continues it.
    `--cancel` is the terminal verdict: writes `.cancelled`, the cell renders
    DONE·cancelled and never comes back (KilledStaysDead).

    Exactly ONE cell by the operator's 2026-08-12 rule: anything matching
    more goes through conduct-stop, so there is a single bulk path and a
    single cap owner.

    Order matters and is the lesson of 2026-07-24: the stop request goes in
    FIRST so reconcile cannot respawn into the gap, then the loop dies, then
    the SUBSTRATE is torn down explicitly — a SIGKILLed loop never runs its
    EXIT trap, so nothing would release the arm lock or delete the per-cell
    kind cluster / dind sidecar, and five orphan clusters once accumulated
    exactly that way.

    Files are NEVER touched: a stop halts the run, it does not judge the
    data. Workspace disposal is a separate, explicit human act."""
    sels = _selector_list(args)
    cids = select_cells_many(sels)
    # Specs with no workspace are invisible to the selector, so a stop that
    # ignored them would leave the cell to be admitted later — the
    # resurrection _shelve_specs prevents, one layer earlier.
    q_only = queued_cids_many(sels)
    if not cids and not q_only:
        print(f"no cells match {' '.join(sels)!r}"); return
    if len(cids) + len(q_only) > 1:
        sys.exit(f"`stop` acts on exactly ONE cell; {' '.join(sels)!r} matches "
                 f"{len(cids)} cell(s) + {len(q_only)} queued spec(s) — bulk "
                 f"stop goes through: conduct-stop MODEL...|all")
    if getattr(args, "dry_run", False):
        verb = "cancel" if getattr(args, "cancel", False) else "stop"
        for cid in cids:
            st = state.cell_state(common.WS / cid, state.loop_pids(), state.containers())
            print(f"would {verb} {cid}"
                  + (f" ({st['state']}·{st['why']})" if st else ""))
        for cid in q_only:
            print(f"would drop queued spec {cid} (no workspace; backed up)")
        print(f"— {len(cids)} cell(s), {len(q_only)} queued spec(s). "
              f"Nothing done (--dry-run).")
        return
    # never cancel a finished verdict: a stop must halt RUNS, not
    # relabel completed data as DONE·cancelled (architecture sweep, 3/3)
    done = [c for c in cids
            if (st := state.cell_state(common.WS / c, {}, set())) and st["state"] == "DONE"]
    cids = [c for c in cids if c not in set(done)]
    for c in done:
        print(f"  {c}: already DONE — left untouched")
    if not cids and not q_only:
        print("nothing to stop (all matches are DONE)"); return
    cancel = getattr(args, "cancel", False)
    # reason "killed" keeps the TLA mapping (request_pause emits Kill for it,
    # Pause otherwise) and resume's named-cid-only guard for cancelled cells;
    # "stopped" renders PAUSED·stopped and resumes like any pause.
    request_pause(cids, "killed" if cancel else "stopped")
    # DURABLE INTENT FIRST, before any slow or failure-prone work. This used to
    # be written per-cid at the END of the teardown loop below, after SIGKILL,
    # `docker rm -f` and the treatment teardown — so a Ctrl-C, an exception or a
    # hung docker call between here and there left the cell holding
    # `.paused reason=killed` with NO `.cancelled`. That cell renders
    # PAUSED·killed instead of DONE·cancelled, which means the kill did not
    # stick: cell_state falls through to the pause axis and the cell is
    # respawnable again.
    #
    # A cancel that is written last can be lost to any interruption between
    # the kill and the write, and a killed cell that reads as merely paused
    # is respawned (.tla/Runs.tla, KilledStaysDead).
    if cancel:
        for cid in cids:
            (common.WS / cid / ".cancelled").write_text(
                f"killed by=operator at={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n")
    # Scrub the queue for BOTH: cells we just cancelled, and specs that never
    # had a workspace to cancel. `queues` was pruned by the selector: a full
    # cid or model name touches only its own lane's file.
    _shelve_specs(set(cids) | set(q_only),
                  "cancelled" if cancel else "stopped")
    parents = state.loop_parents()
    for cid in cids:
        pid = parents.get(cid)
        if pid:
            try:
                _kill_group(pid, signal.SIGKILL)
                print(f"  {cid}: loop {pid} killed")
                # trace conformance: a plain stop emitted Pause (intent only —
                # the model's loop is still live); the SIGKILL is exactly
                # Crash(c) ("SIGKILL / OOM / laptop sleep"). Without it the
                # replay's loop never reaches "none" and the eventual resume
                # Spawn reads as a violation. --cancel already emitted Kill,
                # which takes loop to "none" itself — Crash would be disabled.
                if not cancel:
                    common._emit_transition("Crash", cid, "stopped-by-operator")
            except ProcessLookupError:
                pass
        _bringup_teardown(cid)
        parsed = common.parse_cell_id(cid)
        subprocess.run(["docker", "rm", "-f", "-v", common.agent_container(cid),
                        *common.substrate_containers(parsed[1] if parsed else "", cid)],
                       capture_output=True)
        st = state.cell_state(common.WS / cid, {}, set())
        if st:
            _variant_teardown(st["treatment"], cid)
    # With --cancel, .cancelled is what makes it STICK — the cell renders
    # DONE·cancelled, reconcile treats it as terminal, resume skips DONE, and
    # conduct's doneness check sees a finished cell. It is written above,
    # before teardown, so an interrupted cancel still sticks. (The cancel axis
    # was dead code once, and `resume all` resurrected killed cells — audit
    # finding 5.)
    if cancel:
        print(f"cancelled {len(cids)} cell(s); substrate torn down, workspaces "
              f"untouched (un-cancel: rm workspaces.nosync/<cid>/.cancelled + "
              f"cli.py cell resume <cid>)")
    else:
        print(f"stopped {len(cids)} cell(s) — PAUSED·stopped, resumable "
              f"(cli.py cell resume <cid>); substrate torn down, queued specs "
              f"backed up, workspaces untouched")


def spawn_matrix(args):
    specs = []
    for rep in range(1, args.reps + 1):
        for treatment, conditions in common.definition().matrix.items():
            for condition in conditions:
                specs.append(dict(task=args.task, treatment=treatment,
                                  condition=condition, rep=rep, fresh=args.fresh))
    n = sum(queue.enqueue(args.model, s) is not None for s in specs)
    print(f"enqueued {n} runs for {args.model} — conduct admits them "
          f"(start it if not running: python3 cli.py conduct run)"
          + (f"; {len(specs) - n} already pending" if n < len(specs) else ""))


def _parse_combo(s):
    """'treatment·condition' or 'treatment/condition' -> (treatment, condition)."""
    for sep in ("·", "/"):
        if sep in s:
            t, v = s.split(sep, 1)
            if t in common.definition().matrix and v in common.definition().matrix[t]:
                return t, v
            sys.exit(f"unknown combo '{s}' — valid: "
                     + ", ".join(f"{t}·{v}" for t in common.definition().matrix for v in common.definition().matrix[t]))
    sys.exit(f"combo '{s}' needs the form treatment·condition or treatment/condition")


def _default_combos(conditions, all_conditions):
    """apidocs on every treatment, plus any condition named — or the whole
    matrix. The apidocs batch is the study's comparison; the other conditions
    are opt-in so a bare top-up never widens it."""
    wanted = None if all_conditions else {"apidocs", *conditions}
    for v in conditions:
        if not any(v in vs for vs in common.definition().matrix.values()):
            sys.exit(f"unknown condition '{v}' — valid: "
                     + ", ".join(sorted({x for vs in common.definition().matrix.values() for x in vs})))
    return [(t, v) for t in common.definition().matrix for v in common.definition().matrix[t]
            if wanted is None or v in wanted]


def top_up(args):
    """Fill each selected combo's missing reps up to --to-rep, rep-outer.

    Rep-outer keeps every combination advancing together, so a partially
    drained queue still yields comparable n across the matrix. A rep is
    skipped when its workspace has RUN (any verdict, or attempts in flight) or
    a spec for it is already queued. Workers are NOT started.

    A prepared-but-never-launched workspace does not count as a rep: it holds
    no attempt and will never produce a score, so counting it silently caps
    the combo below target and every coverage report inherits the error.
    """
    combos = [_parse_combo(c) for c in args.combos] if args.combos \
        else _default_combos(getattr(args, "conditions", []), getattr(args, "all_conditions", False))
    queued = set()
    for d_ in (queue.lane_dir(args.model), queue.lane_dir(args.model, parked=True)):
        for p in queue._dir_specs(d_):
            try:
                s = queue.read_spec(p)
            except (OSError, json.JSONDecodeError):
                continue
            queued.add((s["treatment"], s["condition"], s["rep"]))
    have = collections.defaultdict(set)
    unstarted = collections.defaultdict(set)
    for d in common.WS.iterdir():
        if not d.is_dir():
            continue
        p = common.parse_cell_id(d.name)
        if not (p and p[0] == args.model and p[3] == args.task):
            continue
        if common.ledger.parse(d)["iters"] or (d / ".loop").exists():
            have[(p[1], p[2])].add(int(p[4]))
        else:
            unstarted[(p[1], p[2])].add(int(p[4]))
    need = {c: [r for r in range(1, args.to_rep + 1)
                if r not in have[c] and (c[0], c[1], r) not in queued]
            for c in combos}
    specs = [dict(task=args.task, treatment=t, condition=v, rep=rep, fresh=False)
             for rep in range(1, args.to_rep + 1)
             for (t, v) in combos if rep in need[(t, v)]]
    for (t, v) in combos:
        idle = sorted(unstarted[(t, v)] & set(need[(t, v)]))
        print(f"{args.model:7} {t}·{v:9} have={sorted(have[(t, v)])} "
              f"add={need[(t, v)]}"
              + (f"  (of which {idle} were prepared but never ran)" if idle else ""))
    if not specs:
        print("nothing to add — every selected combo is at target or queued")
        return
    if args.dry_run:
        print(f"[dry-run] would enqueue {len(specs)} spec(s) for {args.model}")
        return
    n = sum(queue.enqueue(args.model, s) is not None for s in specs)
    print(f"enqueued {n} spec(s) for {args.model} (nothing started)")


def spawn(args):
    try:
        reps = [int(r.strip()) for r in str(args.rep).split(',')]
    except ValueError:
        sys.exit("invalid --rep format. Must be an integer or comma-separated integers.")
    if len(reps) != 1:
        sys.exit("`spawn` starts exactly ONE cell (operator rule, 2026-08-12); "
                 "for several reps enqueue them (top-up / queue add) and let "
                 "conduct admit under its caps")

    if args.model != "human":
        from fae.driver import image
        if not image.ensure_agent_for(args.treatment):
            sys.exit(f"refusing: the agent image for {args.treatment} could not be built "
                     f"(the lines above say why)")

    live = state.loop_parents()
    for rep in reps:
        # smoke= must flow here exactly as prepare passes it: a SMOKE=1 spawn
        # that computes the unsmoke cid guards one identity while the cell
        # process (which honors SMOKE via load_config) runs under another.
        cid = common.cell_id(args.model, args.treatment, args.condition, rep, args.task,
                      smoke=bool(os.environ.get("SMOKE")))
        if cid in live:
            print(f"refusing: loop already running for {cid} (pid {live[cid]}) — "
                  f"two loops on one workspace corrupt its logs")
            continue
        ws = common.WS / cid
        if ws.is_dir() and not args.fresh:
            L = common.ledger.parse(ws)
            if L["verdict"] in ("green", "failed", "revoked"):
                at = f" @{L['green_at']}" if L["verdict"] == "green" else f" @{L['att']}"
                print(f"skipping {cid}: already DONE·{L['verdict']}{at} — use --fresh to force a new run")
                continue
            if (ws / ".cancelled").exists():
                print(f"skipping {cid}: already DONE·cancelled — use --fresh to force a new run")
                continue
        prestart_clean(cid)
        if args.model == "human":
            # human pseudo-model is INTERACTIVE (the driver pauses on a tty
            # each attempt) — print the command for the person's own terminal
            # instead of detaching it. Same cell machinery, budget and verify.
            print(f"HUMAN cell {cid} — run this in YOUR terminal (tmux for long sessions):\n")
            print(f"  cd {common.ROOT} && "
                  + (f"FRESH=1 " if args.fresh else "")
                  + f"MODEL=human CONDITION={args.condition} "
                  f"python3 -m fae.cell {args.task} {args.treatment} "
                  f"{args.condition} {rep}\n")
            print("Each attempt: edit workspaces*/{cid}/artifacts, press ENTER to "
                  "verify (q to stop).".format(cid=cid))
            continue
        env = dict(os.environ, MODEL=args.model, CONDITION=args.condition)
        if args.fresh:
            env["FRESH"] = "1"
        if _spawn_detached(_cell_argv(args.task, args.treatment,
                                      args.condition, rep),
                           env, cid, "spawn") is None:
            print(f"spawned {cid}")


MAX_RESPAWNS = int(os.environ.get("MAX_RESPAWNS", 3))


RESPAWN_BOOK = common.ORCH / "reconcile.respawns.json"


def _respawn_count(cid, bump=False):
    if not bump:
        book = {}
        if RESPAWN_BOOK.exists():
            try:
                book = json.loads(RESPAWN_BOOK.read_text())
            except json.JSONDecodeError:
                book = {}
        return book.get(cid, 0)
    common.ORCH.mkdir(parents=True, exist_ok=True)
    with common.fs_lock(common.ORCH / "respawn-book.lock"):
        book = {}
        if RESPAWN_BOOK.exists():
            try:
                book = json.loads(RESPAWN_BOOK.read_text())
            except json.JSONDecodeError:
                book = {}
        book[cid] = book.get(cid, 0) + 1
        RESPAWN_BOOK.write_text(json.dumps(book))
        return book[cid]


def _respawn(st, dry):
    """Resume a cell: same spawn as `spawn`, NEVER --fresh (attempts persist).

    True only when a loop was actually launched — the caller owns the spec and
    must put it back if it was not.
    """
    cid = st["cid"]
    n = _respawn_count(cid)
    if n >= MAX_RESPAWNS:
        # --dry-run must not WRITE. reconcile.flagged makes every future
        # reconcile skip this cell permanently until a human resumes it, so
        # touching it from a preview silently disabled supervision of a cell
        # the operator only meant to inspect.
        if not dry:
            (common.WS / cid / "reconcile.flagged").touch()
        common._rec_log(f"{cid} FLAGGED: {n} respawns reached — human needed, not touching again"
                 + (" [dry-run: flag NOT written]" if dry else ""))
        return False
    common._rec_log(f"{cid} RESPAWN (resume, #{n + 1})" + (" [dry-run]" if dry else ""))
    if dry:
        return False
    if state.loop_parents().get(cid):
        common._rec_log(f"{cid} respawn skipped — a live loop already owns the workspace")
        return False
    prestart_clean(cid)
    env = dict(os.environ, MODEL=st["model"], CONDITION=st["condition"])
    if _spawn_detached(_cell_argv(st["task"], st["treatment"],
                            st["condition"], st["rep"]), env, cid, "respawn") is not None:
        # Do NOT bump the respawn budget for a launch that never started: a
        # cell refused on a preflight would otherwise walk to MAX_RESPAWNS and
        # be flagged for a human, with the real cause never reported anywhere.
        common._rec_log(f"{cid} respawn FAILED to start — budget not charged")
        return False
    _respawn_count(cid, bump=True)
    return True


def _claimed(cid):
    """Is this cell's spec claimed — i.e. is conduct already responsible for
    restarting it?"""
    d = queue.rundir(cid.split("_", 1)[0])
    return d.is_dir() and (d / f"{cid}.json").exists()


def refresh_cell_creds(model):
    """Master creds -> every live per-cell copy (staged once at cell start)."""
    if not common.CREDS.exists():
        return
    for d in common.WS.glob(f"{model}_*/.agent-claude"):
        (d / ".credentials.json").write_bytes(common.CREDS.read_bytes())


def tail(args):
    logs = sorted((common.WS / args.cell).glob("agent.attempt-*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit("no attempt logs")
    subprocess.run(["tail", *(["-f"] if args.follow else ["-n", "40"]), str(logs[-1])])


# The agent CLIs write stream-json: one JSON object per line, of which ~75% are
# `system` bookkeeping. `tail -f` on that is unreadable, and it also stops at
# the attempt it opened — an attempt boundary rotates to a NEW file, which is
# exactly the moment an operator is watching for.
_TR_SKIP = {"system"}


def _tr_line(d):
    """One transcript event -> the lines a human wants, or nothing."""
    t = d.get("type")
    if t == "assistant":
        out = []
        for c in d.get("message", {}).get("content", []) or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(c["text"].rstrip())
            elif c.get("type") == "tool_use":
                i = c.get("input") or {}
                arg = (i.get("file_path") or i.get("command") or i.get("pattern")
                       or i.get("path") or i.get("description") or "")
                arg = " ".join(str(arg).split())
                out.append(f"  → {c.get('name','?')}: {arg[:150]}")
        return out
    if t == "user":
        for c in d.get("message", {}).get("content", []) or []:
            if c.get("type") == "tool_result" and c.get("is_error"):
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body
                                    if isinstance(b, dict))
                return [f"  ← ERROR: {' '.join(str(body).split())[:150]}"]
        return []
    if t == "rate_limit_event":
        return [f"  ⚠ RATE LIMIT: {' '.join(str(d).split())[:200]}"]
    if t == "result":
        return [f"── result: {d.get('subtype','?')} "
                f"turns={d.get('num_turns','?')} cost={d.get('total_cost_usd','?')}"]
    if t not in _TR_SKIP:
        return [f"  · {t}"]
    return []


def _tr_newest(ws):
    logs = sorted(ws.glob("agent.attempt-*.log"),
                  key=lambda p: (p.stat().st_mtime, p.name))
    # wait-*.log are rotated copies kept as limit evidence, not the live stream
    logs = [l for l in logs if ".wait-" not in l.name]
    return logs[-1] if logs else None


def tail_readable(args):
    """Stream an agent transcript in human form, following attempt rollovers."""
    ws = common.WS / args.cell
    cur = _tr_newest(ws)
    if not cur:
        sys.exit("no attempt logs")
    f = cur.open(errors="replace")
    if args.follow:
        # Start near the end, so a follow does not replay an hour of transcript.
        lines = f.readlines()[-args.lines:]
        print(f"── {cur.name}", flush=True)
        for l in lines:
            _tr_emit(l)
    else:
        for l in f.readlines()[-args.lines:]:
            _tr_emit(l)
        return 0
    try:
        while True:
            l = f.readline()
            if l:
                _tr_emit(l)
                continue
            nxt = _tr_newest(ws)
            if nxt and nxt != cur:
                f.close()
                cur, f = nxt, nxt.open(errors="replace")
                print(f"\n── {cur.name}", flush=True)
                continue
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _tr_emit(line):
    try:
        d = json.loads(line)
    except ValueError:
        return
    for out in _tr_line(d):
        print(out, flush=True)


def log(args):
    """The cell's most recent story: the verify log if one exists, else the
    treatment hooks' log; a bash-era cell still has its run_cell.log."""
    ws = common.WS / args.cell
    for name in ("verify.log", "hooks.log", "run_cell.log"):
        if (ws / name).exists():
            print(f"== {name}", flush=True)
            subprocess.run(["tail", "-n", "60", str(ws / name)])
            return
    print(f"no log under {ws}")