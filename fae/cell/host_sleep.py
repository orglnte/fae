"""Host sleep, as the two clocks show it.

The monotonic clock stops while the host sleeps (on macOS it is
mach_absolute_time) and the wall clock does not, so over any interval wall
minus monotonic is how long the host slept in it.
"""
from __future__ import annotations

import time
from typing import NamedTuple

GAP_S = 30.0    # wall minus monotonic below this is clock jitter, not a sleep


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
