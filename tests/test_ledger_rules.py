"""The ledger's lifecycle rules (fae/cell/ledger.py: RULES, check): each one
holds on a ledger that keeps it and names its rule and line on one that
breaks it; validate turns a broken one into a taint."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs, at_workspace

import fae.experiment
from fae.cell import ledger

C = "c"


def led(*rows):
    """A ledger from (event, field, ...) rows, one stamped line each, as the
    writers lay them out: ITER is `ITER <result> <note>` (ledger.record_iter),
    every other event carries the cid first (Cell._append)."""
    return "".join(f"2026-10-04T00:00:{i:02d}Z\t"
                   + "\t".join((r[0],) + ((() if r[0] == "ITER" else (C,)) + r[1:])) + "\n"
                   for i, r in enumerate(rows))


GOOD = led(("PREPARED", "by=prepare"), ("START", "attempt=1"), ("ITER", "fail", "attempt=1 stage=e2e"),
           ("START", "attempt=2"), ("ITER", "green", "attempt=2"), ("END", "green=true"),
           ("ALERT", "teardown", "network: x"), ("SHAPE", "attempt=2", "BBS pass"))


class TestEachRule(unittest.TestCase):

    def test_a_ledger_that_keeps_every_rule(self):
        self.assertIsNone(ledger.check(GOOD, 10))

    def test_rule_1_attempts_increase_by_one(self):
        bad = led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"),
                  ("START", "attempt=3"), ("ITER", "fail", "attempt=3"))
        self.assertEqual(ledger.check(bad, 10)[:2], (1, 4))
        self.assertIsNone(ledger.check(bad.split("\n", 2)[0] + "\n", 10))

    def test_rule_2_no_attempt_beyond_the_budget(self):
        rows = [r for n in (1, 2, 3) for r in (("START", f"attempt={n}"), ("ITER", "fail", f"attempt={n}"))]
        self.assertEqual(ledger.check(led(*rows), 2)[:2], (2, 6))
        self.assertIsNone(ledger.check(led(*rows), 3))
        self.assertIsNone(ledger.check(led(*rows), None))

    def test_rule_3_only_bookkeeping_after_the_end(self):
        bad = led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"), ("END", "green=false"),
                  ("START", "attempt=2"))
        self.assertEqual(ledger.check(bad, 10)[:2], (3, 4))
        ok = led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"), ("END", "green=false"),
                 ("PAUSED", "by=op"), ("REVERIFY", "start"), ("REVERIFY_END", "upheld"))
        self.assertIsNone(ledger.check(ok, 10))

    def test_rule_4_one_end_and_a_green_end_needs_a_green_iter(self):
        twice = led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"),
                    ("END", "green=false"), ("END", "green=false"))
        self.assertEqual(ledger.check(twice, 10)[:2], (4, 4))
        ungated = led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"), ("END", "green=true"))
        self.assertEqual(ledger.check(ungated, 10)[:2], (4, 3))
        gated = led(("START", "attempt=1"), ("ITER", "green", "attempt=1"), ("END", "green=true"))
        self.assertIsNone(ledger.check(gated, 10))

    def test_rule_5_an_iter_needs_its_start(self):
        bad = led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"), ("ITER", "fail", "attempt=2"))
        self.assertEqual(ledger.check(bad, 10)[:2], (5, 3))
        no_starts = led(("ITER", "fail", "attempt=1"), ("ITER", "fail", "attempt=2"))
        self.assertIsNone(ledger.check(no_starts, 10))

    def test_comments_and_short_lines_are_not_events(self):
        self.assertIsNone(ledger.check("# header\nnot an event\n" + GOOD, 10))


class TestValidateTaintsABrokenLedger(unittest.TestCase):

    def test_the_rule_and_the_line_are_named(self):
        cid = "sonnet_high_beta_apidocs_T1_r1"
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / cid
            ws.mkdir()
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            (ws / "iterations.log").write_text(
                led(("START", "attempt=1"), ("ITER", "fail", "attempt=1"),
                    ("START", "attempt=3"), ("ITER", "fail", "attempt=3"), ("END", "green=false")))
            definition = mock.Mock(taint_rules=None)
            definition.report_text.return_value = ""
            with mock.patch.object(fae.experiment._experiment.Experiment, "definition", new_callable=mock.PropertyMock, return_value=definition), \
                    at_workspace(Path(d), Path(d)):
                doc = runs.validate_ws(ws)
        self.assertEqual(doc["verdict"], "TAINTED")
        self.assertTrue(any(t.startswith("ledger rule 1 broken at line 4") for t in doc["taints"]),
                        doc["taints"])


if __name__ == "__main__":
    unittest.main()
