"""The experiment's variants: the registry (read from the variant files,
fae/experiment/variants/files.py) and the infra a cell of one gets — its file's
`[infra] class` (fae/cell/infra/base.py), instantiated with the variant.

Slots are NOT taken here: the Conduct takes the work slot and the lock slot
(Queues.try_slots) and hands their fds to the cell process, which holds them for
its whole life (Queues.adopt_slots), so provisioning never waits on a queue.

`python3 -m fae.experiment.variants <variant> setup|teardown|infra <cid> <ws>`
is the operator's hand entry; `cli.py experiment infra` calls the classes
directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fae.cell.infra.base import AgentAccess, HookFailure, NoopInfra, liveness_declared  # noqa: F401
from .base import HARNESS, ROOT, Variant  # noqa: F401
import fae.experiment
# --- registry + CLI -----------------------------------------------------------

NOOP_ENV = "FAE_VARIANT_NOOP"


def registry():
    """id -> class, from the experiment's variant files."""
    return fae.experiment.exp().definition.variants


def infra_for(vid, cid, ws, root, conf=None, *, definition=None):
    """Instantiate the infra for variant `vid` without needing a Cell object.
    FAE_VARIANT_NOOP=1 in the environment is the fixture seam: a root with
    no infra provisions nothing."""
    from fae.experiment import config as _config
    variants = definition.variants if definition is not None else registry()
    cls = variants.get(vid)
    cfg = conf if conf is not None else _config.load(root)
    if os.environ.get(NOOP_ENV) == "1" or cls is None:
        return NoopInfra(cls or Variant, cid=cid, ws=ws, root=root, conf=cfg)
    return cls.INFRA(cls, cid=cid, ws=ws, root=root, conf=cfg)


def for_cell(cell):
    """The cell's infra: its variant's infra class, with the variant."""
    return infra_for(cell.variant, cell.cid, cell.ws, cell.root, cell.conf)


def main(argv=None):
    """python3 -m fae.experiment.variants <variant> setup|teardown|infra <cid> <ws>"""
    a = list(argv if argv is not None else sys.argv[1:])
    if len(a) < 2:
        print(main.__doc__, file=sys.stderr)
        return 2
    vid, hook = a[0], a[1]
    from fae import paths
    root = paths.root()
    if hook == "infra":
        t = infra_for(vid, a[2] if len(a) > 2 else "", a[3] if len(a) > 3 else "/nonexistent", root)
        return 0 if t.ok() else 1
    if len(a) < 4:
        print(main.__doc__, file=sys.stderr)
        return 2
    t = infra_for(vid, a[2], a[3], root)
    if hook == "setup":
        try:
            env = t.cell_setup()
        except HookFailure:
            return 1
        if isinstance(env, AgentAccess):
            env = env.to_legacy_env()
        for k, v in (env or {}).items():
            print(f"{k}='{v}'")
        return 0
    if hook == "teardown":
        t.cell_teardown()
        return 0
    print(main.__doc__, file=sys.stderr)
    return 2
