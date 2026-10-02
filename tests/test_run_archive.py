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
        self.c._treatment = mock.Mock(infra_alive=lambda: True)
        self.c._attempt = 3
        self.ws = self.c.ws

    def verify(self, verdict, exclusive=None, lock=True, during=None):
        def runner(ctx, variant, timeout_s, log_dir=None):
            out = Path(ctx.out)
            (out / "verify.log").write_text(f"run {ctx.arrangement}\n")
            (out / "trace.csv").write_text("t\n")
            (out / "cluster-diag").mkdir(exist_ok=True)
            (out / "cluster-diag" / "op.log").write_text("diag\n")
            with (Path(log_dir) / "verifier.log").open("a") as f:
                f.write(f"verifier output {ctx.arrangement}\n")
            if during:
                during(out)
            return verdict
        vcls = mock.Mock(REQUIRED_OUTPUTS=(), NOT_RUN_STAGES=frozenset(), MEASURED_STAGES=None, FILES=("verify.log", "trace.csv"),
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



class TestTheVerifyWritesOnlyItsOwnDirectory(TestRunArchive):
    """The verify's container writes <ws>/.verify-out alone; the host copies
    its declared outputs up and appends the ledger events it allows."""

    def ledger(self):
        f = self.ws / "iterations.log"
        return f.read_text().splitlines() if f.exists() else []

    def test_the_verifier_runs_in_its_own_directory_and_its_outputs_come_up(self):
        seen = []
        self.verify(Verdict(ok=True, arrangement="A"), during=seen.append)
        self.assertEqual(seen, [self.ws / ".verify-out"])
        self.assertEqual((self.ws / "verify.log").read_text(), "run A\n")
        self.assertEqual((self.ws / "cluster-diag" / "op.log").read_text(), "diag\n")
        self.assertIn("verifier output A", (self.ws / "verifier.log").read_text())

    def test_an_allowed_event_reaches_the_ledger_with_its_own_time_and_this_cells_id(self):
        (self.ws / "iterations.log").write_text("")
        from fae.cell import verify as _verify

        def during(out):
            _verify.record_event(out, "ALERT", "HOST-OVERLOADED ceiling=30qps")
            with (out / _verify.EVENTS).open("a") as f:
                f.write("2026-01-01T00:00:00Z\tVERIFY_READY\theld=10s\n")
        self.verify(Verdict(ok=True, arrangement="A"), during=during)
        lines = [l.split("\t") for l in self.ledger()]
        self.assertEqual([l[1:] for l in lines], [["ALERT", "c", "HOST-OVERLOADED ceiling=30qps"],
                                                  ["VERIFY_READY", "c", "held=10s"]])
        self.assertEqual(lines[1][0], "2026-01-01T00:00:00Z")
        self.assertFalse((self.ws / ".verify-out" / _verify.EVENTS).exists())

    def test_an_event_the_host_does_not_allow_never_reaches_the_ledger(self):
        (self.ws / "iterations.log").write_text("")
        from fae.cell import verify as _verify

        def during(out):
            with (out / _verify.EVENTS).open("a") as f:
                f.write("2026-01-01T00:00:00Z\tITER\tgreen\tattempt=3\n")
                f.write("2026-01-01T00:00:00Z\tEND\tgreen=true\n")
        self.verify(Verdict(ok=False, stage="e2e", arrangement="A"), during=during)
        self.assertEqual(self.ledger(), [])

    def test_a_reverify_records_no_events_in_the_cells_ledger(self):
        (self.ws / "iterations.log").write_text("")
        from fae.cell import verify as _verify
        out = self.ws / "reverify" / "t1"
        out.mkdir(parents=True)

        def runner(ctx, variant, timeout_s, log_dir=None):
            _verify.record_event(ctx.out, "ALERT", "LOAD-SHAPE x")
            return Verdict(ok=True, arrangement="A")
        vcls = mock.Mock(REQUIRED_OUTPUTS=(), NOT_RUN_STAGES=frozenset(), MEASURED_STAGES=None, FILES=(), FEEDBACK_LOGS=())
        with mock.patch.object(cell, "run_verifier", runner), \
                mock.patch.object(cell.Cell, "expected_fp", new_callable=mock.PropertyMock,
                                  return_value=""), \
                mock.patch.object(_experiment, "current",
                                  return_value=mock.Mock(exclusive=None, verifier_class=lambda: vcls)):
            self.c.verify(shape="A", out_dir=out)
        self.assertEqual(self.ledger(), [])

    def test_a_symlinked_output_is_not_copied_and_host_records_are_never_overwritten(self):
        (self.ws / "iterations.log").write_text("the ledger\n")

        def during(out):
            (out / "verify.log").unlink(missing_ok=True)
            (out / "verify.log").symlink_to(self.ws / "iterations.log")
            (out / "iterations.log").write_text("forged\n")
        self.verify(Verdict(ok=True, arrangement="A"), during=during)
        self.assertEqual((self.ws / "iterations.log").read_text().splitlines()[0], "the ledger")
        self.assertFalse((self.ws / "verify.log").is_symlink())



class TestAVerifyThatReachesTheRecordIsVoided(TestRunArchive):
    """Defence in depth behind the read-only mounts: the host compares the
    cell's record before and after every verify."""

    def setUp(self):
        super().setUp()
        (self.ws / "iterations.log").write_text("2026-01-01T00:00:00Z\tSTART\tc\tattempt=3\n")
        (self.ws / "cell.env").write_text("TASK=T1\n")

    def ledger(self):
        return (self.ws / "iterations.log").read_text().splitlines()

    def test_a_rewritten_ledger_voids_the_verify_and_stands_the_cell_down(self):
        from fae.cell import verify as _verify

        def during(out):
            (self.ws / "iterations.log").write_text("forged\n")
            _verify.record_event(out, "VERIFY_READY", "held=1s")
        r = self.verify(Verdict(ok=True, arrangement="A"), during=during)
        self.assertFalse(r.green)
        rec = self.record(self.runs()[-1])
        self.assertEqual((rec["stage"], rec["charge"], rec["stand_down"]),
                         ("integrity", False, ["integrity"]))
        alerts = [l for l in self.ledger() if "\tALERT\t" in l]
        self.assertEqual(len(alerts), 1)
        self.assertIn("INTEGRITY", alerts[0])
        self.assertIn("iterations.log", alerts[0])
        self.assertFalse(any("VERIFY_READY" in l for l in self.ledger()))

    def test_a_changed_cell_env_or_a_file_added_to_the_judged_tree_is_caught(self):
        for change in (lambda: (self.ws / "cell.env").write_text("TASK=T9\n"),
                       lambda: (self.ws / "artifacts" / "planted.py").write_text("x\n")):
            self.verify(Verdict(ok=True, arrangement="A"), during=lambda out: change())
            self.assertEqual(self.record(self.runs()[-1])["stage"], "integrity")

    def test_an_alert_the_supervisor_appends_meanwhile_is_not_a_change(self):
        def during(out):
            with (self.ws / "iterations.log").open("a") as f:
                f.write("2026-01-01T00:01:00Z\tALERT\tc\tPHASE-STALLED verify 3h\n")
        self.verify(Verdict(ok=True, arrangement="A"), during=during)
        self.assertEqual(self.record(self.runs()[-1])["stage"], "")
        self.assertTrue(self.record(self.runs()[-1])["ok"])

    def test_an_appended_iter_line_is_a_change(self):
        def during(out):
            with (self.ws / "iterations.log").open("a") as f:
                f.write("2026-01-01T00:01:00Z\tITER\tgreen\tattempt=3\n")
        self.verify(Verdict(ok=True, arrangement="A"), during=during)
        self.assertEqual(self.record(self.runs()[-1])["stage"], "integrity")


if __name__ == "__main__":
    unittest.main()



class TestRequiredOutputs(unittest.TestCase):
    """An experiment's REQUIRED_OUTPUTS: always copied up; one a verify that
    ran did not write stands the cell down for the operator (a rig defect is
    the same on every retry)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        (d / "ws" / "c" / "artifacts").mkdir(parents=True)
        self.c = cell.Cell("c", workspaces=d / "ws", root=ROOT)
        self.c._treatment = mock.Mock(infra_alive=lambda: True)
        self.c._attempt = 2
        self.ws = self.c.ws
        (self.ws / "iterations.log").write_text("")

    def verify(self, writes, verdict=None, required=("verify.log", "resources.json")):
        def runner(ctx, variant, timeout_s, log_dir=None):
            for name in writes:
                (Path(ctx.out) / name).write_text("x\n")
            return verdict or Verdict(ok=True, arrangement="A")
        vcls = mock.Mock(REQUIRED_OUTPUTS=required, NOT_RUN_STAGES=frozenset({"verifier"}),
                         MEASURED_STAGES=None, FILES=(), FEEDBACK_LOGS=())
        with mock.patch.object(cell, "run_verifier", runner), \
                mock.patch.object(cell.Cell, "expected_fp", new_callable=mock.PropertyMock,
                                  return_value=""), \
                mock.patch.object(_experiment, "current",
                                  return_value=mock.Mock(exclusive=None, verifier_class=lambda: vcls)):
            return self.c.verify(shape="A")

    def last(self):
        base = self.ws / "arrangements"
        return json.loads((sorted(base.iterdir())[-1] / "verdict.json").read_text())

    def test_every_required_output_present_is_judged_copied_up_and_archived(self):
        self.assertTrue(self.verify(["verify.log", "resources.json"]).green)
        self.assertTrue((self.ws / "resources.json").is_file())
        self.assertIn("resources.json", self.last()["files"])

    def test_a_missing_one_stands_the_cell_down_uncharged(self):
        self.verify(["verify.log"])
        rec = self.last()
        self.assertEqual((rec["stage"], rec["charge"], rec["stand_down"]),
                         ("rig-output", False, ["rig-output"]))
        self.assertIn("RIG-OUTPUT the verify left no resources.json",
                      (self.ws / "iterations.log").read_text())

    def test_one_left_by_an_earlier_verify_does_not_count(self):
        import os
        stale = self.ws / ".verify-out" / "resources.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("old\n")
        os.utime(stale, (1, 1))
        self.verify(["verify.log"])
        self.assertEqual(self.last()["stage"], "rig-output")

    def test_a_verifier_that_never_ran_to_its_end_is_not_asked_for_them(self):
        self.verify([], verdict=Verdict(ok=False, stage="verifier", charge=False, arrangement="A"))
        self.assertEqual(self.last()["stage"], "verifier")
