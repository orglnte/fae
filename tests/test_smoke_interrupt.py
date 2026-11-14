"""An interrupted smoke hands the cell SIGTERM and waits for its teardown:
SIGKILL (what subprocess.run sends) would leave the cell's verify running
on its host."""
import signal
import subprocess
import unittest
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.experiment import _experiment


class FakeProc:
    def __init__(self, waits):
        self.waits, self.signals, self.killed, self.returncode = list(waits), [], False, 0

    def wait(self, timeout=None):
        w = self.waits.pop(0)
        if isinstance(w, BaseException):
            raise w
        return w

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


class TestRunToTeardown(unittest.TestCase):
    def run_with(self, proc):
        with mock.patch.object(_experiment.subprocess, "Popen", return_value=proc):
            return _experiment._run_to_teardown(["cell"])

    def test_a_cell_that_ends_is_its_exit_code(self):
        p = FakeProc([0])
        self.assertEqual(self.run_with(p).returncode, 0)
        self.assertEqual(p.signals, [])

    def test_an_interrupt_gives_the_cell_sigterm_and_its_teardown_time(self):
        p = FakeProc([KeyboardInterrupt(), 0])
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(p)
        self.assertEqual(p.signals, [signal.SIGTERM])
        self.assertFalse(p.killed)

    def test_a_teardown_past_its_grace_is_killed(self):
        p = FakeProc([KeyboardInterrupt(), subprocess.TimeoutExpired("cell", 1), 0])
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(p)
        self.assertTrue(p.killed)


if __name__ == "__main__":
    unittest.main()
