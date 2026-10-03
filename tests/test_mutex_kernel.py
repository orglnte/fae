"""The kernel is the lock. These tests prove the properties the whole design
rests on, before any caller depends on them.

A lock lives on the open file description, not on disk. So it is released by
the kernel when the holder dies — no steal, no grace, no reaper — and it is
NOT released while any inherited fd is still open, which is the one way this
mechanism fails silently.

The holder here is a Python process that takes the lock the way every holder
in this rig does — an fd it opened itself, flock'd, kept open (Arena,
Cell.verify_lock_acquire, Verify.rig_lock_acquire, fae/mutex.py's fs_lock). The bash
holder these tests used to drive went with the bash hooks.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _ctx import ROOT, runs  # noqa: F401  (ROOT is what we need)

sys.path.insert(0, str(Path(ROOT) / "fae"))
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("mutex", Path(ROOT) / "fae" / "mutex.py")
mutex = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(mutex)

# A holder: flock on its own fd, then `body`. "child-inherits" hands the fd
# down the way the Arena does (inheritable, close_fds=False); "child-clean"
# starts the same child under subprocess's default close_fds=True. Prints
# "ready <child pid or ->" once the lock is held.
HOLDER = r'''
import fcntl, os, subprocess, sys, time
lock, body = sys.argv[1], sys.argv[2]
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
child = None
if body == "child-inherits":
    os.set_inheritable(fd, True)
    child = subprocess.Popen(["sleep", "300"], close_fds=False)
elif body == "child-clean":
    child = subprocess.Popen(["sleep", "300"])
print("ready", child.pid if child else "-", flush=True)
if body == "exit":
    sys.exit(0)
time.sleep(300)
'''


class KernelLockTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.lock = self.dir / "slot-1"
        self.lock.touch()
        self.addCleanup(self._tmp.cleanup)

    def held(self):
        return mutex.probe_held(self.lock)

    def holder(self, body="sleep"):
        """A process holding the lock on an fd it opened, like a cell driver.
        Returns (process, child pid or None)."""
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.lock), body],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
        line = p.stdout.readline().split()
        self.assertEqual(line[:1], ["ready"], "the holder never acquired the lock")
        self.addCleanup(self._reap, p)
        child = int(line[1]) if len(line) > 1 and line[1] != "-" else None
        if child:
            self.addCleanup(self._kill_pid, child)
        return p, child

    @staticmethod
    def _kill_pid(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _reap(self, p):
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)
        if p.stdout is not None:
            p.stdout.close()

    def _kill_and_settle(self, p):
        p.send_signal(signal.SIGKILL)
        p.wait(timeout=10)
        # No steal, no grace: poll briefly only to let the kernel reap the
        # process, not to wait out any protocol timer.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and self.held():
            time.sleep(0.02)


class TestTheKernelReleasesOnDeath(KernelLockTestCase):
    def test_a_sigkilled_holder_releases_the_lock_immediately(self):
        """The point of the whole design. Under a holder-file protocol this
        lock would stay held until something judged it stale."""
        p, _ = self.holder()
        self.assertTrue(self.held(), "the holder should hold it")
        self._kill_and_settle(p)
        self.assertFalse(self.held(),
                         "a SIGKILLed holder's lock must be free at once")

    def test_a_normal_exit_releases_the_lock(self):
        p, _ = self.holder(body="exit")
        p.wait(timeout=10)
        self.assertFalse(self.held())


class TestInheritedFdsPinTheLock(KernelLockTestCase):
    """A flock is released when the LAST fd on the open file description
    closes. Anything forked with the lock fd inherits it and keeps a dead
    holder's lock alive — which is why subprocess's close_fds=True default is
    load-bearing and no child of a holder is handed the arena. The negative
    control below is the reason that discipline exists; do not delete it as
    redundant."""

    def test_a_child_without_the_fd_does_not_pin_it(self):
        p, _ = self.holder(body="child-clean")
        self._kill_and_settle(p)
        self.assertFalse(self.held(),
                         "a child started under close_fds=True must let the lock go")

    def test_a_child_that_inherited_the_fd_DOES_pin_it(self):
        # NEGATIVE CONTROL. This asserts the hazard, not a desired behaviour:
        # if it ever starts passing as "released", the fd is no longer being
        # inherited and the close_fds discipline has quietly become untested.
        p, child = self.holder(body="child-inherits")
        self._kill_and_settle(p)
        self.assertTrue(self.held(),
                        "an inherited fd is expected to pin the lock — that is "
                        "why no long-lived child may be handed it")
        self._kill_pid(child)


class TestMutualExclusion(KernelLockTestCase):
    def test_a_second_holder_is_refused(self):
        self.holder()
        fd = os.open(str(self.lock), os.O_RDWR)
        try:
            self.assertFalse(mutex.try_fd(fd), "two holders at once")
        finally:
            os.close(fd)

    def test_try_fds_picks_the_first_free_slot(self):
        paths = [self.dir / f"slot-{i}" for i in range(1, 4)]
        for p in paths:
            p.touch()
        fds = [os.open(str(p), os.O_RDWR) for p in paths]
        self.addCleanup(lambda: [os.close(f) for f in fds])
        blocker = os.open(str(paths[0]), os.O_RDWR)
        self.addCleanup(os.close, blocker)
        self.assertTrue(mutex.try_fd(blocker))
        self.assertEqual(mutex.try_fds(fds), fds[1],
                         "the scan must skip the taken slot")

    def test_re_flocking_our_own_open_file_description_succeeds(self):
        # A second flock on the SAME description is not contention — the
        # kernel's rule that lets a holder re-assert a lock it already has.
        fd = os.open(str(self.lock), os.O_RDWR)
        self.addCleanup(os.close, fd)
        self.assertTrue(mutex.try_fd(fd))
        self.assertTrue(mutex.try_fd(fd))


if __name__ == "__main__":
    unittest.main()
