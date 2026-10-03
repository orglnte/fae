"""A Cell is read without its lock and changed only under it: one writer at a
time, the running loop included. Intent markers reach a held cell anyway; a
running loop is paused and killed by signal."""
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT
from fae import mutex
from fae.cell import cell as cellmod
from fae.cell.cell import Busy, Cell

CID = "sonnet_high_beta_apidocs_T1_r1"


class CellCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = self.root / CID
        (self.ws / "artifacts").mkdir(parents=True)
        self.log = self.root / "transitions.log"

    def cell(self):
        return Cell(CID, workspaces=self.root, root=ROOT, locks=self.root / ".locks",
                    transitions=self.log)

    def hold(self):
        """The cell's lock, taken as another process would."""
        path = self.root / ".locks" / "loop-locks" / CID
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = mutex.open_lock(path)
        self.assertTrue(mutex.try_fd(fh))
        self.addCleanup(fh.close)
        return fh

    def actions(self):
        if not self.log.exists():
            return []
        return [l.split("\t")[1] for l in self.log.read_text().splitlines()]


class TestReadingTakesNothing(CellCase):
    def test_constructing_a_cell_loads_no_configuration(self):
        with mock.patch.object(cellmod._config, "load", side_effect=AssertionError("loaded")):
            c = self.cell()
            self.assertFalse(c.sealed)
            self.assertIsNone(c.verdict)

    def test_the_configuration_loads_on_first_use(self):
        with mock.patch.object(cellmod._config, "load", return_value="conf") as load:
            c = self.cell()
            self.assertEqual((c.conf, c.conf), ("conf", "conf"))
        load.assert_called_once()

    def test_a_held_cell_is_still_readable(self):
        self.hold()
        self.assertEqual(self.cell().intent(), "run")


class TestChangingTakesTheLock(CellCase):
    def test_a_held_cell_refuses_a_seal(self):
        self.hold()
        with self.assertRaises(Busy):
            self.cell().seal("green", 1)
        self.assertFalse((self.ws / ".sealed").exists())

    def test_a_held_cell_refuses_a_derived_file(self):
        self.hold()
        with self.assertRaises(Busy):
            self.cell().write_derived("score.json", "{}")
        self.assertFalse((self.ws / "score.json").exists())

    def test_only_derived_files_are_written_that_way(self):
        with self.assertRaises(ValueError):
            self.cell().write_derived("iterations.log", "x")

    def test_the_lock_is_reentrant_for_its_holder_and_released_after(self):
        c = self.cell()
        with c.changing():
            c.seal("green", 1)
            c.write_derived("validation.json", "{}")
        self.assertTrue((self.ws / ".sealed").exists())
        self.hold()                           # free again: another process takes it

    def test_the_taints_are_carried_on_the_seal(self):
        c = self.cell()
        c.seal("green", 1)
        c.seal_taints(["a rig fault"])
        self.assertIn("taint=a rig fault", (self.ws / ".sealed").read_text())

    def test_a_heartbeat_is_not_cleared_while_a_loop_holds_the_cell(self):
        (self.ws / ".loop").write_text("x")
        self.hold()
        self.assertFalse(self.cell().clear_heartbeat())
        self.assertTrue((self.ws / ".loop").exists())


class TestIntentReachesAHeldCell(CellCase):
    def test_a_pause_reaches_a_cell_a_writer_holds(self):
        self.hold()
        self.cell().pause("manual", who="operator")
        self.assertTrue((self.ws / ".paused").read_text().startswith("manual by=operator"))
        self.assertEqual(self.actions(), ["Pause"])

    def test_killed_is_a_kill(self):
        self.cell().pause("killed")
        self.assertEqual(self.actions(), ["Kill"])
        self.assertEqual(self.cell().intent(), "killed")

    def test_unpause_records_the_resume_only_for_a_recorded_pause(self):
        c = self.cell()
        (self.ws / ".paused").write_text("raw\n")
        c.unpause()
        self.assertEqual(self.actions(), [])
        c.pause("manual")
        c.unpause()
        self.assertEqual(self.actions(), ["Pause", "Resume"])
        self.assertFalse((self.ws / ".paused").exists())

    def test_a_cancelled_cell_keeps_its_pause(self):
        c = self.cell()
        c.pause("killed")
        c.cancel()
        self.assertFalse(c.unpause())
        self.assertTrue((self.ws / ".paused").exists())

    def test_flag_and_unflag(self):
        c = self.cell()
        self.assertFalse(c.unflag())
        c.flag()
        self.assertTrue(c.unflag())
        self.assertFalse((self.ws / "reconcile.flagged").exists())


