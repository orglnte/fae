"""Fleet state: which loops are alive, which cell owns which slot, and the
per-cell state derivation that status/watch/monitor and conduct's supervision
all read.

loop_pids/loop_parents/containers are the process-table facts; heartbeat reads
a workspace's own declared state; cell_state combines both into one row.
Everything here is a query: nothing writes but the two small caches
(_LPCACHE, _PIDCACHE), which exist only to bound how often the process table
is swept. A cell's folder is changed through its Cell.
"""
from __future__ import annotations

import os
import ujson as json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fae.driver import common
from fae.driver.common import (
    parse_cell_id, awake_age, faults,
)

def loop_pids():
    """pid -> cell_id for loops that tee their console to
    <ws>/run_cell.log (the bash-era driver). The python driver writes no
    such file, so its loops are found by heartbeat and by loop_parents()."""
    pids = {}
    # Anchored to THIS invocation's workspace root, not to a hardcoded
    # directory name: with a parametric root (WORKSPACES_DIR) the old
    # workspaces(.nosync)? pattern read every alternative-root loop as DEAD.
    pat = re.compile(re.escape(str(common.WS)) + r"/([^/ ]+)/run_cell\.log")
    for line in common.sh(["ps", "-axww", "-o", "pid=,command="]).splitlines():
        if "tee -a " not in line:
            continue
        m = pat.search(line)
        if m:
            pids[int(line.split(None, 1)[0])] = m.group(1)
    return pids


_LPCACHE = {"t": 0.0, "v": {}}


def live_loops():
    """cid -> loop pid, 2s-cached (loop_parents sweeps ps; cell_state asks per
    workspace)."""
    now = time.time()
    if now - _LPCACHE["t"] > 2:
        _LPCACHE["v"], _LPCACHE["t"] = loop_parents(), now
    return _LPCACHE["v"]


def loop_parents():
    """cid -> the ACTUAL driver loop pid.

    PRIMARY source is each workspace's declared heartbeat ($ws/.loop): the loop
    writes its own pid, so identity needs no ps-argv archaeology at all. The
    legacy ps scan below still runs and is UNIONED in, because kill paths
    (stop / drain / pause) must never miss a loop: a loop that has not written
    its heartbeat yet is exactly the orphan that caused the 2026-07-24
    incident, where this function returned the tee logger's PPID — the spawn
    WRAPPER shell — so stop-all and drain "killed" wrappers while every
    driver loop survived orphaned, cascade-stole the arm lock, provisioned 5
    orphan kind clusters, and carried a stale FP pin into a burned attempt.

    The ps fallback matches `-m fae.cell` followed by its 3 positionals
    (task variant rep) — not merely anywhere in the line: children
    (deploy, the agent) carry the driver's path in their env and would
    otherwise shadow the real loop pid. AGENT/EFFORT come from the exec-time
    env (spawn sets them); CELL_ID is set inside the driver and invisible to
    ps, so the cid is rebuilt.
    The env text is scanned for those keys only and never printed — it also
    carries agent credentials."""
    out = {}
    if common.WS.exists():
        for ws in common.WS.iterdir():
            if not ws.is_dir():
                continue
            hb = heartbeat(ws)
            if hb:
                out[ws.name] = hb["pid"]
    for line in common.sh(["ps", "-axww", "-E", "-o", "pid=,command="]).splitlines():
        # The package run as `-m fae.cell` with its three positionals.
        m = re.match(r"\s*(\d+)\s+.*?-m fae\.cell"
                     r"\s+(\S+)\s+(\S+)\s+(\d+)", line)
        if not m:
            continue
        pid, task, variant, rep = m.groups()
        agent = re.search(r"\bAGENT=(\S+)", line)
        if not agent:
            continue
        eff = re.search(r"\bEFFORT=(\S+)", line)
        prefix = f"{agent.group(1)}{'_' + eff.group(1) if eff else '_high'}"
        if re.search(r"\bSMOKE=1", line):
            prefix += "_smoke"
        out.setdefault(f"{prefix}_{variant}_{task}_r{rep}", int(pid))
    return out


