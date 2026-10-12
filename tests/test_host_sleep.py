"""A host sleep is read off the two clocks: the monotonic clock stands still
while the host sleeps and the wall clock does not. An arrangement that spans
one is a rig fault, VOID and refunded, never a verdict; a wake is trusted
only once it outlasts a dark wake.
"""
import os
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae import cell
from fae.cell import host_sleep
from fae.cell.host_sleep import Reading


class TestTheClockDetector(unittest.TestCase):

    def test_an_interval_that_kept_both_clocks_together_is_no_gap(self):
        self.assertEqual(host_sleep.gap(wall_dt=1.02, mono_dt=1.0), 0.0)

    def test_an_interval_whose_wall_clock_ran_ahead_is_the_gap(self):
        # 900s of wall-clock inside a 1s monotonic tick: the host was gone
        self.assertAlmostEqual(host_sleep.gap(wall_dt=901.0, mono_dt=1.0), 900.0, places=3)

    def test_small_scheduler_jitter_is_below_the_threshold(self):
        self.assertEqual(host_sleep.gap(wall_dt=12.0, mono_dt=1.0), 0.0)
        self.assertGreater(host_sleep.gap(wall_dt=40.0, mono_dt=1.0), 0.0)

    def test_slept_compares_two_readings(self):
        self.assertEqual(host_sleep.slept(Reading(1000.0, 10.0), Reading(1300.0, 310.0)), 0.0)
        self.assertEqual(host_sleep.slept(Reading(1000.0, 10.0), Reading(4900.0, 310.0)), 3600.0)


class Clock:
    """Both clocks of a host, advanced by hand: `pause` is awake time,
    `sleep_for` a suspend."""

    def __init__(self):
        self.wall, self.mono, self.pauses = 1000.0, 10.0, []
        self.suspends = {}          # pause index -> seconds asleep inside it

    def read(self):
        return Reading(self.wall, self.mono)

    def pause(self, s):
        asleep = self.suspends.get(len(self.pauses), 0.0)
        self.pauses.append(s)
        self.wall += s + asleep
        self.mono += s


class TestWaitingForAWake(unittest.TestCase):

    def test_an_awake_host_waits_the_settle_time_and_no_longer(self):
        c = Clock()
        waited = host_sleep.wait_awake(awake_s=120, poll_s=5, read=c.read, pause=c.pause)
        self.assertEqual(sum(c.pauses), 120)
        self.assertEqual(waited, 120)

    def test_a_sleep_inside_the_wait_starts_the_count_again(self):
        # awake 40s (a dark wake), asleep 600s, then up for good
        c = Clock()
        c.suspends = {8: 600.0}
        waited = host_sleep.wait_awake(awake_s=120, poll_s=5, read=c.read, pause=c.pause)
        self.assertEqual(sum(c.pauses), 45 + 120)
        self.assertEqual(waited, 45 + 120 + 600)

    def test_dark_wakes_never_end_the_wait(self):
        # asleep 300s after every 45s awake, seven times, then up for good:
        # the wait ends 120s after the last of them, not after any dark wake
        c = Clock()
        c.suspends = {i: 300.0 for i in range(0, 60, 9)}
        waited = host_sleep.wait_awake(awake_s=120, poll_s=5, read=c.read, pause=c.pause)
        self.assertEqual(len(c.pauses), 55 + 24)
        self.assertEqual(waited, 5 * (55 + 24) + 7 * 300.0)


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
