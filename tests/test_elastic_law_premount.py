"""A spike absorbed by a cache that no earlier spike holds — mounted just
before it, up at its start — never saturates the store, and the law reads
a void. With the LAW_CHARGE_PREMOUNT switch, off by default, it is charged
as mounted before the spike. (Off is pinned by test_elastic_law_tolerance's
GOLDEN.)"""
import json
import os
import unittest

from _ctx import ROOT  # noqa: F401  (sys.path)

import test_elastic_law as T
from fae.experiment import config as config_mod

law = T.law

# blip [8..30], spike [38..91], blip [99..121]
BSB = {"shape": "BSB", "events": [{"kind": "blip", "t0": 8.0, "t1": 30.0},
                                  {"kind": "spike", "t0": 38.0, "t1": 91.0},
                                  {"kind": "blip", "t0": 99.0, "t1": 121.0}]}


def absorbed(mounted):
    """No tick saturates: the cache is up through the spike."""
    return T.rows(140, saturated_from=None, mounted=mounted)


class TestAPremountedSpike(unittest.TestCase):
    def test_is_void_with_the_switch_off(self):
        v, why = T.judge(absorbed([(37, 95)]), BSB)
        self.assertEqual(v, "void")
        self.assertIn("spike 1 never saturated", why[0])

    def test_is_charged_with_it_on(self):
        v, why = T.judge(absorbed([(37, 95)]), BSB, charge_premount=True)
        self.assertEqual(v, "false")
        self.assertEqual(why, ["episode 1 mounted before spike 1 (ts=37, spike at 38) with no "
                               "earlier spike to hold it, and absorbed it: p99 max 12ms < 150ms "
                               "over [38..91]"])

    def test_a_mount_inside_the_grace_after_the_start_counts(self):
        self.assertEqual(T.judge(absorbed([(40, 95)]), BSB, charge_premount=True)[0], "false")

    def test_a_mount_after_the_grace_is_not_one(self):
        self.assertEqual(T.judge(absorbed([(42, 95)]), BSB, charge_premount=True)[0], "void")

    def test_an_episode_released_by_the_start_is_not_one(self):
        self.assertEqual(T.judge(absorbed([(20, 41)]), BSB, charge_premount=True)[0], "void")

    def test_an_earlier_spikes_cache_stays_the_held_failure(self):
        r = T.rows(170, saturated_from=20, mounted=[(28, 140)])
        v, why = T.judge(r, T.SSB, charge_premount=True)
        self.assertEqual(v, "false")
        self.assertIn("never released before spike", why[0])

    def test_errors_ride_along(self):
        v, why = T.judge(absorbed([(37, 95)]), BSB, errors=6, charge_premount=True)
        self.assertEqual(v, "false")
        self.assertIn("load errors 6/6000", why[1])

    def test_a_spike_that_saturated_is_judged_as_before(self):
        r = T.rows(140, saturated_from=40, mounted=[(46, 95)])
        self.assertEqual(T.judge(r, BSB, charge_premount=True), T.judge(r, BSB))


class TestTheSwitch(unittest.TestCase):
    def test_main_reads_it(self):
        self.assertEqual(law.switches({"LAW_CHARGE_PREMOUNT": "1"}), {"charge_premount": True})
        self.assertEqual(law.switches({"LAW_CHARGE_PREMOUNT": ""}), {})

    def test_is_off_by_default_and_reaches_the_verify_container_when_set(self):
        self.assertEqual(config_mod.ENGINE_DEFAULTS["LAW_CHARGE_PREMOUNT"], "")
        self.assertIn("LAW_CHARGE_PREMOUNT", config_mod._EXPORT_KEYS)


if __name__ == "__main__":
    unittest.main()
