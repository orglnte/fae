"""The live trace must describe the fleet that exists.

Two ways it stopped doing so on 2026-08-18, both of which leave the model
holding resources for cells that hold nothing — and a model with every slot
leaked stops enabling `AcquireSlot` for ANYBODY, so the conformance check goes
from "one broken cell" to "judging a world that does not exist" (71 violations
over 300 events, 12 with a cause of their own).

  1. A lock hook that had not sourced lib.sh skipped its transition silently:
     `declare -F emit_transition >/dev/null && emit_transition ...`. (The
     bash hooks are gone; the driver's transitions all go through Cell.apply,
     pinned in tests/test_cell_object.py.)
  2. A cell readmitted every ~32s outran the 300s grace on crash detection, so
     each respawn emitted `Spawn` while the model still had the previous loop
     live.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT, runs, OrchTmpCase

class TestACrashIsRecordedBeforeTheRespawn(OrchTmpCase):

    CID = "sonnet_high_beta_apidocs_T1_r1"

    def setUp(self):
        super().setUp()   # temp tree + WS/ORCH/TRANSITIONS_LOG patched
        self.log = self.orch / "transitions.log"

    def trace(self, *lines):
        self.log.write_text("".join(
            f"2026-08-18T00:0{i}:00Z\t{a}\t{c}\t\n"
            for i, (a, c) in enumerate(lines)))

    def actions(self):
        return [l.split("\t")[1] for l in self.log.read_text().splitlines()]

    def test_a_loop_that_ended_silently_is_crashed_first(self):
        # AcquireSlot last => the model still has this cell at `agent`, slot
        # held. Spawning on top of that is not enabled.
        self.trace(("Spawn", self.CID), ("AcquireSlot", self.CID))
        with mock.patch.object(runs.state, "loop_parents", return_value={}):
            self.assertTrue(runs.ops._crash_before_spawn(self.CID))
        self.assertEqual(self.actions()[-1], "Crash")

    def test_a_cleanly_ended_loop_is_not_crashed(self):
        # The inverse: ReleaseSlot already returned the model to `none`, and a
        # Crash on top of it would be a transition that did not happen.
        self.trace(("Spawn", self.CID), ("AcquireSlot", self.CID),
                   ("ReleaseSlot", self.CID))
        with mock.patch.object(runs.state, "loop_parents", return_value={}):
            self.assertFalse(runs.ops._crash_before_spawn(self.CID))
        self.assertNotIn("Crash", self.actions())

    def test_a_first_ever_spawn_is_not_crashed(self):
        self.trace()
        with mock.patch.object(runs.state, "loop_parents", return_value={}):
            self.assertFalse(runs.ops._crash_before_spawn(self.CID))

    def test_a_LIVE_loop_is_never_declared_crashed(self):
        # Declaring a running cell dead desyncs every later event for it —
        # the same failure, pointed the other way.
        self.trace(("Spawn", self.CID), ("AcquireSlot", self.CID))
        with mock.patch.object(runs.state, "loop_parents",
                               return_value={self.CID: 4242}):
            self.assertFalse(runs.ops._crash_before_spawn(self.CID))
        self.assertNotIn("Crash", self.actions())

    def test_it_runs_on_the_spawn_path_itself_not_only_in_supervision(self):
        # The grace that gates supervision (MODEL_DEAD_GRACE) is longer than a
        # readmission cycle, so supervision alone can never win this race.
        self.trace(("Spawn", self.CID), ("AcquireSlot", self.CID))
        (self.ws / self.CID).mkdir(parents=True)
        with mock.patch.object(runs.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            runs.ops._spawn_detached(["bash", "-c", "true"], {}, self.CID, "spawn")
        self.assertEqual(self.actions()[-1], "Crash")
        self.assertLess(runs.supervise.MODEL_DEAD_GRACE, 3600)


if __name__ == "__main__":
    unittest.main()
