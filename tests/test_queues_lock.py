"""The queues-lock: every change to .queues/ holds .locks/queues-lock for the
change alone, so concurrent writers never interleave, and a cell waiting for
or holding a slot never blocks a change."""
import fcntl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _ctx import ROOT

from fae.queues import Queues

WRITER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from fae.queues import Queues
q = Queues(sys.argv[2], cell_id=lambda a, v, r, t: f"{a}_high_{v}_{t}_r{r}")
who, n = sys.argv[3], int(sys.argv[4])
for i in range(n):
    q.enqueue("aaa", {"variant": f"v{who}", "rep": i}, front=bool(i % 2))
"""


def cell_id(agent, variant, rep, task):
    return f"{agent}_high_{variant}_{task}_r{rep}"


class LockCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plane = Path(tmp.name)
        self.q = Queues(self.plane / ".queues", locks=self.plane / ".locks", cell_id=cell_id)

    def writer(self, who, n):
        return subprocess.Popen([sys.executable, "-c", WRITER, str(ROOT),
                                 str(self.plane / ".queues"), who, str(n)])


class TestConcurrentWritersStayConsistent(LockCase):

    def test_two_processes_filling_one_lane_lose_nothing_and_share_no_sequence(self):
        procs = [self.writer(w, 40) for w in ("a", "b")]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        specs = self.q.lane_specs("aaa")
        self.assertEqual(len(specs), 80)
        seqs = [p.name.split(".", 1)[0] for p in specs]
        self.assertEqual(len(set(seqs)), 80, "two specs share one place in the lane")
        self.assertFalse([p for p in specs if p.name.startswith("tmp-")])

    def test_the_lock_is_the_one_in_locks(self):
        self.q.enqueue("aaa", {"variant": "v", "rep": 1})
        self.assertTrue((self.plane / ".locks" / "queues-lock").is_file())


class TestASlotNeverBlocksAChange(LockCase):

    def test_a_held_slot_does_not_block_an_enqueue(self):
        slots, queue = self.q.acquire_slots("c1", 1)
        self.addCleanup(slots.close)
        self.assertIsNone(queue)
        p = self.writer("x", 1)
        self.assertEqual(p.wait(timeout=10), 0)
        self.assertEqual(len(self.q.lane_specs("aaa")), 1)

    def test_a_cell_waiting_for_a_slot_does_not_block_an_enqueue(self):
        slot = self.plane / ".queues" / "work-slots" / "slot-1"
        slot.parent.mkdir(parents=True)
        f = open(slot, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = {}
        t = threading.Thread(target=lambda: got.update(
            r=self.q.acquire_slots("c2", 1, poll=0.1)), daemon=True)
        t.start()
        time.sleep(0.3)
        self.assertTrue(t.is_alive(), "the cell is queued on the full pool")
        p = self.writer("y", 1)
        self.assertEqual(p.wait(timeout=10), 0)
        f.close()
        t.join(timeout=10)
        slots, queue = got["r"]
        self.addCleanup(slots.close)
        self.assertIsNone(queue)


if __name__ == "__main__":
    unittest.main()
