"""A wiped workspace retires its cell: the id's next events are a new cell.

`prepare(fresh=True)` moves the workspace aside and the same id starts over
(every smoke; `queue-add --fresh`). The transitions log carries a `Retire`
line there, and every reader of the log must treat what follows as a new
cell — the replay above all, which otherwise judges the new cell's Spawn
against the old cell's verdict and cascades on every later event.
"""
import subprocess
import unittest
from pathlib import Path

from _ctx import ROOT, OrchTmpCase, runs
from fae.cell.fsm import LOOP_CLEARED_BY

TLA_VERIFY = runs.check.tla_verify_path()
SPEC = Path(ROOT) / ".tla" / "Runs.tla"
CID = "ref_high_smoke_beta_reference_T1_r1"


def _run(t0):
    """One cell from spawn to a green verdict, starting at minute t0."""
    return [(f"2026-09-21T10:{t0:02d}:00Z", "Admit", ""),
            (f"2026-09-21T10:{t0:02d}:02Z", "AcquireVerify", ""),
            (f"2026-09-21T10:{t0 + 5:02d}:00Z", "VerifyGreen", "attempt=1")]


class TestTheLogReadersSeeANewCell(OrchTmpCase):

    def write(self, rows):
        runs.shared.workspace().transitions.write_text(
            "".join(f"{ts}\t{a}\t{CID}\t{x}\n" for ts, a, x in rows))

    def test_a_retired_cells_pause_does_not_carry_over(self):
        self.write([("2026-09-21T10:00:00Z", "Pause", ""),
                    ("2026-09-21T10:01:00Z", "Retire", "moved=/x")])
        self.assertEqual(runs.shared.current().cell(CID).intent(), "run")

    def test_a_retired_cell_has_no_loop(self):
        self.write(_run(0)[:2] + [("2026-09-21T10:03:00Z", "Retire", "moved=/x")])
        self.assertEqual(runs.host.last_transitions()[CID][0], "Retire")
        self.assertIn("Retire", LOOP_CLEARED_BY)


class TestTheCheckerIsTheEnvsElseFaes(unittest.TestCase):
    """The environment may name another checker; else fae's own runs, and
    no PATH lookup can pick up a different one."""

    def test_the_env_var_wins(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            mine = Path(d) / "mine"
            mine.write_text("#!/usr/bin/env python3\n")
            with mock.patch.dict(os.environ, {"FAE_TLA_VERIFY": str(mine)}):
                self.assertEqual(runs.check.tla_verify_path(), str(mine))

    def test_else_the_one_fae_ships(self):
        import os
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "FAE_TLA_VERIFY"}
        with mock.patch.dict(os.environ, dict(env, PATH="/nonexistent"), clear=True):
            self.assertEqual(Path(runs.check.tla_verify_path()),
                             Path(ROOT) / "fae" / "utils" / "tla_verify.py")

    def test_a_named_file_that_does_not_exist_is_none(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"FAE_TLA_VERIFY": "/nonexistent/tla_verify"}):
            self.assertIsNone(runs.check.tla_verify_path())


class TestTheReplayJudgesTheNewCellFromInit(unittest.TestCase):

    def replay(self, rows, tmp):
        log = Path(tmp) / "transitions.log"
        log.write_text("".join(f"{ts}\t{a}\t{CID}\t{x}\n" for ts, a, x in rows))
        r = subprocess.run(["python3", TLA_VERIFY, "--live-trace", str(log), str(SPEC)],
                           capture_output=True, text=True, timeout=120)
        return r.stdout + r.stderr

    def test_a_reused_id_after_retire_is_conformant(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self.replay(_run(0) + [("2026-09-21T10:20:00Z", "Retire", "moved=/x")]
                              + _run(30), tmp)
        self.assertIn("0 violations", out)
        self.assertIn("2 cell(s)", out)

    def test_without_the_retire_the_reuse_is_a_violation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self.replay(_run(0) + _run(30), tmp)
        self.assertIn("VIOLATION", out)
        self.assertIn("Admit", out)


if __name__ == "__main__":
    unittest.main()
