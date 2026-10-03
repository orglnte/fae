"""The slots a cell holds while it runs: one file per slot, flocked by the
cell's own process; closing is the release."""
import fcntl
import tempfile
import unittest
from pathlib import Path

from fae.queues import Queues


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
        order = []
        slots, queue = self.q.acquire_slots(
            "c1", 1, lock="beta", lock_slots=1,
            on_wait_work=lambda: order.append("wait-work"),
            on_work_slot=lambda: order.append("work"),
            on_wait_lock=lambda: order.append("wait-lock"))
        self.addCleanup(slots.close)
        self.assertIsNone(queue)
        self.assertEqual(order, ["wait-work", "work", "wait-lock"])
        self.assertEqual([p.parent.name for p in slots.held], ["work-slots", "arm-beta.slots"])
        self.assertTrue(all(self.q.slot_held(p) for p in slots.held))
        self.assertEqual(self.q.slot_note(slots.held[0])[0], "c1")

    def test_closing_is_the_release(self):
        slots, _ = self.q.acquire_slots("c1", 1)
        held = slots.held[0]
        slots.close()
        self.assertFalse(self.q.slot_held(held))

    def test_a_full_pool_and_a_pause_stand_the_cell_down_by_queue_name(self):
        ws = Path(self.base).parent / "workspaces.nosync" / "c1"
        ws.mkdir(parents=True)
        (ws / ".paused").write_text("test\n")
        self.hold(self.base / "work-slots" / "slot-1")
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"WORKSPACES_DIR": str(ws.parent)}):
            slots, queue = self.q.acquire_slots("c1", 1)
        self.addCleanup(slots.close)
        self.assertEqual(queue, "slot-queue")
        self.assertEqual(slots.held, [])

    def test_the_holder_notes_naming_a_cell_are_cleared(self):
        slots, _ = self.q.acquire_slots("c1", 1)
        self.addCleanup(slots.close)
        self.q.clear_holder_notes("c1")
        self.assertIsNone(self.q.slot_note(slots.held[0]))


if __name__ == "__main__":
    unittest.main()
