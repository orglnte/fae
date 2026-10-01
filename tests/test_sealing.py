"""Sealing: a cell that reached a verdict is finished evidence, and read-only.

Two independent entry points have to refuse a sealed cell, because neither
passes through the other: `python3 -m fae.cell ...` by hand never touches
cli.py, and a queued spec never touches the driver until cli.py launches it.
So the guard exists twice and is tested twice.

Cell.seal is exercised for real on a temp workspace; the refusal ORDERING
inside the driver (before anything is written) is asserted on the source,
since reaching it for real needs docker, an agent container and credentials.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT, runs, OrchTmpCase

HARNESS = Path(ROOT) / "fae"
DRIVER = HARNESS / "driver"
CELL_PY = (HARNESS / "cell" / "cell.py").read_text()
CLI_PY = (Path(ROOT) / "fae" / "cli.py").read_text()
# fae/driver/ (where seal/reseal logic lives) + cli.py (the entry point) — the
# seal/unseal invariant must hold on every front end that can act on a cell.
OPERATOR_SURFACE = [CLI_PY] + [p.read_text() for p in DRIVER.glob("*.py")]


class TestTheSeal(unittest.TestCase):
    """Cell.seal / Cell.sealed, run for real on a temp workspace."""

    CID = "sonnet_high_beta_apidocs_T1_r1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / self.CID / "artifacts").mkdir(parents=True)
        self.ws = self.root / self.CID

    def cell(self):
        from fae.cell import Cell
        return Cell(self.CID, workspaces=self.root, root=ROOT)

    def test_an_unsealed_workspace_reads_as_unsealed(self):
        self.assertFalse(self.cell().sealed)

    def test_sealing_marks_it_and_records_the_verdict(self):
        self.assertTrue(self.cell().seal("green", attempts=4))
        marker = self.ws / ".sealed"
        self.assertTrue(marker.exists())
        body = marker.read_text()
        self.assertIn("verdict=green", body)
        self.assertIn("attempts=4", body)
        self.assertRegex(body, r"sealed=\d{4}-\d\d-\d\dT")
        self.assertTrue(self.cell().sealed)

    def test_sealing_twice_does_not_rewrite_the_first_seal(self):
        # The first verdict is the true one. A re-seal that overwrote it would
        # let a later pass relabel how a cell ended.
        self.cell().seal("green", attempts=4)
        first = (self.ws / ".sealed").read_text()
        self.assertFalse(self.cell().seal("budget", attempts=10))
        self.assertEqual((self.ws / ".sealed").read_text(), first)

    def test_the_marker_is_not_writable(self):
        self.cell().seal("green", attempts=4)
        self.assertFalse(os.access(self.ws / ".sealed", os.W_OK))


class TestReverifyAddsEvidenceInsteadOfReplacingIt(unittest.TestCase):
    """The destructive driver (reverify_cell.sh) is gone. Its replacement
    cannot overwrite a recorded result at all: the output directory is a
    parameter, and a sealed cell may only be verified into a new one."""

    def test_a_verify_that_would_write_into_the_workspace_is_refused(self):
        block = CELL_PY[CELL_PY.index("    def verify(self"):
                        CELL_PY.index("    def gate(self")]
        self.assertIn("if self.sealed and out_dir is None:", block)
        self.assertIn("raise Sealed", block)

    def test_reverify_writes_under_a_timestamped_directory(self):
        block = CELL_PY[CELL_PY.index("    def reverify(self"):
                        CELL_PY.index("    # --- the attempt loop")]
        self.assertIn('self.ws / "reverify"', block)
        self.assertIn("out_dir=out", block)
        # and never touches the ledger
        self.assertNotIn("_append", block)
        self.assertNotIn("record(", block)

    def test_the_destructive_driver_is_gone(self):
        self.assertFalse((HARNESS / "reverify_cell.sh").exists(),
                         "the in-place re-verify is back")


class SealedFleetTestCase(OrchTmpCase):
    """WS / ORCH / TRANSITIONS_LOG patched to a temp tree (via OrchTmpCase).
    Nothing here starts a process: every refusal must happen before one is
    launched."""

    def cell(self, name, ledger_lines, sealed=None):
        d = self.ws / name
        d.mkdir(parents=True)
        (d / "iterations.log").write_text(ledger_lines)
        if sealed:
            (d / ".sealed").write_text(sealed)
        return d

    GREEN = ("2026-08-01T00:00:00Z\tSTART\tc\tattempt=1\n"
             "2026-08-01T00:10:00Z\tITER\tgreen\tattempt=1\n"
             "2026-08-01T00:10:01Z\tEND\tc\tgreen=true\n")
    OPEN = ("2026-08-01T00:00:00Z\tSTART\tc\tattempt=1\n"
            "2026-08-01T00:10:00Z\tITER\tfail\tattempt=1 stage=scaling\n")


class TestNoLaunchPathStartsASealedCell(SealedFleetTestCase):

    CID = "sonnet_high_beta_apidocs_T1_r1"

    def test_spawn_is_refused_and_no_process_is_started(self):
        self.cell(self.CID, self.GREEN, sealed="sealed=x\tverdict=green\n")
        with mock.patch.object(runs.subprocess, "Popen") as popen:
            rc = runs.ops._spawn_detached([sys.executable, "-m", "fae.cell"], {},
                                      self.CID, "spawn")
        popen.assert_not_called()
        self.assertEqual(rc, runs.SEAL_EXIT)

    def test_an_unsealed_cell_is_still_spawned(self):
        # The inverse: the guard must not refuse everything.
        self.cell(self.CID, self.OPEN)
        with mock.patch.object(runs.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            runs.ops._spawn_detached(["bash", "-c", "true"], {}, self.CID, "spawn")
        popen.assert_called_once()

    def test_queueing_a_sealed_cell_is_refused(self):
        self.cell(self.CID, self.GREEN, sealed="sealed=x\tverdict=green\n")
        spec = dict(task="T1", treatment="beta", condition="apidocs",
                    rep=1, fresh=False)
        self.assertIsNone(runs.queue.enqueue("sonnet", spec))
        self.assertEqual(runs.queue.lane_specs("sonnet"), [])

    def test_queueing_an_unsealed_cell_still_works(self):
        self.cell(self.CID, self.OPEN)
        spec = dict(task="T1", treatment="beta", condition="apidocs",
                    rep=1, fresh=False)
        self.assertIsNotNone(runs.queue.enqueue("sonnet", spec))


class TestTheSealCommand(SealedFleetTestCase):

    def args(self, **kw):
        base = dict(selector="all", apply=False, verbose=False)
        base.update(kw)
        return SimpleNamespace(**base)

    BUDGET = "".join(
        f"2026-08-01T00:0{i}:00Z\tSTART\tc\tattempt={i}\n"
        f"2026-08-01T00:0{i}:30Z\tITER\t{'budget' if i == 10 else 'fail'}\t"
        f"attempt={i} stage=scaling\n" for i in range(1, 11)
    ) + "2026-08-01T01:00:00Z\tEND\tc\tgreen=false\n"

    def test_dry_by_default(self):
        self.cell("sonnet_high_beta_apidocs_T1_r1", self.GREEN)
        runs.ops.seal(self.args())
        self.assertFalse((self.ws / "sonnet_high_beta_apidocs_T1_r1"
                          / ".sealed").exists())

    def test_apply_seals_a_green_cell(self):
        d = self.cell("sonnet_high_beta_apidocs_T1_r1", self.GREEN)
        runs.ops.seal(self.args(apply=True))
        self.assertIn("verdict=green", (d / ".sealed").read_text())

    def test_apply_seals_a_budget_exhausted_cell(self):
        # 30% of finished results are these; sealing only greens would leave
        # them unprotected.
        d = self.cell("sonnet_high_beta_apidocs_T1_r2", self.BUDGET)
        runs.ops.seal(self.args(apply=True))
        self.assertIn("verdict=budget", (d / ".sealed").read_text())

    def test_an_open_cell_is_left_alone(self):
        d = self.cell("sonnet_high_beta_apidocs_T1_r3", self.OPEN)
        runs.ops.seal(self.args(apply=True))
        self.assertFalse((d / ".sealed").exists())

    def test_a_cancelled_cell_is_not_a_result(self):
        d = self.cell("sonnet_high_beta_apidocs_T1_r4", self.OPEN)
        (d / ".cancelled").write_text("operator\n")
        runs.ops.seal(self.args(apply=True))
        self.assertFalse((d / ".sealed").exists())

    def test_a_cell_with_a_live_loop_seals_itself_instead(self):
        d = self.cell("sonnet_high_beta_apidocs_T1_r5", self.GREEN)
        (d / ".loop").write_text("pid=1 cid=x phase=verify attempt=1 ts=1\n")
        with mock.patch.object(runs.state, "loop_parents",
                               return_value={d.name: 4242}):
            runs.ops.seal(self.args(apply=True))
        self.assertFalse((d / ".sealed").exists())

    def test_an_already_sealed_cell_is_not_rewritten(self):
        d = self.cell("sonnet_high_beta_apidocs_T1_r6", self.GREEN,
                      sealed="sealed=ORIGINAL\tverdict=green\tattempts=1\n")
        runs.ops.seal(self.args(apply=True))
        self.assertIn("ORIGINAL", (d / ".sealed").read_text())


class TestThereIsNoUnseal(unittest.TestCase):
    """Redoing a cell is delete-and-requeue: two-stage, and it leaves a trace.
    An unseal switch would let one stray respawn overwrite a recorded result
    with nothing to show it ever differed."""

    def test_no_command_or_flag_removes_a_seal(self):
        for src in OPERATOR_SURFACE:
            for forbidden in ('add_parser("unseal"', "--unseal", "--force-seal",
                              'command("unseal"'):
                self.assertNotIn(forbidden, src)

    def test_nothing_deletes_the_marker(self):
        # safe_wipe is the one legitimate remover, and it removes the WHOLE
        # workspace — the two-stage delete, not a quiet unseal in place.
        for src in OPERATOR_SURFACE:
            for forbidden in ("SEAL_MARKER).unlink", 'SEAL_MARKER}").unlink'):
                self.assertNotIn(forbidden, src)
        for sh_file in sorted(HARNESS.glob("*.sh")):
            self.assertNotRegex(sh_file.read_text(),
                                r'rm\s+(-f\s+)?"[^"]*\.sealed"', sh_file.name)
        self.assertNotRegex(CELL_PY, r"SEAL_MARKER\)\.unlink|\.sealed\"\)\.unlink")


if __name__ == "__main__":
    unittest.main()
