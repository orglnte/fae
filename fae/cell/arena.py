"""The fd arena — every candidate slot file, open on a reserved fd.

A flock lives on the open file description, a kernel object: the driver opens
every candidate lock file on a reserved literal fd and Cell.acquire_slots
flocks whichever one it wins; release is the driver closing the fd. The
holder is the cell process, for the cell's whole life.

The literal numbers are a legacy of the bash hooks that once flocked
/dev/fd/N on an inherited fd; nothing inherits them any more. `subprocess`
defaults to close_fds=True, so a child that was not explicitly handed the
arena cannot inherit it, and handing it over would be a deliberate
`pass_fds` — which nothing in this package writes. (In the shell era the
default was the opposite, and one unwrapped background daemon leaked the rig
lock and wedged the fleet for five hours on 2026-08-17.)
"""
from __future__ import annotations

import os
from pathlib import Path

# Literal and documented: the numbers the fleet's locks have always lived on.
FD_LOOP, FD_VERIFY, FD_RIG = 200, 201, 202
FD_SLOT_BASE, FD_ARM_BASE = 210, 230
MAX_SLOTS, MAX_ARM_SLOTS = 16, 8


class Arena:

    def __init__(self, queues, work_slots=7, arm=None, arm_slots=1):
        self.queues = Path(queues)
        self.work_slots = int(work_slots)
        self.arm = arm
        self.arm_slots = int(arm_slots)
        self._open = {}

    def _adopt(self, path, fd):
        """Open `path` and place it on the literal fd number the hooks expect."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            raise RuntimeError(
                f"{path} is a lock DIRECTORY, but a lock is a FILE here. "
                f"mkdir locks and flock locks do not exclude each other at "
                f"all, so a tree holding both has no mutual exclusion.")
        src = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if src != fd:
                os.dup2(src, fd, inheritable=True)
        finally:
            if src != fd:
                os.close(src)
        os.set_inheritable(fd, True)
        self._open[fd] = path
        return fd

    def open(self):
        if self.work_slots > MAX_SLOTS:
            raise RuntimeError(f"WORK_SLOTS={self.work_slots} exceeds the "
                               f"reserved fd block ({MAX_SLOTS})")
        d = self.queues / "work-slots"
        for i in range(1, self.work_slots + 1):
            self._adopt(d / f"slot-{i}", FD_SLOT_BASE + i)
        if self.arm:
            if self.arm_slots > MAX_ARM_SLOTS:
                raise RuntimeError(f"arm '{self.arm}' wants {self.arm_slots} "
                                   f"slots, over the reserved block")
            d = self.queues / f"arm-{self.arm}.slots"
            for i in range(1, self.arm_slots + 1):
                self._adopt(d / f"slot-{i}", FD_ARM_BASE + i)
        return self

    @property
    def fds(self):
        """Exactly what a hook must inherit — nothing else does."""
        return tuple(sorted(self._open))

    def close(self):
        """THE release. Closing an unopened fd is a no-op, so this is
        idempotent, and the kernel does it anyway if we die."""
        for fd in sorted(self._open, reverse=True):
            try:
                os.close(fd)
            except OSError:
                pass
        self._open.clear()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False
