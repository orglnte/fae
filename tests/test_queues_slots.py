"""The slots a cell holds while it runs: one file per slot, flocked by the
cell's own process; closing is the release."""
import fcntl
import tempfile
import unittest
from pathlib import Path

from fae.conduct import Queues


class SlotCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / ".queues"
        self.q = Queues(self.base)

    def hold(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)


class TestOpeningTheSlots(SlotCase):

    def test_one_file_per_slot_of_each_pool(self):
        s = self.q.open_slots(2, lock="beta", lock_slots=3)
        self.addCleanup(s.close)
        self.assertEqual([p.name for p in self.q.slot_files()], ["slot-1", "slot-2"])
        self.assertEqual(len(self.q.slot_files("beta")), 3)
        self.assertEqual(self.q.slot_pools(), ["beta"])

    def test_a_lock_DIRECTORY_is_refused(self):
        # mkdir locks and flock locks do not exclude each other at all, so a
        # tree holding both has no cap — refusing to start beats running
        # uncapped.
        (self.base / "work-slots" / "slot-1").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            self.q.open_slots(1)

    def test_a_cap_over_the_slot_block_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.q.open_slots(99)

    def test_closing_twice_is_a_no_op(self):
        s = self.q.open_slots(1)
        s.close()
        s.close()
        self.assertEqual(s.held, [])


class TestTakingASlot(SlotCase):

    def test_the_work_slot_then_the_lock_slot(self):
        slots, pool = self.q.try_slots("c1", 1, lock="beta", lock_slots=1)
        self.addCleanup(slots.close)
        self.assertIsNone(pool)
        self.assertEqual([p.parent.name for p in slots.held], ["work-slots", "arm-beta.slots"])
        self.assertTrue(all(self.q.slot_held(p) for p in slots.held))
        self.assertEqual(self.q.slot_note(slots.held[0])[0], "c1")

    def test_only_the_held_files_stay_open(self):
        slots, _ = self.q.try_slots("c1", 3)
        self.addCleanup(slots.close)
        self.assertEqual(len(slots.fds()), 1)
        fd, path = slots.handover().split(":", 1)
        self.assertEqual(int(fd), slots.fds()[0])
        self.assertEqual(Path(path), slots.held[0])

    def test_closing_is_the_release(self):
        slots, _ = self.q.try_slots("c1", 1)
        held = slots.held[0]
        slots.close()
        self.assertFalse(self.q.slot_held(held))

    def test_a_full_work_pool_takes_nothing_and_never_waits(self):
        self.hold(self.base / "work-slots" / "slot-1")
        self.assertEqual(self.q.try_slots("c1", 1, lock="beta", lock_slots=1), (None, "work"))
        self.assertFalse(self.q.slot_held(self.base / "arm-beta.slots" / "slot-1"))

    def test_a_full_lock_pool_gives_the_work_slot_back(self):
        self.hold(self.base / "arm-beta.slots" / "slot-1")
        self.assertEqual(self.q.try_slots("c1", 1, lock="beta", lock_slots=1), (None, "lock"))
        self.assertFalse(self.q.slot_held(self.base / "work-slots" / "slot-1"))

    def test_the_holder_notes_naming_a_cell_are_cleared(self):
        slots, _ = self.q.try_slots("c1", 1)
        self.addCleanup(slots.close)
        self.q.clear_holder_notes("c1")
        self.assertIsNone(self.q.slot_note(slots.held[0]))


class TestAdoptingHandedSlots(SlotCase):
    """The cell process adopts the open slot files its admission handed it."""

    def _handed(self):
        import os
        taken, _ = self.q.try_slots("c1", 1)
        fd = os.dup(taken.fds()[0])          # what the child inherits
        path = taken.held[0]
        taken.close()                        # the admitting side closes its copy
        return f"{fd}:{path}", path

    def test_a_handed_slot_stays_held_and_is_noted_for_the_cell(self):
        import os
        handover, path = self._handed()
        slots = self.q.adopt_slots("c9", handover)
        self.addCleanup(slots.close)
        self.assertEqual(slots.held, [path])
        self.assertTrue(self.q.slot_held(path))
        self.assertEqual(self.q.slot_note(path)[:2], ("c9", os.getpid()))

    def test_a_slot_held_by_another_holder_is_not_adopted(self):
        import os
        path = self.base / "work-slots" / "slot-1"
        self.hold(path)
        fd = os.open(str(path), os.O_RDWR)
        self.assertIsNone(self.q.adopt_slots("c9", f"{fd}:{path}"))

    def test_nothing_handed_is_nothing_adopted(self):
        self.assertIsNone(self.q.adopt_slots("c9", ""))


if __name__ == "__main__":
    unittest.main()
