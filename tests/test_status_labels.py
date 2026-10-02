"""What experiment status and the supervisor call a cell that is only waiting:
queued, or a long wait, never a crash or a stall."""
import unittest
from unittest import mock

from _ctx import runs
from test_reconcile_safety import SweepCase


def st(state, why="loop", **kw):
    return dict(cid="sonnet_high_beta_apidocs_T1_r1", state=state, why=why, **kw)


class TestARequeuedCrash(unittest.TestCase):
    def test_reads_as_queued_when_its_spec_waits_in_its_lane(self):
        with mock.patch.object(runs.state, "_queued", return_value=True):
            self.assertTrue(runs.render.requeued(st("CRASHED")))
            self.assertEqual(runs.render.display_state(st("CRASHED")), "QUEUED·interrupted")

    def test_reads_as_crashed_when_nobody_will_restart_it(self):
        with mock.patch.object(runs.state, "_queued", return_value=False):
            self.assertFalse(runs.render.requeued(st("CRASHED")))
            self.assertEqual(runs.render.display_state(st("CRASHED")), "CRASHED·loop")

    def test_other_states_are_untouched(self):
        with mock.patch.object(runs.state, "_queued", return_value=True):
            self.assertEqual(runs.render.display_state(st("PAUSED", "drain")), "PAUSED·drain")
            self.assertEqual(runs.render.display_state(st("DONE", "green")), "DONE·green")


class TestTheTriage(unittest.TestCase):
    def test_a_paused_cells_alerts_are_not_attention(self):
        body = open(runs.render.__file__).read()
        self.assertIn('s["state"] not in ("DONE", "PAUSED")', body)
        self.assertIn('if s["state"] == "CRASHED" and not requeued(s):', body)


class TestALongWaitIsNotAStall(SweepCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(runs.common._PHASE_ALERTED.clear)
        runs.common._PHASE_ALERTED.clear()

    def _alert_for(self, phase):
        limit = runs.supervise.PHASE_LIMITS[phase][0]
        with mock.patch.object(runs.state, "heartbeat",
                               return_value={"phase": phase, "phase_age": limit + 60,
                                             "age": 1.0, "attempt": 1}), \
             mock.patch.object(runs.state, "_arm_slot_of", return_value=None), \
             mock.patch.object(runs.supervise, "_reclaim"):
            self.sweep(self.st(state="WAITING", why=phase))
        return (self.cell / "iterations.log").read_text()

    def test_a_lock_wait_past_its_limit_is_a_long_wait(self):
        ledger = self._alert_for("arm-lock")
        self.assertIn("WAIT-LONG 'arm-lock'", ledger)
        self.assertNotIn("PHASE-STALLED", ledger)

    def test_a_working_phase_past_its_limit_is_still_a_stall(self):
        self.assertIn("PHASE-STALLED 'setup'", self._alert_for("setup"))


if __name__ == "__main__":
    unittest.main()
