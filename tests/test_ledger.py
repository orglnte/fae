"""fae/cell/ledger.py — the single verdict derivation.

Before this module there were five independent parsers and every historical
data-integrity incident was two of them disagreeing: revoked greens scored as
green, mid-gate resumes nearly minting ungated greens, stranded reverifies
leaving the population silently. One parser makes a verdict bug a one-file fix
— and makes it worth pinning here.

Every test builds its own ledger in a TemporaryDirectory. Nothing reads the
live workspace tree.
"""
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT
from fae.cell import ledger

TS = "2026-07-30T09:00:00Z"
CID = "sonnet_high_beta_apidocs_T1_r1"


_KEEP = []      # TemporaryDirectory handles, freed when the module unloads


def ws_with(*lines):
    """A throwaway workspace whose iterations.log holds exactly these lines.

    The TemporaryDirectory handle is parked in _KEEP because a Path cannot
    carry it and letting it be collected would delete the tree mid-test.
    """
    tmp = tempfile.TemporaryDirectory()
    _KEEP.append(tmp)
    ws = Path(tmp.name)
    (ws / "iterations.log").write_text("".join(l + "\n" for l in lines))
    return ws


def ev(name, *fields):
    return "\t".join((TS, name) + fields)


def shape(arrangement, attempt=1, ok=True, reverify=False):
    """A SHAPE line in the exact shape the emitters write.

    run_cell.sh:450   <ts>\\tSHAPE\\t<cid>\\tattempt=N\\t<ARR> pass
    reverify_cell.sh:108  <ts>\\tSHAPE\\t<cid>\\treverify\\t<ARR> pass

    ledger.parse keys off the trailing " pass" and off whether the word
    "reverify" appears, so both details are load-bearing.
    """
    where = "reverify" if reverify else f"attempt={attempt}"
    tail = f"{arrangement} pass" if ok else f"{arrangement} FAIL (passed: none)"
    return ev("SHAPE", CID, where, tail)


class TestVerdict(unittest.TestCase):

    def test_no_log_is_in_flight(self):
        with tempfile.TemporaryDirectory() as d:
            p = ledger.parse(Path(d))
        self.assertIsNone(p["verdict"])
        self.assertEqual(p["events"], 0)

    def test_green_end(self):
        p = ledger.parse(ws_with(ev("START", "attempt=1"),
                                 ev("ITER", "green", "attempt=1 shapes=all"),
                                 ev("END", "green=true")))
        self.assertEqual(p["verdict"], "green")
        self.assertEqual(p["green_at"], 1)
        self.assertTrue(p["green_seen"])

    def test_failed_end(self):
        p = ledger.parse(ws_with(ev("START", "attempt=1"),
                                 ev("ITER", "fail", "attempt=1 stage=scaling"),
                                 ev("END", "green=false")))
        self.assertEqual(p["verdict"], "failed")
        self.assertIsNone(p["green_at"])

    def test_in_flight_has_no_verdict(self):
        p = ledger.parse(ws_with(ev("START", "attempt=1"),
                                 ev("ITER", "fail", "attempt=1 stage=scaling"),
                                 ev("START", "attempt=2")))
        self.assertIsNone(p["verdict"])
        self.assertEqual(p["att"], 1)

    def test_green_at_reports_the_attempt_that_solved_it(self):
        p = ledger.parse(ws_with(
            ev("ITER", "fail", "attempt=1 stage=scaling"),
            ev("ITER", "fail", "attempt=2 stage=scaling"),
            ev("ITER", "green", "attempt=3 shapes=all"),
            ev("END", "green=true")))
        self.assertEqual(p["green_at"], 3)

    def test_revoked_is_not_failed(self):
        """A green the 6-shape gate later revoked solved the task once and is
        not shape-robust. Merging it with never-solved-it destroys the
        distinction the gate exists to draw."""
        p = ledger.parse(ws_with(
            ev("ITER", "green", "attempt=1 shapes=all"),
            ev("END", "green=true"),
            ev("REVERIFY", CID, "start 6-shape gate on frozen solution"),
            ev("REVERIFY", CID, "REVOKED: failed G4 (passed: G1)"),
            ev("END", "green=false")))
        self.assertEqual(p["verdict"], "revoked")
        self.assertTrue(p["reverified"])

    def test_trailing_reverify_does_not_unfinish_a_cell(self):
        """2026-07-25: a spurious appended REVERIFY line hid seven verdicts,
        because the last event stopped being the END."""
        p = ledger.parse(ws_with(
            ev("ITER", "green", "attempt=1 shapes=all"),
            ev("END", "green=true"),
            ev("REVERIFY", CID, "start 6-shape gate on frozen solution")))
        self.assertEqual(p["verdict"], "green")
        self.assertEqual(p["last_ev"], "END")

    def test_halt_does_not_burn_an_attempt(self):
        p = ledger.parse(ws_with(ev("START", "attempt=1"),
                                 ev("HALT", "fingerprint changed")))
        self.assertEqual(p["att"], 0)
        self.assertIn("fingerprint", p["halt_cause"])


