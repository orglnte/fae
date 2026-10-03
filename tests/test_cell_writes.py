"""A Cell is read without its lock and changed only under it: one writer at a
time, the running loop included. Intent markers reach a held cell anyway."""
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
    def test_a_pause_request_reaches_a_running_cell(self):
        self.hold()
        self.cell().request_pause("manual", who="operator")
        self.assertTrue((self.ws / ".paused").read_text().startswith("manual by=operator"))
        self.assertEqual(self.actions(), ["Pause"])

    def test_killed_is_a_kill(self):
        self.cell().request_pause("killed")
        self.assertEqual(self.actions(), ["Kill"])
        self.assertEqual(self.cell().intent(), "killed")

    def test_unpause_records_the_resume_only_for_a_recorded_pause(self):
        c = self.cell()
        (self.ws / ".paused").write_text("raw\n")
        c.unpause()
        self.assertEqual(self.actions(), [])
        c.request_pause("manual")
        c.unpause()
        self.assertEqual(self.actions(), ["Pause", "Resume"])
        self.assertFalse((self.ws / ".paused").exists())

    def test_a_cancelled_cell_keeps_its_pause(self):
        c = self.cell()
        c.request_pause("killed")
        c.cancel()
        self.assertFalse(c.unpause())
        self.assertTrue((self.ws / ".paused").exists())

    def test_flag_and_unflag(self):
        c = self.cell()
        self.assertFalse(c.unflag())
        c.flag()
        self.assertTrue(c.unflag())
        self.assertFalse((self.ws / "reconcile.flagged").exists())


if __name__ == "__main__":
    unittest.main()
