"""Low-level primitives every other driver module imports: paths, the shell
helper, the cell-id grammar, the shared constants, and the one-per-language
mutex/faults modules loaded by path.

This is the leaf of the runs/ package split: it depends on nothing else in
fae/driver/, so importing it can never cycle. The parametric WORKSPACE root (WS)
and the scheduling plane (QUEUES, CONDUCT, LOCKS, TRANSITIONS_LOG; fae/plane.py)
live here as module-level globals — the single patch point every other driver
module reads, which is why the test suite patches them at `runs.common.*`
rather than per-module.
"""
from __future__ import annotations

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
from fae import plane as _plane  # noqa: E402
from fae.queues import Queues as _Queues  # noqa: E402

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
# The scheduling plane is NOT parametric: it serializes the ONE physical rig,
# so it stays global no matter which workspace root a cell lives in.
QUEUES = _plane.queues(ROOT)
CONDUCT = _plane.conduct(ROOT)
LOCKS = _plane.locks(ROOT)
# The reference-benchmark and smoke cells live here, never among scored cells.
SMOKE_WS = ROOT / "smoke-workspaces.nosync"


def experiment_dir():
    """The experiment definition the engine runs (fae/cell/config.py)."""
    from fae.cell import config as _config
    return _config.experiment_dir(ROOT)

# THE filesystem mutex, THE provider-fault vocabulary: one module each,
# imported (the driver decides "retry this attempt" and faults decides "cool
# this lane" on one text).
from fae import mutex  # noqa: E402
from fae.cell import faults  # noqa: E402

# The experiment definition (fae/experiment.py) and its variants are read
# through definition(); nothing here copies them.
from fae import experiment as _experiment  # noqa: E402


from fae.experiment import cell_id, definition, parse_cell_id  # noqa: E402,F401


# --- sealing ------------------------------------------------------------------
# A cell that reached a terminal verdict is finished evidence (Cell.seal), and
# the driver refuses to start, resume or queue it again. There is no unseal:
# redoing a cell is delete-and-requeue.

def is_sealed(cid):
    return cell(cid).sealed


def seal_reason(cid):
    """The seal's own record — verdict, attempts, who sealed it."""
    return cell(cid).seal_record().replace("\t", " ") or "sealed"


def queues():
    """The queues (fae/queues.py) as the driver sees them: QUEUES and its lock
    in LOCKS, the cell-id grammar, sealed cells refused, the agents' logs
    read from WS."""
    return _Queues(QUEUES, locks=LOCKS, cell_id=cell_id, refuse=_queue_refusal,
                   workspaces=WS)


def _queue_refusal(cid):
    # Queueing a sealed cell would put a spec in a lane that conduct can only
    # ever refuse — a permanently stuck queue entry.
    return f"SEALED — {seal_reason(cid)}" if is_sealed(cid) else None


# TLA+ live-trace conformance: the one transitions log every cell writes
# (through Cell, by appending), read here for each cell's last transition.
TRANSITIONS_LOG = _plane.transitions_log(ROOT)


def workspace(path=None):
    """The workspace (fae/experiment.py) as the driver sees it: WS unless
    `path` names another root, on the plane (QUEUES, CONDUCT, LOCKS,
    TRANSITIONS_LOG), the cell-id grammar deciding what is a cell."""
    return _experiment.Workspace(ROOT, path or WS, parse=parse_cell_id, queues=queues(),
                                 conduct=CONDUCT, locks=LOCKS, transitions=TRANSITIONS_LOG)


def experiment():
    """The experiment this root runs (fae/experiment.py), on the driver's workspace."""
    return _experiment.Experiment(ROOT, workspace())


def cell(cid, task=None, variant=None, rep=1, agent=None, reference=False,
         workspaces=None):
    """The cell `cid` on the driver's workspace (workspace()), or on the root
    `workspaces` names. Named by what it runs (`variant`), it may not exist
    yet: what a start prepares."""
    return workspace(workspaces).cell(cid, task, variant, rep, agent=agent, reference=reference)


def named_cell(cid, variant=None):
    """The cell `cid`, its identity read from its id where its workspace does
    not record it."""
    p = parse_cell_id(cid)
    return cell(cid, p[2] if p else "T1", variant or (p[1] if p else ""), p[3] if p else 1)


# --- selecting cells ----------------------------------------------------------
# One selector language for every verb (fae/experiment.py: matches).
matches = _experiment.matches


def select_cells(*selectors):
    """cids with a workspace that any selector matches, sorted."""
    return workspace().select(*selectors)


def is_blanket(selectors):
    """A selection naming `all`: standing operator decisions (roster/manual
    pauses, a cancel) survive it, and yield only to a cell or agent named."""
    return any(s in ("all", "*") for s in selectors)


