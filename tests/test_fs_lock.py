"""fs_lock — the Python half of the mutex.

A lock is a FILE held by flock(2) on an open fd. Bash and Python contend for
the same file and the kernel arbitrates, so there is no on-disk protocol to
keep in step.

Every test locks inside a TemporaryDirectory. None of them touch the real
`.locks/`, which the live fleet is using right now.
"""
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from _ctx import ROOT, runs

MUTEX_PY = Path(ROOT) / "fae" / "mutex.py"


class Deadline(Exception):
    """Deliberately NOT a TimeoutError. TimeoutError subclasses OSError, and
    an alarm firing inside one of the mutex's own OSError handlers would be
    swallowed rather than failing the test."""


@contextmanager
def deadline(seconds=30):
    """Fail loudly instead of hanging — fs_lock's acquire polls until it wins,
    so a protocol bug is an infinite loop."""
    def blow(*_):
        raise Deadline(f"fs_lock did not settle within {seconds}s")
    old = signal.signal(signal.SIGALRM, blow)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class FsLockTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.lock = self.root / "some.lock"
        self.addCleanup(self._tmp.cleanup)

    def holder_process(self):
        """A separate process holding self.lock, for real contention."""
        code = (
            "import sys, time, importlib.util as i;"
            f"sp=i.spec_from_file_location('m', {str(MUTEX_PY)!r});"
            "m=i.module_from_spec(sp); sp.loader.exec_module(m);"
            f"f=m.open_lock({str(self.lock)!r});"
            "assert m.try_fd(f.fileno());"
            "print('ready', flush=True); time.sleep(300)"
        )
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p

    def _reap(self, p):
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)
        if p.stdout is not None:
            p.stdout.close()


class TestAcquireRelease(FsLockTestCase):
    def test_the_lock_file_is_created_and_kept(self):
        with deadline(), runs.mutex.fs_lock(self.lock):
            self.assertTrue(self.lock.exists())
        # The FILE outlives the holder on purpose: removing and recreating it
        # would put two holders on two inodes.
        self.assertTrue(self.lock.exists())

    def test_it_is_released_on_exit(self):
        with deadline(), runs.mutex.fs_lock(self.lock):
            pass
        self.assertFalse(runs.mutex.probe_held(self.lock))

    def test_it_is_held_inside_the_block(self):
        with deadline(), runs.mutex.fs_lock(self.lock):
            self.assertTrue(runs.mutex.probe_held(self.lock))

    def test_sequential_acquisitions_of_the_same_lock(self):
        for _ in range(3):
            with deadline(), runs.mutex.fs_lock(self.lock):
                pass

    def test_the_file_object_outlives_enter(self):
        """The file object IS the lock: if it were a local in __enter__ the
        fd would close on return and the lock would be silently released."""
        with deadline(), runs.mutex.fs_lock(self.lock) as lk:
            self.assertIsNotNone(lk._f)
            self.assertTrue(runs.mutex.probe_held(self.lock))

    def test_a_mkdir_era_lock_directory_is_refused_loudly(self):
        self.lock.mkdir()
        with self.assertRaises(IsADirectoryError):
            with runs.mutex.fs_lock(self.lock):
                pass


class TestContention(FsLockTestCase):
    def test_a_live_holder_is_respected(self):
        self.holder_process()
        with self.assertRaises(TimeoutError):
            with runs.mutex.fs_lock(self.lock, timeout=1.0):
                pass

    def test_the_timeout_names_the_holder(self):
        self.holder_process()
        runs.mutex.note_holder(self.lock, "cell-xyz", 4242)
        with self.assertRaises(TimeoutError) as e:
            with runs.mutex.fs_lock(self.lock, timeout=1.0):
                pass
        self.assertIn("cell-xyz", str(e.exception))


class TestDeathReleases(FsLockTestCase):
    """Recovery is the kernel's job: no steal, no grace, no reaper."""

    def test_a_killed_holders_lock_is_free_at_once(self):
        p = self.holder_process()
        self.assertTrue(runs.mutex.probe_held(self.lock))
        p.kill()
        p.wait(timeout=10)
        end = time.monotonic() + 1.0
        while time.monotonic() < end and runs.mutex.probe_held(self.lock):
            time.sleep(0.02)
        with deadline(5), runs.mutex.fs_lock(self.lock):
            pass


if __name__ == "__main__":
    unittest.main()
