"""PY-READINESS row 23: a verify whose load window spans a host suspend is a
rig fault — VOID, refunded — never a verdict.

The sidecar's monotonic clock stands still while the host sleeps; wall-clock
does not. The divergence between the two per tick is the detector; the
driver classifies the arrangement as `host-sleep` and refunds the attempt.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae import cell



class TestTheSidecarClockDetector(unittest.TestCase):

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "trace", Path(ROOT) / "fae" / "cell" / "contrib" / "elastic_resource" / "trace.py")
        self.sc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sc)

    def test_a_tick_that_kept_both_clocks_together_is_no_gap(self):
        self.assertEqual(self.sc.host_sleep_gap(wall_dt=1.02, mono_dt=1.0), 0.0)

    def test_a_tick_whose_wall_clock_ran_ahead_is_the_gap(self):
        # 900s of wall-clock inside a 1s monotonic tick: the host was gone
        self.assertAlmostEqual(self.sc.host_sleep_gap(wall_dt=901.0, mono_dt=1.0),
                               900.0, places=3)

    def test_small_scheduler_jitter_is_below_the_threshold(self):
        self.assertEqual(self.sc.host_sleep_gap(wall_dt=12.0, mono_dt=1.0), 0.0)
        self.assertGreater(self.sc.host_sleep_gap(wall_dt=40.0, mono_dt=1.0), 0.0)


class TestTheDriverRefundsIt(unittest.TestCase):
    """The void-matrix contract for the new stage: HALT, void-tagged
    VerifyFail, slot released, exit 45, no ITER."""

    CID = "opus_high_beta_apidocs_T1_r1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.wsdir = self.root / "workspaces"
        exp = self.root / "experiment"
        exp.mkdir(parents=True, exist_ok=True)
        (exp / "instruments").symlink_to(Path(ROOT) / "experiment" / "instruments")
        os.environ["TRANSITIONS_LOG"] = str(self.root / "transitions.log")
        os.environ["WORKSPACES_DIR"] = str(self.wsdir)
        os.environ["FAE_VARIANT_NOOP"] = "1"     # a scratch root provisions nothing
        self.addCleanup(os.environ.pop, "FAE_VARIANT_NOOP", None)
        self.addCleanup(os.environ.pop, "TRANSITIONS_LOG", None)
        self.addCleanup(os.environ.pop, "WORKSPACES_DIR", None)
        self.overlay = self.root / "overlay"
        self.overlay.mkdir()
        (self.overlay / "authored.txt").write_text("edit\n")

    def test_host_sleep_refunds_the_attempt(self):
        ws = self.wsdir / self.CID
        (ws / "artifacts").mkdir(parents=True)
        (ws / ".skeleton_manifest").touch()
        (ws / "cell.env").write_text("TASK=T1\nVARIANT=beta_apidocs\n"
                                     "REPEAT=1\n")
        c = cell.Cell(self.CID, workspaces=self.wsdir, root=self.root)
        c.prepare = lambda fresh=False: c.ws
        c.infra_ok = lambda: True
        gate = lambda: [cell.VerifyResult(green=False, shape="G2",
                                          stage_failed="host-sleep", charge=False)]
        with self.assertRaises(cell.Halt) as cm:
            c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate)
        self.assertEqual(cm.exception.code, 45)
        lines = (self.root / "transitions.log").read_text().splitlines()
        self.assertIn("void=host-sleep", lines[-2])
        self.assertNotIn("\tITER\t", (ws / "iterations.log").read_text())
