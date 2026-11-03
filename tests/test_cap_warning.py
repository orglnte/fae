"""The run's cap warning compares the global cap with what the non-empty
lanes can admit under their per-lane caps."""
import unittest

from _ctx import ROOT  # noqa: F401  (sys.path)
from fae.conduct import cap_warning


class TestCapWarning(unittest.TestCase):
    def test_a_cap_inside_the_lanes_range_earns_no_warning(self):
        self.assertIsNone(cap_warning(5, {"a": 2, "b": 2, "c": 2}))
        self.assertIsNone(cap_warning(3, {"a": 2, "b": 2, "c": 2}))
        self.assertIsNone(cap_warning(6, {"a": 2, "b": 2, "c": 2}))

    def test_below_one_cell_per_lane_lanes_starve(self):
        self.assertIn("lanes will starve", cap_warning(2, {"a": 2, "b": 2, "c": 2}))

    def test_above_the_summed_lane_caps_the_cap_does_not_limit(self):
        msg = cap_warning(7, {"a": 2, "b": 2, "c": 2})
        self.assertIn("not what limits admission", msg)
        self.assertIn("> 6", msg)

    def test_a_lane_override_counts_its_own_cap(self):
        self.assertIsNone(cap_warning(5, {"a": 1, "b": 3, "c": 1}))
        self.assertIn("> 4", cap_warning(5, {"a": 1, "b": 2, "c": 1}))


if __name__ == "__main__":
    unittest.main()
