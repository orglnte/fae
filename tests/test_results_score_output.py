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
        self.assertEqual(lines[1], f"  [{'█' * 24}] 2/2 done: 1 failed, 1 green, "
                                   "2 VALID, 0 TAINTED")
        self.assertEqual(len(lines), 2)
        agg.assert_called_once()

    def test_a_scoring_failure_is_named_and_counted_on_the_bar(self):
        lines, _ = self.run_score([("a", "green", VALID)], errors=[("a", "boom")])
        self.assertIn("  ERROR scoring a: boom", lines)
        self.assertTrue(lines[-1].endswith("; 1 FAILED to score"), lines[-1])

    def test_nothing_finished_says_so_on_the_bar(self):
        lines, _ = self.run_score([])
        self.assertTrue(lines[1].endswith("no DONE cells match"), lines[1])


if __name__ == "__main__":
    unittest.main()
