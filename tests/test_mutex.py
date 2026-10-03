"""fae/mutex.py — the one mutex, decision logic.

Concurrency behaviour that needs real processes lives in
test_mutex_concurrent.py, and the kernel properties the whole design rests on
live in test_mutex_kernel.py. This file covers the decisions: which fd wins,
does a pause stand us down, what does the CLI return, and does the filesystem
probe reach the right verdict.

Every test locks inside a TemporaryDirectory. Nothing touches the live
`.locks/` the fleet is using.
"""
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401  (puts the repo root on sys.path)

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("mutex", Path(ROOT) / "fae" / "mutex.py")
mutex = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(mutex)


class MutexTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.lock = self.root / "the.lock"
        # Point the module's workspace lookup at the temp tree so pause checks
        # cannot read (or be confused by) the live fleet.
        self._prev = os.environ.get("WORKSPACES_DIR")
        os.environ["WORKSPACES_DIR"] = str(self.root / "ws")
        (self.root / "ws").mkdir()
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("WORKSPACES_DIR", None)
        else:
            os.environ["WORKSPACES_DIR"] = self._prev

    def fd(self, path=None):
        f = mutex.open_lock(path or self.lock)
        self.addCleanup(f.close)
        return f


class TestTryFd(MutexTestCase):
    def test_a_free_lock_is_taken(self):
        self.assertTrue(mutex.try_fd(self.fd().fileno()))

    def test_a_second_open_file_description_is_refused(self):
        self.assertTrue(mutex.try_fd(self.fd().fileno()))
        self.assertFalse(mutex.try_fd(self.fd().fileno()))

    def test_try_fds_returns_the_first_free_fd(self):
        a, b = self.root / "a", self.root / "b"
        fa, fb = self.fd(a), self.fd(b)
        blocker = self.fd(a)
        self.assertTrue(mutex.try_fd(blocker.fileno()))
        self.assertEqual(mutex.try_fds([fa.fileno(), fb.fileno()]), fb.fileno())

    def test_try_fds_returns_None_when_all_busy(self):
        f = self.fd()
        blocker = self.fd()
        self.assertTrue(mutex.try_fd(blocker.fileno()))
        self.assertIsNone(mutex.try_fds([f.fileno()]))


class TestOpenLock(MutexTestCase):
    def test_a_mkdir_era_lock_directory_is_refused_loudly(self):
        """The cutover safety net: mkdir locks and flock locks do not exclude
        each other, so finding one is a half-finished migration, not a lock."""
        self.lock.mkdir()
        with self.assertRaises(IsADirectoryError) as e:
            mutex.open_lock(self.lock)
        self.assertIn("DIRECTORY", str(e.exception))

    def test_the_parent_is_created(self):
        deep = self.root / "a" / "b" / "slot-1"
        f = mutex.open_lock(deep)
        self.addCleanup(f.close)
        self.assertTrue(deep.exists())


class TestProbeHeld(MutexTestCase):
    def test_a_free_lock_probes_as_unheld(self):
        mutex.open_lock(self.lock).close()
        self.assertFalse(mutex.probe_held(self.lock))

    def test_a_held_lock_probes_as_held(self):
        self.assertTrue(mutex.try_fd(self.fd().fileno()))
        self.assertTrue(mutex.probe_held(self.lock))

    def test_probing_does_not_keep_the_lock(self):
        mutex.open_lock(self.lock).close()
        mutex.probe_held(self.lock)
        self.assertTrue(mutex.try_fd(self.fd().fileno()),
                        "the probe must release anything it took")


class TestHolderSidecar(MutexTestCase):
    """Display only. It names who last took the lock and proves nothing."""

    def test_note_and_read_back(self):
        mutex.note_holder(self.lock, "cell-a", 1234)
        self.assertEqual(mutex.holder_name(self.lock), "cell-a")

    def test_absent_sidecar_is_empty_not_an_error(self):
        self.assertEqual(mutex.holder_name(self.lock), "")

    def test_clear(self):
        mutex.note_holder(self.lock, "cell-a", 1234)
        mutex.clear_holder(self.lock)
        self.assertEqual(mutex.holder_name(self.lock), "")

    def test_the_sidecar_is_not_the_lock(self):
        mutex.note_holder(self.lock, "cell-a", 1234)
        self.assertFalse(mutex.probe_held(self.lock),
                         "a sidecar must never make a free lock look held")


