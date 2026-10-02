"""A results.csv row names its variant by id and carries the factors the
variant is a level of, so a table can be cut by either."""
import unittest

from _ctx import ROOT  # noqa: F401

from fae.scoring import aggregate


class TestTheRow(unittest.TestCase):
    def test_the_row_keeps_the_variant_id(self):
        self.assertEqual(aggregate.flat_row({"variant": "x_sealed_apidocs"})["variant"],
                         "x_sealed_apidocs")

    def test_the_factors_are_one_sorted_text_column(self):
        row = aggregate.flat_row({"variant": "v", "factors": {"docs": "apidocs", "access": "sealed"}})
        self.assertEqual(row["factors"], "access=sealed;docs=apidocs")
        self.assertEqual(aggregate.flat_row({"variant": "v"})["factors"], "")


if __name__ == "__main__":
    unittest.main()
