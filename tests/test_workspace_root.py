"""The parametric workspace root — placement moves, the lock plane does not.

WORKSPACES_DIR names an alternative workspace tree (ws-test.nosync for
harness-validation cells). The invariant these tests hold: only cell
PLACEMENT follows the override — the scheduling plane (queues, locks, slots,
transitions.log) serializes the one physical rig and stays global — and the
safe_wipe boundary is per-root, so no root can reach into another's cells.
The choke point is config.load (fae/experiment/config.py resolves the workspace
root through it); the wipe itself is fae/cell/prepare.py.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT, runs, at_workspace, use_workspace

from fae.experiment import config as C


class TestThePythonOrchestratorKnob(unittest.TestCase):

    def test_ws_honors_the_environment(self):
        with mock.patch.dict(os.environ, {"WORKSPACES_DIR": "/x/ws-test.nosync"}):
            w = runs.experiment._experiment.Workspace(Path("/r"))
        self.assertEqual(w.path, Path("/x/ws-test.nosync"))

    def test_the_lock_plane_does_not_follow(self):
        # the override moves cells, not the plane: that stays the root's
        with mock.patch.dict(os.environ, {"WORKSPACES_DIR": "/x/ws-test.nosync"}):
            w = runs.experiment._experiment.Workspace(Path("/r"))
        self.assertEqual(w.plane, Path("/r/workspaces.nosync"))
        self.assertEqual((w.conduct, w.locks), (w.plane / ".conduct", w.plane / ".locks"))
        self.assertEqual(w.queues.base, w.plane / ".queues")

    def test_without_the_environment_the_cells_live_on_the_plane(self):
        env = {k: v for k, v in os.environ.items() if k != "WORKSPACES_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            w = runs.experiment._experiment.Workspace(Path("/r"))
        self.assertEqual(w.path, Path("/r/workspaces.nosync"))

    def test_loop_pids_matches_the_active_root(self):
        with at_workspace(Path("/x/ws-test.nosync")), \
             mock.patch.object(runs.host, "sh", return_value=(
                "77 tee -a /x/ws-test.nosync/opus_high_beta_apidocs_T1_r99/run_cell.log\n"
                "78 tee -a /x/workspaces.nosync/opus_high_beta_apidocs_T1_r1/run_cell.log\n")):
            pids = runs.host.loop_pids()
        # this invocation's root is seen; the OTHER root's loop is not claimed
        self.assertEqual(pids, {77: "opus_high_beta_apidocs_T1_r99"})


class TestThePythonChokePoint(unittest.TestCase):

    def _load(self, extra_env):
        env = dict(os.environ)
        env.pop("WORKSPACES_DIR", None)
        env.pop("SMOKE", None)
        env.update(extra_env)
        C._cache.clear()
        return C.load(ROOT, env=env).get("WORKSPACES_DIR")

    def test_the_config_name_is_the_default(self):
        self.assertTrue(self._load({}).endswith("workspaces.nosync"))

    def test_an_inbound_root_survives_load_config(self):
        self.assertEqual(self._load({"WORKSPACES_DIR": "/x/ws-test.nosync"}),
                         "/x/ws-test.nosync")

    def test_smoke_outranks_the_inbound_root(self):
        # a smoke test must never be able to point itself at a scored tree
        got = self._load({"WORKSPACES_DIR": "/x/ws-test.nosync", "SMOKE": "1"})
        self.assertTrue(got.endswith("ws-smoke.nosync"), got)


class TestTheCellHonorsTheRoot(unittest.TestCase):

    def test_main_falls_through_to_the_environment(self):
        # fae.cell.__main__ builds Cell(cid) bare; the root must arrive
        # via env WORKSPACES_DIR (cell.py's documented fallback chain).
        from fae import cell as cellmod
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws-test"
            (ws / "x_high_beta_apidocs_T1_r99").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"WORKSPACES_DIR": str(ws)}):
                c = cellmod.Cell("x_high_beta_apidocs_T1_r99")
            self.assertEqual(c.workspaces, ws)


class TestTheWipeBoundaryIsPerRoot(unittest.TestCase):

    def test_py_safe_wipe_refuses_a_cross_root_target(self):
        from fae.cell import prepare
        with tempfile.TemporaryDirectory() as d:
            scored = Path(d) / "workspaces.nosync"
            test_root = Path(d) / "ws-test.nosync"
            victim = scored / "opus_high_beta_apidocs_T1_r1"
            victim.mkdir(parents=True)
            test_root.mkdir()
            with self.assertRaises(ValueError):
                prepare.safe_wipe(victim, test_root)
            self.assertTrue(victim.exists())


