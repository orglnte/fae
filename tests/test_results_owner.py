"""The results actions are the Experiment's, on one Workspace: they never
reach the scheduler or the CLI, and score() scores exactly the cells
validate() found finished."""
import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import runs
import fae.experiment

ENGINE = Path(runs.experiment.__file__).resolve().parent.parent


def _imports(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (a.name for a in node.names)


class TestNoDriverOnTheResultsPath(unittest.TestCase):

    def test_the_scoring_modules_import_no_scheduler_or_cli(self):
        for path in sorted((ENGINE / "experiment" / "scoring").glob("*.py")):
            bad = [m for m in _imports(path) if m.startswith(("fae.conduct", "fae.cli"))]
            self.assertEqual(bad, [], path.name)

    def test_no_experiment_module_imports_the_cli(self):
        for path in sorted((ENGINE / "experiment").rglob("*.py")):
            bad = [m for m in _imports(path) if m.startswith("fae.cli")]
            self.assertEqual(bad, [], path.name)

    def test_the_experiment_results_methods_import_no_scheduler_or_cli(self):
        E = runs.experiment._experiment.Experiment
        for name in ("validate_cell", "finished", "validate", "score", "aggregate",
                     "run_report", "_workspace_of"):
            src = inspect.getsource(getattr(E, name))
            self.assertNotIn("fae.conduct", src, name)
            self.assertNotIn("fae.cli", src, name)

    def test_there_is_no_driver_package(self):
        self.assertFalse((ENGINE / "driver" / "__init__.py").exists())


class TestScoreScoresWhatValidateFound(unittest.TestCase):

    def _score(self, validated, rcs):
        exp = runs.experiment.exp()
        ws = SimpleNamespace(cell=lambda cid: SimpleNamespace(cid=cid))
        calls = []
        from fae.experiment.scoring import score_cell
        with mock.patch.object(exp, "validate", return_value=validated), \
             mock.patch.object(score_cell, "score_one",
                               side_effect=lambda cid, cell: rcs[cid]):
            out = exp.score(workspace=ws,
                            on_validated=lambda r: calls.append(("validated", len(r))),
                            on_cell=lambda i, n, cid: calls.append(("cell", i, n, cid)),
                            on_failed=lambda cid, line: calls.append(("failed", cid)))
        return out, calls

    def test_each_validated_cell_is_scored_held_ones_included(self):
        (n_ok, failed), calls = self._score(
            [("a", "green", {"verdict": "VALID"}), ("b", "failed", None)], {"a": 0, "b": 0})
        self.assertEqual((n_ok, failed), (2, []))
        self.assertEqual(calls, [("validated", 2), ("cell", 0, 2, "a"), ("cell", 1, 2, "b")])

    def test_a_failure_is_that_cells_and_the_sweep_goes_on(self):
        (n_ok, failed), calls = self._score(
            [("a", "green", {}), ("b", "green", {})], {"a": 1, "b": 0})
        self.assertEqual(n_ok, 1)
        self.assertEqual([cid for cid, _ in failed], ["a"])
        self.assertIn(("failed", "a"), calls)


class TestFinished(unittest.TestCase):
    """A cell counts when status calls it DONE for an outcome; a cancel is
    not an outcome, and a live cell is not finished."""

    def _finished(self, state, why, has_ledger=True, cid="sonnet_high_beta_apidocs_T1_r1"):
        cell = mock.Mock(cid=cid, has_ledger=has_ledger)
        cell.status.return_value = {"state": state, "why": why}
        ws = runs.experiment.exp().workspace
        return runs.experiment.exp().finished(cell, ws)

    def test_outcomes_count(self):
        for why in ("green", "failed", "revoked"):
            self.assertEqual(self._finished("DONE", why), why)

    def test_cancelled_live_and_unprepared_do_not(self):
        self.assertIsNone(self._finished("DONE", "cancelled"))
        self.assertIsNone(self._finished("RUNNING", "agent"))
        self.assertIsNone(self._finished("DONE", "green", has_ledger=False))
        self.assertIsNone(self._finished("DONE", "green", cid="not-a-cell"))


class TestValidationPrinting(unittest.TestCase):

    def _out(self, results, quiet=False):
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(runs.experiment.exp(), "validate", return_value=results), \
             contextlib.redirect_stdout(buf):
            runs.render.results_validate(SimpleNamespace(selector=None), quiet=quiet)
        return buf.getvalue()

    def test_nothing_finished(self):
        self.assertIn("no DONE cells match", self._out([]))

    def test_a_tainted_cell_is_always_named_or_counted(self):
        doc = {"verdict": "TAINTED", "taints": ["rig"], "warns": []}
        self.assertIn("c1: rig", self._out([("c1", "green", doc)]))
        self.assertIn("1 TAINTED", self._out([("c1", "green", doc)], quiet=True))

    def test_a_held_cell_is_shown_not_validated(self):
        self.assertIn("HELD", self._out([("c1", "green", None)]))


if __name__ == "__main__":
    unittest.main()
