"""The treatment interface and the registry of the experiment's variants.

`base.Variant` is what the driver calls — setup / teardown / infra_ok
— and what every variant answers. The variants themselves belong to the
experiment, which declares them (fae/cell/experiment.py); this package
reads that registry, keyed by arm.

Slots are NOT taken here: the driver holds the work slot and the arm slot on
its own fds for the cell's whole life (Cell.acquire_slots), so provisioning
never waits on a queue.

`python3 -m fae.cell.variants <arm> setup|teardown|infra <cid> <ws>`
is the operator's hand entry; `cli.py experiment infra` calls the classes
directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import (HARNESS, ROOT, HookFailure, NoopVariant, Variant, liveness_declared,  # noqa: F401
                   _ok, _run, cksum, write_env)
# --- registry + CLI -----------------------------------------------------------

NOOP_ENV = "FAE_VARIANT_NOOP"


def registry():
    """arm -> class, from the experiment definition (fae/cell/experiment.py)."""
    from .. import experiment as _experiment
    return _experiment.current().variants


def for_cell(cell):
    """The cell's treatment. FAE_VARIANT_NOOP=1 in the environment is the
    fixture seam: a root with no infra provisions nothing."""
    if os.environ.get(NOOP_ENV) == "1":
        return NoopVariant(cell)
    cls = registry().get(cell.treatment)
    if cls is None:
        return NoopVariant(cell)
    return cls(cell)


class _ShimCell:
    """What the operator's infra preflight (`cli.py experiment infra`)
    hands the treatment: the cell as the hooks knew it
    — cid, workspace, root, config, condition."""

    def __init__(self, cid, ws, root):
        from .. import config as _config
        self.cid = cid
        self.ws = Path(ws)
        self.root = Path(root)
        self.conf = _config.load(self.root)
        self.treatment = ""
        self.condition = os.environ.get("CONDITION", "")
        try:
            for line in (self.ws / "cell.env").read_text().splitlines():
                k, _, v = line.partition("=")
                if k == "TREATMENT":
                    self.treatment = v.strip()
                elif k == "CONDITION" and not self.condition:
                    self.condition = v.strip()
        except OSError:
            pass


def main(argv=None):
    """python3 -m fae.cell.variants <arm> setup|teardown|infra <cid> <ws>"""
    a = list(argv if argv is not None else sys.argv[1:])
    if len(a) < 2:
        print(main.__doc__, file=sys.stderr)
        return 2
    arm, hook = a[0], a[1]
    from fae import paths
    root = paths.root()
    if hook == "infra":
        cell = _ShimCell(a[2] if len(a) > 2 else "", a[3] if len(a) > 3 else "/nonexistent", root)
        cell.treatment = arm
        return 0 if for_cell(cell).infra_ok() else 1
    if len(a) < 4:
        print(main.__doc__, file=sys.stderr)
        return 2
    cell = _ShimCell(a[2], a[3], root)
    cell.treatment = arm
    t = for_cell(cell)
    if hook == "setup":
        try:
            env = t.author_setup()
        except HookFailure:
            return 1
        for k, v in env.items():
            print(f"{k}='{v}'")
        return 0
    if hook == "teardown":
        t.author_teardown()
        return 0
    print(main.__doc__, file=sys.stderr)
    return 2
