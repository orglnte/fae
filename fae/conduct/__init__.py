"""THE scheduler: the only thing that turns queued specs into cells, and the
only spawner and supervisor at once — every --supervise-interval it runs
the supervision pass (supervise.py, here), so repairs are convergence (a
crashed cell's spec is still claimed in running/, and the next pass restarts
it) rather than a second controller racing the first.

A `Conduct` owns the scheduler's state: conduct.pid in .conduct/ and what the
supervision pass has already reported. The CLI builds one for every operator
verb that acts on the run as a whole (run, pause, resume, stop, diagnose,
repair); only `run` loops. The fleet views (status, watch, monitor) are
fae/driver/render.py's, reading through it.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import ujson as json
from datetime import datetime, timezone
from pathlib import Path

from fae.cell.cell import Cell
from fae import experiment as _experiment
from fae import mutex
from fae.queues import hhmm
from fae import host
from . import records, supervise, zombies

# One live cell per agent, everywhere: admission enforces it and resume defers
# respawns past it (--force overrides).
PER_AGENT_CAP = int(os.environ.get("PER_AGENT_CAP", 1))
# Cell exit codes that mean every cell would fail the same way: 1 the verify
# surface is unguarded, 2 no agent command, 42 HALT[agent] without credentials.
SYSTEMIC_EXITS = frozenset({1, 2, 42})
STANDDOWN_COOL_S = int(os.environ.get("STANDDOWN_COOL_S", 300))
MAX_RESPAWNS = int(os.environ.get("MAX_RESPAWNS", 3))
CONDUCT_LIFTED = ("arm-stuck", "verify-wedged", "silent-hang", "phase-stalled-")


def _default_ws():
    d = _experiment.current().root / "workspaces.nosync"
    return d if d.is_dir() else _experiment.current().root / "workspaces"


def _ws_is_default():
    """Keyed on the operator's OVERRIDE, not on WS itself: the hazard is an
    environment naming another tree, and a test patching WS in-process is
    not that."""
    override = os.environ.get("WORKSPACES_DIR")
    return not override or Path(override).resolve() == _default_ws().resolve()


def _spec_of(st):
    """The queue-spec equivalent of a cell's state — how an interrupted cell
    re-enters the backlog (experiment resume, supervision repair). NEVER fresh:
    attempts persist."""
    return dict(task=st["task"], variant=st["variant"], rep=int(st["rep"]), fresh=False)


def _known_agents():
    """Every agent with a lane (live or parked) or a workspace."""
    known = {_experiment.workspace().queues.lane_agent(d) for d in _experiment.workspace().queues.lane_dirs(include_parked=True)}
    known |= {d.name.split("_", 1)[0] for d in _experiment.workspace().path.iterdir()
              if d.is_dir() and _experiment.parse_cell_id(d.name)}
    return known


def _confirm_stop(blanket, agents, loops, pending, assume_yes):
    """Ask before a hard stop. Returns True to proceed.

    The warning quantifies the blast radius instead of describing it: a scope
    word alone does not tell the operator how many attempts are about to be
    thrown away.
    """
    what = "ALL lanes" if blanket else ", ".join(agents)
    print(f"WARNING: hard stop of {what}.")
    if loops:
        print(f"  * {len(loops)} loop(s) TERMed MID-ATTEMPT — that work is "
              f"discarded: {', '.join(loops[:3])}"
              f"{'...' if len(loops) > 3 else ''}")
    else:
        print("  * no loops are running")
    print("  * agent containers removed; workspaces preserved")
    if blanket:
        print("  * conduct itself is TERMed — nothing starts until "
              "`experiment run`")
    print(f"  * queues are NOT touched: {pending} pending spec(s) stay queued")
    print("  Use `experiment pause` instead for a graceful stop at the next "
          "attempt boundary.")
    if assume_yes:
        print("  Proceed? [y/N] y (--yes)")
        return True
    if not sys.stdin.isatty():
        print("  refusing: not a terminal and --yes was not given")
        return False
    try:
        return input("  Proceed? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


class Conduct:
    """The scheduler and supervisor of one experiment root."""

    def __init__(self):
        self.pidfile = _experiment.workspace().conduct / "conduct.pid"
        self.respawn_book = _experiment.workspace().conduct / "reconcile.respawns.json"
        self.alerts = supervise.Alerts()

    def pid(self):
        """The live scheduler's pid, or None when none is running."""
        try:
            pid = int(self.pidfile.read_text().partition(" ")[0])
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def run_line(self):
        """`run: UP (cap=N)`, `run: DOWN`, or `run: DOWN (stale pidfile)`."""
        if not self.pidfile.exists():
            return "run: DOWN"
        try:
            pid_s, _, cap = self.pidfile.read_text().partition(" ")
            os.kill(int(pid_s), 0)
            return f"run: UP ({cap.strip() or '?'})"
        except (OSError, ValueError):
            return "run: DOWN (stale pidfile)"

    def started_at(self):
        """Epoch of the current run's start (conduct.pid's mtime), or None."""
        try:
            return self.pidfile.stat().st_mtime
        except OSError:
            return None

    # --- what the run sees: read-only, for every reader ----------------------
    # The host's view of the fleet (host.py) and the leftovers it finds
    # (zombies.py) are the run's; readers outside it ask here.

    @staticmethod
    def loop_parents():
        """cid -> the pid of its running cell loop."""
        return host.loop_parents()

    @staticmethod
    def loop_pids():
        return host.loop_pids()

    @staticmethod
    def containers():
        """The names of the running containers."""
        return host.containers()

    @staticmethod
    def agent_containers(boxes):
        """The names in `boxes` that are agent containers."""
        prefix = host.agent_container("")
        return [n for n in boxes if n.startswith(prefix)]

    @staticmethod
    def mem_pressure():
        """The host's memory: the kernel's pressure band, and swap."""
        return host.mem_pressure()

    @staticmethod
    def cell_state(ws, loops=None, boxes=None):
        """The cell in `ws` as status shows it, with what the host knows."""
        return host.cell_state(ws, loops or {}, boxes or set())

    @staticmethod
    def all_states(running_only=False):
        return host.all_states(running_only)

    @staticmethod
    def heartbeat(ws, cell=None):
        """The cell's declared liveness, when its loop is alive."""
        return host.heartbeat(ws, cell)

    @staticmethod
    def queued(cid):
        """Does the cell's spec wait in its lane?"""
        return host.queued(cid)

    @staticmethod
    def find_zombies():
        """Leftovers of dead cells: [(kind, ident, owner, note)]."""
        return zombies.find_zombies()

    @staticmethod
    def janitor_lines():
        return zombies.janitor_lines()

    def act_on_requests(self):
        """The operators' requests to one cell (`cell pause`, `cell stop`),
        acted on here; while a run is up, the run acts on them at its next
        pass instead."""
        pid = self.pid()
        if pid and pid != os.getpid():
            print(f"  the run (pid {pid}) acts on it at its next pass", flush=True)
            return
        qs = _experiment.workspace().queues
        for p, r in qs.requests():
            cid, verb = r.get("cid", ""), r.get("verb")
            if verb == "pause":
                reason = r.get("reason", "manual")
                if _experiment.current().cell(cid).request_pause(reason, r.get("who", "operator")):
                    print(f"  {cid}: paused [{reason}] — its loop stops at its next safe "
                          f"point; workspace preserved")
                else:
                    print(f"  {cid}: not paused (already paused, or done)")
            elif verb == "stop":
                self.stop_cell(cid, cancel=bool(r.get("cancel")))
            qs.request_done(p)

    def stop_cell(self, cid, cancel=False):
        """The run's half of `cell stop`: the cell's queued specs out of the
        backlog (backed up), then the cell halted (Cell.stop)."""
        n = _experiment.workspace().queues.shelve_cell(cid, "cancelled" if cancel else "stopped")
        if n:
            print(f"  {n} spec(s) out of the backlog (restore from .queues/backups/)")
        outcome = _experiment.workspace().named_cell(cid).stop(cancel=cancel)
        if outcome != "absent":
            print(f"  {cid}: loop {outcome}")
        if cancel:
            print(f"  {cid}: cancelled; infra torn down, workspace untouched (un-cancel: "
                  f"rm workspaces.nosync/{cid}/.cancelled + cli.py cell resume {cid})")
        else:
            print(f"  {cid}: stopped — PAUSED·stopped, resumable (cli.py cell resume "
                  f"{cid}); infra torn down, queued specs backed up, workspace untouched")

    # --- respawning: a crashed cell brought back, on a budget ----------------

    def respawn_count(self, cid, bump=False):
        """How many times this cell was repaired; `bump` counts one more."""
        if not bump:
            return self._respawn_book().get(cid, 0)
        _experiment.workspace().conduct.mkdir(parents=True, exist_ok=True)
        with mutex.fs_lock(_experiment.workspace().conduct / "respawn-book.lock"):
            book = self._respawn_book()
            book[cid] = book.get(cid, 0) + 1
            self.respawn_book.write_text(json.dumps(book))
            return book[cid]

    def reset_respawn_budgets(self, cids):
        """Forget each cell's repair count; returns how many had one."""
        _experiment.workspace().conduct.mkdir(parents=True, exist_ok=True)
        with mutex.fs_lock(_experiment.workspace().conduct / "respawn-book.lock"):
            book = self._respawn_book()
            n = sum(book.pop(c, None) is not None for c in cids)
            if n:
                self.respawn_book.write_text(json.dumps(book))
            return n

    def _respawn_book(self):
        try:
            return json.loads(self.respawn_book.read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def is_claimed(cid):
        """Is this cell's spec claimed — is the run already responsible for
        restarting it?"""
        return _experiment.workspace().queues.is_claimed(cid.split("_", 1)[0], cid)

    def refusal_while_up(self, verb, ignore):
        """A manual start runs without slots; while the run admits cells with
        theirs, it would run outside the cap. The refusal, or None."""
        pid = self.pid()
        if pid and not ignore:
            return (f"refusing to {verb}: the run is up (pid {pid}) and admits cells with "
                    f"their slots; a manual start runs without slots, outside the cap. "
                    f"Pass --dangerously-ignore-slots to start it anyway")
        return None

    def respawn(self, st, dry, ignore_slots=False):
        """Resume a cell without slots: the same start as `cell spawn`, never
        fresh (attempts persist), on its repair budget. True only when a loop
        was launched — the caller owns the spec and puts it back otherwise."""
        cid = st["cid"]
        n = self.respawn_count(cid)
        if n >= MAX_RESPAWNS:
            # a dry run must not write: the flag disables supervision of the
            # cell until a human resumes it
            if not dry:
                _experiment.current().cell(cid).flag()
            records.rec_log(f"{cid} FLAGGED: {n} respawns reached — human needed, not "
                            f"touching again" + (" [dry-run: flag NOT written]" if dry else ""))
            return False
        records.rec_log(f"{cid} RESPAWN (resume, #{n + 1})" + (" [dry-run]" if dry else ""))
        if dry:
            return False
        if host.loop_parents().get(cid):
            records.rec_log(f"{cid} respawn skipped — a live loop already owns the workspace")
            return False
        refusal = self.refusal_while_up("respawn", ignore_slots)
        if refusal:
            records.rec_log(f"{cid} {refusal}")
            print(refusal)
            return False
        cell = _experiment.current().cell(cid, st["task"], st["variant"], st["rep"], agent=st["agent"])
        if not cell.ready_image():
            records.rec_log(f"{cid} respawn FAILED: its agent image could not be built")
            return False
        warning = cell.shared_resource_warning()
        if warning:
            print(warning)
        if self.launch(cell, st["agent"], what="respawn") is not None:
            # a launch that never started is not charged: a cell refused at
            # preflight would otherwise walk to MAX_RESPAWNS with its cause unreported
            records.rec_log(f"{cid} respawn FAILED to start — budget not charged")
            return False
        self.respawn_count(cid, bump=True)
        return True

    def diagnose(self, _args):
        """READ-ONLY one-shot: the conduct loop's judgment without waiting for
        (or running) the loop — what supervision would do, what is zombie, what
        admission would do next. Mutates nothing: supervision runs dry, zombies
        are listed not reaped, queue lines are read but never popped."""
        qs = _experiment.workspace().queues
        print("— SUPERVISION (dry run) " + "—" * 36)
        supervise.supervise_pass(self.alerts, dry=True)
        zs = zombies.find_zombies()
        print(f"\n— ZOMBIES ({len(zs)}) — listed only; a live `experiment run` reaps "
              f"on the 2nd consecutive sighting")
        for kind, ident, owner, note in zs:
            print(f"  {kind:<10} {ident}  owner={owner}  {note}")
        live = host.loop_parents()
        up = self.pidfile.exists()
        print(f"\n— ADMISSION PREVIEW — {len(live)} live loop(s), per-agent cap "
              f"{PER_AGENT_CAP}, run {'UP' if up else 'DOWN'}"
              + ("" if up else " (nothing admits until `experiment run`)"))
        for d in qs.lane_dirs(include_parked=True):
            m = qs.lane_agent(d)
            parked = d.name.endswith(".parked")
            paths = qs.specs_in(d)
            claims = qs.running_specs(m)
            _cu = qs.cooldown_until(m)
            if parked:
                note = "parked — no admission until experiment resume"
            elif _cu > time.time():
                note = (f"limit-cooling until "
                        f"{datetime.fromtimestamp(_cu, timezone.utc):%m-%d %H:%M}Z "
                        f"— the run retries then")
            elif len(claims) >= PER_AGENT_CAP:
                held = ", ".join(sorted(qs.spec_cid(p) for p in claims))
                note = f"HELD at {PER_AGENT_CAP}/lane — claimed: {held}"
            else:
                skipped = 0
                for p in paths:
                    cid = qs.spec_cid(p)
                    ws = _experiment.workspace().path / cid
                    if ws.is_dir():
                        if _experiment.current().cell(cid).flagged:
                            skipped += 1
                            continue
                        st = host.cell_state(ws, {}, set())
                        if st and st["state"] == "DONE":
                            skipped += 1
                            continue
                    if _experiment.current().cell(cid).pause_reason or live.get(cid):
                        skipped += 1
                        continue
                    note = f"next: {cid} — would admit"
                    break
                else:
                    note = "nothing admissible"
                if skipped:
                    note += f" ({skipped} flagged/done/paused ahead of it)"
            print(f"  {m:<8}{len(paths):>4} pending{' [PAUSED]' if parked else '':<9} "
                  f"{len(claims)} claimed  {note}")
        for l in zombies.janitor_lines():
            print(l)

    def repair(self, args):
        """One-shot supervision sweep. A live `run` runs the same pass every
        --supervise-interval, so this is never a second controller."""
        supervise.supervise_pass(self.alerts, dry=args.dry_run, only=args.only or "")
        if args.dry_run:
            for kind, ident, owner, note in zombies.find_zombies():
                print(f"would reap {kind:<10} {ident}  owner={owner}  {note}")
            return
        for line in zombies.reap_sweep():
            print(line)

    def run(self, args):
        """THE scheduler — the ONLY thing that turns queued specs into cells.

        Foreground: run it and watch it (the narration IS the monitoring).
        Ctrl-C detaches: live cells keep running (they are setsid-detached) and
        nothing new starts until conduct is run again.

        ONE LIVE CELL PER NON-EMPTY LANE is the invariant. `--per-agent` (1) is
        what enforces it; `-n` is the global ceiling and should equal the lane
        count — conduct warns when it does not, because a lower cap silently
        starves lanes. Lanes admit starved-first, round-robin.

        Admission takes the cell's slots, never waiting: a work slot and, for a
        variant with a lock, a slot of the lock's pool; a lane whose lock pool is
        full is passed over this round. The cell process is handed the open slot
        files and holds them for its whole life.

        A lane whose spawn dies immediately with a systemic code (creds, empty
        AGENT_CMD, seed FATAL) is FROZEN and reported instead of drained into the
        failure; exit 43 (loop lock already held) just skips the spec. A parked
        lane (see `experiment pause M`) is never admitted from and never counts as
        'done' — conduct idles while only parked backlog remains.

        Also the SUPERVISOR: every --supervise-interval it runs supervise_pass
        (hang/crash classification, DONE validation, zombie reap). Repairs are
        convergence, not respawns: a crashed cell's spec is still claimed in
        running/, and the next pass restarts it. One controller, one spawner.
        """
        qs = _experiment.workspace().queues
        n = args.limit
        if not _ws_is_default():
            # The backlog lives in the GLOBAL .queues: a scheduler running against
            # an alternative root would drain the scored queue into it. The test
            # root is spawn-by-hand only.
            print(f"the run refuses: WORKSPACES_DIR={_experiment.workspace().path} is not the scored root "
                  f"({_default_ws()}); the backlog is global and would be drained "
                  f"into the wrong tree. Spawn validation cells by hand.",
                  file=sys.stderr)
            return 2
        _experiment.workspace().conduct.mkdir(parents=True, exist_ok=True)
        pidfile = self.pidfile
        if pidfile.exists():
            try:
                _pid, _, _ = pidfile.read_text().partition(" ")
                os.kill(int(_pid), 0)
                print(f"run: already running (pid {_pid}) — refusing a second "
                      f"instance: two schedulers would each think they own the cap",
                      flush=True)
                return
            except (OSError, ValueError):
                pass                                # stale pidfile: take over
        pidfile.write_text(f"{os.getpid()} cap={n}")
        # Tee the narration to tmp/conduct-<PID>.log: if this instance dies from
        # outside, the per-pid file records what it did and when output stopped.
        _logdir = Path(os.environ.get("CONDUCT_LOG_DIR", _experiment.current().root / "tmp"))
        _logdir.mkdir(parents=True, exist_ok=True)
        _logf = (_logdir / f"conduct-{os.getpid()}.log").open("a")

        class _Tee:
            def __init__(self, *streams): self.streams = streams

            def write(self, s):
                for st in self.streams:
                    st.write(s)
                _logf.flush()

            def flush(self):
                for st in self.streams:
                    st.flush()

        _old_out, _old_err = sys.stdout, sys.stderr
        sys.stdout = _Tee(_old_out, _logf)
        sys.stderr = _Tee(_old_err, _logf)
        print(f"run[{os.getpid()}]: logging to {_logf.name}", flush=True)
        if not self.preflight():
            pidfile.unlink(missing_ok=True)
            sys.stdout, sys.stderr = _old_out, _old_err
            return
        self._adopt_live_cells()
        frozen: set[str] = set()
        rr = 0
        admitted: set[str] = set()      # narrated at admission; skip their START
        prev_states, first = {}, True
        parked_announced = False
        sup_interval = getattr(args, "supervise_interval", 300)
        last_sweep = None               # None = sweep on the FIRST iteration, so
                                        # a conduct starting after an outage
                                        # validates/repairs before admitting
        zombie_seen: set[str] = set()   # 2nd-consecutive-sighting reap (watch's rule)
        poll_i = 0                      # liveness tick cadence
        warned_lanes = None             # re-warn only when the lane count moves
        per_agent_override = getattr(args, "per_agent_override", None) or {}
        override_txt = (f", override {per_agent_override}" if per_agent_override else "")
        print(f"run: global cap {n}, {args.per_agent}/agent{override_txt}, "
              f"round-robin, poll {args.interval}s"
              + (f", supervision every {sup_interval}s" if sup_interval else
                 ", supervision OFF") +
              ". Ctrl-C detaches (cells keep running).", flush=True)
        try:
            while True:
                host.host_sleep_observe()
                self.act_on_requests()
                # Supervision inside the ONE controller: repair requeues at the
                # lane front and the admission below picks it up — conduct is the
                # only spawner. (reconcile --watch is gone; this replaced it.)
                if sup_interval and (last_sweep is None or
                                     time.time() - last_sweep >= sup_interval):
                    last_sweep = time.time()
                    supervise.supervise_pass(self.alerts, dry=False)
                    zs = zombies.find_zombies()
                    ripe = [z for z in zs if z[1] in zombie_seen]
                    for line in (zombies.reap_zombies(ripe) if ripe else []):
                        print(f"  [{hhmm()}] zombie: {line}", flush=True)
                    zombie_seen = {z[1] for z in zs}
                self._converge_running(frozen)
                agents = [qs.lane_agent(d) for d in qs.lane_dirs()]
                pending = {m: len(qs.lane_specs(m)) for m in agents}
                active = [m for m in agents if pending[m]]
                if warned_lanes != len(active) and active and len(active) != n:
                    print(f"  [{hhmm()}] WARNING: global cap {n} != {len(active)} "
                          f"non-empty lane(s) — "
                          + ("lanes will starve" if n < len(active)
                             else "the cap is not what limits admission")
                          + f"; one cell per lane needs -n {len(active)}",
                          flush=True)
                warned_lanes = len(active)
                live = host.loop_parents()
                boxes = host.containers()

                # Narrate state changes: cells STARTing (not admitted by us —
                # reconcile respawns, operator spawns), reaching a verdict,
                # crashing. First pass establishes the baseline silently.
                states, _, _ = host.all_states()
                now = {s["cid"]: f"{s['state']}·{s['why']}" for s in states}
                if first:
                    first = False
                else:
                    for cid, st in sorted(now.items()):
                        was = prev_states.get(cid)
                        if was == st:
                            continue
                        if was is None:
                            if cid not in admitted:
                                print(f"  [{hhmm()}] START    {cid}", flush=True)
                        elif st.startswith("DONE"):
                            extra = ""
                            if st == "DONE·green":
                                try:
                                    L = _experiment.current().cell(cid).read_ledger(
                                        gate_n=_experiment.definition().gate.arity)
                                    extra = (f" (attempt {L.get('green_at', '?')}"
                                             f"/{L.get('att', '?')}, "
                                             f"gate {L.get('gate', 0)}/{L.get('gate_n', 6)})")
                                except OSError:
                                    pass
                            print(f"  [{hhmm()}] {st.split('·')[1].upper():<8} "
                                  f"{cid}{extra}", flush=True)
                        elif st.startswith("CRASHED"):
                            print(f"  [{hhmm()}] CRASHED  {cid} ({st})", flush=True)
                        elif st.startswith("PAUSED"):
                            # only the driver's own stand-downs: an operator pause
                            # flips whole lanes at once and already narrates itself
                            r = _experiment.current().cell(cid).pause_request()
                            if r and r.reason and r.who == "driver":
                                print(f"  [{hhmm()}] STOOD-DOWN {cid} ({st}) — "
                                      f"{r.detail}", flush=True)
                prev_states = now

                if not any(pending.values()) and not live and not qs.running_specs():
                    parked_n = qs.parked_count()
                    if parked_n:
                        if not parked_announced:
                            print(f"  [{hhmm()}] all live lanes empty, {parked_n} "
                                  f"spec(s) parked — idling (experiment resume to "
                                  f"reactivate)", flush=True)
                            parked_announced = True
                        time.sleep(args.interval)
                        continue
                    print(f"  [{hhmm()}] backlog empty, no loops — done.", flush=True)
                    pidfile.unlink(missing_ok=True)
                    return
                parked_announced = False
                # Limit cooldowns: an expired one lifts the lane's limit-wall
                # locks and admission retries; a live one keeps the lane out of
                # the round entirely. If the wall persists, the retried cell
                # walls again and the next sweep re-arms the cooldown.
                now_t = time.time()
                for m in agents:
                    cu = qs.cooldown_until(m)
                    if cu and cu <= now_t:
                        qs.clear_cooldown(m)
                        lifted = 0
                        for c2 in _experiment.workspace().select(m):
                            if _experiment.current().cell(c2).pause_reason == "limit-wall":
                                _experiment.current().cell(c2).unpause(); lifted += 1
                        print(f"  [{hhmm()}] lane {m}: limit cooldown expired — "
                              f"{lifted} lock(s) lifted, retrying", flush=True)
                self._lift_standdowns(agents, now_t)
                qs.weekly_budget_apply(qs.weekly_cap_observe(
                    Cell.all_agent_logs(_experiment.workspace().path), now=now_t), now_t)
                agents = [qs.lane_agent(d) for d in qs.lane_dirs()]   # a hold changes the lanes
                pending = {m: len(qs.lane_specs(m)) for m in agents}
                order = [m for m in agents if pending.get(m) and m not in frozen
                         and qs.cooldown_until(m) <= now_t]
                # Starvation guard: lanes with no live cell admit first, so the lane
                # left out by the cap rotates instead of sticking to one agent.
                lane_live = {m: sum(1 for c in live if c.startswith(m + "_")) for m in order}
                order.sort(key=lambda m: lane_live[m])
                idle_sweep = 0
                while order and idle_sweep < len(order):
                    live = host.loop_parents()
                    if len(live) >= n:
                        break
                    m = order[rr % len(order)]
                    rr += 1
                    if len(qs.running_specs(m)) >= per_agent_override.get(m, args.per_agent):
                        idle_sweep += 1
                        continue
                    p, cid = self._next_admissible(m, boxes)
                    if p is None:
                        idle_sweep += 1
                        continue
                    cell = self._cell(cid, qs.read_spec(p), m)
                    slots, why = self.slots_for(cell)
                    if slots is None:
                        idle_sweep += 1
                        if why == "no-slot:work":
                            break                  # every work slot is held: nothing admits now
                        continue
                    claimed = qs.claim(m, p)          # QUEUED -> RUNNING, one rename
                    spec = qs.read_spec(claimed)
                    rc = self._start(cell, m, slots, fresh=bool(spec.get("fresh")), what="conduct")
                    if rc == "no-prepare":
                        qs.release(m, claimed)
                        idle_sweep += 1
                        continue
                    if rc is None:
                        idle_sweep = 0
                        admitted.add(cid)
                        print(f"  [{hhmm()}] admitted {cid} "
                              f"({len(host.loop_parents())}/{n} live)", flush=True)
                    elif rc in (Cell.LOCK_EXIT, Cell.PAUSE_EXIT):
                        # LOCK_EXIT: workspace already owned; PAUSE: a pause landed
                        # in the claim->spawn window. Cell-specific, not lane-wide:
                        # the claim stands and the next converge decides.
                        idle_sweep += 1
                    elif rc in (Cell.INFRA_EXIT, Cell.CRASH_EXIT):
                        # the infra failed, or the driver crashed outright,
                        # under the driver at admission: keep the claim for
                        # converge, but count it toward the cap
                        self.respawn_count(cid, bump=True)
                        kind = "infra HALT" if rc == Cell.INFRA_EXIT else "crash"
                        print(f"  [{hhmm()}] {kind} at admission of {cid} "
                              f"— counted toward its {MAX_RESPAWNS} repairs", flush=True)
                        idle_sweep += 1
                    elif rc in SYSTEMIC_EXITS:
                        qs.release(m, claimed)
                        frozen.add(m)
                        print(f"  [{hhmm()}] lane {m} FROZEN: spawn died rc={rc} — "
                              f"fix the cause, then restart `experiment run`", flush=True)
                        break                      # order is stale; next round excludes it
                    else:
                        # unknown non-systemic death: hand the spec back to the
                        # head for a later round, don't condemn the lane
                        qs.release(m, claimed)
                        idle_sweep += 1
                        print(f"  [{hhmm()}] {cid} spawn died rc={rc} — spec "
                              f"back at the head, lane NOT frozen", flush=True)
                if frozen and set(m for m in agents if pending.get(m)) <= frozen:
                    print(f"  [{hhmm()}] every pending lane is frozen ({sorted(frozen)}) "
                          f"— exiting", flush=True)
                    pidfile.unlink(missing_ok=True)
                    return
                poll_i += 1
                if poll_i % 10 == 0:      # sign of life on a quiet fleet
                    cool = sorted(m for m in agents
                                  if qs.cooldown_until(m) > time.time())
                    print(f"  [{hhmm()}] alive — {len(host.loop_parents())}/{n} live, "
                          f"{sum(pending.values())} pending"
                          + (f", cooling: {', '.join(cool)}" if cool else "")
                          + f", {qs.weekly_line()}", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pidfile.unlink(missing_ok=True)
            print(f"\nrun: detached — {len(host.loop_parents())} loop(s) keep "
                  f"running; nothing new starts until `cli.py experiment run` runs again.")
        finally:
            sys.stdout, sys.stderr = _old_out, _old_err
            _logf.close()

    def pause(self, args):
        """GRACEFUL bulk pause (the old `drain`); nothing is killed mid-attempt;
        queues are PRESERVED.

        `all` (full drain): stop conduct FIRST — it is the only admission path —
        then pause every non-terminal cell and wait for zero loops (plus
        FP-pinned verifiers). THE maintenance window for editing FP-guarded
        files, whose fingerprint is pinned per loop at start. Release with
        `experiment resume all` and restart conduct deliberately (it does NOT come
        back on its own).

        AGENT names (partial): park those lanes, pause those agents' running
        cells, return immediately; conduct keeps serving the other lanes.
        `--admission-only` parks the lanes and leaves the running cells to
        finish (the old queue-pause). Release with `experiment resume M...`.

        Cells report PAUSED·drain. Same per-cell locks as `cell pause`, same
        cooperative exit — no separate mechanism and no separate state."""
        qs = _experiment.workspace().queues
        scope = list(args.scope)
        blanket = _experiment.is_blanket(scope)
        agents = [] if blanket else scope
        admission_only = getattr(args, "admission_only", False)
        if blanket and admission_only:
            sys.exit("--admission-only is per-lane; the fleet-wide admission stop "
                     "is stopping the run itself (Ctrl-C, or experiment pause all)")
        if agents:
            known = _known_agents()
            bad = [m for m in agents if m not in known]
            if bad:
                sys.exit(f"unknown agent(s): {', '.join(bad)} — experiment pause "
                         f"takes AGENT names or `all` (lanes present: "
                         f"{', '.join(sorted(known)) or 'none'})")
        cids = _experiment.workspace().select(*(agents or ["all"]))
        if args.dry_run:
            parents = {c: p for c, p in host.loop_parents().items() if p > 1}
            if agents:
                parents = {c: p for c, p in parents.items()
                           if c.split("_", 1)[0] in set(agents)}
            for cid in sorted(parents):
                st = host.cell_state(_experiment.workspace().path / cid, host.loop_pids(), host.containers())
                print(f"would pause {cid} ({st['state']}·{st['why']})" if st
                      else f"would pause {cid}")
            if agents:
                for m in agents:
                    if not qs.is_parked(m):
                        print(f"would park queue[{m}]")
                if admission_only:
                    print("would leave the running cells undisturbed (--admission-only)")
                print("would leave conduct running for the other lanes")
            else:
                if self.pidfile.exists():
                    print("would stop conduct (TERM)")
                ws = _experiment.workspace()
                queued = sorted(set(ws.queued_cells("all")) - set(ws.cells()))
                if queued:
                    print(f"would leave {len(queued)} queued spec(s) in place — "
                          f"conduct, the only thing that admits them, is stopped")
            return
        if agents:
            # PARTIAL: park the lanes (stops admission for their backlog) and,
            # unless --admission-only, pause their running cells. Returns
            # immediately — the pause is cooperative and the FP window needs a
            # FULL pause anyway (any live loop pins the fingerprint).
            for m in agents:
                if qs.park_lane(m) == "parked":
                    print(f"  queue[{m}]: parked — conduct stops admitting from it")
            if admission_only:
                print(f"admission stopped for {', '.join(agents)} — running cells "
                      f"finish undisturbed. Release with: experiment resume "
                      f"{' '.join(agents)}")
                return
            self.request_pause(cids, "drain")
            print(f"pause requested [drain] for {len(cids)} cell(s) of "
                  f"{', '.join(agents)} — each loop stops at its next safe point. "
                  f"Release with: experiment resume {' '.join(agents)}")
            return
        # FULL drain. STOP CONDUCT FIRST. Pausing only covers cells that already
        # have a workspace; the scheduler is free to pop a spec that has none, and
        # pause_lock on a nonexistent workspace returns None, so the new cell
        # starts and the window is not a window. On 2026-07-30 a drain leaked five
        # cells this way — the queue went 20 -> 15 while it was "draining" (via
        # the old per-agent workers; conduct inherited the same hazard and drain
        # never stopped it). Conduct is the only thing that starts a cell from
        # the queue, so stopping it is what closes the window. It does NOT come
        # back with `resume all`; restart it deliberately.
        if self.stop_conductor():
            print("stopped conduct — queued specs stay queued; restart conduct "
                  "yourself after the window")
        self.request_pause(cids, "drain")
        print(f"pause requested [drain] for {len(cids)} cell(s) — waiting for loops "
              f"to reach a safe point", flush=True)
        while True:
            parents = {c: p for c, p in host.loop_parents().items() if p > 1}
            # Cell loops are not the only FP-pinned processes: a reverify, an
            # exp1 verify or a smoke run holds a pinned fingerprint too, and
            # "DRAIN COMPLETE" while one runs invited an edit that voided it
            # mid-batch (audit finding 9). Wait for them as well.
            others = [l for l in host.sh(["ps", "-axww", "-o", "pid=,command="]).splitlines()
                      if re.search(zombies.VERIFY_HOLDER_ARGV, l)]
            if not parents and not others:
                break
            if not parents and others:
                print(f"waiting: {len(others)} non-loop FP-pinned process(es) "
                      f"(reverify/exp1/smoke verify)", flush=True)
                time.sleep(args.interval)
                continue
            pids, boxes = host.loop_pids(), host.containers()
            lbl = []
            for c in sorted(parents):
                st = host.cell_state(_experiment.workspace().path / c, pids, boxes)
                lbl.append(f"{c}[{st['why'] or st['state'] if st else '?'}]")
            print(f"waiting: {len(parents)} loop(s) still up: {', '.join(lbl)}", flush=True)
            time.sleep(args.interval)
        for pid in host.loop_pids():   # tees: reap ORPHANS only (ppid 1) — a live
            try:                   # reverify's tee dies with its owner, not here
                ppid = int(host.sh(["ps", "-o", "ppid=", "-p", str(pid)]).strip() or 0)
                if ppid == 1:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        print("DRAIN COMPLETE — no loops left; safe to edit FP-guarded files. "
              "Release the window with: python3 cli.py experiment resume all  "
              "(then restart the scheduler: python3 cli.py experiment run)")

    def resume(self, args):
        """Bulk resume WITHOUT spawning: unpark lanes, lift pause locks, requeue
        interrupted cells at the FRONT of their lane. Only a running conduct
        turns them back into loops, under its caps.

        This is the burst-race fix (2026-08-12): the old `resume all` respawned
        directly, and each spawn raced the stale loop_parents() view of the ones
        before it — ~15 loops started against a 1/agent cap. At bulk scale
        conduct is the only spawner; `cell resume CID` keeps the direct path
        because n=1 cannot burst.

        Blanket (`all`): standing operator decisions survive — roster/manual
        pauses and cancelled cells are skipped, exactly the old `resume all`
        guard (the 2026-07-24 resurrection incident). Naming agents lifts
        roster/manual for those agents."""
        qs = _experiment.workspace().queues
        scope = list(args.scope)
        blanket = _experiment.is_blanket(scope)
        if not blanket:
            known = _known_agents()
            bad = [m for m in scope if m not in known]
            if bad:
                sys.exit(f"unknown agent(s): {', '.join(bad)} — experiment resume "
                         f"takes AGENT names or `all` (lanes present: "
                         f"{', '.join(sorted(known)) or 'none'})")
        lanes = sorted(qs.lane_agent(d) for d in qs.parked_lanes()) if blanket \
            else scope
        for m in lanes:
            r = qs.unpark_lane(m)
            if r == "resumed":
                print(f"  queue[{m}]: unparked — the run admits from it again")
                qs.weekly_hold_clear(m)
            elif r == "conflict":
                print(f"  queue[{m}]: BOTH the live and the parked lane exist — "
                      f"merge by hand, refusing to clobber")
        parents = host.loop_parents()
        pids, boxes = host.loop_pids(), host.containers()   # once — per-cell ps/docker
                                                  # calls made resume-all crawl
        requeued, lifted = 0, 0
        budget_resets = []
        for cid in _experiment.workspace().select(*(["all"] if blanket else scope)):
            ws = _experiment.workspace().path / cid
            st = host.cell_state(ws, pids, boxes)
            if st is None:
                continue
            reason = _experiment.current().cell(cid).pause_reason
            if reason == "killed":
                continue      # cancel is terminal; only `cell resume CID` names it back
            if reason == "contract":
                continue      # the driver's own stand-down: the operator digs, then names the cell
            if reason in ("roster", "manual") and blanket:
                continue      # standing operator decisions survive a blanket resume
            acted = []
            if reason:
                _experiment.current().cell(cid).unpause(); acted.append("pause lifted"); lifted += 1
            if _experiment.current().cell(cid).unflag():
                acted.append("flag cleared")
            # the flag means 'a human must look'; a bulk resume IS that human —
            # budget resets are collected here and written under ONE lock below
            # (a per-cell fs_lock costs its 1s settle 400+ times over a fleet)
            budget_resets.append(cid)
            if st["state"] == "DONE" or parents.get(cid):
                if acted:
                    print(f"  {cid}: {', '.join(acted)} (no requeue — "
                          f"{'done' if st['state'] == 'DONE' else 'loop alive'})")
                continue
            agent = cid.split("_", 1)[0]
            if qs.enqueue(agent, _spec_of(st), front=True) is None:
                acted.append("already queued")
            else:
                acted.append("requeued at FRONT")
                requeued += 1
            if acted:
                print(f"  {cid}: {', '.join(acted)}")
        n_reset = self.reset_respawn_budgets(budget_resets) if budget_resets else 0
        if n_reset:
            print(f"  respawn budgets reset for {n_reset} cell(s)")
        hint = ("a live `experiment run` admits them under its caps"
                if self.pidfile.exists()
                else "the run is DOWN — nothing starts until you run: "
                     "python3 cli.py experiment run")
        print(f"experiment resume: {lifted} lock(s) lifted, {requeued} cell(s) "
              f"requeued at front, NO loops spawned — {hint}")

    def stop(self, args):
        """HARD halt NOW, scoped: TERM loops mid-attempt and remove containers.
        `all` also TERMs conduct. Queues are left alone — a stop halts what is
        RUNNING, and the backlog is not run state. RESUMABLE: cells read
        PAUSED·stopped and come back via experiment resume. The terminal verdict
        lives elsewhere (`cell stop --cancel`). Confirms before acting."""
        qs = _experiment.workspace().queues
        scope = list(args.scope)
        blanket = _experiment.is_blanket(scope)
        agents = None if blanket else scope
        if agents:
            known = _known_agents()
            bad = [m for m in agents if m not in known]
            if bad:
                sys.exit(f"unknown agent(s): {', '.join(bad)} — experiment stop "
                         f"takes AGENT names or `all` (lanes present: "
                         f"{', '.join(sorted(known)) or 'none'})")
        def _in_scope(cid):
            return blanket or cid.split("_", 1)[0] in set(agents)
        _loops_now = sorted(c for c in host.loop_parents() if _in_scope(c))
        _pending = sum(len(qs.specs_in(d)) for d in qs.lane_dirs(include_parked=True)
                       if not agents or qs.lane_agent(d) in set(agents))
        if not _confirm_stop(blanket, agents, _loops_now, _pending,
                             getattr(args, "yes", False)):
            print("aborted — nothing stopped")
            return
        # intent FIRST: without locks the stopped cells read CRASHED and a
        # supervision sweep requeues them into the operator stop within minutes —
        # the same gap stop_cells closes, forgotten here (audit finding 4).
        # Existing locks keep their reasons (request_pause never overwrites).
        self.request_pause([c for c in _experiment.workspace().select("all") if _in_scope(c)], "stopped")
        # Scoped stops leave conduct running: the lane's cells stop and the other
        # lanes keep being served.
        if blanket and self.stop_conductor():
            print("conduct stopped (TERM)")
        loops = {c: p for c, p in host.loop_parents().items() if _in_scope(c)}
        # Through the one teardown path. TERM alone left the dind sidecar, every
        # anonymous volume and the cell's kind cluster behind whenever the EXIT
        # trap did not complete, and the arm slot with them.
        for cid in loops:
            _experiment.workspace().named_cell(cid).take_down("stopped", log=records.rec_log)
        _held = sorted(c for c in _experiment.workspace().select("all")
                       if _in_scope(c) and _experiment.current().cell(c).pause_reason)
        if _held:
            # pause locks are operator decisions, not run state: stopping the fleet
            # must not silently un-pause a roster somebody parked on purpose
            print(f"note: {len(_held)} cell(s) stay paused ({', '.join(_held[:3])}"
                  f"{'...' if len(_held) > 3 else ''}) — release with: "
                  f"cli.py experiment resume {' '.join(agents) if agents else 'all'}")
        print(f"stopped [{'all' if blanket else ', '.join(agents)}]: "
              f"{'conduct stopped, ' if blanket else ''}"
              f"{len(loops)} loop(s) TERMed, containers removed "
              f"(workspaces and queues preserved)")

    @staticmethod
    def request_pause(cids, reason, who="operator"):
        """Pause each cell (Cell.request_pause); the cids it asked."""
        return [cid for cid in cids if _experiment.current().cell(cid).request_pause(reason, who)]

    def stop_conductor(self):
        """TERM a live conduct and clear its pidfile. Returns True if one was
        signalled.

        Shared by experiment pause all and experiment stop all: conduct is the ONLY thing that turns a
        queued spec into a running cell, so anything claiming to have stopped the
        fleet has to stop it. Liveness-checks the pid before signalling — a
        SIGKILLed conduct never reaches its own pidfile.unlink(), so a stale file
        can name a RECYCLED pid, and TERMing that hits an unrelated process.
        """
        pf = self.pidfile
        if not pf.exists():
            return False
        try:
            pid_s, _, _ = pf.read_text().partition(" ")
            pid = int(pid_s)
        except (OSError, ValueError):
            pf.unlink(missing_ok=True)
            return False
        stopped = False
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, PermissionError):
            pass
        pf.unlink(missing_ok=True)
        return stopped

    def preflight(self):
        """Make the infra usable before admitting anything, or say why not.

        Builds the agent image when it is missing — a fresh clone and a reset
        Docker VM look identical from here, and every spawn dies at preflight
        until it exists. Docker itself and the arm tools are NOT installed: that
        is a machine-level change, and it is reported instead.
        """
        ok, why = mutex.fs_enforces_flock(_experiment.workspace().locks)
        if not ok:
            print(f"run: STOP — filesystem locking is not enforced on\n"
                  f"  {_experiment.workspace().locks}\n"
                  f"  detected: {why}\n"
                  f"  Every arm cap, work slot and verify lock in this rig is a "
                  f"flock(2) on a file in that directory. Without enforcement each "
                  f"one succeeds for everyone at once: two access cells provision "
                  f"two infra and the host falls over, silently.\n"
                  f"  Fix: put workspaces.nosync on a local disk. A network mount, "
                  f"a synced folder, or some virtiofs/9p shares are the usual "
                  f"causes.\n"
                  f"  Probe it yourself: python3 fae/mutex.py fscheck {_experiment.workspace().locks}",
                  flush=True)
            return False
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            print("run: STOP — the docker daemon is not reachable. Start "
                  "Docker, then run `experiment run` again.", flush=True)
            return False
        local, fstype = mutex.fs_is_local(_experiment.workspace().path)
        if local is False:
            print(f"run: WARNING — {_experiment.workspace().path} is on {fstype}, not a local disk. Cells and "
                  f"conduct append to the same ledgers; appends interleave safely only on a "
                  f"local disk.", flush=True)
        # the agent image every spawn uses: the base (built when missing, its
        # clients current) and the experiment's layer over it
        from fae.cell.agent_image import AgentImage
        if not AgentImage(_experiment.current().root, _experiment.definition()).ready(
                log=lambda t: print(f"run: {t}", flush=True)):
            print("run: STOP — the agent image could not be built. "
                  "Every spawn would die at preflight.", flush=True)
            return False
        # Every variant's own preflight — its daemon, its images (built here,
        # not under a cell), its tools — and a sweep of its stale infra:
        # what `cli.py experiment infra` shows, run once before admission.
        from fae.driver import check
        print("run: infra preflight", flush=True)
        bad = check.probe_variants()
        if bad:
            print(f"run: NOTE — {bad} variant(s) refused their preflight; cells of "
                  f"those variants will HALT.", flush=True)
        return True

    def admit(self, cell, agent, fresh=False, what="admit", wait=False, poll=5.0):
        """Start `cell` holding its slots: its agent image ready (never while a
        slot is held), its slots taken without waiting — with `wait`, until
        free — then its workspace prepared and its process started with the
        slots handed over. Returns the start's rc (None: alive), or
        "no-image" / "no-slot:<pool>" / "no-prepare" when it did not start."""
        slots, why = self.slots_for(cell, wait=wait, poll=poll)
        if slots is None:
            return why
        return self._start(cell, agent, slots, fresh=fresh, what=what)

    def slots_for(self, cell, wait=False, poll=5.0):
        """(slots, None) with the cell's agent image ready and its slots held,
        or (None, "no-image" / "no-slot:<pool>")."""
        if not cell.ready_image(log=lambda t: print(f"  [{hhmm()}] {t}", flush=True)):
            return None, "no-image"
        while True:
            slots, pool = cell.take_slots()
            if slots is not None:
                return slots, None
            if not wait:
                return None, f"no-slot:{pool}"
            time.sleep(poll)

    def _start(self, cell, agent, slots, fresh=False, what="admit"):
        """Prepare the workspace and start the cell's process with `slots`;
        this process's copies are closed either way. "no-prepare" when the
        workspace could not be prepared (the reason is printed)."""
        try:
            try:
                cell.prepare(fresh=fresh)
            except (OSError, RuntimeError) as e:
                print(f"  [{hhmm()}] {cell.cid} could not be prepared: {e}", flush=True)
                return "no-prepare"
            return self.launch(cell, agent, slots=slots, what=what)
        finally:
            slots.close()

    SPAWN_PROBE_S = 2.0

    def launch(self, cell, agent, slots=None, what="spawn"):
        """Start `cell`'s process as `agent`, the cell readied first
        (Cell.before_start). With `slots`, the new process is handed their
        open files (CELL_SLOT_FDS) and runs holding them; the caller closes
        its own copies. Without, it is asked to run without slots. Returns
        None while it lives after a short probe, else its exit code."""
        rc = cell.before_start(what)
        if rc is not None:
            return rc
        env = dict(os.environ, AGENT=agent)
        env.pop(cell.SLOT_FDS_ENV, None)
        env.pop(cell.IGNORE_SLOTS_ENV, None)
        if slots is not None:
            env[cell.SLOT_FDS_ENV] = slots.handover()
        else:
            env[cell.IGNORE_SLOTS_ENV] = "1"
        return self._spawn(cell.process_argv(), env, cell.cid, what,
                           pass_fds=slots.fds() if slots is not None else ())

    def _spawn(self, argv, env, cid, what="spawn", pass_fds=()):
        """Launch a cell process detached, its stderr kept for its whole life
        in .conduct/cell.<cid>.err, then confirm it did not die on the spot: a
        refusal (sealed, lock held, no credentials, an infra fault) is
        immediate, and is reported rather than taken for a start."""
        _experiment.workspace().conduct.mkdir(parents=True, exist_ok=True)
        # Opened "w": one file per cid, never unlinked, so a crash hours later
        # still has somewhere to land.
        errf = _experiment.workspace().conduct / f"cell.{cid}.err"
        with errf.open("w") as e:
            e.write(f"=== {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} "
                    f"{what} {' '.join(str(a) for a in argv)}\n")
            e.flush()
            p = subprocess.Popen(argv, cwd=_experiment.current().root, env=env, stdout=subprocess.DEVNULL,
                                 stderr=e, start_new_session=True, pass_fds=tuple(pass_fds))
        time.sleep(self.SPAWN_PROBE_S)
        if p.poll() is None:
            return None
        print(f"FAILED to {what} {cid}: the driver exited {p.returncode} immediately")
        try:
            for line in errf.read_text(errors="replace").strip().splitlines()[-6:]:
                print(f"  {line}")
        except OSError:
            pass
        print(f"  (stderr kept at {errf})")
        return p.returncode

    @staticmethod
    def _cell(cid, spec, agent):
        return _experiment.current().cell(cid, spec.get("task", "T1"), spec["variant"], spec["rep"], agent=agent)

    def _next_admissible(self, agent, boxes):
        """The lane's first spec that may start now, with the ones it skipped
        accounted for. Returns (path, cid) or (None, reason).

        Nothing is moved while deciding — a spec only leaves the queue when it is
        claimed, so an interrupted decision costs nothing."""
        qs = _experiment.workspace().queues
        for p in qs.lane_specs(agent):
            cid = qs.spec_cid(p)
            ws = _experiment.workspace().path / cid
            if ws.is_dir():
                # A flagged cell is quarantined from admission too: repair stops
                # bringing it back, and a pending spec would otherwise respawn it
                # right past the flag. The operator's resume clears the flag.
                if _experiment.current().cell(cid).flagged:
                    continue
                st = host.cell_state(ws, {}, boxes)
                if st and st["state"] == "DONE":
                    qs.finish(agent, p)
                    continue
            if _experiment.current().cell(cid).pause_reason or host.loop_parents().get(cid):
                continue
            return p, cid
        return None, "none-admissible"

    def _adopt_live_cells(self):
        """Claim any live cell conduct did not admit itself.

        A cell outliving the conduct that started it (Ctrl-C, restart) has no
        claim, so its lane would read as free and admit a second cell. Adoption
        makes the claim match reality before the first admission."""
        n = 0
        for cid, _pid in host.loop_parents().items():
            if self.is_claimed(cid):
                continue
            st = host.cell_state(_experiment.workspace().path / cid, {}, set())
            if not st:
                continue
            _experiment.workspace().queues.adopt(cid.split("_", 1)[0], cid, _spec_of(st))
            n += 1
        if n:
            print(f"run: adopted {n} live cell(s) started outside this run",
                  flush=True)

    def _lift_standdowns(self, agents, now_t):
        for m in agents:
            for cid in _experiment.workspace().select(m):
                c = _experiment.current().cell(cid)
                r = c.pause_request()
                if not r or not r.reason:
                    continue
                reason, who, at = r.reason, r.who, r.at
                if who != "conduct" or not reason.startswith(CONDUCT_LIFTED):
                    continue
                if c.cancelled or c.flagged:
                    continue
                if at is not None and host.awake_age(at, now_t) < STANDDOWN_COOL_S:
                    continue
                n = self.respawn_count(cid)
                if n >= MAX_RESPAWNS:
                    _experiment.current().cell(cid).flag()
                    print(f"  [{hhmm()}] FLAGGED  {cid}: {n} stand-downs "
                          f"({reason}) — human needed, spec held in the queue "
                          f"until you resume it", flush=True)
                    continue
                _experiment.current().cell(cid).unpause()
                self.respawn_count(cid, bump=True)
                self.alerts.forget(cid)
                print(f"  [{hhmm()}] lifted {cid}: {reason} stand-down "
                      f"(repair {n + 1} of {MAX_RESPAWNS})", flush=True)

    def _converge_running(self, frozen):
        """Make the world match the claimed specs: every file under running/ is a
        lane's cell and must be alive, finished, or handed back.

        This is the whole repair path — a claimed spec sits in running/ until it
        reaches a verdict, so a conduct that dies mid-attempt (or a cell killed by
        a hang sweep) is recovered by the next pass with no journal to replay."""
        qs = _experiment.workspace().queues
        live = host.loop_parents()
        boxes = host.containers()
        for p in qs.running_specs():
            agent, cid = p.parent.name, qs.spec_cid(p)
            if live.get(cid):
                continue
            st = host.cell_state(_experiment.workspace().path / cid, {}, boxes)
            if st and st["state"] == "DONE":
                qs.finish(agent, p)
                continue
            if _experiment.current().cell(cid).pause_reason:
                # operator (or a wall stand-down) owns this cell: hand the spec
                # back so the lane can serve the rest of its backlog
                qs.release(agent, p)
                continue
            if qs.cooldown_until(agent) > time.time():
                continue                      # lane is walled: restarting its cell
                                              # only walls again
            n = self.respawn_count(cid)
            if n >= MAX_RESPAWNS:
                _experiment.current().cell(cid).flag()   # a cell with no workspace keeps no flag: the
                                     # spec going back to the queue is the record
                qs.release(agent, p)
                print(f"  [{hhmm()}] FLAGGED  {cid}: {n} repairs — human needed, "
                      f"spec held in the queue until you resume it", flush=True)
                continue
            try:
                spec = qs.read_spec(p)
            except (OSError, ValueError):
                qs.shelve(p, "unreadable")
                continue
            cell = self._cell(cid, spec, agent)
            rc = self.admit(cell, agent, what="repair")
            if isinstance(rc, str):
                continue                   # no slot or no image yet: next pass
            if rc is None:
                self.respawn_count(cid, bump=True)
                print(f"  [{hhmm()}] repaired {cid} (attempt {n + 1} of "
                      f"{MAX_RESPAWNS})", flush=True)
            elif rc in (Cell.LOCK_EXIT, Cell.PAUSE_EXIT):
                pass                       # owned or paused meanwhile: next pass
            elif rc in (Cell.INFRA_EXIT, Cell.CRASH_EXIT):
                # the infra failed, or the driver crashed outright, under the
                # fresh attempt: a repair that spends budget like any other, so a
                # deterministic host-level fault cannot spin forever uncounted.
                self.respawn_count(cid, bump=True)
                kind = "infra HALT" if rc == Cell.INFRA_EXIT else "crash"
                print(f"  [{hhmm()}] {kind} on repair of {cid} "
                      f"(repair {n + 1} of {MAX_RESPAWNS})", flush=True)
            elif rc in SYSTEMIC_EXITS:
                frozen.add(agent)
                print(f"  [{hhmm()}] lane {agent} FROZEN: repair spawn died "
                      f"rc={rc} — fix the cause, then restart `experiment run`", flush=True)
