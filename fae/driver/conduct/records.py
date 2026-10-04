"""The Conduct's own records in .conduct/: the reconcile log every action is
written to, and the host-sleep book that keeps ages honest across a suspend.

The monotonic clock does not advance while the host sleeps, so wall minus
monotonic across one observation is the sleep; gaps are kept on disk so ages
computed after a conduct restart still exclude sleeps seen before it.
"""
from __future__ import annotations

import json
import time

from fae.driver import common

HOST_SLEEP_GAP_S = 30
HOST_SLEEP_BOOK_DAYS = 7
_sleep_clocks = None            # (wall, monotonic) at the last observation
_sleep_gaps = None              # the book, loaded on first use


def reconcile_log():
    return common.CONDUCT / "reconcile.log"


def rec_log(msg):
    """Print `msg` and append it, stamped, to the reconcile log."""
    log = reconcile_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}"
    print(line)
    with log.open("a") as f:
        f.write(line + "\n")


def _host_sleep_book():
    return common.CONDUCT / "host_sleep.json"


def _host_sleep_gaps():
    global _sleep_gaps
    if _sleep_gaps is None:
        try:
            _sleep_gaps = [g for g in json.loads(_host_sleep_book().read_text())
                           if isinstance(g, dict) and "start" in g and "s" in g]
        except (OSError, ValueError, TypeError):
            _sleep_gaps = []
    return _sleep_gaps


def host_sleep_observe(now=None, mono=None):
    """Record a host suspend since the previous call, if one happened.
    Returns the gap in seconds (0 when none)."""
    global _sleep_clocks
    wall = time.time() if now is None else now
    mono = time.monotonic() if mono is None else mono
    gap = 0.0
    if _sleep_clocks is not None:
        gap = (wall - _sleep_clocks[0]) - (mono - _sleep_clocks[1])
        if gap > HOST_SLEEP_GAP_S:
            gaps = _host_sleep_gaps()
            gaps.append({"start": _sleep_clocks[0], "s": gap})
            keep = wall - HOST_SLEEP_BOOK_DAYS * 86400
            gaps[:] = [g for g in gaps if g["start"] + g["s"] >= keep]
            try:
                _host_sleep_book().parent.mkdir(parents=True, exist_ok=True)
                _host_sleep_book().write_text(json.dumps(gaps))
            except OSError:
                pass
        else:
            gap = 0.0
    _sleep_clocks = (wall, mono)
    return gap


def awake_age(t_wall, now=None):
    """Seconds since the wall stamp t_wall, host sleep excluded. A sleep is
    counted from the last awake observation for its whole length; the
    overlap with [t_wall, now] is what is subtracted."""
    now = time.time() if now is None else now
    age = now - t_wall
    for g in _host_sleep_gaps():
        start, end = g["start"], g["start"] + g["s"]
        overlap = min(end, now) - max(start, t_wall)
        if overlap > 0:
            age -= overlap
    return age
