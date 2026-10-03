#!/usr/bin/env python3
"""THE mutex — kernel-owned flock(2). One implementation, every holder.

A lock is a FILE. Acquisition is flock(fd, LOCK_EX|LOCK_NB) on an fd that the
HOLDING process opened. The lock lives on the open file description, so the
kernel releases it when that process dies by any means — SIGKILL, OOM, panic,
host sleep. Nothing on disk has to be judged stale, so there is no steal, no
adoption, no settle window and no orphan grace.

The holders are the cell driver (fae/queues.py's acquire_slots for the work
and lock slots, Cell.verify_lock_acquire, Cell.loop_lock,
Cell.exclusive_acquire) and fs_lock below. Each keeps the open file
OBJECT for as long as it holds the lock; closing it is the release.

TWO RULES THAT BREAK MUTUAL EXCLUSION SILENTLY IF VIOLATED:
  * Never unlink a lock file. Unlink-and-recreate leaves two holders on two
    inodes, with no error anywhere.
  * Never let a process that outlives the holder inherit the fd. The lock is
    freed when the LAST fd on the open file description closes, so an
    inherited fd keeps a dead holder's lock alive. Python opens O_CLOEXEC by
    default and subprocess closes fds by default, so only an explicit
    pass_fds / close_fds=False can hit this.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# The driver's stand-down exit code (fae/cell Cell.PAUSE_EXIT reads the
# same number), so it is a contract.
PAUSE_EXIT = int(os.environ.get("PAUSE_EXIT", 44))

BUSY, HELD = 1, 0

# A filesystem either refuses flock outright or refuses the SECOND holder.
# Sets because these collapse to one value on macOS and differ on Linux.
FLOCK_UNSUPPORTED = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOLCK, errno.EINVAL}
FLOCK_CONTENDED = {errno.EWOULDBLOCK, errno.EAGAIN}


def _log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}",
          file=sys.stderr)


# --- acquisition -------------------------------------------------------------

def try_fd(fd):
    """One non-blocking attempt on an ALREADY-OPEN fd."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def try_fds(fds):
    """N-ary semaphore: the winning fd, or None. The scan is N flock calls."""
    for fd in fds:
        if try_fd(fd):
            return fd
    return None


def wait_fds(fds, cid, label, poll=5.0, ppid=None, stop=None):
    """Block until one of `fds` is ours. Returns the fd, or None to stand down
    when `stop()` says the operator paused `cid`.

    ppid: give up if our parent died while we queued — a helper that wins a
    lock on an fd whose owning process is already gone holds it for nobody.
    """
    waited = 0
    while True:
        got = try_fds(fds)
        if got is not None:
            return got
        if stop is not None and stop():
            _log(f"{label}: {cid} standing down from the queue — operator pause")
            return None
        if ppid is not None and os.getppid() != ppid:
            _log(f"{label}: parent {ppid} died while queued — abandoning")
            raise SystemExit(BUSY)
        if waited % 60 == 0:
            _log(f"{label}: {cid} waiting ({len(fds)} candidate(s), all busy)")
        time.sleep(poll)
        waited += poll