def containers():
    return set(common.sh(["docker", "ps", "--format", "{{.Names}}"]).split())


WAIT_REASON_MAX = 300


def wait_reason(ws):
    # Newest by MTIME. The name sorts wrong twice over: the attempt number is
    # lexical ("attempt-6" > "attempt-10") and the wait suffix is a bare HHMMSS
    # that wraps every midnight. Both put an older snapshot last, and this text
    # is what _set_cooldown reads a reset hint out of — so a stale one parks the
    # whole lane on a wall that already expired.
    snaps = sorted(ws.glob("agent.attempt-*.wait-*.log"),
                   key=lambda p: p.stat().st_mtime)
    if not snaps:
        return ""
    # The whole line, not a prefix: the provider's reset hint is the tail of
    # its message, and the lane cooldown is parsed out of this text.
    return faults.reason(snaps[-1].read_text(errors="replace"))[:WAIT_REASON_MAX]
# phases a loop declares that mean "alive but parked, waiting on something it
# will leave by itself" — as opposed to spending wall clock productively.
WAIT_PHASES = {"verify-lock", "limit"}


def _cell(where):
    """The Cell for a cid (in WS) or for a workspace path."""
    if isinstance(where, Path):
        return common.cell(where.name, workspaces=where.parent)
    return common.cell(where)


def pause_lock(cid):
    """The operator's stop request for ONE cell: its reason, or None. Intent
    lives IN THE WORKSPACE (beside the cancellation) — a tmp/ wipe must
    never erase an operator decision."""
    r = _cell(cid).pause_request()
    return r.reason if r else None


def pause_meta(cid):
    """(reason, who, at_epoch) of the cell's stop request, or None. `who`
    and `at` are None when the request predates them."""
    r = _cell(cid).pause_request()
    return (r.reason, r.who, r.at) if r and r.reason else None


def pause_detail(cid):
    """The violation the driver stood down on, written under its stop request."""
    r = _cell(cid).pause_request()
    return r.detail if r else ""


def flagged(cid):
    """Quarantined for a human: no repair, no admission until resumed."""
    return _cell(cid).flagged


def cancelled(cid):
    return _cell(cid).cancelled


def cell_intent(ws, cell=None):
    """AXIS 2 — (intent, why). Everything but 'run' comes from a marker
    somebody wrote on purpose, so a deliberate stop can never be mistaken for
    a crash (or the reverse)."""
    c = cell or _cell(ws)
    if c.cancelled:
        return "cancel", "cancelled"
    r = c.pause_request()
    if r and r.reason:
        return "pause", r.reason
    return "run", ""


_PIDCACHE = {"t": 0.0, "v": set()}


def run_cell_pids():
    """{pid} of every live cell loop, in either implementation — one ps sweep,
    2s cache.

    A pid missing here makes heartbeat() call the cell's .loop a corpse, and
    the reaper then unlinks a LIVE loop's declared liveness. So both drivers
    must be recognised: the bash script, and the package run as a module.

    Deliberately does NOT use `ps -E`: this only has to answer "is this pid
    still a run_cell loop", so the environment (which carries
    CLAUDE_CODE_OAUTH_TOKEN) never enters the process-table dump at all.
    Identity comes from the heartbeat file, not from parsing argv positions."""
    now = time.time()
    if now - _PIDCACHE["t"] > 2:
        _PIDCACHE["v"] = {
            int(l.split(None, 1)[0])
            for l in common.sh(["ps", "-axww", "-o", "pid=,command="]).splitlines()
            if re.search(r"bash\s+\S*harness/run_cell\.sh\s"
                         r"|-m fae\.cell\s", l)}
        _PIDCACHE["t"] = now
    return _PIDCACHE["v"]


