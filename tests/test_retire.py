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

TLA_VERIFY = runs.rig.tla_verify_path()
SPEC = Path(ROOT) / ".tla" / "Runs.tla"
CID = "ref_high_smoke_beta_reference_T1_r1"


def _run(t0):
    """One cell from spawn to a green verdict, starting at minute t0."""
    return [(f"2026-09-21T10:{t0:02d}:00Z", "Admit", ""),
            (f"2026-09-21T10:{t0:02d}:02Z", "AcquireVerify", ""),
            (f"2026-09-21T10:{t0 + 5:02d}:00Z", "VerifyGreen", "attempt=1")]


class TestTheLogReadersSeeANewCell(OrchTmpCase):

    def write(self, rows):
        runs.common.TRANSITIONS_LOG.write_text(
            "".join(f"{ts}\t{a}\t{CID}\t{x}\n" for ts, a, x in rows))

    def test_a_retired_cells_pause_does_not_carry_over(self):
        self.write([("2026-09-21T10:00:00Z", "Pause", ""),
                    ("2026-09-21T10:01:00Z", "Retire", "moved=/x")])
        self.assertEqual(runs.common._ledger_intent(CID), "run")

    def test_a_retired_cell_has_no_loop(self):
        self.write(_run(0)[:2] + [("2026-09-21T10:03:00Z", "Retire", "moved=/x")])
        self.assertEqual(runs.common._last_transitions()[CID][0], "Retire")
        self.assertIn("Retire", runs.common.LOOP_CLEARED_BY)


class TestTheCheckerIsFoundByEnvOrPath(unittest.TestCase):
    """No machine's layout is baked in: the environment names the checker,
    else PATH does, else there is none and the replay is skipped."""

    def _checker(self, d, name="tla_verify"):
        p = Path(d) / name
        p.write_text("#!/usr/bin/env python3\n")
        p.chmod(0o755)
        return str(p)

    def test_the_env_var_wins(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            env_one = self._checker(d, "mine")
            on_path = tempfile.mkdtemp(dir=d)
            self._checker(on_path)
            with mock.patch.dict(os.environ, {"FAE_TLA_VERIFY": env_one, "PATH": on_path}):
                self.assertEqual(runs.rig.tla_verify_path(), env_one)

    def test_else_path(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            found = self._checker(d)
            env = {k: v for k, v in os.environ.items() if k != "FAE_TLA_VERIFY"}
            with mock.patch.dict(os.environ, dict(env, PATH=d), clear=True):
                self.assertEqual(runs.rig.tla_verify_path(), found)

    def test_else_none(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items() if k != "FAE_TLA_VERIFY"}
            with mock.patch.dict(os.environ, dict(env, PATH=d), clear=True):
                self.assertIsNone(runs.rig.tla_verify_path())

    def test_a_named_file_that_does_not_exist_is_none(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"FAE_TLA_VERIFY": "/nonexistent/tla_verify", "PATH": "/nonexistent"}):
            self.assertIsNone(runs.rig.tla_verify_path())


@unittest.skipUnless(TLA_VERIFY, "no tla_verify (FAE_TLA_VERIFY or PATH)")
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
