"""fae/mutex.py under real concurrency — N OS processes, one lock.

This is the test the bash implementation could never have. The mutex's entire
job is to coordinate separate processes through the filesystem, so it is
exercised with separate processes: tests/_mutex_worker.py, launched via
subprocess, each holding the lock for its own pid.

The invariant every test here checks is the same one: in the interleaving log
the workers write, an IN is always followed by the matching OUT before any
other IN appears. Two INs in a row means two holders — mutual exclusion lost.

Each test locks inside a TemporaryDirectory. Nothing touches the live `.orch/`
locks the fleet is using.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _ctx import ROOT

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "mutex", Path(ROOT) / "fae" / "mutex.py")
mutex = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(mutex)

WORKER = Path(__file__).resolve().parent / "_mutex_worker.py"
DEAD_PID = 999999


class ConcurrentTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.lock = self.root / "the.lock"
        self.log = self.root / "order.log"
        self.log.write_text("")
        (self.root / "ws").mkdir()
        self.env = dict(os.environ,
                        WORKSPACES_DIR=str(self.root / "ws"),
                        REPO_ROOT=str(ROOT))

    def tearDown(self):
        self._tmp.cleanup()

    def spawn(self, cid, hold=0.15, op="acquire"):
        return subprocess.Popen(
            [sys.executable, str(WORKER), str(self.lock), cid,
             str(self.log), str(hold), op],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def events(self):
        return [l.split() for l in self.log.read_text().splitlines() if l.strip()]

    def assert_mutual_exclusion(self, ignore=()):
        """No IN may follow an IN. That is the whole contract."""
        holder = None
        for kind, cid in self.events():
            if cid in ignore:
                continue
            if kind == "IN":
                self.assertIsNone(
                    holder,
                    f"{cid} entered while {holder} still held the lock — "
                    f"mutual exclusion violated. Log: {self.events()}")
                holder = cid
            else:
                self.assertEqual(holder, cid, f"OUT from non-holder {cid}")
                holder = None
        return holder


class TestMutualExclusion(ConcurrentTestCase):

    def test_six_contenders_serialise(self):
        procs = [self.spawn(f"cell-{i}") for i in range(6)]
        rcs = [p.wait(timeout=120) for p in procs]
        self.assertEqual(rcs, [0] * 6, "a contender failed to acquire")
        self.assert_mutual_exclusion()
        self.assertEqual(len(self.events()), 12, "not every worker ran")
        # The lock FILE outlives every holder on purpose: unlinking it and
        # recreating it would put two holders on two inodes.
        self.assertTrue(self.lock.exists(), "the lock file must not be removed")
        self.assertFalse(mutex.probe_held(self.lock), "still held after release")

    def test_all_six_distinct_cells_get_a_turn(self):
        procs = [self.spawn(f"cell-{i}") for i in range(6)]
        for p in procs:
            p.wait(timeout=120)
        seen = {cid for kind, cid in self.events() if kind == "IN"}
        self.assertEqual(seen, {f"cell-{i}" for i in range(6)})

    def test_contenders_queue_behind_a_killed_holder_and_all_proceed(self):
        """Recovery under contention. There is nothing to steal: the kernel
        released the killed holder's lock, so the queue simply drains."""
        holder = self.spawn("ghost", hold=30)
        time.sleep(0.6)                        # let it take the lock
        procs = [self.spawn(f"cell-{i}") for i in range(4)]
        time.sleep(0.3)
        holder.kill()
        holder.wait(timeout=30)
        rcs = [p.wait(timeout=120) for p in procs]
        self.assertEqual(rcs, [0] * 4)
        # The killed holder never wrote its OUT — that is what being killed
        # means. Exclusion is asserted over the survivors.
        self.assert_mutual_exclusion(ignore={"ghost"})


class TestOneWinnerPerLock(ConcurrentTestCase):
    """LOCK_EX is exclusive by construction, so the one-loop-per-workspace
    guarantee needs no separate variant: N racing non-blocking acquirers must
    produce exactly one winner."""

    def test_only_one_of_five_wins(self):
        procs = [self.spawn("same-cell", hold=0.4, op="try")
                 for _ in range(5)]
        rcs = [p.wait(timeout=120) for p in procs]
        self.assertEqual(rcs.count(0), 1,
                         f"expected exactly one winner, got rcs={rcs}")
        self.assertEqual(rcs.count(1), 4)
        self.assert_mutual_exclusion()


class TestPauseUnderContention(ConcurrentTestCase):

    def test_a_paused_contender_stands_down_instead_of_queuing(self):
        ws = self.root / "ws" / "paused-cell"
        ws.mkdir()
        (ws / ".paused").write_text("roster by=operator\n")
        blocker = self.spawn("holder-cell", hold=1.0)
        time.sleep(0.6)                       # let the blocker take it
        paused = self.spawn("paused-cell", hold=0.1)
        self.assertEqual(paused.wait(timeout=60), 44,
                         "paused cell did not stand down with PAUSE_EXIT")
        blocker.wait(timeout=60)
        ins = [cid for kind, cid in self.events() if kind == "IN"]
        self.assertNotIn("paused-cell", ins, "took a lock it must not use")


if __name__ == "__main__":
    unittest.main()