class TestReverifyCompletion(unittest.TestCase):

    GREEN = (ev("ITER", "green", "attempt=1 shapes=all"),
             ev("END", "green=true"),
             ev("REVERIFY", CID, "start 6-shape gate on frozen solution"))

    def test_explicit_reverify_end_closes_the_gate(self):
        """v2 marker, preferred going forward."""
        p = ledger.parse(ws_with(*self.GREEN,
                                 ev("REVERIFY_END", CID, "upheld")))
        self.assertFalse(p["reverify_active"])

    def test_reverify_end_closes_on_every_outcome(self):
        for outcome in ("upheld", "revoked", "rig-error"):
            with self.subTest(outcome=outcome):
                p = ledger.parse(ws_with(*self.GREEN,
                                         ev("REVERIFY_END", CID, outcome)))
                self.assertFalse(p["reverify_active"])

    def test_old_ledgers_fall_back_to_payload_inference(self):
        """No REVERIFY_END line exists in pre-v2 logs, so the text inference
        must still close the gate or every historical reverify reads as
        stranded."""
        p = ledger.parse(ws_with(
            *self.GREEN,
            ev("REVERIFY", CID, "shapes=all PASS — green upheld (robust)")))
        self.assertFalse(p["reverify_active"])

    def test_open_gate_is_active(self):
        p = ledger.parse(ws_with(*self.GREEN,
                                 shape("G1", reverify=True)))
        self.assertTrue(p["reverify_active"])
        self.assertEqual(p["rev_pass"], 1)

    def test_reverify_arrangements_do_not_count_toward_the_run_gate(self):
        """`gate` describes the in-run attempt; reverify passes are counted
        separately in rev_pass. Mixing them would let a reverify inflate the
        gate of the attempt that produced the green."""
        p = ledger.parse(ws_with(*self.GREEN,
                                 shape("G1", reverify=True),
                                 shape("G3", reverify=True)))
        self.assertEqual(p["rev_pass"], 2)
        self.assertEqual(p["gate"], 6)     # from shapes=all, not the reverify


class TestGate(unittest.TestCase):

    def test_shapes_all_is_a_full_gate(self):
        p = ledger.parse(ws_with(ev("ITER", "green", "attempt=1 shapes=all")))
        self.assertEqual(p["gate"], 6)

    def test_stage_failure_never_opened_the_gate(self):
        p = ledger.parse(ws_with(ev("ITER", "fail", "attempt=1 stage=scaling")))
        self.assertEqual(p["gate"], 0)

    def test_partial_gate_counts_arrangements_of_that_attempt(self):
        p = ledger.parse(ws_with(
            shape("G1", attempt=1),
            shape("G2", attempt=1),
            ev("ITER", "fail", "attempt=1 shape-gate=G4")))
        self.assertEqual(p["gate"], 3)     # primary verify + 2 arrangements

    def test_partial_gate_ignores_other_attempts_arrangements(self):
        """Arrangements are keyed by attempt — attempt 1's passes must not
        inflate attempt 2's gate."""
        p = ledger.parse(ws_with(
            shape("G1", attempt=1), shape("G2", attempt=1),
            ev("ITER", "fail", "attempt=1 shape-gate=G4"),
            shape("G1", attempt=2),
            ev("ITER", "fail", "attempt=2 shape-gate=G2")))
        self.assertEqual(p["gate"], 2)     # primary verify + 1 arrangement

    def test_failed_arrangements_do_not_count(self):
        p = ledger.parse(ws_with(
            shape("G1", attempt=1),
            shape("G2", attempt=1, ok=False),
            ev("ITER", "fail", "attempt=1 shape-gate=G2")))
        self.assertEqual(p["gate"], 2)

    def test_live_shape_pass_tracks_the_unjudged_attempt(self):
        """A cell deep in a 6-arrangement gate has no ITER line yet, so `gate`
        is frozen at the previous attempt. Status must read this instead."""
        p = ledger.parse(ws_with(
            ev("ITER", "fail", "attempt=1 stage=scaling"),
            shape("G1", attempt=2),
            shape("G2", attempt=2)))
        self.assertEqual(p["gate"], 0)
        self.assertEqual(p["live_shape_pass"], 2)

    def test_no_notes_means_no_gate_info(self):
        p = ledger.parse(ws_with(ev("START", "attempt=1")))
        self.assertIsNone(p["gate"])


