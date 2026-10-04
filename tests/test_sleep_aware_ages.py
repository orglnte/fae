"""A host suspend must not read as a stall.

Every supervisory age is wall clock minus a wall stamp; conduct records the
sleeps it observes (wall advanced, monotonic did not) and subtracts them.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs, patch_plane


class SleepCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patch_plane(self, Path(self._tmp.name))
        self.patches = [mock.patch.object(runs.host, "_sleep_clocks", None),
                        mock.patch.object(runs.host, "_sleep_gaps", None)]
        for p in self.patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def book(self):
        return json.loads(runs.host._host_sleep_book().read_text())


class TestObservingTheClocks(SleepCase):
    def test_clocks_that_advance_together_record_nothing(self):
        runs.host.host_sleep_observe(now=1000.0, mono=10.0)
        self.assertEqual(runs.host.host_sleep_observe(now=1300.0, mono=310.0), 0)
        self.assertFalse(runs.host._host_sleep_book().exists())

    def test_wall_running_ahead_of_monotonic_is_a_sleep(self):
        runs.host.host_sleep_observe(now=1000.0, mono=10.0)
        gap = runs.host.host_sleep_observe(now=8200.0, mono=310.0)
        self.assertEqual(gap, 6900.0)
        self.assertEqual(self.book(), [{"start": 1000.0, "s": 6900.0}])

    def test_scheduler_jitter_stays_below_the_threshold(self):
        runs.host.host_sleep_observe(now=1000.0, mono=10.0)
        self.assertEqual(runs.host.host_sleep_observe(now=1320.0, mono=310.0), 0)

    def test_the_first_observation_has_nothing_to_compare(self):
        self.assertEqual(runs.host.host_sleep_observe(now=1000.0, mono=10.0), 0)

    def test_old_gaps_are_pruned(self):
        stale = {"start": 1.0, "s": 100.0}
        runs.host._host_sleep_book().write_text(json.dumps([stale]))
        now = 30 * 86400.0
        runs.host.host_sleep_observe(now=now, mono=10.0)
        runs.host.host_sleep_observe(now=now + 500.0, mono=20.0)
        self.assertEqual(self.book(), [{"start": now, "s": 490.0}])

    def test_a_corrupt_book_is_an_empty_one(self):
        runs.host._host_sleep_book().write_text("{not json")
        self.assertEqual(runs.host._host_sleep_gaps(), [])
        self.assertEqual(runs.host.awake_age(0.0, now=50.0), 50.0)


class TestAwakeAge(SleepCase):
    def gaps(self, *gs):
        runs.host._host_sleep_book().write_text(
            json.dumps([{"start": a, "s": s} for a, s in gs]))

    def test_a_sleep_inside_the_interval_is_subtracted(self):
        self.gaps((1000.0, 6900.0))
        self.assertEqual(runs.host.awake_age(500.0, now=8200.0), 800.0)

    def test_a_sleep_before_the_stamp_is_not(self):
        self.gaps((100.0, 300.0))
        self.assertEqual(runs.host.awake_age(500.0, now=800.0), 300.0)

    def test_a_sleep_straddling_the_stamp_counts_its_overlap(self):
        self.gaps((400.0, 300.0))            # asleep 400..700
        self.assertEqual(runs.host.awake_age(500.0, now=800.0), 100.0)

    def test_no_book_means_plain_wall_age(self):
        self.assertEqual(runs.host.awake_age(500.0, now=800.0), 300.0)


class TestTheAgesTheSupervisorReads(SleepCase):
    """The sites conduct judges a stall by, each fed a 2h sleep."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        runs.host._host_sleep_book().write_text(json.dumps(
            [{"start": self.now - 7000.0, "s": 6900.0}]))

    def test_phase_age_excludes_the_sleep(self):
        ws = Path(self._tmp.name) / "aaa_high_beta_apidocs_T1_r1"
        ws.mkdir()
        (ws / ".loop").write_text(
            f"pid={os.getpid()} cid={ws.name} phase=verify-lock attempt=3 "
            f"ts={int(self.now)} since={int(self.now - 7200)}\n")
        with mock.patch.object(runs.host, "run_cell_pids",
                               return_value={os.getpid()}):
            hb = runs.host.heartbeat(ws)
        self.assertLess(hb["phase_age"], 400)
        self.assertGreater(hb["phase_age"], 250)

    def test_transition_age_excludes_the_sleep(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 7200))
        age = runs.supervise._age_of(stamp)
        self.assertLess(age, 400)
        self.assertGreater(age, 250)

    def test_arm_slot_age_excludes_the_sleep(self):
        slot = self.queues / "arm-keda.slots" / "slot-1"
        slot.parent.mkdir()
        slot.touch()
        Path(str(slot) + ".holder").write_text(
            f"cid 1234 {int(self.now - 7200)}\n")
        with mock.patch.object(runs.mutex, "holder_name", return_value="cid"), \
             mock.patch.object(runs.queues_module.Queues, "slot_held", return_value=True):
            held = runs.supervise._arm_slot_of("keda", "cid")
        self.assertLess(held, 400)
        self.assertGreater(held, 250)


if __name__ == "__main__":
    unittest.main()
