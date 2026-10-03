"""smoke_variants: one reference cell per distinct way of being judged.
Variants that differ only in what the agent reads share one; a different
infra class, access, parameters, run, verify layer or lock is a different
way and gets its own."""
import unittest
from unittest import mock

from _ctx import runs

from fae.cell.infra.base import Infra


class TestOneSmokePerWayOfBeingJudged(unittest.TestCase):
    def setUp(self):
        self.d = runs.common.definition()

    def test_variants_that_differ_only_in_their_inputs_share_one(self):
        picked = runs.common.experiment().smoke_variants()
        self.assertEqual(len([v for v in picked if v.startswith("alpha_")]), 1)
        self.assertEqual(len([v for v in picked if v.startswith("beta_")]), 1)

    def test_another_infra_class_is_another_way(self):
        class Other(Infra):
            def alive(self):
                return True
        with mock.patch.object(self.d.variant("alpha_howto"), "INFRA", Other):
            picked = runs.common.experiment().smoke_variants()
        self.assertIn("alpha_howto", picked)

    def test_another_access_is_another_way(self):
        with mock.patch.object(self.d.variant("alpha_howto"), "ACCESS_INFRA", True):
            self.assertIn("alpha_howto", runs.common.experiment().smoke_variants())


if __name__ == "__main__":
    unittest.main()
