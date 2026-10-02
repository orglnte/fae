"""Low-level primitives every other driver module imports: paths, the shell
helper, the cell-id grammar, the shared constants, and the one-per-language
ledger/mutex/faults modules loaded by path.

This is the leaf of the runs/ package split: it depends on nothing else in
fae/driver/, so importing it can never cycle. The parametric WORKSPACE root (WS)
and the lock plane (ORCH) live here as module-level globals — the single
patch point every other driver module reads (`common.WS`, `common.ORCH`),
which is why the test suite patches them at `runs.common.WS` /
`runs.common.ORCH` rather than per-module.
"""
from __future__ import annotations

import fcntl
import importlib.util as _ilu
import json
import os
import re
import subprocess
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

# fae/driver/ sits directly under the repo root, so the repo root is one up.
from fae import paths as _paths  # noqa: E402

ROOT = _paths.ROOT

# The WORKSPACE root is parametric: WORKSPACES_DIR in the environment names an
# alternative tree (e.g. ws-test.nosync for harness-validation cells), so
# validation runs can never touch the scored tree. workspaces.nosync wins when
# present (iCloud-exclusion rename); plain workspaces/ is the portable default.
# Accessed everywhere as common.WS so a single patch point reaches every module.
if os.environ.get("WORKSPACES_DIR"):
    WS = Path(os.environ["WORKSPACES_DIR"])
else:
    WS = ROOT / "workspaces.nosync"
    if not WS.is_dir():
        WS = ROOT / "workspaces"
# The LOCK PLANE is NOT parametric: .orch (rig lock, verify lock, slots,
# transitions.log) serializes the ONE physical rig, so it stays global no
# matter which workspace root a cell lives in.
ORCH = ROOT / "workspaces.nosync" / ".orch"
# The reference-benchmark and smoke cells live here, never among scored cells.
SMOKE_WS = ROOT / "smoke-workspaces.nosync"


def experiment_dir():
    """The experiment definition the engine runs (fae/cell/config.py)."""
    from fae.cell import config as _config
    return _config.experiment_dir(ROOT)

# One live cell per model, everywhere: conduct enforces it at admission and
# resume defers respawns past it (--force overrides).
PER_MODEL_CAP = int(os.environ.get("PER_MODEL_CAP", 1))

# Attempts-to-green is the dependent variable, so the budget is a CONSTANT and
# not a knob: two cells run at different budgets are not comparable, and a
# "raise the budget and resume" path would be a standing exception to sealing.
ATTEMPT_BUDGET = 10

# A cell that reached a terminal verdict is finished evidence: the driver
# writes <ws>/.sealed, and this side refuses to start, resume or queue it again.
SEAL_MARKER = ".sealed"
SEAL_EXIT = 46          # the driver's refusal code: "finished", not "broken"
VALIDATION = "validation.json"  # per-cell validator output; mandatory at DONE

# THE ledger parser, THE filesystem mutex, THE provider-fault vocabulary:
# one module each, imported (the driver decides "retry this attempt" and
# faults decides "cool this lane" on one text).
from fae import ledger, mutex  # noqa: E402
from fae.cell import faults  # noqa: E402

CREDS = ROOT / ".agent-home" / ".claude" / ".credentials.json"
# The experiment definition (fae/cell/experiment.py): the matrix
# (arm -> conditions), the pinned seed docs and each arm's variant class
# are read through definition(); nothing here copies them.


def definition():
    """The loaded experiment definition (fae/cell/experiment.py)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from fae.cell import experiment as _experiment
    return _experiment.load(experiment_dir())


def agent_container(cid):
    """The agent container's name (fae/cell/config.py names it)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from fae.cell import config as _cellconfig
    return _cellconfig.agent_container(cid)


def infra_containers(treatment, cid):
    """The containers a cell of this arm provisions, as its variant names them."""
    s = definition().variant(treatment)
    return [i for k, i in (s.infra_identities(cid) if s else []) if k == "container"]


def __getattr__(name):
    if name == "AGENT_CONTAINER_PREFIX":
        return agent_container("")
    raise AttributeError(name)


