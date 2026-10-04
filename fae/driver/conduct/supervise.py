"""The one automatic controller: judges every cell by MECHANISM (process
alive? output growing? log advancing?) and repairs, requeues, or hands it
back to the operator. Runs inside conduct's loop every --supervise-interval,
and as the one-shot `reconcile` / `experiment diagnose` (dry).

A cell dies by conduct's hand one way, its own (Cell.take_down).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import ujson as json
from datetime import datetime, timezone

from fae.cell.cell import Busy, Cell
from fae.cell.fsm import LOOP_CLEARED_BY, WAIT_PHASES
from fae import experiment as _experiment
from fae.cell import faults
from fae.driver import validate as taint

from . import host, records, zombies

# A lane that hits a provider quota/rate wall cools for the provider's
# parsed reset hint, else this long, rather than retrying a multi-hour cap
# in-cell.
COOLDOWN_DEFAULT_S = int(os.environ.get("LIMIT_COOLDOWN_S", 3 * 3600))


def _is_quota_wall(text):
    """True only for provider QUOTA/RATE messages. The harness's `limit`
    phase also covers transient connection faults (refused, closed
    mid-response, timeouts), which its own retry loop heals in minutes —
    cooling a lane hours for those idles a healthy agent.

    `hit your (usage|session) limit` plus a bare `resets \\d` (clock-time
    form, "resets 7:40pm (UTC)") cover the Claude Code CLI's own wording,
    which matches none of the other branches: it says "hit your ... limit",
    not "limit reached/exceeded", and "resets 7:40pm" has no "in"/"at" for
    the relative-duration branch.
    """
    return faults.quota_wall(text)


def _parse_reset_hint(text):
    """Seconds until a provider limit resets, parsed from its error message;
    None when the message carries no usable hint. Day-scale hints ("Resets
    in 3 days") come from weekly caps; a clock-time hint ("resets 7:40pm
    (UTC)") is always UTC and a future point today or tomorrow."""
    return faults.reset_hint_s(text, now=datetime.now(timezone.utc))


def _set_cooldown(agent, detail):
    until = time.time() + (_parse_reset_hint(detail) or COOLDOWN_DEFAULT_S)
    return _experiment.workspace().queues.set_cooldown(agent, until, detail[:Cell.WAIT_REASON_MAX])

def _arm_slot_of(arm, cid):
    """Seconds this cid has held a slot of `arm`, or None if it holds none.
    The note's timestamp is when the slot was taken; the kernel says whether
    it is still held — the note alone would age a slot released long ago."""
    if not arm:
        return None
    q = _experiment.workspace().queues
    for slot in q.slot_files(arm):
        note = q.slot_note(slot)
        if not note or note[0] != cid or not q.slot_held(slot):
            continue
        return records.awake_age(note[2]) if note[2] is not None else None
    return None


def _retire_finished_specs(dry=False):
    """Move a queued spec whose cell is already terminal to done/.

    Admission retires these too, but only for a lane it is about to admit
    from — a lane at its per-agent cap is never scanned, so a finished cell's
    spec sits at the head counting as backlog and naming itself as `next:`.
    A cell resumed by hand leaves exactly that: the loop runs and finishes
    while the spec it came from is still in the lane, unclaimed.
    """
    qs = _experiment.workspace().queues
    boxes = host.containers()
    for d in qs.lane_dirs():
        agent = qs.lane_agent(d)
        for p in qs.specs_in(d):
            cid = qs.spec_cid(p)
            if not (_experiment.workspace().path / cid).is_dir():
                continue
            st = host.cell_state(_experiment.workspace().path / cid, {}, boxes)
            if not st or st["state"] != "DONE":
                continue
            records.rec_log(f"{cid} spec retired — cell is DONE·{st['why'] or '?'}"
                     + (" [dry-run]" if dry else ""))
            if dry:
                continue
            # Through the claim, so done/ holds one filename shape whether the
            # spec got there via admission or from the queue.
            try:
                qs.finish(agent, qs.claim(agent, p))
            except FileExistsError:
                qs.shelve(p, "done-duplicate")


T_HANG = int(os.environ.get("T_HANG", 600))     # no output this long = wedged


T_STALL = int(os.environ.get("T_STALL", 1500))  # long verify tolerance


VERIFY_HELD_ALERT_S = int(os.environ.get("VERIFY_HELD_ALERT_S", 300))




VERIFY_WEDGED_S = int(os.environ.get("VERIFY_WEDGED_S", 14400))




ARM_HELD_ALERT_S = int(os.environ.get("ARM_HELD_ALERT_S", 10800))


ARM_STALL_S = int(os.environ.get("ARM_STALL_S", 1800))


PHASE_LIMITS = {
    "setup":       (int(os.environ.get("PHASE_SETUP_ALERT_S", 900)),
                    int(os.environ.get("PHASE_SETUP_KILL_S", 2700))),
    # A single agent call runs past 90 minutes on the slower agents, so this
    # sits well above the observed normal rather than at it.
    "agent":       (int(os.environ.get("PHASE_AGENT_ALERT_S", 10800)), None),
    "verify-lock": (int(os.environ.get("PHASE_WAIT_ALERT_S", 3600)), None),
}


_SIZE_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
               "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _bytes(s):
    """'59.8MB' -> 59800000.0. None when docker prints something else."""
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", s.strip())
    if not m:
        return None
    return float(m.group(1)) * _SIZE_UNITS.get(m.group(2).lower(), 0)


def _agent_io(boxes=()):
    """cid -> (rx_bytes, cpu_pct) for every live agent container, one call.

    `boxes` short-circuits the shell-out: with no agent container running there
    is nothing to sample, and `docker stats` costs seconds even to say so.

    Received bytes is cumulative, so a delta across sweeps proves the API is
    still delivering tokens. CPU% is a snapshot: a working agent reads 0.00%
    whenever it is blocked on the socket, so it can corroborate liveness but
    never refute it.
    """
    out = {}
    pfx = host.agent_container("")
    if not any(str(b).startswith(pfx) for b in boxes):
        return out
    try:
        r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                            "{{.Name}}\t{{.NetIO}}\t{{.CPUPerc}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 3 or not f[0].startswith(pfx):
            continue
        rx = _bytes(f[1].split("/")[0])
        try:
            cpu = float(f[2].rstrip("% "))
        except ValueError:
            cpu = 0.0
        if rx is not None:
            out[f[0][len(pfx):]] = (rx, cpu)
    return out


def _agent_io_book(write=None):
    book = _experiment.workspace().queues.agent_io_book()
    if write is not None:
        return book.save(write)
    return book.load()


def _agent_progressing(cid, io_now, book):
    """Is the AGENT (not the loop) still doing something?

    (verdict, note). True = moving, False = flat since the last sweep,
    None = no reading, which means unknown and never dead.
    """
    if cid not in io_now:
        return None, "no container reading"
    rx, cpu = io_now[cid]
    prev = book.get(cid, {})
    if cpu > 0.5:
        return True, f"cpu={cpu:.1f}%"
    if "rx" not in prev:
        return None, "first reading"
    if rx > prev["rx"]:
        return True, f"rx +{int(rx - prev['rx'])}B"
    return False, f"rx flat at {int(rx)}B"


def _attempt_out(ws):
    """(size, age_s) of the newest agent.attempt-*.log, or (None, None)."""
    logs = sorted(ws.glob("agent.attempt-*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None, None
    stt = logs[-1].stat()
    return stt.st_size, records.awake_age(stt.st_mtime)


AGENT_DEAD_GRACE = int(os.environ.get("AGENT_DEAD_GRACE", 300))


_VERIFY_MARK_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\t(?:VERIFY_READY|SHAPE)\t")


def _verify_progress(ws, since_ts):
    """(stamp, age) of the newest VERIFY_READY/SHAPE mark the current verify
    has written, or (None, None) when it has written none yet."""
    newest = None
    for line in _experiment.current().cell(ws.name, workspaces=ws.parent).ledger_text().splitlines():
        m = _VERIFY_MARK_RE.match(line)
        if m and (since_ts is None or m.group(1) >= since_ts):
            newest = m.group(1)
    if newest is None:
        return None, None
    return newest, _age_of(newest)


def _age_of(ts):
    """Seconds since an ISO-Z stamp (host sleep excluded), or None if it does
    not parse."""
    try:
        return records.awake_age(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def _reconcile_dead_loop(cid, loop_pid, in_box, out_age, last, dry,
                         terminal=False):
    """Emit the Crash a killed loop could not emit itself.

    Runs for EVERY cell, including paused ones the rest of supervision leaves
    alone: this writes a transition and touches nothing else. Without it a
    cell killed while paused keeps a live loop in the agent, its next
    legitimate Admit replays as illegal, and every later event for it cascades
    — the conformance check goes deaf on exactly the cell that broke.

    Signals must agree before declaring the loop gone, because a loop re-execs
    (the FP re-pin) and is briefly pidless, and a freshly spawned one has not
    reached `ps` yet: no process, no container, an agent log untouched for
    AGENT_DEAD_GRACE, and a last transition at least that old. The transition
    age is what protects a cell that is starting right now — it has no agent
    log at all, so the log canary alone would call it dead.
    """
    last_action, last_ts = last if last else (None, None)
    if loop_pid or in_box or terminal:
        return False       # a verdict ends the loop in the agent too
    if last_action in LOOP_CLEARED_BY or last_action is None:
        return False
    if out_age is not None and out_age < AGENT_DEAD_GRACE:
        return False
    last_age = _age_of(last_ts)
    if last_age is not None and last_age < AGENT_DEAD_GRACE:
        return False
    records.rec_log(f"{cid} loop gone without a Crash event (last={last_action} "
             f"{int(last_age) if last_age is not None else -1}s ago, "
             f"out_age={int(out_age) if out_age is not None else -1}s) -> "
             f"reconciling the agent" + (" [dry-run]" if dry else ""))
    if not dry:
        _experiment.current().cell(cid).crashed("loop-vanished")
    return True


def _conducts(cid):
    """Is this cell conduct's to restart: claimed (converge restarts it) or
    its spec waiting in its lane (admission restarts it)?"""
    if _experiment.workspace().queues.is_claimed(cid.split("_", 1)[0], cid):
        return True
    parsed = _experiment.parse_cell_id(cid)
    return bool(parsed and _experiment.workspace().queues.lane_has(parsed[0], cid))


def _reclaim(st, dry):
    """Report who owns the recovery of a cell supervision just classified.

    Supervision NEVER creates a claim: the claimed set is exactly conduct's
    responsibility, and inventing claims for old corpses would restart cells
    the operator never asked for — every crashed workspace on disk, ignoring
    caps and lane cooldowns. An unclaimed cell is the operator's: it shows in
    TRIAGE until `cell resume` puts a spec back in its lane; one whose spec
    already waits in its lane is conduct's again, and says nothing."""
    cid = st["cid"]
    if _conducts(cid):
        return
    records.rec_log(f"{cid} unclaimed — resume it to put its spec back in the lane")


class Alerts:
    """What the supervision pass has already reported, so it reports each
    thing once. Owned by the Conduct that runs the pass."""

    def __init__(self):
        self.verify_slow = {}     # cid -> AcquireVerify ts already alerted on
        self.validation = {}      # cid -> the error last reported for it
        self.verify_wedged = {}   # cid -> AcquireVerify ts already acted on
        self.memory = {"level": 1}   # last pressure band logged, for rising-edge dedup
        self.arm = set()          # cids alerted for holding their arm too long
        self.phase = set()        # (cid, phase, attempt) already reported

    def forget(self, cid):
        """A lifted stand-down starts the cell's alerts afresh."""
        self.arm.discard(cid)
        self.phase = {k for k in self.phase if k[0] != cid}


def supervise_pass(alerts, dry=False, only=""):
    """One supervision sweep: judge every cell by MECHANISM (process alive?
    output growing? log advancing?) and act: ORPHAN loop -> kill; SILENT
    HANG -> kill container + loop, requeue; DEAD loop -> requeue; bounded by
    MAX_RESPAWNS then human-flag; DONE cells get mandatory validation.
    Never touches workspace data. Runs inside conduct's loop every
    --supervise-interval, and as the one-shot `reconcile` /
    `experiment diagnose` (dry)."""
    records.host_sleep_observe()
    _mp = host.mem_pressure()
    if _mp["level"] >= 2 and _mp["level"] != alerts.memory["level"]:
        records.rec_log(f"MEMORY PRESSURE {_mp['label']} — "
                 f"{_mp['used_gb']:.1f}/{_mp['total_gb']:.1f}GB used "
                 f"({_mp['avail_pct']}% avail); a heavy cell fleet risks an OOM "
                 f"kill on this host")
    elif _mp["level"] < 2 and alerts.memory["level"] >= 2:
        records.rec_log("memory pressure back to normal")
    alerts.memory["level"] = _mp["level"]
    loops, boxes = host.loop_pids(), host.containers()
    parents = host.loop_parents()
    last_tr = host.last_transitions()
    io_now = _agent_io(boxes)
    io_book = _agent_io_book()
    io_flat = {c: v.get("flat", 0) for c, v in io_book.items()}
    for ws in sorted(_experiment.workspace().path.iterdir()):
            if not ws.is_dir():
                continue
            st = host.cell_state(ws, loops, boxes)
            if not st:
                continue
            cid = st["cid"]
            if only and only not in cid:
                continue
            # Before every hands-off guard: a dead loop is a fact about the
            # world, and the agent has to learn it whatever the operator
            # intends for the cell. Writes one transition, nothing else.
            _reconcile_dead_loop(cid, parents.get(cid),
                                 host.agent_container(cid) in boxes,
                                 _attempt_out(ws)[1], last_tr.get(cid), dry,
                                 terminal=st["state"] == "DONE")
            c = _experiment.current().cell(ws.name, workspaces=ws.parent)
            if c.flagged:
                continue                      # human-flagged: hands off
            if c.declared_intent()[0] != "run":
                continue      # operator declared cancel/pause/drain — hands off
            terminal = st["state"] == "DONE"
            if terminal and st["why"] != "cancelled" \
                    and c.read_derived("validation.json") is None:
                # one cell's evidence must never stop supervision of the fleet:
                # the cell stays unvalidated, is reported once, and is retried
                try:
                    doc = taint._validate_cell(ws)
                except Busy:
                    pass                      # its loop is ending: next pass
                except Exception as e:
                    if alerts.validation.get(cid) != repr(e):
                        alerts.validation[cid] = repr(e)
                        records.rec_log(f"{cid} ALERT — validation FAILED, left unvalidated "
                                        f"and retried every pass: {type(e).__name__}: {e}")
                else:
                    alerts.validation.pop(cid, None)
                    records.rec_log(f"{cid} validated: {doc['verdict']}"
                             + (f" ({'; '.join(doc['taints'])})" if doc["taints"] else ""))
            loop_pid = parents.get(cid)
            in_box = host.agent_container(cid) in boxes
            out_size, out_age = _attempt_out(ws)
            iter_age = records.awake_age(c.mtimes()["ledger"] / 1e9)
            # A cell still inside verify_lock_acquire (last transition
            # AcquireVerify, no later one yet) that has held it past the
            # threshold: write an ALERT to the cell's own ledger, same shape
            # as HOST-OVERLOADED, so it surfaces in experiment status TRIAGE like
            # any other alert — every other lane may be queued behind this
            # one global lock. Once per episode (dedup keyed on the
            # AcquireVerify ts itself, so a fresh verify re-alerts).
            if not terminal:
                _act, _ts = last_tr.get(cid, (None, None))
                if _act == "AcquireVerify":
                    _held = _age_of(_ts)
                    # Slow means NOT PROGRESSING. A green runs all six
                    # arrangements under the one lock (~15 min), each leaving
                    # a VERIFY_READY/SHAPE mark; the age of the newest mark is
                    # what a stuck arrangement inflates, the total hold is not.
                    _mark_ts, _quiet = _verify_progress(ws, _ts)
                    _slow = _held if _quiet is None else _quiet
                    _key = (_ts, _mark_ts)
                    if _slow is not None and _slow > VERIFY_HELD_ALERT_S \
                            and alerts.verify_slow.get(cid) != _key:
                        records.rec_log(f"{cid} ALERT — verify quiet {int(_slow)}s > "
                                 f"{VERIFY_HELD_ALERT_S}s (lock held {int(_held or 0)}s)"
                                 + (" [dry-run]" if dry else ""))
                        if not dry:
                            _experiment.current().cell(cid).alert(f"VERIFY-SLOW no progress for {int(_slow)}s > {VERIFY_HELD_ALERT_S}s (global verify-lock held {int(_held or 0)}s) — every other lane queues behind it")
                            alerts.verify_slow[cid] = _key
                    # Past the wedge threshold, end it. The alert above is
                    # report-only, and a global lock nobody frees stalls every
                    # lane for as long as it takes an operator to notice.
                    if _held is not None and _held > VERIFY_WEDGED_S \
                            and alerts.verify_wedged.get(cid) != _ts:
                        records.rec_log(f"{cid} ALERT — verify wedged {int(_held)}s > "
                                 f"{VERIFY_WEDGED_S}s -> stand down"
                                 + (" [dry-run]" if dry else ""))
                        if not dry:
                            _experiment.current().cell(cid).alert(f"VERIFY-WEDGED held the global verify-lock {int(_held)}s > {VERIFY_WEDGED_S}s — standing it down; this attempt is lost")
                            alerts.verify_wedged[cid] = _ts
                            _experiment.workspace().named_cell(cid, st["variant"]).take_down(
                                "verify-wedged", unblock_agent=True, log=records.rec_log)
                            _reclaim(st, dry)
                        continue
            # A phase that has stood still too long. phase_age is the only
            # progress signal: the heartbeat says the ticker lives, not that
            # the cell is getting anywhere.
            if not terminal and st["state"] in ("RUNNING", "WAITING"):
                _hbp = host.heartbeat(ws)
                _ph = (_hbp or {}).get("phase")
                _pa = (_hbp or {}).get("phase_age")
                _lim = PHASE_LIMITS.get(_ph)
                if _lim and _pa is not None:
                    _alert_s, _kill_s = _lim
                    _key = (cid, _ph, str(_hbp.get("attempt", "")))
                    if _pa > _alert_s and _key not in alerts.phase:
                        # a wait phase is someone else's time: long, not stalled
                        _kind = "WAIT-LONG" if _ph in WAIT_PHASES else "PHASE-STALLED"
                        records.rec_log(f"{cid} ALERT — {_kind} '{_ph}' unchanged "
                                 f"{int(_pa)}s > {_alert_s}s"
                                 + (" [dry-run]" if dry else ""))
                        if not dry:
                            _experiment.current().cell(cid).alert(f"{_kind} '{_ph}' unchanged {int(_pa)}s > {_alert_s}s")
                            alerts.phase.add(_key)
                    if _kill_s is not None and _pa > _kill_s:
                        records.rec_log(f"{cid} ALERT — phase '{_ph}' unchanged "
                                 f"{int(_pa)}s > {_kill_s}s -> stand down"
                                 + (" [dry-run]" if dry else ""))
                        if not dry:
                            _experiment.workspace().named_cell(cid, st["variant"]).take_down(
                                f"phase-stalled-{_ph}", unblock_agent=True,
                                log=records.rec_log)
                            _reclaim(st, dry)
                            continue
            # A wedged arm-slot holder: overaged AND its heartbeat has stopped.
            # Ending the process is the only way a flock is released, so this
            # goes through the same teardown path as any other induced death.
            if not terminal and st["state"] in ("RUNNING", "WAITING"):
                _arm = _experiment.definition().lock_of(st["variant"])
                _slot = _arm_slot_of(_arm, cid) if _arm else None
                if _slot is not None and cid not in alerts.arm:
                    # Stalled means NOT PROGRESSING, which is phase_age. The
                    # heartbeat's own age only says the ticker is alive: the
                    # ticker rewrites .loop every HB_TICK for as long as the
                    # loop lives, so a wedged cell reads as freshly beating.
                    #
                    # `agent` is exempt. A single agent call runs for the best
                    # part of an hour without any phase change, so an unchanged
                    # phase there is the normal shape of work, not a stall.
                    # The wait phases too: a cell queued on the verify lock or
                    # a provider wall is waiting on someone else, not wedged —
                    # the lock holder is VERIFY-WEDGED's, the wall the wall
                    # branch's.
                    _hb = host.heartbeat(ws)
                    _phase = (_hb or {}).get("phase")
                    _silent = (_hb.get("phase_age") if _hb else None)
                    if _silent is None:
                        _silent = ARM_STALL_S + 1
                    if _phase == "agent" or _phase in WAIT_PHASES:
                        _silent = 0
                    if _slot > ARM_HELD_ALERT_S and _silent > ARM_STALL_S:
                        records.rec_log(f"{cid} ALERT — arm '{_arm}' slot held "
                                 f"{int(_slot)}s and phase unchanged "
                                 f"{int(_silent)}s -> stand down"
                                 + (" [dry-run]" if dry else ""))
                        if not dry:
                            _experiment.current().cell(cid).alert(f"ARM-STUCK held the {_arm} arm {int(_slot)}s with no progress for {int(_silent)}s — standing it down")
                            alerts.arm.add(cid)
                            _experiment.workspace().named_cell(cid, st["variant"]).take_down(
                                "arm-stuck", unblock_agent=True, log=records.rec_log)
                            _reclaim(st, dry)
                        continue
            # stranded reverify: an in-flight gate (shape 0/6..5/6 — 6/6 is a
            # COMPLETED gate) whose reverify process is gone leaves the cell
            # Repair the ledger with an explicit rig-abort line: green stays
            # intact and the cell rejoins the reverification population.
            _L = c.read_ledger()
            if terminal and _L["reverify_active"] \
                    and iter_age > T_HANG \
                    and not any(re.search(zombies.VERIFY_HOLDER_ARGV, l)
                                for l in host.sh(["ps", "-axww", "-o", "command="]).splitlines()):
                records.rec_log(f"{cid} stranded reverify ({st['shape']}) -> ledger repair"
                         + (" [dry-run]" if dry else ""))
                if not dry:
                    try:
                        _experiment.current().cell(cid).repair_stranded_reverify()
                    except Busy:
                        records.rec_log(f"{cid} stranded reverify: cell held, repair next pass")
            if terminal and loop_pid:
                # A loop alive on a DONE cell is normal for the teardown
                # window: END is written BEFORE the EXIT trap deletes the
                # per-cell cluster/sidecar (minutes for a cluster). Killing it
                # there orphans exactly the infra the trap was removing
                # (audit finding 14) — so give grace, and when we do kill,
                # tear down explicitly like stop_cells.
                if iter_age < T_HANG:
                    continue
                records.rec_log(f"{cid} ORPHAN loop pid={loop_pid} (terminal "
                         f"{st['state']}, {int(iter_age)}s past END) -> kill + teardown"
                         + (" [dry-run]" if dry else ""))
                if not dry:
                    os.kill(loop_pid, signal.SIGKILL)
                    subprocess.run(["docker", "rm", "-f", "-v", host.agent_container(cid),
                                    *host.infra_containers(st["variant"], cid)],
                                   capture_output=True)
                    _experiment.workspace().named_cell(cid, st["variant"]).teardown()
            elif st["state"] == "CRASHED" and not in_box and _conducts(cid):
                continue    # claimed or queued: conduct restarts it, and
                            # says so when it does
            elif st["state"] == "CRASHED" and st["why"] == "infra" and not in_box:
                # a rig fault is RESUMABLE, not terminal: it burns no attempt,
                # so the cell is respawned like any other crash (2026-07-24
                # blind spot: a provision-race HALT sat unrespawned until a
                # human noticed). No corpse-age guard anymore: a deliberate
                # stop is a pause LOCK now, never an age to be guessed at.
                records.rec_log(f"{cid} CRASHED/infra, no loop (resumable)")
                # Same reason as the CRASHED branch below: this runs every
                # sweep while the cell stays crashed, so the reconcile at the
                # top of the sweep is the one place that may emit the Crash.
                _reclaim(st, dry)
            elif terminal:
                continue                      # done and quiet — the good end
            elif st["state"] == "WAITING" and st["why"] == "limit" \
                    and not _is_quota_wall(st.get("detail") or ""):
                continue    # transient API fault — the loop's own retry
                            # heals it; a lane cooldown here idles a healthy
                            # agent for hours (observed: ConnectionRefused)
            elif st["state"] == "WAITING" and st["why"] == "limit":
                # A quota-walled cell burns nothing but HOLDS its arm lock and
                # work slot, and every cell of that arm queues behind it for
                # as long as the wall lasts. Stand it down cooperatively
                # (the retry sleep is a pause safe point; the EXIT trap frees
                # the locks), requeue it, and cool the lane until the
                # provider's reset hint — or 3h when the message has none.
                # MUST sit above the silent-hang branch: wall retries keep the
                # heartbeat fresh, so its veto `continue` would swallow
                # exactly these cells.
                agent = cid.split("_", 1)[0]
                detail = st.get("detail") or ""
                records.rec_log(f"{cid} LIMIT WALL ({detail[:60]}) -> stand down, "
                         f"cool lane {agent}"
                         + (" [dry-run]" if dry else ""))
                if not dry:
                    _experiment.current().cell(cid).request_pause("limit-wall", who="conduct")
                    _reclaim(st, dry)
                    until = _set_cooldown(agent, detail)
                    records.rec_log(f"lane {agent}: cooling until "
                             f"{datetime.fromtimestamp(until, timezone.utc):%H:%M}Z")
            elif in_box and (out_size in (None, 0) or (out_age or 0) > T_HANG) \
                    and iter_age > T_HANG:
                # A wall wearing a hang's face: some CLIs log a usage-limit
                # error and then retry INSIDE the process — no output growth,
                # no exit. The log NAMES the cause, and named evidence is
                # decided on immediately; the progress checks below are
                # inference and wait for a second opinion.
                _logs = sorted(ws.glob("agent.attempt-*.log"),
                               key=lambda p: p.stat().st_mtime)
                _tail = _logs[-1].read_text(errors="replace")[-3000:] \
                    if _logs else ""
                _line = next((l for l in _tail.splitlines()
                              if _is_quota_wall(l) and "error" in l.lower()),
                             None)
                if _line is not None:
                    agent = cid.split("_", 1)[0]
                    records.rec_log(f"{cid} LIMIT WALL inside a stuck agent "
                             f"({_line.strip()[-90:]}) -> kill agent, cool "
                             f"lane {agent}" + (" [dry-run]" if dry else ""))
                    if not dry:
                        # Tear down through the one path: a bare SIGKILL frees
                        # the arm slot in the kernel while this cell's cluster
                        # is still up, and the next acquirer would provision
                        # against it.
                        _outcome = _experiment.workspace().named_cell(cid, st["variant"]).take_down(
                            "limit-wall", unblock_agent=True, log=records.rec_log)
                        if _outcome in ("termed", "killed"):
                            c.crashed("limit-wall")
                        _reclaim(st, dry)
                        until = _set_cooldown(agent, _line)
                        records.rec_log(f"lane {agent}: cooling until "
                                 f"{datetime.fromtimestamp(until, timezone.utc):%m-%d %H:%M}Z")
                    continue
                # out_size==0 means the agent's first turn has not returned
                # yet: nothing has been written, so there is nothing that
                # could have grown. Silence is not evidence of death, so ask
                # the AGENT whether it is moving.
                moving, why = _agent_progressing(cid, io_now, io_book)
                if moving:
                    io_flat.pop(cid, None)
                    continue                  # the API is still delivering
                if moving is None and out_size in (None, 0):
                    continue      # no reading and nothing written: unknown,
                                  # which is not a reason to kill
                flat = io_flat.get(cid, 0) + 1
                io_flat[cid] = flat
                if flat < 2:
                    records.rec_log(f"{cid} no agent progress ({why}) — 1st sighting, "
                             f"deciding on the next sweep")
                    continue                  # two flat sweeps before acting
                records.rec_log(f"{cid} SILENT HANG ({why}, out_size={out_size} "
                         f"out_age={int(out_age or -1)}s iter_age={int(iter_age)}s)"
                         + (" [dry-run]" if dry else ""))
                if not dry:
                    _outcome = _experiment.workspace().named_cell(cid, st["variant"]).take_down(
                        "silent-hang", unblock_agent=True, log=records.rec_log)
                    if _outcome in ("termed", "killed"):
                        c.crashed("silent-hang")
                _reclaim(st, dry)
            elif st["state"] == "CRASHED" and st["why"] == "agent":
                records.rec_log(f"{cid} CRASHED/agent ({st['detail'][:40]}) — needs a "
                         f"human (creds/agent fault), not a respawn")
            elif st["state"] == "CRASHED" and Cell.never_started(st):
                continue      # prepared, never launched — nothing to resume
            elif st["state"] == "CRASHED" and not loop_pid and not in_box:
                # loop died mid-cell (crash/limit-kill); container gone too.
                # The old T_ABANDON corpse-age guard ("old = stopped on
                # purpose") is retired: deliberate stops are declared pause
                # locks now, and guessing intent from age is how a suspended
                # roster got auto-respawned. A pre-migration corpse is the one
                # remaining case — those sit behind reconcile.flagged or get
                # respawned once and either finish or crash into MAX_RESPAWNS.
                records.rec_log(f"{cid} CRASHED/{st['why']} (state={st['state']} "
                         f"iter_age={int(iter_age)}s)")
                # No Crash is emitted here. This branch runs on EVERY sweep for
                # as long as the cell stays crashed and unclaimed, so emitting
                # produced one legal Crash and then an illegal one every sweep
                # after it: the modelled loop is already none. The reconcile at
                # the top of the sweep covers the same condition exactly once,
                # and is guarded against repeating itself.
                _reclaim(st, dry)
            elif in_box and iter_age > T_STALL and (out_age or 0) < T_HANG:
                records.rec_log(f"{cid} STALL-BUT-ALIVE (iter_age={int(iter_age)}s, "
                         f"output still growing) — leaving")
            # else HEALTHY — silent
    _retire_finished_specs(dry)
    # This sweep's readings are the next sweep's baseline. A preview must not
    # write them, or it consumes the delta the real sweep needs.
    if not dry:
        _agent_io_book({c: {"rx": rx, "cpu": cpu, "flat": io_flat.get(c, 0)}
                        for c, (rx, cpu) in io_now.items()})
