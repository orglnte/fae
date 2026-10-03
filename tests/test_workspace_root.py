"""The parametric workspace root — placement moves, the lock plane does not.

WORKSPACES_DIR names an alternative workspace tree (ws-test.nosync for
harness-validation cells). The invariant these tests hold: only cell
PLACEMENT follows the override — the scheduling plane (queues, locks, slots,
transitions.log) serializes the one physical rig and stays global — and the
safe_wipe boundary is per-root, so no root can reach into another's cells.
The choke point is config.load (fae/cell/config.py resolves the workspace
root through it); the wipe itself is fae/cell/prepare.py.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT, runs

from fae.cell import config as C


class TestThePythonOrchestratorKnob(unittest.TestCase):

    def test_ws_honors_the_environment(self):
        src = (Path(ROOT) / "fae" / "driver" / "common.py").read_text()
        i = src.index("The WORKSPACE root is parametric")
        block = src[i:i + 1200]
        self.assertIn('os.environ.get("WORKSPACES_DIR")', block)
        self.assertIn('WS = Path(os.environ["WORKSPACES_DIR"])', block)

    def test_the_lock_plane_does_not_follow(self):
        # The plane is derived from ROOT and a literal, never from WS: the
        # override moves cells, not locks.
        src = (Path(ROOT) / "fae" / "driver" / "common.py").read_text()
        for name in ("QUEUES", "CONDUCT", "LOCKS"):
            line = [l for l in src.splitlines() if l.startswith(f"{name} = ")][0]
            self.assertIn("(ROOT)", line)
            self.assertNotIn("WS", line.split("=", 1)[1])
        plane = (Path(ROOT) / "fae" / "plane.py").read_text()
        self.assertIn('Path(root) / "workspaces.nosync"', plane)

    def test_loop_pids_matches_the_active_root(self):
        with mock.patch.object(runs.common, "WS", Path("/x/ws-test.nosync")), \
             mock.patch.object(runs.common, "sh", return_value=(
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
        self.assertTrue(got.endswith("smoke-workspaces.nosync"), got)


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


