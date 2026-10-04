"""What the host shows about the fleet: which cell loops run (the process
table, each cell's declared heartbeat), which containers exist, each cell's
status with those facts put in, the host's memory, and the book of host
suspends that keeps ages honest across a sleep.

Everything here is a query except the host-sleep book (.conduct/host_sleep.json)
and two small caches (_LPCACHE, _PIDCACHE) that bound how often the process
table is swept. What a cell's status means is the Cell's (Cell.status); this
module only supplies what the host knows.

The monotonic clock does not advance while the host sleeps, so wall minus
monotonic across one observation is the sleep; gaps are kept on disk so ages
computed after a restart still exclude sleeps seen before it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time

import fae.experiment
from fae.cell.fsm import LOOP_UNCHANGED_BY
from fae import experiment as _experiment


HOST_SLEEP_GAP_S = 30
HOST_SLEEP_BOOK_DAYS = 7
_sleep_clocks = None            # (wall, monotonic) at the last observation
_sleep_gaps = None              # the book, loaded on first use


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw).stdout


def loop_pids():
    """pid -> cell_id for loops that tee their console to
    <ws>/run_cell.log (the bash-era driver). The python driver writes no
    such file, so its loops are found by heartbeat and by loop_parents()."""
    pids = {}
    # Anchored to THIS invocation's workspace root, so an alternative root's
    # loops are not read as dead.
    pat = re.compile(re.escape(str(fae.experiment.exp().workspace.path)) + r"/([^/ ]+)/run_cell\.log")
    for line in sh(["ps", "-axww", "-o", "pid=,command="]).splitlines():
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
    if fae.experiment.exp().workspace.path.exists():
        for ws in fae.experiment.exp().workspace.path.iterdir():
            if not ws.is_dir():
                continue
            hb = heartbeat(ws)
            if hb:
                out[ws.name] = hb["pid"]
    for line in sh(["ps", "-axww", "-E", "-o", "pid=,command="]).splitlines():
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
    return set(sh(["docker", "ps", "--format", "{{.Names}}"]).split())


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
            for l in sh(["ps", "-axww", "-o", "pid=,command="]).splitlines()
            if re.search(r"bash\s+\S*harness/run_cell\.sh\s"
                         r"|-m fae\.cell\s", l)}
        _PIDCACHE["t"] = now
    return _PIDCACHE["v"]


def _cell(ws):
    return fae.experiment.exp().cell(ws.name, workspaces=ws.parent)


def heartbeat(ws, cell=None):
    """The loop's declared liveness, its pid confirmed a live cell loop
    (Cell.live_heartbeat), ages excluding host sleep; None for a corpse."""
    return (cell or _cell(ws)).live_heartbeat(run_cell_pids(), awake_age)


def queued(cid):
    """Does the cell's spec wait in its lane (admission resumes it)?"""
    parsed = _experiment.parse_cell_id(cid)
    return bool(parsed) and fae.experiment.exp().workspace.queues.lane_has(parsed[0], cid)


def cell_state(ws, loops=None, boxes=None):
    """The cell in `ws` as status shows it (Cell.status), with what the host
    knows: its live heartbeat, a loop the process table shows, its spec
    waiting in the lane. None for a folder that is not a prepared cell.
    `loops` and `boxes` are the callers' sweeps, read nowhere now."""
    parsed = _experiment.parse_cell_id(ws.name)
    if not parsed:
        return None
    c = _cell(ws)
    if not c.has_ledger:
        return None
    return c.status(parsed, fae.experiment.exp().definition.gate.arrangements_nr, heartbeat(ws, c),
                    looping=lambda: ws.name in live_loops(), queued=lambda: queued(ws.name))


def all_states(running_only=False):
    loops, boxes = loop_pids(), containers()
    active_cids = set(loop_parents().keys()) if running_only else None
    out = []
    for w in sorted(fae.experiment.exp().workspace.path.iterdir()):
        if not w.is_dir():
            continue
        if active_cids is not None and w.name not in active_cids:
            continue
        s = cell_state(w, loops, boxes)
        if s:
            out.append(s)
    return out, loops, boxes


def agent_containers(boxes):
    """The names in `boxes` that are agent containers."""
    prefix = agent_container("")
    return [n for n in boxes if n.startswith(prefix)]


def agent_container(cid):
    """The agent container's name (fae/cell/config.py names it)."""
    from fae.cell import config as _cellconfig
    return _cellconfig.agent_container(cid)


def infra_containers(variant, cid):
    """The containers a cell of this variant provisions, as its infra class names them."""
    s = fae.experiment.exp().definition.variant(variant)
    return [i for k, i in (s.INFRA.identities(cid) if s else []) if k == "container"]