def heartbeat(ws, cell=None):
    """The loop's DECLARED liveness — dict(pid, phase, attempt, age) or None.

    run_cell writes $ws/.loop at every phase transition. Declared beats
    inferred: the pid comes from the loop itself instead of ps-argv pattern
    matching (whose failure once made stop/drain kill wrappers while every
    loop survived orphaned), and the phase is a fact rather than a guess off
    the last log line. A file whose pid is no longer a run_cell process is a
    corpse's leftover and makes no liveness claim."""
    kv = (cell or _cell(ws)).heartbeat()
    try:
        pid = int(kv["pid"])
    except (TypeError, ValueError, KeyError):
        return None
    if pid not in run_cell_pids():
        return None
    if kv.get("cid") not in (None, ws.name):
        return None   # a recycled pid validated another cell's corpse file —
                      # identity, not mere pid-liveness (audit finding 7)
    kv["pid"], kv["age"] = pid, awake_age(kv.pop("mtime"))
    # How long this phase has been current. NOT derivable from `age` or `ts`:
    # the ticker rewrites .loop every HB_TICK precisely to keep its mtime
    # young, so both say "seconds" no matter how long the cell has been stuck.
    # hb_phase carries `since` forward while phase+attempt are unchanged.
    try:
        kv["phase_age"] = awake_age(float(kv["since"]))
    except (KeyError, TypeError, ValueError):
        kv["phase_age"] = None      # a .loop written before `since` existed
    return kv


