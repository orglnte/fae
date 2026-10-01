"""Import shim: the one place the test suite's `runs` facade is built.

There is no runs.py any more (Milestone 6) — the orchestrator is the
fae/driver/ package plus cli.py. But the whole suite was written against one
qualified surface (`runs.common.WS`, `runs.state.X`, `runs.ops.X`, and a
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

from fae.driver import (        # noqa: E402
    common, conduct, ops, queue, render, rig, score, state, supervise,
    weekly, zombies,
)
from fae.cell import experiment as _experiment  # noqa: E402
_experiment.load(_EXPERIMENT)
exp1 = importlib.import_module("experiment.exp1") if (_EXPERIMENT / "exp1.py").is_file() else None
from fae.driver import validate as taint            # noqa: E402
from fae.driver.common import (                     # noqa: E402
    ROOT as _COMMON_ROOT, AUTH_HINTS, LIMIT_HINTS, PAUSE_EXIT_RC, SEAL_EXIT,
    _impl_of, cell_id, ledger, mutex, parse_cell_id,
)

# The facade every test file imports as `runs`: a real module object (not a
# SimpleNamespace) so `mock.patch.object(runs.common, "WS", ...)` and
# `import runs; runs.common` both behave exactly as they did against the
# old flat runs.py.
runs = types.ModuleType("runs")
runs.common = common
runs.conduct = conduct
runs.exp1 = exp1
runs.ops = ops
runs.queue = queue
runs.render = render
runs.rig = rig
runs.score = score
runs.state = state
runs.supervise = supervise
runs.taint = taint          # driver.validate's own name is "validate"
runs.weekly = weekly
runs.zombies = zombies
runs.ROOT = _COMMON_ROOT
runs.AUTH_HINTS = AUTH_HINTS
runs.LIMIT_HINTS = LIMIT_HINTS
runs.PAUSE_EXIT_RC = PAUSE_EXIT_RC
runs.SEAL_EXIT = SEAL_EXIT
runs._impl_of = _impl_of
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


class OrchTmpCase(unittest.TestCase):
    """Base for tests that must never touch the LIVE fleet.

    setUp builds a throwaway tree and patches EVERY path global that points into
    it — the parametric workspace root, the lock plane, and the .orch logs — to
    the temp tree. This is the ONE place those targets are named: when a path
    global moves to another module (e.g. WS/ORCH now live in fae/driver/common.py),
    it is a one-line change here instead of an edit in every test file.

    Provides self.root, self.ws (the scored root) and self.orch. A subclass adds
    its own patches by calling super().setUp() first, then starting its own.
    """

    # (target-module, attribute, value-factory(self)) for each path global. A
    # target that no longer has the attribute is skipped, so this list can lead
    # a move rather than trail it.
    _ORCH_GLOBALS = (
        (runs.common, "WS", lambda s: s.ws),
        (runs.common, "ORCH", lambda s: s.orch),
        (runs.common, "TRANSITIONS_LOG", lambda s: s.orch / "transitions.log"),
        (runs.common, "RECONCILE_LOG", lambda s: s.orch / "reconcile.log"),
        (runs.ops, "RESPAWN_BOOK", lambda s: s.orch / "respawns.json"),
    )

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = self.root / "ws"
        self.orch = self.ws / ".orch"
        self.orch.mkdir(parents=True)
        for target, attr, val in self._ORCH_GLOBALS:
            if not hasattr(target, attr):
                continue
            p = mock.patch.object(target, attr, val(self))
            p.start()
            self.addCleanup(p.stop)