def mem_pressure():
    """Host memory snapshot, read straight from the kernel — the numbers that
    predict an OOM (Jetsam) kill under a heavy cell fleet.

    Memory pressure is NOT swap depth: kern.memorystatus_vm_pressure_level is
    the kernel's own pressure band (1 normal / 2 warning / 4 critical — the
    signal Jetsam acts on), driven by how much physical memory is available
    (kern.memorystatus_level, a percent), not by how many pages are in swap.
    Both are reported; conduct watches the band every supervision pass and
    status surfaces the usage. `label` is None off macOS, where these sysctls
    do not exist."""
    if sys.platform != "darwin":
        return {"label": None, "level": 0, "avail_pct": 0, "used_gb": 0.0,
                "total_gb": 0.0, "swap_used_mb": 0.0, "swap_total_mb": 0.0}
    def _n(name, default=0):
        # str() coerces a mocked/non-string sh() result so a test that patches
        # runs.sh and calls supervise_pass never trips on the sysctl parse.
        try:
            return int(str(sh(["sysctl", "-n", name])).strip() or default)
        except (ValueError, TypeError):
            return default
    level = _n("kern.memorystatus_vm_pressure_level", 1)
    label = {1: "normal", 2: "WARN", 4: "CRITICAL"}.get(level, f"level={level}")
    avail = _n("kern.memorystatus_level", 0)          # % of physical memory available
    total_gb = _n("hw.memsize") / 2**30
    used_gb = total_gb * (1 - avail / 100) if avail else 0.0
    m = re.search(r"used = ([\d.]+)M.*free = ([\d.]+)M", str(sh(["sysctl", "-n", "vm.swapusage"])))
    su = float(m.group(1)) if m else 0.0
    sf = float(m.group(2)) if m else 0.0
    return {"label": label, "level": level, "avail_pct": avail,
            "used_gb": used_gb, "total_gb": total_gb,
            "swap_used_mb": su, "swap_total_mb": su + sf}


def last_transitions():
    """cid -> (last LOOP-AFFECTING action, its timestamp), or {}.

    Intent-only actions are skipped rather than recorded: pausing a cell whose
    loop is already gone must not read as a cell that still has one.
    """
    last = {}
    try:
        for line in fae.experiment.exp().workspace.transitions.read_text(errors="replace").splitlines():
            f = line.split("\t")
            if len(f) < 3 or not f[2] or f[1] in LOOP_UNCHANGED_BY:
                continue
            act = f[1]
            if act == "EPOCH" and "loop=none" not in line:
                act = "EPOCH-live"    # seeded mid-flight; the loop may be gone
            last[f[2]] = (act, f[0])
    except OSError:
        pass
    return last


# --- the host-sleep book -------------------------------------------------------

def _host_sleep_book():
    return fae.experiment.exp().workspace.conduct / "host_sleep.json"


def _host_sleep_gaps():
    global _sleep_gaps
    if _sleep_gaps is None:
        try:
            _sleep_gaps = [g for g in json.loads(_host_sleep_book().read_text())
                           if isinstance(g, dict) and "start" in g and "s" in g]
        except (OSError, ValueError, TypeError):
            _sleep_gaps = []
    return _sleep_gaps


def host_sleep_observe(now=None, mono=None):
    """Record a host suspend since the previous call, if one happened.
    Returns the gap in seconds (0 when none)."""
    global _sleep_clocks
    wall = time.time() if now is None else now
    mono = time.monotonic() if mono is None else mono
    gap = 0.0
    if _sleep_clocks is not None:
        gap = (wall - _sleep_clocks[0]) - (mono - _sleep_clocks[1])
        if gap > HOST_SLEEP_GAP_S:
            gaps = _host_sleep_gaps()
            gaps.append({"start": _sleep_clocks[0], "s": gap})
            keep = wall - HOST_SLEEP_BOOK_DAYS * 86400
            gaps[:] = [g for g in gaps if g["start"] + g["s"] >= keep]
            try:
                _host_sleep_book().parent.mkdir(parents=True, exist_ok=True)
                _host_sleep_book().write_text(json.dumps(gaps))
            except OSError:
                pass
        else:
            gap = 0.0
    _sleep_clocks = (wall, mono)
    return gap


def awake_age(t_wall, now=None):
    """Seconds since the wall stamp t_wall, host sleep excluded. A sleep is
    counted from the last awake observation for its whole length; the
    overlap with [t_wall, now] is what is subtracted."""
    now = time.time() if now is None else now
    age = now - t_wall
    for g in _host_sleep_gaps():
        start, end = g["start"], g["start"] + g["s"]
        overlap = min(end, now) - max(start, t_wall)
        if overlap > 0:
            age -= overlap
    return age
