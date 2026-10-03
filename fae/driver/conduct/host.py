"""What the host shows about the fleet: which cell loops run (the process
table, each cell's declared heartbeat), which containers exist, and each
cell's status with those facts put in. The Conduct's own view; everything
else asks the Conduct (its read-only methods).

Everything here is a query: nothing writes but the two small caches
(_LPCACHE, _PIDCACHE), which bound how often the process table is swept.
What a cell's status means is the Cell's (Cell.status); this module only
supplies what the host knows.
"""
from __future__ import annotations

import re
import time

from fae.driver import common


def loop_pids():
    """pid -> cell_id for loops that tee their console to
    <ws>/run_cell.log (the bash-era driver). The python driver writes no
    such file, so its loops are found by heartbeat and by loop_parents()."""
    pids = {}
    # Anchored to THIS invocation's workspace root, so an alternative root's
    # loops are not read as dead.
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

    PRIMARY source is each cell's declared heartbeat (.loop): the loop writes
    its own pid, so identity needs no ps-argv archaeology. The ps scan below
    still runs and is UNIONED in, because kill paths must never miss a loop: a
    loop that has not written its heartbeat yet is exactly the orphan that once
    made stop-all kill wrappers while every driver loop survived.

    The ps scan matches `-m fae.cell` followed by its 3 positionals (task
    variant rep) — not merely anywhere in the line: children carry the
    driver's path in their env and would shadow the real loop pid. AGENT and
    EFFORT come from the exec-time env; the cid is rebuilt from them. The env
    text is scanned for those keys only and never printed — it also carries
    agent credentials."""
    out = {}
    if common.WS.exists():
        for ws in common.WS.iterdir():
            if not ws.is_dir():
                continue
            hb = heartbeat(ws)
            if hb:
                out[ws.name] = hb["pid"]
    for line in common.sh(["ps", "-axww", "-E", "-o", "pid=,command="]).splitlines():
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


_PIDCACHE = {"t": 0.0, "v": set()}


def run_cell_pids():
    """{pid} of every live cell loop, in either implementation — one ps sweep,
    2s cache.

    A pid missing here makes a cell's heartbeat a corpse, and the reaper then
    unlinks a LIVE loop's declared liveness, so both drivers are recognised:
    the bash script and the package run as a module. No `ps -E`: this only asks
    "is this pid a cell loop", so the environment (which carries credentials)
    never enters the dump."""
    now = time.time()
    if now - _PIDCACHE["t"] > 2:
        _PIDCACHE["v"] = {
            int(l.split(None, 1)[0])
            for l in common.sh(["ps", "-axww", "-o", "pid=,command="]).splitlines()
            if re.search(r"bash\s+\S*harness/run_cell\.sh\s"
                         r"|-m fae\.cell\s", l)}
        _PIDCACHE["t"] = now
    return _PIDCACHE["v"]


def _cell(ws):
    return common.cell(ws.name, workspaces=ws.parent)


def heartbeat(ws, cell=None):
    """The loop's declared liveness, its pid confirmed a live cell loop
    (Cell.live_heartbeat), ages excluding host sleep; None for a corpse."""
    return (cell or _cell(ws)).live_heartbeat(run_cell_pids(), common.awake_age)


def queued(cid):
    """Does the cell's spec wait in its lane (admission resumes it)?"""
    parsed = common.parse_cell_id(cid)
    return bool(parsed) and common.queues().lane_has(parsed[0], cid)


def cell_state(ws, loops, boxes):
    """The cell in `ws` as status shows it (Cell.status), with what the host
    knows: its live heartbeat, a loop the process table shows, its spec
    waiting in the lane. None for a folder that is not a prepared cell.
    `loops` and `boxes` are the callers' sweeps, read nowhere now."""
    parsed = common.parse_cell_id(ws.name)
    if not parsed:
        return None
    c = _cell(ws)
    if not c.has_ledger:
        return None
    return c.status(parsed, common.definition().gate.arity, heartbeat(ws, c),
                    looping=lambda: ws.name in live_loops(), queued=lambda: queued(ws.name))


def all_states(running_only=False):
    loops, boxes = loop_pids(), containers()
    active_cids = set(loop_parents().keys()) if running_only else None
    out = []
    for w in sorted(common.WS.iterdir()):
        if not w.is_dir():
            continue
        if active_cids is not None and w.name not in active_cids:
            continue
        s = cell_state(w, loops, boxes)
        if s:
            out.append(s)
    return out, loops, boxes
