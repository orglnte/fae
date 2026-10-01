"""fae/driver/score.py's aggregate(): the argv it hands to the fae/scoring/aggregate.py
subprocess must carry every flag the CLI declared, or a flag that parses fine
on the command line silently never reaches the script that reads it."""
import unittest
from types import SimpleNamespace
from unittest import mock

from _ctx import runs

score = runs.score


class TestAggregateForwardsSortDiscrepancy(unittest.TestCase):

    def _argv(self, **kw):
        with mock.patch.object(score.subprocess, "run") as run:
            score.aggregate(SimpleNamespace(**kw))
        return run.call_args.args[0]

    def test_sort_discrepancy_true_is_forwarded(self):
        self.assertIn("--sort-discrepancy", self._argv(sort_discrepancy=True))

    def test_sort_discrepancy_false_is_not_forwarded(self):
        self.assertNotIn("--sort-discrepancy", self._argv(sort_discrepancy=False))

    def test_sort_discrepancy_absent_is_not_forwarded(self):
        # Namespaces built without the field at all (an older caller) must
        # not crash aggregate() and must not silently enable the flag.
        self.assertNotIn("--sort-discrepancy", self._argv())

    def test_sort_significant_true_is_forwarded(self):
        self.assertIn("--sort-significant", self._argv(sort_significant=True))

    def test_sort_significant_false_is_not_forwarded(self):
        self.assertNotIn("--sort-significant", self._argv(sort_significant=False))

    def test_sort_significant_absent_is_not_forwarded(self):
        self.assertNotIn("--sort-significant", self._argv())

    def test_both_sort_flags_can_be_forwarded_together(self):
        argv = self._argv(sort_discrepancy=True, sort_significant=True)
        self.assertIn("--sort-discrepancy", argv)
        self.assertIn("--sort-significant", argv)


if __name__ == "__main__":
    unittest.main()