def _dur(secs):
    """Compact elapsed time for a table cell: 45s, 12m, 3h20m, 2d4h."""
    if secs is None:
        return "-"
    s = int(max(0, secs))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, h = divmod(s // 3600, 24)
    return f"{d}d{h:02d}h" if h else f"{d}d"


def cell_liveness(ws, boxes, cell=None):
    """AXIS 3 — (state, why, detail) from MECHANISM alone: no verdicts, no
    operator markers. Prefers the declared heartbeat; falls back to the older
    inference for loops started before heartbeats existed.

    Took `loops`, `last_ev` and `last_line` and read none of them, so every
    caller built a full loop_pids() ps sweep purely to hand it an argument it
    discarded. Dropped 2026-07-30.
    """
    cid = ws.name
    hb = heartbeat(ws, cell)
    if hb:
        phase = hb.get("phase", "?")
        if phase in WAIT_PHASES:
            detail = wait_reason(ws) if phase == "limit" else ""
            return "WAITING", phase, detail
        return "RUNNING", phase, ""

    # --- no heartbeat: minimal fallback ---------------------------------------
    # Every loop born after 2026-07-25 writes .loop from its first breath (the
    # early trap covers pre-setup exits). The old inference stack — container
    # presence, WAIT-age arithmetic, log-tail lock parsing — carried the very
    # ambiguities heartbeats were built to remove (idle vs mid-verify), so it
    # is retired. A live argv-matched loop with no heartbeat can only be
    # between exec boundaries: report it working, qualifier unknown.
    if cid in live_loops():
        return "RUNNING", "idle", ""
    if _queued(cid):
        return "CRASHED", "loop", "no loop — queued, admission resumes it"
    return "CRASHED", "loop", "no loop — resume: cli.py cell resume <cid>"


def _queued(cid):
    parsed = common.parse_cell_id(cid)
    return bool(parsed) and common.queues().lane_has(parsed[0], cid)


def never_started(st):
    """A prepared workspace that was never launched: not part of the run yet.

    Distinguished so reconcile doesn't 'respawn' it — auto-starting agent runs
    nobody launched (audit finding 13). Counts EVENTS, not ITER history: a
    cell whose attempt 1 started (START logged) but never finished has an
    empty history too, and the first predicate stranded six paused-then-
    crashed cells as 'never started' (2026-07-25).

    The `events == 0` test ALONE went dead when the prepare (fae/cell/prepare.py) began
    writing a v2 PREPARED birth event: ledger.parse counts it, so a freshly
    prepared workspace has events == 1 and this returned False for every one
    of them. `cli.py experiment prepare` seeds the whole matrix and launches nothing, so
    reconcile would then classify each as CRASHED-with-no-loop and _respawn it
    — paid agent runs nobody ordered, which is exactly what the guard exists
    to prevent and a direct breach of the no-unordered-restart rule.
    ledger.parse already returns `prepared` for this purpose; nothing read it.

    So: never launched == the ledger holds NOTHING but the birth event. Both
    v1 (zero events) and v2 (PREPARED only) shapes are covered, and a cell that
    genuinely started — START logged, or any attempt judged — has more events
    and is NOT caught, which keeps the 2026-07-25 fix intact.

    Deliberately does NOT test st["att"]: cell_state reassigns that to the
    IN-FLIGHT attempt NUMBER for non-terminal cells (`max(att + 1, 1)`, so
    never 0), not to a count of judged attempts. An `att == 0` condition here
    would be permanently false and leave this guard as dead as it was.
    """
    if st["state"] != "CRASHED":
        return False
    if st["events"] == 0:
        return True                      # v1 ledgers: no birth event at all
    return bool(st.get("prepared")) and st["events"] == 1


def _e2e_failed(cell):
    """The metrics are rewritten by every arrangement, so this reads the one
    in flight only while a verify runs."""
    met = cell.read_metrics()
    return bool(met.get("e2e_total")) and met.get("e2e_pass") != met.get("e2e_total")


def cell_state(ws, loops, boxes):
    """dict for one workspace, or None.

    Thin wrapper: NOEDIT fills the detail column when nothing more urgent
    claimed it, on every path _cell_state can return through.
    """
    st = _cell_state(ws, loops, boxes)
    if st and not st["detail"] and st["noedit"]:
        st["detail"] = f"NOEDIT ×{st['noedit']} — investigate"
    return st


def _cell_state(ws, loops, boxes):
    parsed = parse_cell_id(ws.name)
    if not parsed:
        return None
    c = _cell(ws)
    if not c.has_ledger:
        return None
    agent, variant, task, rep = parsed
    L = c.read_ledger(gate_n=common.definition().gate.arity)
    env = c.env
    budget = env.get("ATTEMPT_BUDGET") if env.get("ATTEMPT_BUDGET", "").isdigit() else "?"
    mver = env.get("AGENT_MODEL") or "-"
    st = dict(cid=ws.name, agent=agent, variant=variant,
              task=task, rep=int(rep), att=L["att"], budget=budget,
              hist=c.history(L), events=L["events"], agent_model=mver,
              # `prepared` is read by never_started: with the v2 PREPARED birth
              # event, events==0 no longer identifies a workspace that was
              # prepared but never launched, and reconcile must not respawn one.
              prepared=L["prepared"],
              noedit=L["noedit"], noedit_last=L["noedit_last"],
              alerts=L["alerts"], alerts_open=L.get("alerts_open", 0), alert_last=L["alert_last"],
              detail="", green_at=None, why="", live="-",
              shape=(f"{L['rev_pass']}/{L['gate_n']}" if L["reverify_active"]
                     else f"{L['gate']}/{L['gate_n']}" if L["gate"] is not None
                     else "-"))
    # GATE, mid-verify: the arrangements passed so far in the attempt RUNNING
    # NOW. L['gate'] is the last JUDGED attempt and cannot move while one is in
    # flight, so leaving it there reads as a gate stuck for the whole verify.
    # Starred, because it is a count in progress rather than a verdict.
    # An X after the star: the in-flight arrangement's e2e stage failed.
    hb = heartbeat(ws, c)
    if hb and hb.get("phase") == "verify" and not L["reverify_active"]:
        st["shape"] = f"{L['live_shape_pass']}/{L['gate_n']}*" + ("X" if _e2e_failed(c) else "")
    try:
        st["taint"] = json.loads(c.read_derived("validation.json") or "{}").get("verdict") == "TAINTED"
    except json.JSONDecodeError:
        st["taint"] = False

    # --- compose: cancel > outcome > pause > liveness ------------------------
    # The ONLY place precedence between the axes is decided. Outcome comes
    # from THE ledger library; intent and liveness resolve as before.
    intent, why = cell_intent(ws, c)
    if intent == "cancel":
        st["state"], st["why"] = "DONE", "cancelled"
        return st
    if L["verdict"] == "green":                              # AXIS 1: outcome
        st["state"], st["why"], st["green_at"] = "DONE", "green", L["green_at"]
        return st
    if L["verdict"] == "revoked":
        st["state"], st["why"] = "DONE", "revoked"
        st["detail"] = "green revoked by the 6-shape gate"
        return st
    if L["verdict"] == "failed":
        st["state"], st["why"] = "DONE", "failed"
        v = ws / "verify.log"
        if v.exists():
            fails = [l for l in v.read_text().splitlines() if "FAIL[" in l]
            st["detail"] = fails[-1].split("FAIL", 1)[-1][:70] if fails else ""
        return st
    st["att"] = max(L["att"] + (0 if L["last_ev"] == "ITER" else 1), 1)
    live = cell_liveness(ws, boxes, c)
    # LIVE column: e2e OK/NOK + in-flight shape-gate progress for the
    # attempt that hasn't been judged yet — `gate`/`hist` are frozen at the
    # last JUDGED attempt, so a cell deep in a 6-arrangement shape gate
    # reads as stuck at 0/6 for its whole duration otherwise (2026-07-25
    # user question). Only shown while a verify is genuinely in flight
    # (heartbeat phase `verify`, which now spans the whole gate — the separate
    # `shape` phase was display-only and is gone) — metrics.json is overwritten
    # by each arrangement, so outside that window it would just be stale.
    hb = heartbeat(ws, c)
    if hb and hb.get("phase") == "verify":
        met = c.read_metrics()
        e2ep, e2et = met.get("e2e_pass"), met.get("e2e_total")
        if e2et:
            # OK/NOK already says whether e2ep == e2et; printing the pair
            # again spent width on a fact the verdict carries.
            st["live"] = (f"e2e:{'OK' if e2ep == e2et else 'NOK'} "
                           f"{L['live_shape_pass']}/{L['gate_n']}")
    if intent == "pause" and live[0] == "CRASHED":
        # asked to stop and no longer running: the request is honoured
        st["state"], st["why"] = "PAUSED", why
        return st
    if L["last_ev"] == "HALT" and live[0] == "CRASHED":
        # the rig broke under it — not a verdict: no attempt burned. Cause
        # routes the response: infra respawns, agent/auth needs a human.
        st["state"] = "CRASHED"
        st["why"] = "infra" if "infra" in L["halt_cause"] else "agent"
        st["detail"] = L["halt_cause"]
        return st
    st["state"], st["why"], st["detail"] = live
    if intent == "pause":
        st["detail"] = (st["detail"] + "  (pause pending)").strip()
    return st


def _arm_slot_of(arm, cid):
    """Seconds this cid has held a slot of `arm`, or None if it holds none.

    The note's timestamp is when the slot was taken; the kernel says whether
    it is still held. Both are needed — the note alone would age a slot its
    holder released long ago.
    """
    if not arm:
        return None
    q = common.queues()
    for slot in q.slot_files(arm):
        note = q.slot_note(slot)
        if not note or note[0] != cid or not q.slot_held(slot):
            continue
        return awake_age(note[2]) if note[2] is not None else None
    return None


def all_states(running_only=False):
    loops, boxes = loop_pids(), containers()
    active_cids = set(loop_parents().keys()) if running_only else None
    out = []
    for w in sorted(common.WS.iterdir()):
        if not w.is_dir(): continue
        if active_cids is not None and w.name not in active_cids: continue
        s = cell_state(w, loops, boxes)
        if s: out.append(s)
    return out, loops, boxes
