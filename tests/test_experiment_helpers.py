"""Experiment verb helpers (conduct_pid, run_active, with_verify_lock) and
Cell.SLEEP_GAP_S keep experiment commands off fae.conduct and fae.cell.host_sleep."""
import unittest
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401

import fae.conduct
import fae.experiment
from fae.cell import host_sleep
from fae.cell.cell import Cell


class TestExperimentVerbHelpers(unittest.TestCase):
    def test_sleep_gap_s_exposed_on_cell(self):
        self.assertEqual(Cell.SLEEP_GAP_S, host_sleep.GAP_S)

    def test_conduct_pid_and_run_active_delegate_to_conduct(self):
        exp = fae.experiment.exp()
        with mock.patch.object(fae.conduct.Conduct, "pid", return_value=12345):
            self.assertEqual(exp.conduct_pid(), 12345)
            self.assertTrue(exp.run_active())

        with mock.patch.object(fae.conduct.Conduct, "pid", return_value=None):
            self.assertIsNone(exp.conduct_pid())
            self.assertFalse(exp.run_active())

    def test_with_verify_lock_releases_on_normal_and_error_exit(self):
        exp = fae.experiment.exp()
        closed = []
        lock = SimpleNamespace(close=lambda: closed.append("closed"))
        cell = SimpleNamespace(cid="c1", verify_lock_acquire=lambda poll=5.0: lock)

        with exp.with_verify_lock(cell) as held:
            self.assertIs(held, lock)
        self.assertEqual(closed, ["closed"])

        with self.assertRaises(ValueError):
            with exp.with_verify_lock(cell):
                raise ValueError("fail")
        self.assertEqual(closed, ["closed", "closed"])

    def test_with_verify_lock_raises_when_stood_down(self):
        exp = fae.experiment.exp()
        cell = SimpleNamespace(cid="c2", verify_lock_acquire=lambda poll=5.0: None)
        with self.assertRaises(RuntimeError) as ctx:
            with exp.with_verify_lock(cell):
                pass
        self.assertIn("c2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
