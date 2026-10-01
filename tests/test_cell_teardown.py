"""A failed or partial setup must still release everything it took.

The verify releases the rig on every path (a `finally:`), never on the path
that could not take it; the driver's run loop tears down on its unconditional
path (tests/test_one_process.py pins that one). The bash callers this file
once covered — smoke.sh's trap, the _work_slots.sh holder check — went with
the bash harness: slots are taken and released by the driver's own fds, and
holder notes are cleared by Cell.release_slot (tests/test_cell_object.py).
"""
import re
import unittest
from pathlib import Path

from _ctx import ROOT


class TestTheVerifyReleasesOnEveryPath(unittest.TestCase):

    def test_the_engine_releases_the_exclusive_lock_after_the_verifier(self):
        # The lock is the cell's own fd, released LAST: the verifier still
        # holds the rig while it tears down the substrate the lock is capping.
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        block = src[src.index("    def verify(self"):src.index("    def _persist_verdict")]
        tail = block[block.index("try:"):]
        self.assertIn("run_verifier(", tail)
        self.assertIn("finally:", tail)
        self.assertGreater(tail.index("fh.close()"), tail.index("run_verifier("))

    def test_a_failed_exclusive_lock_runs_no_verifier(self):
        # The store and cache belong to the OTHER run that holds the lock.
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        block = src[src.index("    def verify(self"):src.index("    def _persist_verdict")]
        block = block[block.index("if fh is None:"):block.index("try:")]
        self.assertIn('stage="exclusive-lock", charge=False', block)
        self.assertIn("return VerifyResult", block)
        self.assertNotIn("run_verifier(", block)


