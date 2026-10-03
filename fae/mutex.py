#!/usr/bin/env python3
"""THE mutex — kernel-owned flock(2). One implementation, every holder.

A lock is a FILE. Acquisition is flock(fd, LOCK_EX|LOCK_NB) on an fd that the
HOLDING process opened. The lock lives on the open file description, so the
kernel releases it when that process dies by any means — SIGKILL, OOM, panic,
host sleep. Nothing on disk has to be judged stale, so there is no steal, no
adoption, no settle window and no orphan grace.

The holders are the cell driver (fae/queues.py's acquire_slots for the work
and lock slots, Cell.verify_lock_acquire, Cell.loop_lock,
Cell.exclusive_acquire) and fae/driver/common.py's fs_lock. Each keeps the open file
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
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path.cwd()).resolve()

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


def _workspaces_dir():
    """Honour WORKSPACES_DIR the way the driver does (Cell reads it before
    the config's default), so a queue standing down under an alternative
    root looks at that root's .paused."""
    env = os.environ.get("WORKSPACES_DIR")
    if env:
        return Path(env)
    nosync = REPO_ROOT / "workspaces.nosync"
    return nosync if nosync.is_dir() else REPO_ROOT / "workspaces"


def pause_requested(cid):
    """EXISTENCE of .paused, matching the driver's `(ws / ".paused").exists()`.
    Reading the contents instead would call an empty .paused "not paused" —
    the opposite of what the loop inside the cell concludes."""
    return bool(cid) and (_workspaces_dir() / cid / ".paused").is_file()


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


def wait_fds(fds, cid, label, poll=5.0, ppid=None):
    """Block until one of `fds` is ours. Returns the fd, or None to stand down.

    ppid: give up if our parent died while we queued — a helper that wins a
    lock on an fd whose owning process is already gone holds it for nobody.
    """
    waited = 0
    while True:
        got = try_fds(fds)
        if got is not None:
            return got
        if pause_requested(cid):
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
# Names who last took the lock, for display. It is NOT proof that anyone holds
# it and no code path may branch on it: "is it held?" is a question for the
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
