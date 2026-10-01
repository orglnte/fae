"""The results table names arms by their family label; the records keep ids."""
import unittest
from unittest import mock

from _ctx import ROOT  # noqa: F401

from fae.scoring import aggregate


class _Def:
    variants = {"kedap_sealed": type("K", (), {"LABEL": "KEDA"}),
                "terraform": type("T", (), {"LABEL": "Terraform"}),
                "plain_sealed": type("P", (), {"LABEL": ""})}

    def variant(self, arm):
        return self.variants.get(arm)


class TestArmLabel(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(aggregate, "_definition", return_value=_Def())
        p.start()
        self.addCleanup(p.stop)

    def test_a_labelled_pair_arm_reads_as_family_and_half(self):
        self.assertEqual(aggregate.arm_label("kedap_sealed"), "KEDA sealed")

    def test_a_labelled_single_arm_reads_as_its_label(self):
        self.assertEqual(aggregate.arm_label("terraform"), "Terraform")

    def test_an_unlabelled_or_unknown_arm_keeps_its_id(self):
        self.assertEqual(aggregate.arm_label("plain_sealed"), "plain_sealed")
        self.assertEqual(aggregate.arm_label("gone_access"), "gone_access")

    def test_the_csv_row_keeps_the_id(self):
        self.assertEqual(aggregate.flat_row({"treatment": "kedap_sealed"})["treatment"],
                         "kedap_sealed")


if __name__ == "__main__":
    unittest.main()