def open_lock(path):
    """Open a lock file for a PYTHON holder. Returns a file OBJECT.

    The caller must keep the reference: garbage-collecting it closes the fd,
    which silently releases the lock.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_dir():
        raise IsADirectoryError(
            f"{p} is a lock DIRECTORY, but a lock is a FILE here. mkdir locks "
            f"and flock locks do not exclude each other at all, so a tree "
            f"holding both has no mutual exclusion. Drain and remove it.")
    return open(p, "a+")


class fs_lock:
    """Python side of THE mutex — flock(2) on a file, held for the with-block.

    THE FILE OBJECT IS THE LOCK. It is stored on self, not in a local: a
    garbage-collected file object closes its fd, and closing the fd releases
    the flock. Losing exclusion that way is silent, not a crash, which is why
    it is spelled out rather than left to `with open(...)`.

    If this process dies the kernel releases the lock; that is the whole
    recovery story. Python opens fds O_CLOEXEC by default, so no subprocess of
    ours can pin the lock past our death the way an inheriting shell child can.
    """

    def __init__(self, d, poll=0.25, timeout=None):
        self.path = Path(d)
        self.poll, self.timeout = poll, timeout
        self._f = None

    def __enter__(self):
        self._f = open_lock(self.path)
        # Polled LOCK_NB rather than a blocking LOCK_EX: an operator running
        # cli.py interactively must be able to Ctrl-C out of a queue behind a
        # 25-minute verify, and a blocking flock offers no cadence to do it in.
        t0 = time.monotonic()
        while True:
            try:
                fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                note_holder(self.path, f"runspy-{os.getpid()}", os.getpid())
                return self
            except OSError:
                if self.timeout is not None and time.monotonic() - t0 > self.timeout:
                    self._f.close()
                    self._f = None
                    raise TimeoutError(
                        f"{self.path} held by "
                        f"{holder_name(self.path) or '?'}")
                time.sleep(self.poll)

    def __exit__(self, *exc):
        try:
            clear_holder(self.path)
        finally:
            self._f.close()          # closing the fd IS the release
            self._f = None


def probe_held(path):
    """Is anyone holding this lock? For DISPLAY only.

    Momentarily takes the lock when it is free, so an acquirer racing this
    loses at most one poll tick. Never blocks.
    """
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False                    # we got it, so nobody held it
    except OSError:
        return True
    finally:
        os.close(fd)                    # releases it again if we took it


# --- the holder sidecar ------------------------------------------------------
# Names who last took the lock. It is NOT proof that anyone holds it and no
# code path may branch on it alone: "is it held?" is a question for the
# kernel (probe_held), and only then is "by whom?" worth asking.

def _holder_path(path):
    return Path(str(path) + ".holder")


def note_holder(path, cid, pid):
    try:
        _holder_path(path).write_text(f"{cid} {pid} {int(time.time())}\n")
    except OSError:
        pass


def holder_name(path):
    try:
        return _holder_path(path).read_text().split()[0]
    except (OSError, IndexError):
        return ""


def holder_pid(path):
    """The pid the sidecar names, or None. Ask only once probe_held says the
    lock is held, and confirm the process is who it should be."""
    try:
        return int(_holder_path(path).read_text().split()[1])
    except (OSError, IndexError, ValueError):
        return None


def clear_holder(path):
    try:
        _holder_path(path).unlink(missing_ok=True)
    except OSError:
        pass


# --- filesystem capability ---------------------------------------------------

def _probe_same_process(path):
    """(ok, detail). Two open file descriptions on one file — the second MUST
    be refused."""
    a = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            return False, (f"flock(LOCK_EX) was rejected outright: "
                           f"{errno.errorcode.get(e.errno, e.errno)} ({e.strerror})")
        b = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in FLOCK_CONTENDED:
                return True, "enforced (a second exclusive lock was refused)"
            if e.errno in FLOCK_UNSUPPORTED:
                return False, (f"flock is not supported here: "
                               f"{errno.errorcode.get(e.errno, e.errno)} ({e.strerror})")
            return False, (f"flock failed with an unexpected error: "
                           f"{errno.errorcode.get(e.errno, e.errno)} ({e.strerror})")
        finally:
            os.close(b)
        return False, ("the SAME file was locked EXCLUSIVELY TWICE — flock is "
                       "accepted but not enforced")
    finally:
        os.close(a)


def _probe_cross_process(path):
    """(ok, detail) using a real second process. Confirms a failure; never
    produces one on its own.

    Some mounts implement flock over POSIX record locks, which are held per
    PROCESS — two fds inside one process then never conflict, and the cheap
    probe would call a filesystem that works perfectly "not enforced".
    """
    a = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(a)
        return False, (f"flock(LOCK_EX) was rejected outright: "
                       f"{errno.errorcode.get(e.errno, e.errno)} ({e.strerror})")
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "_flock_child", str(path)],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not run the confirming probe process: {e}"
    finally:
        os.close(a)
    if r.returncode == 0:
        return True, ("enforced across processes (flock here maps onto POSIX "
                      "record locks, which is fine)")
    if r.returncode == 1:
        return False, "a second PROCESS took the same exclusive lock"
    return False, (f"the confirming probe could not decide (rc={r.returncode}): "
                   f"{r.stderr.decode(errors='replace').strip()[:120]}")


REMOTE_FS = frozenset({"nfs", "nfs4", "smbfs", "cifs", "smb3", "afpfs", "webdav", "sshfs",
                       "fuse.sshfs", "9p", "virtiofs", "ceph", "glusterfs", "fuse.glusterfs",
                       "davfs", "fuse.davfs2"})


def parse_mount(table, mountpoint):
    """(local, fstype) of `mountpoint` in `mount`'s output; local is None when
    the mount point is not listed. Reads both the Linux form (`on X type T
    (opts)`) and the macOS form (`on X (T, opts)`, where `local` is an option)."""
    for line in table.splitlines():
        m = re.match(r"^.+? on (.+?) type (\S+) \(.*\)$", line)
        if m and m.group(1) == mountpoint:
            return m.group(2) not in REMOTE_FS, m.group(2)
        m = re.match(r"^.+? on (.+?) \(([^,)]+)(.*)\)$", line)
        if m and m.group(1) == mountpoint:
            opts = [o.strip() for o in m.group(3).split(",")]
            return "local" in opts and m.group(2) not in REMOTE_FS, m.group(2)
    return None, "?"


def fs_is_local(path):
    """(local, fstype) for the filesystem holding `path` (or its nearest
    existing parent). Appending to a shared file is atomic per write only on a
    local disk; local is None when it cannot be told."""
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        df = subprocess.run(["df", "-P", str(path)], capture_output=True, text=True, timeout=10)
        mountpoint = df.stdout.splitlines()[-1].split(None, 5)[-1]
        table = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except Exception:       # whatever stops the probe, the answer is "cannot tell"
        return None, "?"
    return parse_mount(table, mountpoint)


def fs_enforces_flock(d):
    """(ok, detail) — does <d>'s filesystem actually ENFORCE flock(2)?

    Every arm cap, work slot and verify lock is a flock on a file in this
    directory. A filesystem that accepts flock without enforcing it turns all
    of them into no-ops that report success, so two access cells provision two
    infra at once and nothing anywhere says so.
    """
    d = Path(d)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"cannot create the lock directory: {e}"
    probe = d / ".flock-probe"
    try:
        ok, why = _probe_same_process(probe)
        return (True, why) if ok else _probe_cross_process(probe)
    except OSError as e:
        return False, f"could not create the probe file {probe.name}: {e}"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _flock_child(path):
    """Second process for _probe_cross_process. 0 = correctly refused."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in FLOCK_CONTENDED:
            return 0
        print(f"{errno.errorcode.get(e.errno, e.errno)}: {e.strerror}",
              file=sys.stderr)
        return 2 if e.errno in FLOCK_UNSUPPORTED else 3
    finally:
        os.close(fd)
    return 1


# --- CLI: the operator's filesystem probe ------------------------------------
# (The try/wait/held bridge the bash hooks used went with them.)

def main(argv):
    """`fscheck <dir>` — exit 0 iff <dir>'s filesystem enforces flock;
    `_flock_child` is the probe's own second process. 2 = usage."""
    if not argv:
        print("usage: mutex.py fscheck <dir>", file=sys.stderr)
        return 2
    op, a = argv[0], argv[1:]
    if op == "fscheck":
        ok, why = fs_enforces_flock(a[0])
        print(f"{'OK' if ok else 'FAIL'}: {a[0]}: {why}", file=sys.stderr)
        return 0 if ok else 1
    if op == "_flock_child":
        return _flock_child(a[0])
    print(f"mutex.py: unknown op {op!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
