"""The scheduling plane: where the queues, the scheduler's own state, the
machine-wide locks and the cells' lifecycle log live under an experiment root.

    <root>/workspaces.nosync/
        .queues/          queued, running and done specs, slots, agents' books
        .conduct/         the scheduler process's own state and logs
        .locks/           the locks a cell takes while it runs
        transitions.log   every cell's lifecycle, in the order it happened

Not parametric: it serializes the one machine the cells run on, so it stays
here whichever workspace root (WORKSPACES_DIR) a cell lives in.
"""
from __future__ import annotations

from pathlib import Path


def base(root):
    return Path(root) / "workspaces.nosync"


def queues(root):
    return base(root) / ".queues"


def conduct(root):
    return base(root) / ".conduct"


def locks(root):
    return base(root) / ".locks"


def transitions_log(root):
    return base(root) / "transitions.log"
