"""A cell's files are read through Cell, and transitions.log is only ever
appended to: Cell's records, and the EPOCH block a re-anchor appends."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import OrchTmpCase, runs

from fae.cell.cell import Cell

CID = "opus_high_beta_apidocs_T1_r1"


class CellCase(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.ws = self.root / "ws" / CID
        self.ws.mkdir(parents=True)
        self.log = self.root / "transitions.log"

    def cell(self):
        return Cell(CID, workspaces=self.ws.parent, root=self.root, transitions=self.log)


class TestTheMarkersAsRead(CellCase):

    def test_a_stop_request_and_its_detail(self):
        (self.ws / ".paused").write_text("manual by=driver at=2026-10-01T00:00:00Z\n"
                                         "e2e violated twice\n")
        r = self.cell().pause_request()
        self.assertEqual((r.reason, r.who, r.at, r.detail),
                         ("manual", "driver", 1790812800.0, "e2e violated twice"))

    def test_an_empty_request_still_stands(self):
        (self.ws / ".paused").write_text("")
        c = self.cell()
        self.assertTrue(c.paused)
        self.assertIsNone(c.pause_request().reason)

    def test_no_request(self):
        c = self.cell()
        self.assertFalse(c.paused)
        self.assertIsNone(c.pause_request())

    def test_cancelled_and_flagged(self):
        c = self.cell()
        self.assertFalse(c.cancelled or c.flagged)
        c.cancel()
        c.flag()
        self.assertTrue(c.cancelled and c.flagged)

    def test_the_heartbeat_carries_its_mtime(self):
        self.assertIsNone(self.cell().heartbeat())
        (self.ws / ".loop").write_text("pid=12 cid=x phase=agent\n")
        os.utime(self.ws / ".loop", (100, 100))
        self.assertEqual(self.cell().heartbeat(),
                         {"pid": "12", "cid": "x", "phase": "agent", "mtime": 100.0})


class TestTheRecordAsRead(CellCase):

    def test_env_ledger_and_their_mtimes(self):
        c = self.cell()
        self.assertEqual((c.env, c.has_ledger, c.ledger_text()), ({}, False, ""))
        self.assertEqual(c.mtimes(), {"env": None, "ledger": None, "metrics": None})
        (self.ws / "cell.env").write_text("TASK=T1\nIMPL=fae\n")
        (self.ws / "iterations.log").write_text("2026-10-01T00:00:00Z\tITER\tgreen\tattempt=1\n")
        c = self.cell()
        self.assertEqual(c.env, {"TASK": "T1", "IMPL": "fae"})
        self.assertEqual(c.read_ledger()["iters"], ["green"])
        self.assertIsNotNone(c.mtimes()["ledger"])

    def test_only_a_derived_file_is_read_as_one(self):
        c = self.cell()
        self.assertIsNone(c.read_derived("validation.json"))
        with self.assertRaises(ValueError):
            c.read_derived("iterations.log")

    def test_the_seal_stamp(self):
        c = self.cell()
        self.assertIsNone(c.sealed_at)
        c.seal("green", attempts=1)
        self.assertRegex(c.sealed_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class TestTheLogIsAppendedTo(CellCase):

    def rows(self):
        return [l.split("\t") for l in self.log.read_text().splitlines()]

    def test_crash_and_retire_append_one_line_each(self):
        self.log.write_text("earlier\n")
        c = self.cell()
        c.crashed("loop-vanished")
        c.retired("/x/.to_be_deleted/1/" + CID)
        rows = self.rows()
        self.assertEqual(rows[0], ["earlier"])
        self.assertEqual([r[1:] for r in rows[1:]],
                         [["Crash", CID, "loop-vanished"],
                          ["Retire", CID, f"moved=/x/.to_be_deleted/1/{CID}"]])

    def test_a_reanchor_is_one_block_after_what_was_there(self):
        self.log.write_text("earlier\n")
        Cell.reanchor([("a", "outcome=none"), ("b", "outcome=green")],
                      root=self.root, transitions=self.log)
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines[0], "earlier")
        self.assertTrue(lines[1].startswith("# "))
        self.assertEqual([l.split("\t")[1:] for l in lines[2:]],
                         [["EPOCH", "a", "outcome=none"], ["EPOCH", "b", "outcome=green"]])


class TestTraceResetAppends(OrchTmpCase):

    def test_the_log_is_kept_and_the_epoch_block_follows_it(self):
        log = runs.common.TRANSITIONS_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"2026-10-01T00:00:00Z\tAdmit\t{CID}\t\n")
        cells = [dict(cid=CID, outcome="green", intent="paused", loop="none", attempts=1,
                      slot=False, verify=False),
                 dict(cid="opus_high_beta_apidocs_T1_r2", outcome="none", intent="run",
                      loop="none", attempts=0, slot=False, verify=False)]
        with mock.patch.object(runs.rig, "_observed_epoch", return_value=cells):
            runs.rig.trace_reset(SimpleNamespace(dry_run=False))
        lines = log.read_text().splitlines()
        self.assertEqual(lines[0].split("\t")[1], "Admit")
        self.assertEqual([l.split("\t")[1:3] for l in lines[2:]],
                         [["EPOCH", CID], ["EPOCH", "opus_high_beta_apidocs_T1_r2"]])
        self.assertEqual(sorted(p.name for p in log.parent.glob("transitions*")),
                         ["transitions.log"])


if __name__ == "__main__":
    unittest.main()