class TestMalformedInput(unittest.TestCase):

    def test_comment_lines_are_skipped(self):
        p = ledger.parse(ws_with("# v2 header",
                                 ev("ITER", "green", "attempt=1 shapes=all")))
        self.assertEqual(p["events"], 1)

    def test_short_lines_are_skipped(self):
        p = ledger.parse(ws_with("garbage", TS + "\tSTART",
                                 ev("ITER", "green", "attempt=1 shapes=all")))
        self.assertEqual(p["events"], 1)

    def test_prepared_marks_a_workspace_that_never_ran(self):
        p = ledger.parse(ws_with(ev("PREPARED", "cid", "by=prepare_cell")))
        self.assertTrue(p["prepared"])
        self.assertEqual(p["att"], 0)
        self.assertIsNone(p["verdict"])


class TestHist(unittest.TestCase):

    def test_green_renders_as_green(self):
        p = ledger.parse(ws_with(ev("ITER", "green", "attempt=1 shapes=all")))
        self.assertEqual(ledger.hist(p), "green")

    def test_repeated_failures_run_length_compress(self):
        p = ledger.parse(ws_with(*[ev("ITER", "fail", f"attempt={i} stage=scaling")
                                   for i in range(1, 8)]))
        self.assertEqual(ledger.hist(p), "scaling ×7")

    def test_passing_e2e_is_not_repeated_per_attempt(self):
        p = ledger.parse(ws_with(
            ev("ITER", "fail", "attempt=1 stage=scaling e2e=7/7")))
        self.assertEqual(ledger.hist(p), "scaling")

    def test_failing_e2e_is_shown(self):
        p = ledger.parse(ws_with(
            ev("ITER", "fail", "attempt=1 stage=e2e e2e=3/7")))
        self.assertIn("e2e NOK:3/7", ledger.hist(p))

    def test_empty_ledger_renders_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ledger.hist(ledger.parse(Path(d))), "")


class TestAttentionEvents(unittest.TestCase):
    """NOEDIT and ALERT are counted so a status view can raise them. Neither is
    a verdict: they must not touch iters, att, or the verdict."""

    NOEDIT = ev("NOEDIT", CID, "attempt=2",
                "INVESTIGATE — no file modified; agent said: Error: 503")
    ALERT = ev("ALERT", CID, "SETUP-FAILED rc=1 — alpha cell_setup")

    def test_noedit_is_counted_with_its_last_detail(self):
        p = ledger.parse(ws_with(
            ev("NOEDIT", CID, "attempt=1", "INVESTIGATE — first"),
            ev("ITER", "fail", "attempt=1 stage=no-edit"),
            self.NOEDIT,
            ev("ITER", "fail", "attempt=2 stage=no-edit")))
        self.assertEqual(p["noedit"], 2)
        self.assertIn("503", p["noedit_last"])

    def test_alerts_are_counted_with_their_last_detail(self):
        p = ledger.parse(ws_with(self.ALERT))
        self.assertEqual(p["alerts"], 1)
        self.assertIn("SETUP-FAILED", p["alert_last"])

    def test_neither_changes_the_verdict_or_the_attempt_count(self):
        p = ledger.parse(ws_with(self.ALERT, self.NOEDIT,
                                 ev("ITER", "green", "attempt=1 shapes=all"),
                                 ev("END", "green=true")))
        self.assertEqual(p["verdict"], "green")
        self.assertEqual(p["att"], 1)

    def test_a_trailing_alert_does_not_un_finish_a_cell(self):
        p = ledger.parse(ws_with(ev("ITER", "green", "attempt=1 shapes=all"),
                                 ev("END", "green=true"), self.ALERT))
        self.assertEqual(p["verdict"], "green")

    def test_open_alerts_are_those_since_the_last_start(self):
        # the cumulative count is evidence; the open count is what still
        # needs a human — a (re)start is the human's answer
        p = ledger.parse(ws_with(self.ALERT, self.ALERT,
                                 ev("START", "attempt=2"), self.ALERT))
        self.assertEqual((p["alerts"], p["alerts_open"]), (3, 1))
        p = ledger.parse(ws_with(self.ALERT, ev("START", "attempt=2")))
        self.assertEqual((p["alerts"], p["alerts_open"]), (1, 0))

    def test_absent_events_read_zero(self):
        p = ledger.parse(ws_with(ev("ITER", "fail", "attempt=1 stage=scaling")))
        self.assertEqual((p["noedit"], p["alerts"]), (0, 0))
        self.assertEqual((p["noedit_last"], p["alert_last"]), ("", ""))


