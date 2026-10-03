#!/usr/bin/env python3
"""One contender for the multi-process mutex tests. Not a test itself.

Run as a real separate process — that is the point. Threads share a GIL and
an address space, so a threaded test proves nothing about a mutex whose whole
job is to coordinate processes. flock is per open file description, so a
thread test would not even exercise the same object.

  argv: <lockfile> <cid> <logfile> <hold_s> [op]
  op:   acquire (default) | try

Writes "IN <cid>" and "OUT <cid>" around the critical section. Each write is
a single short append under O_APPEND, so lines from concurrent workers cannot
interleave (well under PIPE_BUF — the same guarantee the rig relies on for
iterations.log).

Exit: 0 held-and-released, 1 busy, 44 stood down for a pause.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fae import mutex  # noqa: E402


def note(logfile, text):
    with open(logfile, "a") as f:
        f.write(text + "\n")


def main(argv):
    lockfile, cid, logfile, hold = argv[0], argv[1], argv[2], float(argv[3])
    op = argv[4] if len(argv) > 4 else "acquire"

    # THE FILE OBJECT IS THE LOCK. Held in a local for the whole critical
    # section: letting it be collected closes the fd and drops the lock.
    f = mutex.open_lock(lockfile)

    if op == "try":
        if not mutex.try_fd(f.fileno()):
            return 1
    else:
        paused = Path(os.environ["WORKSPACES_DIR"]) / cid / ".paused"
        if mutex.wait_fds([f.fileno()], cid, "test", poll=0.05,
                          stop=paused.exists) is None:
            return mutex.PAUSE_EXIT

    note(logfile, f"IN {cid}")
    time.sleep(hold)
    note(logfile, f"OUT {cid}")
    f.close()                       # closing the fd IS the release
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
