"""The archive reader, and the validator's checks on it: runs that were not
charged, and an archive that disagrees with the ledger."""
import json
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell import archive
from fae.scoring.validate import archive_warns


def ws_with(*runs):
    ws = Path(tempfile.mkdtemp())
    for name, verdict in runs:
        d = ws / "arrangements" / name
        d.mkdir(parents=True)
        (d / "verify.log").write_text(name + "\n")
        if verdict is not None:
            (d / "verdict.json").write_text(json.dumps(verdict))
    return ws


def iters(*lines):
    return "".join(f"2026-09-27T00:00:00Z\tITER\t{kind}\tattempt={n}{rest}\n"
                   for kind, n, rest in lines)


class TestTheReader(unittest.TestCase):
    def test_names_parse_and_legacy_folders_count_as_judged(self):
        ws = ws_with(("01-BBS", None), ("02-a2-SBB-refunded", {}),
                     ("03-a2-SBB-charged", {}), ("04-a3-BBS-interrupted", {}))
        rs = archive.runs(ws)
        self.assertEqual([(r.attempt, r.shape, r.end_state) for r in rs],
                         [(None, "BBS", None), (2, "SBB", "refunded"),
                          (2, "SBB", "charged"), (3, "BBS", "interrupted")])
        self.assertEqual([p.parent.name for p in archive.judged_files(ws, "verify.log")],
                         ["01-BBS", "03-a2-SBB-charged"])

    def test_a_shape_with_dashes_keeps_them(self):
        ws = ws_with(("01-a1-prod-eu-green", {}))
        self.assertEqual(archive.runs(ws)[0].shape, "prod-eu")


class TestTheChecks(unittest.TestCase):
    def test_one_refund_is_quiet(self):
        ws = ws_with(("01-a1-A-refunded", {"stage": "verifier-timeout"}),
                     ("02-a1-A-green", {}))
        self.assertEqual(archive_warns(archive.runs(ws), iters(("green", 1, ""))), [])

    def test_a_repeated_refund_stage_warns(self):
        ws = ws_with(("01-a1-A-refunded", {"stage": "verifier-timeout"}),
                     ("02-a1-A-refunded", {"stage": "verifier-timeout"}),
                     ("03-a1-A-green", {}))
        w = archive_warns(archive.runs(ws), iters(("green", 1, "")))
        self.assertEqual(w, ["2 verify run(s) not charged (verifier-timeoutx2)"])

    def test_many_refunds_of_any_stage_warn(self):
        ws = ws_with(("01-a1-A-refunded", {"stage": "verifier"}),
                     ("02-a1-A-interrupted", {"stage": "interrupted"}),
                     ("03-a1-A-refunded", {"stage": "infra"}),
                     ("04-a1-A-green", {}))
        self.assertIn("3 verify run(s) not charged", archive_warns(archive.runs(ws), iters(("green", 1, "")))[0])

    def test_the_archive_and_the_ledger_agreeing_is_quiet(self):
        ws = ws_with(("01-a1-A-charged", {}), ("02-a2-A-green", {}), ("03-a2-B-green", {}))
        self.assertEqual(archive_warns(archive.runs(ws), iters(("fail", 1, " stage=e2e"), ("green", 2, ""))), [])

    def test_a_charged_attempt_with_no_judged_run_warns(self):
        ws = ws_with(("01-a1-A-refunded", {"stage": "verifier"}), ("02-a2-A-green", {}))
        w = archive_warns(archive.runs(ws), iters(("fail", 1, " stage=e2e"), ("green", 2, "")))
        self.assertEqual(w, ["archive/ledger mismatch at attempt 1: ledger charged, archive no judged run"])

    def test_a_judged_run_the_ledger_never_charged_warns(self):
        ws = ws_with(("01-a1-A-charged", {}))
        self.assertEqual(archive_warns(archive.runs(ws), ""),
                         ["archive/ledger mismatch at attempt 1: ledger no verdict, archive charged"])

    def test_no_edit_attempts_and_attempts_before_the_archive_are_skipped(self):
        ws = ws_with(("01-BBS", None), ("02-a3-A-green", {}))
        text = iters(("fail", 1, " stage=scaling"), ("fail", 2, " stage=no-edit"), ("green", 3, ""))
        self.assertEqual(archive_warns(archive.runs(ws), text), [])


if __name__ == "__main__":
    unittest.main()