PAUSE_EXIT_RC = int(os.environ.get("PAUSE_EXIT", 44))
INFRA_EXIT_RC = 45          # Cell.INFRA_EXIT: the driver halted on its infra
LOCK_EXIT_RC = 43               # Cell.LOCK_EXIT: another loop owns the workspace, benign
GENERIC_CRASH_EXIT_RC = 47      # an uncaught driver-side exception, not otherwise classified
# driver exit codes that mean EVERY cell would fail the same way:
#   1  _fp FATAL (unguarded verify surface); 2  AGENT_CMD empty; 42  HALT[agent] no creds
SYSTEMIC_EXITS = frozenset({1, 2, 42})
# LIMIT_HINTS is the OUTER gate in monitor(); AUTH_HINTS must stay a SUBSET of it.
LIMIT_HINTS = ("limit", "quota", "not logged in", "overloaded",
               "please run /login", "authentication")
AUTH_HINTS = ("not logged in", "please run /login", "authentication")


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw).stdout


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
        # runs.sh and calls _supervise_pass never trips on the sysctl parse.
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


def cell_id(model, treatment, condition, rep, task="T1", effort="high", smoke=False):
    # Mirrors the CELL_PREFIX construction: effort="" (not just unset) disables
    # the suffix. THE one implementation: fae/cell/__main__.py and
    # fae/cell/prepare.py import this rather than re-encoding the format.
    prefix = model + (f"_{effort}" if effort else "") + ("_smoke" if smoke else "")
    return f"{prefix}_{treatment}_{condition}_{task}_r{rep}"


_MODEL_RE = re.compile(r"^[a-zA-Z0-9-]+$")
_TASK_RE = re.compile(r"^T\d$")
_REP_RE = re.compile(r"^r(\d+)$")


def parse_cell_id(cid):
    """<model>_<effort>[_smoke]_<arm>_<condition>_<task>_r<rep>, or None.

    Model, effort and condition carry no underscore; the arm may (`x_sealed`),
    so it is whatever lies between the effort and the last three tokens — and
    it must be one of the experiment's arms: a name from another experiment
    or a retired vocabulary is not a cell of this one."""
    t = cid.split("_")
    if len(t) < 6:
        return None
    rep = _REP_RE.match(t[-1])
    if not rep or not _TASK_RE.match(t[-2]) or not _MODEL_RE.match(t[0]):
        return None
    i = 3 if t[2] == "smoke" else 2
    arm, condition = "_".join(t[i:-3]), t[-3]
    if not arm or not condition or arm not in definition().arms:
        return None
    return t[0], arm, condition, t[-2], rep.group(1)  # model, treatment, condition, task, rep


def _hhmm():
    return f"{datetime.now(timezone.utc):%H:%M:%S}"


# --- sealing + the global transition log --------------------------------------
# A cell that reached a terminal verdict is finished evidence: the driver writes
# <ws>/.sealed, and the orchestrator refuses to start/resume/queue it again.
# There is no unseal: redoing a cell is delete-and-requeue.

def _impl_of(cid):
    """Which implementation ran this cell. Absent means bash (the driver every
    cell sealed before the field existed); the python driver is the only one
    that starts cells now."""
    try:
        m = re.search(r"^IMPL=(\w+)$", (WS / cid / "cell.env").read_text(), re.M)
        return m.group(1) if m else "bash"
    except OSError:
        return "bash"


def is_sealed(cid):
    return (WS / cid / SEAL_MARKER).exists()


def seal_reason(cid):
    """The seal's own record — verdict, attempts, who sealed it."""
    try:
        return (WS / cid / SEAL_MARKER).read_text().splitlines()[0].replace("\t", " ")
    except (OSError, IndexError):
        return "sealed"


# TLA+ live-trace conformance: the same global transitions log the driver writes
# through Cell.apply(), mirrored here for the actions this side owns
# (Pause/Resume/Kill/Crash).
TRANSITIONS_LOG = ORCH / "transitions.log"


def _emit_transition(action, cid, extra=""):
    ORCH.mkdir(parents=True, exist_ok=True)
    with TRANSITIONS_LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t{action}\t{cid}\t{extra}\n")