if __name__ == "__main__":
    unittest.main()


class TestTheLedgerHasOneWriter(unittest.TestCase):
    """Three producers write these files — the cell, its shell shim, and
    supervision — and the atomicity they rely on only holds if every record is
    one complete line written once."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        (self.ws / "iterations.log").write_text("")

    def test_no_producer_formats_the_line_itself(self):
        for f in sorted((Path(ROOT) / "fae").rglob("*.py")):
            self.assertNotIn('iterations.log").open("a")', f.read_text(), f.name)

    def test_tabs_and_newlines_cannot_break_the_tsv(self):
        line = ledger.append(self.ws, "NOEDIT", "cell-x", "said: a\tb\nc\rd")
        self.assertEqual(line.count("\t"), 3)
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)

    def test_a_long_field_is_capped(self):
        line = ledger.append(self.ws, "NOEDIT", "cell-x", "x" * 5000)
        self.assertLess(len(line), 1000)

    def test_only_the_three_iter_results_are_accepted(self):
        for ok in ("fail", "green", "budget"):
            ledger.record_iter(self.ws, ok, "attempt=1")
        for bad in ("passed", "GREEN", "", "ok"):
            with self.assertRaises(ValueError, msg=bad):
                ledger.record_iter(self.ws, bad)

    def test_an_unprepared_workspace_is_refused(self):
        empty = Path(self._tmp.name) / "nope"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError):
            ledger.record_iter(empty, "green")

    def test_concurrent_producers_do_not_interleave(self):
        import os
        pids = []
        for w in range(8):
            pid = os.fork()
            if pid == 0:
                for i in range(100):
                    ledger.append(self.ws, "CKPT", f"cell-{w}",
                                  f"attempt={i} " + "x" * 200)
                os._exit(0)
            pids.append(pid)
        for p in pids:
            os.waitpid(p, 0)
        lines = (self.ws / "iterations.log").read_text().splitlines()
        self.assertEqual(len(lines), 800)
        for l in lines:
            self.assertEqual(l.count("\t"), 3)
            self.assertRegex(l, r"^\d{4}-\d\d-\d\dT")

    def test_what_the_parser_reads_is_what_the_writer_wrote(self):
        ledger.append(self.ws, "PREPARED", "cell-x", "by=test")
        ledger.append(self.ws, "START", "cell-x", "attempt=1")
        ledger.record_iter(self.ws, "green", "attempt=1")
        ledger.append(self.ws, "END", "cell-x", "green=true")
        L = ledger.parse(self.ws)
        self.assertEqual(L["iters"], ["green"])
        self.assertEqual(L["verdict"], "green")


class TestGateArity(unittest.TestCase):
    """The gate's number of arrangements is the experiment's; the ledger text stays `shapes=all`."""

    def test_a_full_gate_counts_the_declared_arrangements(self):
        p = ledger.parse(ws_with(ev("ITER", "green", "attempt=1 shapes=all")), gate_n=3)
        self.assertEqual((p["gate"], p["gate_n"]), (3, 3))

    def test_the_default_is_six(self):
        p = ledger.parse(ws_with(ev("ITER", "green", "attempt=1 shapes=all")))
        self.assertEqual((p["gate"], p["gate_n"]), (6, 6))


class TestACellCountsItsOwnGate(unittest.TestCase):
    """A cell reads its ledger over its experiment's gate, not a fixed size."""

    def test_read_ledger_counts_the_gates_arrangements(self):
        import tempfile
        from unittest import mock
        from fae.cell.cell import Cell
        from fae.experiment import Gate
        with tempfile.TemporaryDirectory() as tmp:
            c = Cell("m_high_v_T1_r1", workspaces=tmp, root=tmp, locks=tmp)
            c.ws.mkdir()
            (c.ws / "iterations.log").write_text(ev("ITER", "green", "attempt=1 shapes=all"))
            with mock.patch.object(Cell, "gate_def", new_callable=mock.PropertyMock,
                                   return_value=Gate(("a", "b", "c"))):
                L = c.read_ledger()
        self.assertEqual((L["gate"], L["gate_n"]), (3, 3))
