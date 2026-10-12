"""Host sleep, as the two clocks show it.

The monotonic clock stops while the host sleeps (on macOS it is
mach_absolute_time) and the wall clock does not, so over any interval wall
minus monotonic is how long the host slept in it. A laptop with its lid shut
also wakes for seconds at a time (a dark wake) and runs processes then; only a
wake that lasts AWAKE_S is the host being back.
"""
from __future__ import annotations

import time
from typing import NamedTuple

GAP_S = 30.0        # wall minus monotonic below this is clock jitter, not a sleep
AWAKE_S = 120.0     # observed dark wakes last under a minute


class Reading(NamedTuple):
    wall: float
    mono: float


def reading():
    return Reading(time.time(), time.monotonic())


def gap(wall_dt, mono_dt):
    """Seconds slept over an interval in which the wall clock advanced
    wall_dt and the monotonic clock mono_dt; 0 below GAP_S."""
    g = wall_dt - mono_dt
    return g if g > GAP_S else 0.0


def slept(since, now=None):
    """Seconds the host slept between two readings; 0 below GAP_S."""
    now = now or reading()
    return gap(now.wall - since.wall, now.mono - since.mono)


def wait_awake(awake_s=AWAKE_S, poll_s=5.0, read=reading, pause=time.sleep):
    """Return once the host has stayed awake `awake_s` in a row, counted from
    this call: a sleep observed between two polls starts the count again, so a
    dark wake never ends the wait. Returns the wall seconds waited."""
    first = last = read()
    awake_since = first.mono
    while last.mono - awake_since < awake_s:
        pause(poll_s)
        now = read()
        if slept(last, now):
            awake_since = now.mono
        last = now
    return last.wall - first.wall
