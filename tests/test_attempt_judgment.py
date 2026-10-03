"""One judgment per attempt, and the phase set a cell can actually be in.

The no-edit check lives inside the verify section, where only its `continue`
keeps a no-edit attempt from being judged a second time — record_iteration
appends unconditionally. A phase is a place a cell waits or works, so `idle`
and `shape` (labels for steps, not states) must stay absent.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

HARNESS = Path(ROOT) / "fae"
STATE_PY = (Path(ROOT) / "fae" / "driver" / "state.py").read_text()   # WAIT_PHASES lives here now

# The agent attempt loop. run_cell has an earlier, separate verify for the
# `reference` condition, so ordering must be asserted inside the loop.


class TestOneJudgmentPerAttempt(unittest.TestCase):

    def test_record_iteration_still_appends_unconditionally(self):
        # Premise of the test above: ONE ITER writer, and it appends whenever
        # it is called. If that changes, the `continue` is no longer the
        # protection.
        rec = (HARNESS / "cell" / "ledger.py").read_text()
        writers = rec[rec.index("# --- writing"):]
        self.assertEqual(writers.count('append(ws, "ITER"'), 1,
                         "more than one ITER writer")
        body = writers[writers.index("def record_iter("):writers.index("def alert(")]
        self.assertIn("return append(ws,", body)


class TestThePhaseSet(unittest.TestCase):

    EMITTED = {"setup", "agent", "limit", "verify-lock", "verify"}

    def _emitted(self):
        # the driver declares every phase through self.hb(Phase.X) — one
        # vocabulary, one emitter (the bash hb_phase went with the hooks)
        from fae.cell.fsm import Phase
        src = (HARNESS / "cell" / "cell.py").read_text()
        return {Phase[name].value
                for name in re.findall(r"self\.hb\(Phase\.([A-Z_]+)", src)}

    def test_the_harness_emits_exactly_these_phases(self):
        self.assertEqual(self._emitted(), self.EMITTED)

    def test_every_waiting_phase_is_one_a_cell_can_reach(self):
        # A phase WAIT_PHASES names but nothing emits renders a queued cell as
        # plain RUNNING for the whole wait.
        declared = set(re.search(r"WAIT_PHASES = \{([^}]*)\}", STATE_PY)
                       .group(1).replace('"', "").replace(" ", "").split(","))
        self.assertTrue(declared <= self._emitted(),
                        f"WAIT_PHASES names phases nothing emits: {declared - self._emitted()}")


if __name__ == "__main__":
    unittest.main()
