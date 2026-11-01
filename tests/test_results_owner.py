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
                     "_workspace_of"):
            src = inspect.getsource(getattr(E, name))
            self.assertNotIn("fae.conduct", src, name)
            self.assertNotIn("fae.cli", src, name)

    def test_there_is_no_driver_package(self):
        self.assertFalse((ENGINE / "driver" / "__init__.py").exists())


class TestScoreScoresWhatValidateFound(unittest.TestCase):

    def _score(self, validated, rcs):
        exp = runs.experiment.exp()
        ws = SimpleNamespace(cell=lambda cid: SimpleNamespace(cid=cid, sealed=False))
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


class _Cell:
    """A sealed cell's derived files, in memory."""
    def __init__(self, cid, sealed=True, files=None, mtimes=None):
        self.cid, self.sealed, self.files = cid, sealed, dict(files or {})
        self._mtimes = mtimes or {"env": 1, "ledger": 2, "metrics": 3}

    @property
    def sealed_at(self):
        return "2026-11-01T00:00:00Z" if self.sealed else None

    def mtimes(self):
        return dict(self._mtimes)

    def read_derived(self, name):
        return self.files.get(name)

    def write_derived(self, name, text):
        self.files[name] = text


class TestAnUnchangedCellIsNotRecomputed(unittest.TestCase):

    def _done(self, rules="r1", **kw):
        import json
        c = _Cell("a", **kw)
        c.files.update({"validation.json": json.dumps({"verdict": "VALID"}), "score.json": "{}"})
        c.files["derived-key.json"] = json.dumps(
            runs.experiment.exp()._derived_key(c, rules))
        return c

    def test_same_rules_same_inputs_sealed_is_unchanged(self):
        self.assertTrue(runs.experiment.exp().unchanged(self._done(), "r1"))

    def test_new_rules_recompute(self):
        self.assertFalse(runs.experiment.exp().unchanged(self._done(), "r2"))

    def test_a_changed_input_recomputes(self):
        c = self._done()
        c._mtimes["ledger"] = 99
        self.assertFalse(runs.experiment.exp().unchanged(c, "r1"))

    def test_an_unsealed_cell_or_a_missing_record_recomputes(self):
        exp = runs.experiment.exp()
        self.assertFalse(exp.unchanged(_Cell("a", sealed=False), "r1"))
        c = self._done()
        del c.files["score.json"]
        self.assertFalse(exp.unchanged(c, "r1"))
        c = self._done()
        del c.files["derived-key.json"]
        self.assertFalse(exp.unchanged(c, "r1"))

    def test_score_skips_the_unchanged_and_keys_what_it_scored(self):
        exp = runs.experiment.exp()
        kept, fresh = self._done(), _Cell("b")
        cells = {"a": kept, "b": fresh}
        ws = SimpleNamespace(cell=cells.__getitem__)

        def validate(selector, ws_, on_cell=None, reuse=None):
            out = []
            for cid, c in cells.items():
                out.append((cid, "green", reuse(c) or {"verdict": "VALID", "fresh": True}))
            return out
        from fae.experiment.scoring import score_cell
        scored = []
        with mock.patch.object(exp, "rules_hash", return_value="r1"), \
             mock.patch.object(exp, "validate", side_effect=validate), \
             mock.patch.object(score_cell, "score_one",
                               side_effect=lambda cid, cell: scored.append(cid) or 0):
            n_ok, failed = exp.score(workspace=ws)
        self.assertEqual((n_ok, failed, scored), (2, [], ["b"]))
        import json
        self.assertEqual(json.loads(fresh.files["derived-key.json"])["rules"], "r1")

    def test_the_rules_hash_follows_the_experiments_code(self):
        import tempfile
        from fae.experiment import config as _config
        exp = runs.experiment.exp()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "taint.py").write_text("RULES = 1\n")
            with mock.patch.object(_config, "experiment_dir", return_value=d):
                h1 = exp.rules_hash()
                self.assertEqual(h1, exp.rules_hash())
                (Path(d) / "taint.py").write_text("RULES = 2\n")
                self.assertNotEqual(h1, exp.rules_hash())


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
