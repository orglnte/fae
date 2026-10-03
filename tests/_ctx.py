"""Import shim: the one place the test suite's `runs` facade is built.

There is no runs.py any more (Milestone 6) — the orchestrator is the
fae/driver/ package plus cli.py. But the whole suite was written against one
qualified surface (`runs.common.WS`, `runs.host.X`, `runs.cli.X`, and a
handful of bare names — `runs.cell_id`, `runs.ROOT`, `runs.ledger`, ...)
because that discipline is what makes mock.patch.object targets stable
across a refactor. Rebuilding that surface here, once, means the ~50 test
files that read `from _ctx import runs` need no changes at all: this is
the one place a moved or renamed attribute gets fixed, exactly as
OrchTmpCase already does for the path globals below.

`python3 -m unittest discover -s tests` puts the repo root on sys.path
already when invoked from the root, but not when the suite is run from
elsewhere or by an IDE. Doing it explicitly keeps the tests runnable from
any cwd.
"""
import argparse
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

from fae.driver import check, common, conduct, render, score  # noqa: E402
from fae.driver.conduct import host, supervise, zombies  # noqa: E402
from fae import cli as _cli     # noqa: E402
from fae import queues as _queues_module  # noqa: E402
from fae import experiment as _experiment  # noqa: E402
_experiment.load(_EXPERIMENT)
exp1 = importlib.import_module("experiment.exp1") if (_EXPERIMENT / "exp1.py").is_file() else None
from fae.driver import validate as taint            # noqa: E402
from fae.driver.common import (                     # noqa: E402
    ROOT as _COMMON_ROOT, AUTH_HINTS, LIMIT_HINTS, PAUSE_EXIT_RC,
    cell_id, mutex, parse_cell_id,
)
from fae.cell import ledger  # noqa: E402

# The facade every test file imports as `runs`: a real module object (not a
# SimpleNamespace) so `mock.patch.object(runs.common, "WS", ...)` and
# `import runs; runs.common` both behave exactly as they did against the
# old flat runs.py.
runs = types.ModuleType("runs")
runs.common = common
runs.conduct = conduct
runs.exp1 = exp1
runs.cli = _cli


class _QueuesNow:
    """The driver's Queues as this test has patched the plane: resolved at
    each lookup, so a patched common.QUEUES is always the one used."""

    def __getattr__(self, name):
        return getattr(common.queues(), name)


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
runs.score = score
runs.host = host
runs.Cell = _Cell
runs.supervise = supervise
runs.taint = taint          # driver.validate's own name is "validate"
runs.zombies = zombies
runs.ROOT = _COMMON_ROOT
runs.AUTH_HINTS = AUTH_HINTS
runs.LIMIT_HINTS = LIMIT_HINTS
runs.PAUSE_EXIT_RC = PAUSE_EXIT_RC
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


def plane_globals(plane):
    """{(module, attribute): path} for every scheduling-plane global, rooted at
    `plane` the way fae/plane.py roots them at <root>/workspaces.nosync."""
    return {
        (runs.common, "QUEUES"): plane / ".queues",
        (runs.common, "CONDUCT"): plane / ".conduct",
        (runs.common, "LOCKS"): plane / ".locks",
        (runs.common, "TRANSITIONS_LOG"): plane / "transitions.log",
        (runs.common, "RECONCILE_LOG"): plane / ".conduct" / "reconcile.log",
    }


def patch_plane(case, plane):
    """Patch every scheduling-plane global to a temp `plane` for the life of
    the test `case`, and expose case.plane / .queues / .conduct / .locks."""
    case.plane = plane
    case.queues, case.conduct, case.locks = plane / ".queues", plane / ".conduct", plane / ".locks"
    for d in (case.queues, case.conduct, case.locks):
        d.mkdir(parents=True, exist_ok=True)
    for (target, attr), val in plane_globals(plane).items():
        p = mock.patch.object(target, attr, val)
        p.start()
        case.addCleanup(p.stop)


class OrchTmpCase(unittest.TestCase):
    """Base for tests that must never touch the LIVE fleet.

    setUp builds a throwaway tree and patches EVERY path global that points into
    it — the parametric workspace root and the scheduling plane — to the temp
    tree. This is the ONE place those targets are named.

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
        p = mock.patch.object(runs.common, "WS", self.ws)
        p.start()
        self.addCleanup(p.stop)
        patch_plane(self, self.ws)
