"""fae/cell/ledger.py — what the parser and the writers do to a real file.

tests/test_ledger.py pins the verdict rules; this file pins the record they
are derived from: every field of `parse()`, the line-level contract (tabs,
comments, short lines, undecodable bytes), the ordering rules the module
docstring states, and the three writers reading back through the parser.

Every test writes its own iterations.log into a TemporaryDirectory. Nothing
reads the live workspace tree.
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401 — puts the tree on sys.path
# By package name: the module under test is fae/cell/ledger.py itself, not
# fae/driver/common's path-loaded copy of it.
from fae.cell import ledger

TS = "2026-07-30T09:00:00Z"
CID = "sonnet_high_beta_apidocs_T1_r1"


def ev(name, *fields):
    return "\t".join((TS, name) + fields)


def shape(arrangement, attempt=1, ok=True, reverify=False):
    """A SHAPE line as the emitters write it: `attempt=N` for the run's own
    gate, the word `reverify` for the gate on a frozen green."""
    where = "reverify" if reverify else f"attempt={attempt}"
    tail = f"{arrangement} pass" if ok else f"{arrangement} FAIL (passed: none)"
    return ev("SHAPE", CID, where, tail)


REVERIFY_START = ev("REVERIFY", CID, "start 6-shape gate on frozen solution")


class LedgerCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)

    def write(self, *lines):
        (self.ws / "iterations.log").write_text("".join(l + "\n" for l in lines))
        return self.ws

    def parse(self, *lines):
        return ledger.parse(self.write(*lines))


EMPTY_RECORD = dict(
    events=0, iters=[], iter_notes=[], gate=None, gate_n=6, att=0,
    last_ev="", last_line="", last_end_green=None, green_seen=False,
    reverified=False, reverify_active=False, rev_pass=0, prepared=False,
    verdict=None, green_at=None, halt_cause="", live_shape_pass=0,
    noedit=0, noedit_last="", alerts=0, alerts_open=0, alert_last="")


class TestTheParsedRecord(LedgerCase):
    """The whole record, field by field — a consumer reads any of them."""

    def test_an_absent_ledger_is_the_empty_record(self):
        self.assertEqual(ledger.parse(self.ws), EMPTY_RECORD)

    def test_an_empty_ledger_is_the_empty_record(self):
        self.assertEqual(self.parse(), EMPTY_RECORD)

    def test_a_green_cell_field_by_field(self):
        end = ev("END", CID, "green=true")
        p = self.parse(
            ev("PREPARED", CID, "by=prepare"),
            ev("START", CID, "attempt=1"),
            ev("ITER", "fail", "attempt=1 stage=scaling e2e=7/7"),
            ev("START", CID, "attempt=2"),
            shape("G1", attempt=2),
            shape("G2", attempt=2, ok=False),
            ev("ITER", "fail", "attempt=2 shape-gate=G2"),
            ev("START", CID, "attempt=3"),
            *(shape(a, attempt=3) for a in ("G1", "G2", "G3", "G4", "G5", "G6")),
            ev("ITER", "green", "attempt=3 shapes=all"),
            end)
        self.assertEqual(p, dict(
            events=16, iters=["fail", "fail", "green"],
            iter_notes=["attempt=1 stage=scaling e2e=7/7",
                        "attempt=2 shape-gate=G2",
                        "attempt=3 shapes=all"],
            gate=6, gate_n=6, att=3, last_ev="END", last_line=end,
            last_end_green=True, green_seen=True,
            reverified=False, reverify_active=False, rev_pass=0,
            prepared=True, verdict="green", green_at=3, halt_cause="",
            live_shape_pass=0, noedit=0, noedit_last="",
            alerts=0, alerts_open=0, alert_last=""))


class TestLineShape(LedgerCase):
    """Fields are TAB-separated: a timestamp, an event, at least one field."""

    def test_every_well_formed_line_counts_once_whatever_its_event(self):
        p = self.parse(ev("START", "attempt=1"),
                       ev("RESTORE", CID, "attempt=1", "tree=abc"),
                       ev("PAUSED", CID, "attempt=1", "operator"),
                       ev("WAIT", CID, "attempt=1", "529"),
                       ev("HEAL", CID, "attempt=1", "reverted 1 file"))
        self.assertEqual(p["events"], 5)

    def test_a_line_needs_a_timestamp_an_event_and_one_field(self):
        p = self.parse("garbage", TS + "\tSTART", ev("START", "attempt=1"))
        self.assertEqual(p["events"], 1)
        self.assertEqual(p["last_ev"], "START")

    def test_a_comment_is_skipped_even_when_it_is_tab_shaped(self):
        p = self.parse("#\tITER\tgreen\tattempt=1 shapes=all",
                       "# v2 header",
                       ev("ITER", "fail", "attempt=1 stage=scaling"))
        self.assertEqual((p["events"], p["iters"]), (1, ["fail"]))

    def test_fields_split_on_tabs_only_so_free_text_keeps_its_spaces(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling e2e=3/7"),
                       ev("HALT", CID, "attempt=2", "restore-failed: index locked"))
        self.assertEqual(p["iter_notes"], ["attempt=1 stage=scaling e2e=3/7"])
        self.assertEqual(p["halt_cause"], "restore-failed: index locked")

    def test_an_iter_note_is_the_fourth_field_or_empty(self):
        p = self.parse(ev("ITER", "green"),
                       ev("ITER", "fail", "attempt=2 stage=scaling"),
                       ev("ITER", "budget", "attempt=3", "extra"))
        self.assertEqual(p["iters"], ["green", "fail", "budget"])
        self.assertEqual(p["iter_notes"], ["", "attempt=2 stage=scaling", "attempt=3"])

    def test_undecodable_bytes_do_not_lose_the_rest_of_the_ledger(self):
        (self.ws / "iterations.log").write_bytes(
            ev("ITER", "fail", "attempt=1 stage=scaling").encode() + b"\n"
            + b"\xff\xfe\x00 not utf-8\n"
            + ev("ITER", "green", "attempt=2 shapes=all").encode() + b"\n"
            + ev("END", CID, "green=true").encode() + b"\n")
        p = ledger.parse(self.ws)
        self.assertEqual((p["events"], p["iters"]), (3, ["fail", "green"]))
        self.assertEqual(p["verdict"], "green")


class TestDoneness(LedgerCase):
    """The last END or HALT decides; only an attempt event reopens the cell."""

    def test_end_green_false_is_failed_and_records_its_colour(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       ev("END", CID, "green=false"))
        self.assertEqual(p["verdict"], "failed")
        self.assertIs(p["last_end_green"], False)
        self.assertIs(p["green_seen"], False)

    def test_trailing_bookkeeping_never_unfinishes_a_failed_cell(self):
        end = ev("END", CID, "green=false")
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"), end,
                       shape("G1", attempt=1),
                       ev("PAUSED", CID, "attempt=1", "operator"),
                       ev("ALERT", CID, "SETUP-FAILED rc=1"),
                       ev("RESTORE", CID, "attempt=1", "tree=abc"))
        self.assertEqual((p["last_ev"], p["last_line"]), ("END", end))
        self.assertEqual(p["verdict"], "failed")

    def test_a_halt_stays_authoritative_over_trailing_bookkeeping(self):
        halt = ev("HALT", CID, "attempt=1", "infra")
        p = self.parse(ev("START", CID, "attempt=1"), halt,
                       ev("PAUSED", CID, "attempt=1", "operator"))
        self.assertEqual((p["last_ev"], p["last_line"]), ("HALT", halt))
        self.assertEqual(p["halt_cause"], "infra")
        self.assertEqual((p["att"], p["verdict"]), (0, None))

    def test_verify_nostart_is_a_halt_shape_that_burns_nothing(self):
        nostart = ev("VERIFY_NOSTART", CID, "attempt=1", "bring-up never held /health")
        p = self.parse(ev("START", CID, "attempt=1"), nostart,
                       ev("PAUSED", CID, "attempt=1", "operator"))
        self.assertEqual((p["last_ev"], p["last_line"]), ("VERIFY_NOSTART", nostart))
        self.assertEqual(p["halt_cause"], "bring-up never held /health")
        self.assertEqual((p["att"], p["verdict"]), (0, None))

    def test_a_start_after_a_halt_reopens_the_cell(self):
        p = self.parse(ev("START", CID, "attempt=1"),
                       ev("HALT", CID, "attempt=1", "infra"),
                       ev("START", CID, "attempt=1"))
        self.assertEqual(p["last_ev"], "START")
        self.assertEqual(p["halt_cause"], "infra")
        p = self.parse(ev("HALT", CID, "attempt=1", "infra"),
                       ev("START", CID, "attempt=1"),
                       ev("ITER", "fail", "attempt=1 stage=scaling"))
        self.assertEqual((p["last_ev"], p["verdict"]), ("ITER", None))

    def test_halt_cause_is_the_last_field_capped_at_70(self):
        p = self.parse(ev("HALT", CID, "attempt=1", "x" * 100))
        self.assertEqual(p["halt_cause"], "x" * 70)
        p = self.parse(ev("HALT", CID, "attempt=1", "no-creds"),
                       ev("HALT", CID, "attempt=1", "agent-fault"))
        self.assertEqual(p["halt_cause"], "agent-fault")

    def test_green_at_is_the_green_attempt_even_after_revocation(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       ev("ITER", "fail", "attempt=2 stage=e2e"),
                       ev("ITER", "green", "attempt=3 shapes=all"),
                       ev("END", CID, "green=true"),
                       REVERIFY_START,
                       shape("G1", reverify=True),
                       ev("REVERIFY", CID, "REVOKED: failed G4 (passed: G1)"),
                       ev("END", CID, "green=false"))
        self.assertEqual(p["verdict"], "revoked")
        self.assertEqual(p["green_at"], 3)
        self.assertIs(p["green_seen"], True)
        self.assertIs(p["last_end_green"], False)
        self.assertIs(p["reverify_active"], False)
        self.assertEqual(p["rev_pass"], 1)

    def test_a_cell_that_never_solved_it_cannot_be_revoked(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       ev("END", CID, "green=false"),
                       ev("REVERIFY", CID, "ERROR[rig]: stranded mid-gate"),
                       ev("END", CID, "green=false"))
        self.assertEqual(p["verdict"], "failed")
        self.assertIs(p["reverified"], True)


class TestReverifyGate(LedgerCase):
    """A gate opens on a REVERIFY start line and closes on an explicit
    REVERIFY_END, a completion word, or the cell ending."""

    def test_a_gate_opening_on_the_first_line_is_still_a_gate(self):
        p = self.parse(REVERIFY_START, shape("G1", reverify=True))
        self.assertIs(p["reverify_active"], True)
        self.assertEqual(p["rev_pass"], 1)

    def test_only_a_start_line_opens_a_gate(self):
        p = self.parse(ev("REVERIFY", CID, "ERROR[rig]: stranded mid-gate"))
        self.assertEqual((p["reverified"], p["reverify_active"], p["rev_pass"]),
                         (True, False, 0))

    def test_a_restarted_gate_counts_from_zero_and_stays_open(self):
        p = self.parse(REVERIFY_START,
                       shape("G1", reverify=True), shape("G2", reverify=True),
                       REVERIFY_START)
        self.assertEqual((p["rev_pass"], p["reverify_active"]), (0, True))
        p = self.parse(REVERIFY_START, shape("G1", reverify=True),
                       REVERIFY_START, shape("G3", reverify=True))
        self.assertEqual(p["rev_pass"], 1)

    def test_each_completion_word_closes_the_gate(self):
        for tail in ("shapes=all PASS — green upheld",
                     "REVOKED: failed G4 (passed: G1)",
                     "ERROR[rig]: stranded mid-gate"):
            with self.subTest(tail=tail):
                p = self.parse(REVERIFY_START, shape("G1", reverify=True),
                               ev("REVERIFY", CID, tail))
                self.assertIs(p["reverify_active"], False)
                self.assertEqual(p["rev_pass"], 1)

    def test_end_halt_and_nostart_close_the_gate(self):
        for closer in (ev("END", CID, "green=false"),
                       ev("HALT", CID, "attempt=1", "infra"),
                       ev("VERIFY_NOSTART", CID, "attempt=1", "no /health")):
            with self.subTest(closer=closer.split("\t")[1]):
                p = self.parse(REVERIFY_START, closer)
                self.assertIs(p["reverify_active"], False)

    def test_other_events_neither_count_nor_close(self):
        p = self.parse(REVERIFY_START,
                       shape("G1", reverify=True, ok=False),
                       shape("G2", attempt=1),
                       ev("PAUSED", CID, "attempt=1", "operator"),
                       ev("ALERT", CID, "verify-wedged"))
        self.assertIs(p["reverify_active"], True)
        self.assertEqual(p["rev_pass"], 0)

    def test_a_pass_with_trailing_whitespace_still_counts(self):
        p = self.parse(REVERIFY_START, shape("G1", reverify=True) + "  ")
        self.assertEqual(p["rev_pass"], 1)

    def test_an_explicit_end_marker_closes_the_gate_for_good(self):
        p = self.parse(REVERIFY_START, shape("G1", reverify=True),
                       ev("REVERIFY_END", CID, "upheld"),
                       shape("G2", reverify=True))
        self.assertIs(p["reverify_active"], False)
        self.assertEqual(p["rev_pass"], 1)


class TestGateProgress(LedgerCase):
    """`gate` is frozen at the last judged attempt; arrangements belong to
    the attempt their line names."""

    def test_the_gate_reads_the_last_judged_attempt_only(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       shape("G1", attempt=2),
                       ev("ITER", "fail", "attempt=2 shape-gate=G2"),
                       ev("ITER", "fail", "attempt=3 stage=e2e"))
        self.assertEqual(p["gate"], 0)
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       ev("ITER", "fail", "attempt=2 stage=e2e"),
                       shape("G1", attempt=3), shape("G2", attempt=3),
                       ev("ITER", "fail", "attempt=3 shape-gate=G3"))
        self.assertEqual(p["gate"], 3)

    def test_a_gate_that_failed_its_first_arrangement_is_one(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 shape-gate=G1"))
        self.assertEqual(p["gate"], 1)

    def test_the_note_names_the_attempt_the_arrangements_belong_to(self):
        # the note's attempt number is authoritative over the ITER count
        p = self.parse(shape("G1", attempt=2),
                       ev("ITER", "fail", "attempt=2 shape-gate=G2"))
        self.assertEqual(p["gate"], 2)
        p = self.parse(shape("G1", attempt=1),
                       ev("ITER", "fail", "attempt=2 shape-gate=G2"))
        self.assertEqual(p["gate"], 1)

    def test_a_note_without_an_attempt_number_falls_back_to_the_count(self):
        p = self.parse(shape("G1", attempt=1), shape("G2", attempt=1),
                       ev("ITER", "fail", "shape-gate=G3"))
        self.assertEqual(p["gate"], 3)

    def test_a_note_with_neither_stage_nor_gate_has_no_gate_info(self):
        p = self.parse(ev("ITER", "fail", "attempt=1"))
        self.assertIsNone(p["gate"])

    def test_a_pass_with_trailing_whitespace_still_counts(self):
        p = self.parse(shape("G1", attempt=1) + " ",
                       ev("ITER", "fail", "attempt=1 shape-gate=G2"))
        self.assertEqual(p["gate"], 2)

    def test_live_progress_is_zero_until_the_next_attempt_passes_one(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"))
        self.assertEqual(p["live_shape_pass"], 0)
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       shape("G1", attempt=2))
        self.assertEqual(p["live_shape_pass"], 1)


class TestAttentionCounters(LedgerCase):

    def test_tails_are_the_last_field_capped_at_110(self):
        p = self.parse(ev("NOEDIT", CID, "attempt=1", "y" * 200),
                       ev("ALERT", CID, "z" * 200))
        self.assertEqual(p["noedit_last"], "y" * 110)
        self.assertEqual(p["alert_last"], "z" * 110)

    def test_tails_are_the_last_detail_written(self):
        p = self.parse(ev("NOEDIT", CID, "attempt=1", "INVESTIGATE — first"),
                       ev("NOEDIT", CID, "attempt=2", "INVESTIGATE — no file modified"),
                       ev("ALERT", CID, "SETUP-FAILED rc=1"),
                       ev("ALERT", CID, "HOST-OVERLOADED qps=12"),
                       ev("ALERT", CID, "verify-wedged"))
        self.assertEqual((p["noedit"], p["noedit_last"]),
                         (2, "INVESTIGATE — no file modified"))
        self.assertEqual((p["alerts"], p["alerts_open"], p["alert_last"]),
                         (3, 3, "verify-wedged"))

    def test_only_a_start_answers_an_open_alert(self):
        p = self.parse(ev("ALERT", CID, "SETUP-FAILED rc=1"),
                       ev("ITER", "fail", "attempt=1 stage=scaling"),
                       ev("PAUSED", CID, "attempt=1", "operator"),
                       ev("NOEDIT", CID, "attempt=1", "nothing"))
        self.assertEqual(p["alerts_open"], 1)
        p = self.parse(ev("ALERT", CID, "SETUP-FAILED rc=1"),
                       ev("START", CID, "attempt=1"))
        self.assertEqual((p["alerts"], p["alerts_open"]), (1, 0))


class TestHist(LedgerCase):

    def test_only_the_last_token_of_the_history_is_returned(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=scaling"),
                       ev("ITER", "fail", "attempt=2 stage=scaling"),
                       ev("ITER", "fail", "attempt=3 shape-gate=G4"))
        self.assertEqual(ledger.hist(p), "fail gate=G4")

    def test_the_stage_comes_before_the_e2e_and_the_gate(self):
        p = self.parse(ev("ITER", "fail", "attempt=1 stage=e2e e2e=2/7 shape-gate=G1"))
        self.assertEqual(ledger.hist(p), "e2e e2e NOK:2/7 gate=G1")


class TestWriters(LedgerCase):
    """append, record_iter and alert: one stamped line each, read back by
    the same parser."""

    STAMPED = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\t")

    def test_append_writes_the_returned_line_in_one_write(self):
        sink = mock.MagicMock()
        sink.__enter__.return_value = sink
        with mock.patch.object(Path, "open", return_value=sink) as opened:
            line = ledger.append(self.ws, "START", CID, "attempt=1")
        opened.assert_called_once_with("a")
        sink.write.assert_called_once_with(line)
        self.assertRegex(line, self.STAMPED)
        self.assertTrue(line.endswith(f"\tSTART\t{CID}\tattempt=1\n"))
        self.assertEqual(line.count("\n"), 1)

    def test_append_adds_to_what_is_already_there(self):
        self.write(ev("PREPARED", CID, "by=test"))
        line = ledger.append(self.ws, "START", CID, "attempt=1")
        self.assertEqual((self.ws / "iterations.log").read_text(),
                         ev("PREPARED", CID, "by=test") + "\n" + line)

    def test_a_field_is_capped_at_field_max(self):
        line = ledger.append(self.ws, "NOEDIT", CID, "x" * 5000)
        self.assertEqual(line.rstrip("\n").split("\t")[-1], "x" * ledger.FIELD_MAX)

    def test_record_iter_writes_what_the_parser_counts(self):
        self.write()
        line = ledger.record_iter(self.ws, "fail", "attempt=1 stage=scaling")
        self.assertTrue(line.endswith("\tITER\tfail\tattempt=1 stage=scaling\n"))
        ledger.record_iter(self.ws, "green")
        p = ledger.parse(self.ws)
        self.assertEqual(p["iters"], ["fail", "green"])
        self.assertEqual(p["iter_notes"], ["attempt=1 stage=scaling", ""])
        self.assertEqual(p["events"], 2)

    def test_alert_is_an_alert_event_about_the_cell(self):
        line = ledger.alert(self.ws, CID, "SETUP-FAILED rc=1 — cell_setup")
        self.assertTrue(line.endswith(f"\tALERT\t{CID}\tSETUP-FAILED rc=1 — cell_setup\n"))
        p = ledger.parse(self.ws)
        self.assertEqual((p["alerts"], p["alerts_open"]), (1, 1))
        self.assertEqual(p["alert_last"], "SETUP-FAILED rc=1 — cell_setup")
        self.assertEqual(p["verdict"], None)


if __name__ == "__main__":
    unittest.main()