def _ledger_intent(cid):
    """The MODEL's intent for this cell, replayed from the current ledger: the
    last of EPOCH/Pause/Resume/Kill wins. Resume must answer a ledger Pause, or
    it is an illegal transition the model never admits."""
    intent = "run"
    try:
        for line in TRANSITIONS_LOG.read_text().splitlines():
            p = line.split("\t")
            if len(p) < 3 or p[2] != cid:
                continue
            if p[1] == "EPOCH":
                m = re.search(r"intent=(\w+)", p[3] if len(p) > 3 else "")
                intent = {"paused": "paused", "killed": "killed"}.get(
                    m.group(1) if m else "", "run")
            elif p[1] == "Pause":
                intent = "paused"
            elif p[1] in ("Resume", "Retire"):
                intent = "run"
            elif p[1] == "Kill":
                intent = "killed"
    except OSError:
        pass
    return intent


class fs_lock:
    """Python side of THE mutex — flock(2) on a file, held for the with-block.

    THE FILE OBJECT IS THE LOCK. It is stored on self, not in a local: a
    garbage-collected file object closes its fd, and closing the fd releases
    the flock. Losing exclusion that way is silent, not a crash, which is why
    it is spelled out rather than left to `with open(...)`.

    If this process dies the kernel releases the lock; that is the whole
    recovery story. Python opens fds O_CLOEXEC by default, so no subprocess of
    ours can pin the lock past our death the way an inheriting shell child can.
    """

    def __init__(self, d, poll=0.25, timeout=None):
        self.path = Path(d)
        self.poll, self.timeout = poll, timeout
        self._f = None

    def __enter__(self):
        self._f = mutex.open_lock(self.path)
        # Polled LOCK_NB rather than a blocking LOCK_EX: an operator running
        # cli.py interactively must be able to Ctrl-C out of a queue behind a
        # 25-minute verify, and a blocking flock offers no cadence to do it in.
        t0 = time.monotonic()
        while True:
            try:
                fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                mutex.note_holder(self.path, f"runspy-{os.getpid()}", os.getpid())
                return self
            except OSError:
                if self.timeout is not None and time.monotonic() - t0 > self.timeout:
                    self._f.close()
                    self._f = None
                    raise TimeoutError(
                        f"{self.path} held by "
                        f"{mutex.holder_name(self.path) or '?'}")
                time.sleep(self.poll)

    def __exit__(self, *exc):
        try:
            mutex.clear_holder(self.path)
        finally:
            self._f.close()          # closing the fd IS the release
            self._f = None


RECONCILE_LOG = ORCH / "reconcile.log"


def _rec_log(msg):
    ORCH.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}"
    print(line)
    with RECONCILE_LOG.open("a") as f:
        f.write(line + "\n")


LOOP_CLEARED_BY = {"Crash", "Kill", "ReleaseSlot", "VerifyGreen", "StandDown",
                   "EPOCH", "Retire"}


LOOP_UNCHANGED_BY = {"Pause", "Resume"}


def _last_transitions():
    """cid -> (last LOOP-AFFECTING action, its timestamp), or {}.

    Intent-only actions are skipped rather than recorded: pausing a cell whose
    loop is already gone must not read as a cell that still has one.
    """
    last = {}
    try:
        for line in TRANSITIONS_LOG.read_text(errors="replace").splitlines():
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


# Alert-dedup state shared by fae/driver/supervise.py's sweep and
# fae/driver/conduct.py's _lift_conduct_standdowns: which cids already got an arm-stuck /
# phase-stalled alert this episode, so a repeated sweep doesn't re-alert on
# the same stall.
_ARM_ALERTED = set()
_PHASE_ALERTED = set()   # (cid, phase, attempt) already reported


# --- host-sleep tracking: ages exclude time the host was suspended ----
# The monotonic clock does not advance while the host sleeps, so wall minus
# monotonic across one tick is the sleep; gaps are kept on disk so ages
# computed after a conduct restart still exclude sleeps seen before it.
HOST_SLEEP_GAP_S = 30
HOST_SLEEP_BOOK_DAYS = 7
_sleep_clocks = None            # (wall, monotonic) at the last observation
_sleep_gaps = None              # the book, loaded on first use
def _host_sleep_book():
    return ORCH / "host_sleep.json"
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
                ORCH.mkdir(parents=True, exist_ok=True)
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
