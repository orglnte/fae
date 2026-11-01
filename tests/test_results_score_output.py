"""`results score` prints one line of what it does, one progress bar over
validating and scoring, whose last frame is the validation tally, then the
scoreboard: no per-phase headers between them."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401
from fae.cli import _render

VALID = {"verdict": "VALID", "taints": [], "warns": []}


def fake_score(results, errors=()):
    def score(selector, on_validating, on_validated, on_cell, on_failed):
        for i, (cid, _, _) in enumerate(results):
            on_validating(i, len(results), cid)
        on_validated(results)
        for i, (cid, _, _) in enumerate(results):
            on_cell(i, len(results), cid)
        for cid, line in errors:
            on_failed(cid, line)
        return len(results) - len(errors), list(errors)
    return score


class TestTheScoreOutput(unittest.TestCase):

    def run_score(self, results, errors=(), **args):
        exp = mock.Mock()
        exp.score.side_effect = fake_score(results, errors)
        out = io.StringIO()
        with mock.patch("fae.experiment.exp", return_value=exp), \
                mock.patch.object(_render, "results_aggregate") as agg, redirect_stdout(out):
            _render.results_score(SimpleNamespace(selector=None, unpack=False, **args))
        return out.getvalue().splitlines(), agg

    def test_one_line_one_bar_then_the_scoreboard(self):
        lines, agg = self.run_score([("a", "green", VALID), ("b", "failed", VALID)])
        self.assertEqual(lines[0], "Validating, scoring, aggregating cells... "
                                   "(--unpack for details, --help for filters)")
        # green or not is the scoreboard's to say; this line is the validation's
        self.assertEqual(lines[1], f"  [{'█' * 24}] 2/2 done: 2 VALID, 0 TAINTED")
        self.assertEqual(lines[2], "  warnings: none")
        self.assertEqual(len(lines), 3)
        agg.assert_called_once()

    def test_the_warnings_line_names_each_kind_once_per_cell(self):
        warned = {"verdict": "VALID", "taints": [], "warns": [
            "footprint unreliable (scope=cell, 2 sample(s) disagreed: x: docker stats 9 MB)",
            "2 verify run(s) not charged (host-sleepx2)"]}
        other = {"verdict": "VALID", "taints": [],
                 "warns": ["footprint unreliable (scope=cell, 5 sample(s) disagreed: y)"]}
        lines, _ = self.run_score([("a", "green", warned), ("b", "green", other),
                                   ("c", "green", VALID)])
        self.assertTrue(lines[1].endswith("3 VALID, 0 TAINTED, 2 with warnings"), lines[1])
        self.assertEqual(lines[2], "  ⚠ warnings — 2 cells: footprint unreliable · "
                                   "1 cell: verify run(s) not charged  (--unpack lists them)")

    def test_tainted_is_red_and_warnings_yellow_only_when_any(self):
        rows = [("a", "green", "TAINTED", "x")]
        self.assertIn("\033[31m1 TAINTED\033[0m",
                      _render._validation_tally(rows, [("a", ["x"])], colour=True))
        self.assertNotIn("\033[", _render._validation_tally([("a", "green", "VALID", "-")], [],
                                                           colour=True))
        self.assertIn("\033[33m1 with warnings\033[0m",
                      _render._validation_tally([("a", "green", "VALID", "w")], [], colour=True))
        warned = [("a", "green", {"warns": ["footprint unreliable (x)"]})]
        self.assertIn("\033[33m", _render._warning_recap(warned, colour=True))
        self.assertNotIn("\033[", _render._warning_recap(warned, colour=False))
        self.assertEqual(_render._warning_recap([("a", "green", VALID)], colour=True),
                         "  warnings: none")

    def test_a_scoring_failure_is_named_and_counted_on_the_bar(self):
        lines, _ = self.run_score([("a", "green", VALID)], errors=[("a", "boom")])
        self.assertIn("  ERROR scoring a: boom", lines)
        done = next(l for l in lines if " done: " in l)
        self.assertTrue(done.endswith("; 1 FAILED to score"), done)

    def test_nothing_finished_says_so_on_the_bar(self):
        lines, _ = self.run_score([])
        self.assertTrue(lines[1].endswith("no DONE cells match"), lines[1])


if __name__ == "__main__":
    unittest.main()
