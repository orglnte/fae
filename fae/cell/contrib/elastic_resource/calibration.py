"""What a reference run of one elastic-resource arrangement measured, for
`experiment calibrate`: per spike of the schedule (1-based, in its order),
the seconds that place the policy's resource on the host it ran on.

    spike<i>_sat_s      the saturation tick (law.saturation) less the spike's start
    spike<i>_sat_rps    the offered rate at that tick
    spike<i>_mount_s    the spike's cache episode's start less the saturation
    spike<i>_lag_s      how long the spike's traffic was answered past its
                        scheduled end (law.traffic_lag)
    spike<i>_release_s  the episode's end less the spike's observed traffic end
                        (its scheduled end plus the lag)

A spike's episode is the first that starts inside its window, from the law's
grace before its start to its end. A value the run never reached (no
saturation, no episode, no release) is left out.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from . import law

# One run cannot measure a spread; its resolution is the trace's grain, one
# line per second.
ESTIMATED_SPREAD_S = 1.0
_SPIKE = re.compile(r"^spike(\d+)_")


def values(rows, offered, sched, *, sat_p99, max_lag=20.0):
    """{name: seconds or rps} of one run: `rows` from law.read_trace, `offered`
    the offered rps of each row, `sched` the run's schedule.json."""
    eps, _ = law.episodes(rows)
    out = {}
    spikes = [w for w in sched["events"] if w["kind"] == "spike"]
    for i, spk in enumerate(spikes, 1):
        t0, t1 = spk["t0"], spk["t1"]
        lag = law.traffic_lag(rows, t1, max_lag)
        out[f"spike{i}_lag_s"] = lag
        sat, _ = law.saturation(rows, sat_p99, t0, t1)
        if sat is None:
            continue
        out[f"spike{i}_sat_s"] = sat - t0
        out[f"spike{i}_sat_rps"] = next(float(o) for r, o in zip(rows, offered) if r["ts"] == sat)
        ep = next(((m0, m1) for m0, m1 in eps if t0 - law.GRACE_S <= m0 <= t1), None)
        if ep is None:
            continue
        out[f"spike{i}_mount_s"] = ep[0] - sat
        if ep[1] is not None:
            out[f"spike{i}_release_s"] = ep[1] - (t1 + lag)
    return {k: round(v, 3) for k, v in out.items()}


def values_from(arrangement_dir, *, sat_p99, max_lag=20.0):
    """values() of an archived arrangement: its trace.csv, trace_events.json
    and schedule.json."""
    d = Path(arrangement_dir)
    events = json.loads((d / "trace_events.json").read_text())
    rows = law.read_trace(str(d / "trace.csv"), events)
    with (d / "trace.csv").open() as f:
        offered = [r.get("offered_rps") or 0 for r in csv.DictReader(f)]
    sched = json.loads((d / "schedule.json").read_text())
    return values(rows, offered, sched, sat_p99=sat_p99, max_lag=max_lag)


def estimated_spread(name, value):
    return ESTIMATED_SPREAD_S


def tolerances(spreads, k):
    """(mount_tol, release_tol) for law.judge from one reference's
    {name: spread}: per spike, k times the spread of its mount_s and of its
    release_s; 0 for a spike whose value calibration did not record."""
    n = max((int(m.group(1)) for m in map(_SPIKE.match, spreads) if m), default=0)

    def per(what):
        return tuple(k * float(spreads.get(f"spike{i}_{what}_s", 0.0)) for i in range(1, n + 1))
    return per("mount"), per("release")