class TestPauseAware(MutexTestCase):
    def test_pause_requested_is_existence_not_content(self):
        ws = self.root / "ws" / "cell-a"
        ws.mkdir()
        (ws / ".paused").write_text("")
        self.assertTrue(mutex.pause_requested("cell-a"),
                        "an empty .paused still means paused, as bash reads it")

    def test_wait_stands_down_when_paused(self):
        ws = self.root / "ws" / "cell-a"
        ws.mkdir()
        (ws / ".paused").write_text("roster\n")
        blocker = self.fd()
        self.assertTrue(mutex.try_fd(blocker.fileno()))
        f = self.fd()
        self.assertIsNone(
            mutex.wait_fds([f.fileno()], "cell-a", "test", poll=0.01))

    def test_pause_exit_is_44(self):
        self.assertEqual(mutex.PAUSE_EXIT, 44)


class TestFsEnforcesFlock(MutexTestCase):
    def test_a_real_local_filesystem_passes(self):
        ok, why = mutex.fs_enforces_flock(self.root)
        self.assertTrue(ok, why)
        self.assertIn("enforced", why)

    def test_the_probe_file_is_removed(self):
        mutex.fs_enforces_flock(self.root)
        self.assertFalse((self.root / ".flock-probe").exists())

    def test_a_filesystem_that_does_not_enforce_is_refused(self):
        # flock accepted by everyone => no mutual exclusion at all
        with mock.patch.object(mutex.fcntl, "flock"), \
             mock.patch.object(mutex, "_probe_cross_process",
                               return_value=(False, "second process got it")):
            ok, why = mutex.fs_enforces_flock(self.root)
        self.assertFalse(ok)

    def test_an_unsupported_errno_is_refused(self):
        with mock.patch.object(mutex.fcntl, "flock",
                               side_effect=OSError(errno.ENOTSUP, "nope")), \
             mock.patch.object(mutex, "_probe_cross_process",
                               return_value=(False, "rejected outright")):
            ok, _ = mutex.fs_enforces_flock(self.root)
        self.assertFalse(ok)

    def test_contended_on_the_second_lock_is_the_pass_signal(self):
        calls = []

        def fake(fd, op):
            calls.append(fd)
            if len(calls) > 1:
                raise OSError(errno.EWOULDBLOCK, "would block")

        with mock.patch.object(mutex.fcntl, "flock", side_effect=fake):
            ok, _ = mutex.fs_enforces_flock(self.root)
        self.assertTrue(ok)

    def test_an_unknown_errno_is_never_a_pass(self):
        """A probe that guesses permissively is worse than no probe."""
        with mock.patch.object(mutex.fcntl, "flock",
                               side_effect=OSError(errno.EIO, "io error")), \
             mock.patch.object(mutex, "_probe_cross_process",
                               return_value=(False, "rejected outright")):
            ok, _ = mutex.fs_enforces_flock(self.root)
        self.assertFalse(ok)

    def test_a_cross_process_confirm_can_rescue_a_posix_lock_mount(self):
        """flock mapped onto per-process POSIX locks never conflicts within
        one process, so the cheap probe alone would reject a working mount."""
        with mock.patch.object(mutex.fcntl, "flock"), \
             mock.patch.object(mutex, "_probe_cross_process",
                               return_value=(True, "enforced across processes")):
            ok, why = mutex.fs_enforces_flock(self.root)
        self.assertTrue(ok, why)


class TestCli(MutexTestCase):
    """What is left of the CLI: the operator's filesystem probe. The
    try/wait/held bridge went with the bash hooks that read it."""

    def test_the_bash_bridge_is_gone(self):
        for op in ("try", "wait", "held"):
            self.assertEqual(mutex.main([op, str(self.lock)]), 2, op)

    def test_fscheck_passes_on_a_local_filesystem(self):
        self.assertEqual(mutex.main(["fscheck", str(self.root)]), 0)

    def test_unknown_op_is_a_usage_error(self):
        self.assertEqual(mutex.main(["wat", str(self.lock)]), 2)

    def test_no_args_is_a_usage_error(self):
        self.assertEqual(mutex.main([]), 2)


if __name__ == "__main__":
    unittest.main()
