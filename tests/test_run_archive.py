"""Every verify run leaves arrangements/NN-a<attempt>-<label>-<end state>/
with its logs, the verifier's own output for that run and verdict.json:
green, charged and refunded runs, runs whose verdict names no files, and runs
that never returned."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

from fae.cell import cell
from fae.cell import experiment as _experiment
from fae.cell.verify import Verdict


class TestRunArchive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        (d / "ws" / "c" / "artifacts").mkdir(parents=True)
        self.c = cell.Cell("c", workspaces=d / "ws", root=ROOT)
        self.c._treatment = mock.Mock(substrate_alive=lambda: True)
        self.c._attempt = 3
        self.ws = self.c.ws

    def verify(self, verdict, exclusive=None, lock=True):
        def runner(ctx, variant, timeout_s):
            (self.ws / "verify.log").write_text(f"run {ctx.arrangement}\n")
            (self.ws / "trace.csv").write_text("t\n")
            (self.ws / "cluster-diag").mkdir(exist_ok=True)
            (self.ws / "cluster-diag" / "op.log").write_text("diag\n")
            with (self.ws / "verifier.log").open("a") as f:
                f.write(f"verifier output {ctx.arrangement}\n")
            return verdict
        vcls = mock.Mock(MEASURED_STAGES=None, FILES=("verify.log", "trace.csv"),
                         FEEDBACK_LOGS=("verify.log", "cluster-diag", "k6.log"))
        definition = mock.Mock(exclusive=exclusive, verifier_class=lambda: vcls)
        with mock.patch.object(cell, "run_verifier", runner), \
                mock.patch.object(cell.Cell, "expected_fp", new_callable=mock.PropertyMock,
                                  return_value=""), \
                mock.patch.object(cell.Cell, "exclusive_acquire",
                                  return_value=mock.Mock() if lock else None), \
                mock.patch.object(_experiment, "current", return_value=definition):
            return self.c.verify(shape=verdict.arrangement)

    def runs(self):
        return sorted(p.name for p in (self.ws / "arrangements").iterdir())

    def record(self, name):
        return json.loads((self.ws / "arrangements" / name / "verdict.json").read_text())

    def test_each_end_state_names_its_folder_and_its_verdict(self):
        self.verify(Verdict(ok=True, arrangement="A"))
        self.verify(Verdict(ok=False, stage="e2e", why="500", arrangement="B"))
        self.verify(Verdict(ok=False, stage="verifier-timeout", charge=False, arrangement="C"))
        self.assertEqual(self.runs(), ["01-a3-A-green", "02-a3-B-charged", "03-a3-C-refunded"])
        r = self.record("02-a3-B-charged")
        self.assertEqual((r["end_state"], r["refunded"], r["stage"], r["why"], r["attempt"]),
                         ("charged", False, "e2e", "500", 3))
        self.assertTrue(self.record("03-a3-C-refunded")["refunded"])

    def test_a_verdict_without_files_still_archives_the_declared_logs_and_directories(self):
        self.verify(Verdict(ok=False, stage="verifier", charge=False, arrangement="A"))
        d = self.ws / "arrangements" / "01-a3-A-refunded"
        self.assertEqual((d / "verify.log").read_text(), "run A\n")
        self.assertEqual((d / "cluster-diag" / "op.log").read_text(), "diag\n")
        self.assertTrue((d / "trace.csv").is_file())

    def test_the_verifier_output_is_this_runs_slice_only(self):
        self.verify(Verdict(ok=True, arrangement="A"))
        self.verify(Verdict(ok=True, arrangement="B"))
        d = self.ws / "arrangements" / "02-a3-B-green"
        self.assertEqual((d / "verifier.log").read_text(), "verifier output B\n")

    def test_a_run_that_never_returned_is_archived_at_the_next_verify(self):
        log = self.ws / "verifier.log"
        log.write_text("earlier\n")
        (self.ws / cell.Cell.INFLIGHT).write_text(json.dumps(
            {"attempt": 2, "arrangement": "A", "started": "t0",
             "verifier_log_offset": log.stat().st_size}) + "\n")
        (self.ws / "verify.log").write_text("half a run\n")
        with log.open("a") as f:
            f.write("killed here\n")
        self.verify(Verdict(ok=True, arrangement="B"))
        self.assertEqual(self.runs(), ["01-a2-A-interrupted", "02-a3-B-green"])
        d = self.ws / "arrangements" / "01-a2-A-interrupted"
        self.assertEqual((d / "verify.log").read_text(), "half a run\n")
        self.assertEqual((d / "verifier.log").read_text(), "killed here\n")
        r = self.record("01-a2-A-interrupted")
        self.assertEqual((r["end_state"], r["refunded"], r["started"]), ("interrupted", True, "t0"))
        self.assertFalse((self.ws / cell.Cell.INFLIGHT).exists())

    def test_a_run_that_never_got_the_lock_is_archived_as_refunded(self):
        self.verify(Verdict(ok=True, arrangement="A"), exclusive="rig", lock=False)
        self.assertEqual(self.runs(), ["01-a3-A-refunded"])
        self.assertEqual(self.record("01-a3-A-refunded")["stage"], "exclusive-lock")

    def test_a_log_older_than_the_run_is_not_archived_as_its_own(self):
        import os
        (self.ws / "k6.log").write_text("previous run\n")
        os.utime(self.ws / "k6.log", (1, 1))
        self.verify(Verdict(ok=False, stage="verifier-timeout", charge=False, arrangement="A"))
        d = self.ws / "arrangements" / "01-a3-A-refunded"
        self.assertTrue((d / "verify.log").is_file())
        self.assertFalse((d / "k6.log").exists())


if __name__ == "__main__":
    unittest.main()