class TestControllingARunningLoop(CellCase):
    def loop(self, pid=4242):
        """The cell held by a loop with `pid`."""
        fh = self.hold()
        mutex.note_holder(fh.name, CID, pid)
        p = mock.patch.object(Cell, "_is_loop", staticmethod(lambda q: q == pid))
        p.start()
        self.addCleanup(p.stop)

    def test_the_loop_is_the_process_holding_the_lock(self):
        self.assertIsNone(self.cell().loop_pid())
        self.loop()
        self.assertEqual(self.cell().loop_pid(), 4242)

    def test_a_writer_holding_the_lock_is_not_a_loop(self):
        fh = self.hold()
        mutex.note_holder(fh.name, CID, os.getpid())
        self.assertIsNone(self.cell().loop_pid())

    def test_pausing_a_running_loop_signals_it_and_writes_nothing(self):
        self.loop()
        with mock.patch.object(cellmod.os, "kill") as kill:
            self.cell().pause("roster", who="operator")
        kill.assert_called_once_with(4242, signal.SIGUSR1)
        self.assertFalse((self.ws / ".paused").exists())
        self.assertEqual(self.actions(), ["Pause"])

    def test_the_signalled_loop_writes_the_pause_the_log_records(self):
        c = self.cell()
        c._emit("Pause", "reason=roster by=conduct")
        prev = signal.signal(signal.SIGUSR1, c._paused_by_signal)
        self.addCleanup(signal.signal, signal.SIGUSR1, prev)
        os.kill(os.getpid(), signal.SIGUSR1)
        self.assertTrue((self.ws / ".paused").read_text().startswith("roster by=conduct"))

    def test_kill_without_a_loop_is_absent(self):
        self.assertEqual(self.cell().kill(grace=0), "absent")

    def test_a_loop_that_ends_on_sigterm_is_termed(self):
        self.loop()
        with mock.patch.object(cellmod.os, "kill") as kill, \
                mock.patch.object(Cell, "_gone", staticmethod(lambda pid, grace: True)), \
                mock.patch.object(Cell, "clean_up") as clean:
            self.assertEqual(self.cell().kill(grace=0), "termed")
        kill.assert_called_once_with(4242, signal.SIGTERM)
        clean.assert_not_called()

    def test_a_loop_that_outlives_the_grace_is_killed_and_cleaned_up(self):
        self.loop()
        with mock.patch.object(cellmod.os, "kill"), \
                mock.patch.object(cellmod.os, "getpgid", return_value=7777), \
                mock.patch.object(cellmod.os, "killpg") as killpg, \
                mock.patch.object(Cell, "_gone", staticmethod(lambda pid, grace: False)), \
                mock.patch.object(Cell, "clean_up") as clean:
            self.assertEqual(self.cell().kill(grace=0), "killed")
        killpg.assert_called_once_with(7777, signal.SIGKILL)
        clean.assert_called_once()


class TestTheRunListensForThePause(unittest.TestCase):
    BODY = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()

    def test_the_handler_is_installed_before_the_lock_and_restored_after_it(self):
        run = self.BODY[self.BODY.index("    def run(self"):]
        self.assertLess(run.index("signal.signal(signal.SIGUSR1, self._paused_by_signal)"),
                        run.index("loop_lock = self.loop_lock()"))
        self.assertLess(run.index("loop_lock.close()\n"),
                        run.index("signal.signal(signal.SIGUSR1, on_pause)\n            if awake"))


if __name__ == "__main__":
    unittest.main()
