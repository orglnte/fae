"""Import shim: the one place the test suite's `runs` facade is built.

There is no runs.py any more (Milestone 6) — the orchestrator is the
fae/conduct/ and fae/cli/ packages. But the whole suite was written against one
qualified surface (`runs.host.X`, `runs.cli.X`, and a handful of bare
names — `runs.cell_id`, `runs.ROOT`, `runs.ledger`, ...) because that
discipline is what makes mock.patch.object targets stable across a
refactor. Rebuilding that surface here, once, is the one place a moved or
renamed attribute gets fixed. Where a test runs is the current Experiment
(fae.shared.set_current): use_workspace / at_workspace / patch_plane
swap it, and OrchTmpCase does it for every test that must not touch the
live fleet.

`python3 -m unittest discover -s tests` puts the repo root on sys.path
already when invoked from the root, but not when the suite is run from
elsewhere or by an IDE. Doing it explicitly keeps the tests runnable from
any cwd.
"""
import argparse
import contextlib
import importlib
import datetime as _datetime
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import timezone
from pathlib import Path
from unittest import mock

# mutmut runs the suite from a copy at <repo>/mutants/ whose source files hold
# every mutant of every function. Imports come from that copy (sys.path);
# tests that pin SOURCE TEXT read the pristine tree (ROOT), or the copy's N
# variants fail every count-the-occurrences check.
_TREE = Path(__file__).resolve().parent.parent
ROOT = _TREE.parent if _TREE.name == "mutants" else _TREE
if str(_TREE) not in sys.path:
    sys.path.insert(0, str(_TREE))
os.environ.setdefault("REPO_ROOT", str(_TREE))    # one root, one experiment: this tree
# One process, one experiment. The engine suite runs against its own fixture
# (tests/fixture_experiment); the experiment suite's shim names the real one.
_EXPERIMENT = Path(os.environ.get("FAE_TEST_EXPERIMENT") or _TREE / "tests" / "fixture_experiment")
os.environ["EXPERIMENT_DIR"] = str(_EXPERIMENT)

from fae.cli import render  # noqa: E402
from fae.experiment import check_exp as check  # noqa: E402
from fae import conduct, host  # noqa: E402
from fae.conduct import records, supervise, zombies  # noqa: E402
from fae import cli as _cli     # noqa: E402
from fae import queues as _queues_module  # noqa: E402
from fae import experiment as _experiment  # noqa: E402
from fae import shared as _shared  # noqa: E402
_experiment.load(_EXPERIMENT)
exp1 = importlib.import_module("experiment.exp1") if (_EXPERIMENT / "exp1.py").is_file() else None
from fae.experiment.scoring import validate as taint           # noqa: E402
from fae import mutex  # noqa: E402
from fae.experiment import cell_id, parse_cell_id  # noqa: E402
from fae.cell import ledger  # noqa: E402

# The facade every test file imports as `runs`: a real module object, so
# `mock.patch.object(runs.host, "sh", ...)` patches the module itself.
runs = types.ModuleType("runs")
runs.conduct = conduct
runs.exp1 = exp1
runs.cli = _cli


class _QueuesNow:
    """The current workspace's Queues: resolved at each lookup, so the plane
    a test set is always the one used."""

    def __getattr__(self, name):
        return getattr(_shared.workspace().queues, name)


runs.queues = _QueuesNow()

# No test reaches docker or the network for an agent image: a cell's image
# reads as ready (fae/cell/agent_image.py has its own tests, all mocked). An
# assignment, not a mock patch, so a test's mock.patch.stopall cannot undo it.
from fae.cell.cell import Cell as _Cell  # noqa: E402
_Cell.ready_image = lambda self, log=print: True
runs.queues_module = _queues_module
runs.render = render
runs.check = check
runs.experiment = _experiment
runs.shared = _shared
runs.host = host
runs.records = records
runs.Cell = _Cell
runs.supervise = supervise
runs.taint = taint          # fae/experiment/scoring/validate.py: the engine's taint rules


def _validate_ws(ws):
    """Validate the cell in folder `ws` the way supervision does."""
    exp = _shared.current()
    return exp.validate_cell(exp.cell(Path(ws).name, workspaces=Path(ws).parent))


runs.validate_ws = _validate_ws
runs.zombies = zombies
runs.ROOT = _shared.current().root
runs.AUTH_HINTS = render.AUTH_HINTS
runs.LIMIT_HINTS = render.LIMIT_HINTS
runs.SEAL_EXIT = _Cell.SEAL_EXIT
runs.cell_id = cell_id
runs.ledger = ledger
runs.mutex = mutex
runs.parse_cell_id = parse_cell_id
# stdlib passthroughs some tests patch or reference via runs.<name> — real
# module/function objects, identical whichever accessor reaches them.
runs.argparse = argparse
runs.datetime = _datetime.datetime
runs.timezone = timezone
runs.os = os
runs.shutil = shutil
runs.signal = signal
runs.subprocess = subprocess
runs.sys = sys
runs.time = time


def use_workspace(case, path=None, plane=None, root=None):
    """Make the cells' root `path`, the scheduling plane under `plane` and the
    experiment root `root` (each the current one when None) the current
    Experiment's for the life of the test `case`. Returns the Experiment."""
    x = _workspace_at(path, plane, root)
    case.addCleanup(_shared.set_current, _shared.set_current(x))
    return x


@contextlib.contextmanager
def at_workspace(path=None, plane=None, root=None):
    """use_workspace for a `with` block."""
    prev = _shared.set_current(_workspace_at(path, plane, root))
    try:
        yield _shared.current()
    finally:
        _shared.set_current(prev)


def _workspace_at(path, plane, root):
    cur = _shared.current()
    root = cur.root if root is None else Path(root)
    path = cur.workspace.path if path is None else path
    plane = cur.workspace.plane if plane is None else plane
    return _experiment.Experiment(root, _experiment.Workspace(root, path, plane=plane))


def patch_plane(case, plane):
    """Put the scheduling plane under a temp `plane` for the life of the test
    `case`, the cells' root unchanged, and expose case.plane / .queues /
    .conduct / .locks."""
    case.plane = plane
    case.queues, case.conduct, case.locks = plane / ".queues", plane / ".conduct", plane / ".locks"
    for d in (case.queues, case.conduct, case.locks):
        d.mkdir(parents=True, exist_ok=True)
    use_workspace(case, _shared.workspace().path, plane)


class OrchTmpCase(unittest.TestCase):
    """Base for tests that must never touch the LIVE fleet.

    setUp builds a throwaway tree and makes it the current Experiment's: the
    cells' root and the scheduling plane both under it.

    Provides self.root, self.ws (the scored root, which is also the plane's
    base here), self.queues, self.conduct and self.locks. A subclass adds its
    own patches by calling super().setUp() first, then starting its own.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir(parents=True)
        use_workspace(self, self.ws, self.ws)
        patch_plane(self, self.ws)
