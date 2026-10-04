"""Experiment.aggregate(): the argv it hands to the fae/experiment/scoring/aggregate.py
process must carry every option the CLI declared, or an option that parses
fine on the command line silently never reaches the script that reads it;
and the process must aggregate the workspace it was asked about."""
import unittest
from unittest import mock

from _ctx import runs


class TestAggregateForwardsOptions(unittest.TestCase):

    def _run(self, **kw):
        with mock.patch.object(runs.experiment.subprocess, "run") as run:
            runs.shared.current().aggregate(**kw)
        return run.call_args

    def _argv(self, **kw):
        return self._run(**kw).args[0]

    def test_sort_discrepancy_true_is_forwarded(self):
        self.assertIn("--sort-discrepancy", self._argv(sort_discrepancy=True))

    def test_sort_discrepancy_false_is_not_forwarded(self):
        self.assertNotIn("--sort-discrepancy", self._argv(sort_discrepancy=False))

    def test_sort_discrepancy_absent_is_not_forwarded(self):
        self.assertNotIn("--sort-discrepancy", self._argv())

    def test_sort_significant_true_is_forwarded(self):
        self.assertIn("--sort-significant", self._argv(sort_significant=True))

    def test_sort_significant_absent_is_not_forwarded(self):
        self.assertNotIn("--sort-significant", self._argv())

    def test_both_sort_flags_can_be_forwarded_together(self):
        argv = self._argv(sort_discrepancy=True, sort_significant=True)
        self.assertIn("--sort-discrepancy", argv)
        self.assertIn("--sort-significant", argv)

    def test_filters_and_switches_are_forwarded(self):
        argv = self._argv(variant="beta_apidocs", where=["docs=apidocs"], impl="py",
                          allow_stale=True, include_tainted=True, tainted_cells_details=True)
        for part in ("--allow-stale", "--include-tainted", "--tainted-cells-details"):
            self.assertIn(part, argv)
        self.assertEqual(argv[argv.index("--variant") + 1], "beta_apidocs")
        self.assertEqual(argv[argv.index("--where") + 1], "docs=apidocs")
        self.assertEqual(argv[argv.index("--impl") + 1], "py")

    def test_an_unknown_option_is_refused(self):
        with self.assertRaises(TypeError):
            self._argv(sort_by_magic=True)

    def test_the_process_aggregates_the_workspace_named(self):
        exp = runs.shared.current()
        other = exp.beside(exp.root / "other.nosync")
        env = self._run(workspace=other).kwargs["env"]
        self.assertEqual(env["WORKSPACES_DIR"], str(other.path))

    def test_by_default_the_experiment_workspace(self):
        exp = runs.shared.current()
        self.assertEqual(self._run().kwargs["env"]["WORKSPACES_DIR"], str(exp.workspace.path))


if __name__ == "__main__":
    unittest.main()
