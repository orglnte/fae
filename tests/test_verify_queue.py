"""The verify lock is taken first come, first served (fae/mutex.py
queue_acquire): a waiter that polls slowly still goes before a later arrival
that polls fast, a dead waiter's ticket never blocks the queue, and a pause
stands a waiter down without leaving a ticket behind."""
from __future__ import annotations

import fcntl
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401
from fae import mutex


def tickets(lock):
    q = mutex.queue_dir(lock)
    return sorted(p.name for p in q.iterdir() if not p.name.startswith(".")) if q.is_dir() else []


class Case(unittest.TestCase):

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.lock = self.d / "verify-lock"

    def hold(self):
        fh = mutex.open_lock(self.lock)
        self.assertTrue(mutex.try_fd(fh))
        return fh

    def waiter(self, cid, poll, order):
        def run():
            fh = mutex.queue_acquire(self.lock, cid, poll=poll)
            order.append(cid)
            time.sleep(0.05)
            fh.close()
        t = threading.Thread(target=run)
        t.start()
        return t

    def until(self, cond, timeout=5.0):
        t0 = time.monotonic()
        while not cond():
            if time.monotonic() - t0 > timeout:
                self.fail("timed out")
            time.sleep(0.005)


class TestFirstComeFirstServed(Case):

    def test_a_slow_poller_that_came_first_goes_before_fast_later_arrivals(self):
        holder = self.hold()
        order = []
        a = self.waiter("a", 0.3, order)
        self.until(lambda: len(tickets(self.lock)) == 1)
        b = self.waiter("b", 0.01, order)
        self.until(lambda: len(tickets(self.lock)) == 2)
        c = self.waiter("c", 0.01, order)
        self.until(lambda: len(tickets(self.lock)) == 3)
        holder.close()
        for t in (a, b, c):
            t.join(10)
        self.assertEqual(order, ["a", "b", "c"])
        self.assertEqual(tickets(self.lock), [])

    def test_the_winner_holds_the_lock_itself(self):
        fh = mutex.queue_acquire(self.lock, "a", poll=0.01)
        probe = mutex.open_lock(self.lock)
        self.assertFalse(mutex.try_fd(probe))
        probe.close()
        fh.close()


class TestDeadWaiters(Case):

    def test_a_ticket_nobody_holds_is_skipped_and_removed(self):
        q = mutex.queue_dir(self.lock)
        q.mkdir()
        dead = q / f"{0:020d}-1-dead"
        dead.touch()
        fh = mutex.queue_acquire(self.lock, "a", poll=0.01)
        self.assertIsNotNone(fh)
        self.assertFalse(dead.exists())
        fh.close()

    def test_a_held_older_ticket_keeps_a_later_waiter_out(self):
        q = mutex.queue_dir(self.lock)
        q.mkdir()
        older = open(q / f"{0:020d}-1-older", "a+")
        fcntl.flock(older, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stops = []
        fh = mutex.queue_acquire(self.lock, "b", poll=0.01,
                                 stop=lambda: stops.append(1) or len(stops) > 20)
        self.assertIsNone(fh)
        older.close()
        probe = mutex.open_lock(self.lock)
        self.assertTrue(mutex.try_fd(probe))
        probe.close()


class TestPause(Case):

    def test_a_paused_waiter_stands_down_and_leaves_no_ticket(self):
        holder = self.hold()
        fh = mutex.queue_acquire(self.lock, "a", poll=0.01, stop=lambda: True)
        self.assertIsNone(fh)
        self.assertEqual(tickets(self.lock), [])
        holder.close()


if __name__ == "__main__":
    unittest.main()
