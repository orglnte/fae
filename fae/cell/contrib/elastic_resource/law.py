#!/usr/bin/env python3
"""The SCALING gate: GREEN means the scaling behavior matches the law, not
merely that e2e assertions pass.

The deadline is anchored on what the policy can observe — the store's
SATURATION, the first trace tick inside a spike whose p99 crosses
VERIFY_SAT_P99_MS — never on the load generator's clock: the moment a
capped store tips over under a ramp is a property of the host, and a
policy cannot be judged on a clock it cannot see. A spike that never
saturates the store is a rig fault (the ramp did not reach the host's
knee), reported as VOID, not as a verdict against the policy.

Two modes:

* SCHEDULE-AWARE (a schedule.json exists next to the metrics — written by
  load_shape.py): judges ANY arrangement of blips and spikes, including
  multi-spike re-acquisition. Checks:
    1. every spike saturates the store (else: void);
    2. exactly one mount episode per SPIKE window, starting inside it —
       from the spike's start (grace) to VERIFY_MAX_MOUNT_S after its
       saturation;
    3. no mount episode starting anywhere else (blips and baselines rejected);
    4. each episode releases before the NEXT spike begins (and the last one by
       trace end) — and never before its own spike's hold ends (no mid-spike
       drop);
    5. load errors <= VERIFY_MAX_LOAD_ERR_PCT (default 0: a correct policy
       serves every request; the references achieve exactly 0).

* CANONICAL (no schedule.json): the original single-spike law — no mount
  before saturation, mount within VERIFY_MAX_MOUNT_S of it, exactly one
  episode, released by trace end, errors within threshold.

Usage: law.py <trace.csv> <trace_events.json> <load_total> <load_errors> [schedule.json]
Env:   VERIFY_SAT_P99_MS, VERIFY_MAX_MOUNT_S, VERIFY_MAX_LOAD_ERR_PCT
Output: one line, "true|", "false|<reason>; <reason>..." or "void|<reason>"
"""
from __future__ import annotations

import csv
import json
import os
import sys

GRACE_S = 3.0   # trace/k6 clock alignment slack
VOID = "void"


def read_trace(trace: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(open(trace)):
        rows.append({
            "ts": float(r.get("ts_s") or 0),
            "p99": float(r.get("p99_ms") or 0),
            "up": str(r.get("infra_cache_up", "")).strip() == "True",
        })
    return rows


def episodes(rows: list[dict]):
    """[(start_ts, end_ts_or_None), ...] of infra-confirmed cache episodes,
    plus the last trace ts."""
    eps: list[list] = []
    prev = False
    last_ts = 0.0
    for r in rows:
        if r["up"] and not prev:
            eps.append([r["ts"], None])
        if prev and not r["up"] and eps:
            eps[-1][1] = r["ts"]
        prev = r["up"]
        last_ts = r["ts"]
    return eps, last_ts


def saturation(rows: list[dict], sat_p99: float, lo: float = 0.0, hi: float | None = None):
    """The first tick in [lo, hi] whose p99 reaches sat_p99, and the highest
    p99 seen there (what to report when nothing crossed)."""
    peak = 0.0
    for r in rows:
        if r["ts"] < lo or (hi is not None and r["ts"] > hi):
            continue
        peak = max(peak, r["p99"])
        if r["p99"] >= sat_p99:
            return r["ts"], peak
    return None, peak


def judge(rows: list[dict], events: dict, total: int, errors: int, sched: dict | None,
          *, sat_p99: float, max_mount: float, max_pct: float) -> tuple[str, list[str]]:
    eps, last_ts = episodes(rows)
    released_last = (not eps) or eps[-1][1] is not None or (events.get("release_epoch") is not None)
    err_pct = (100.0 * errors / total) if total else 100.0
    why: list[str] = []

    if sched:
        spikes = [w for w in sched["events"] if w["kind"] == "spike"]
        sats = []
        for i, spk in enumerate(spikes, 1):
            sat_ts, peak = saturation(rows, sat_p99, spk["t0"], spk["t1"])
            if sat_ts is None:
                return VOID, [
                    "spike %d never saturated the store (p99 max %.0fms < %gms over "
                    "[%.0f..%.0f]) — the host's knee is above the ramp; raise [load] top"
                    % (i, peak, sat_p99, spk["t0"], spk["t1"])]
            sats.append(sat_ts)
        # pair episodes to spike windows in order
        if len(eps) != len(spikes):
            why.append("expected %d mount episode(s) (one per spike), saw %d"
                       % (len(spikes), len(eps)))
        for i, (epi, spk, sat_ts) in enumerate(zip(eps, spikes, sats), 1):
            m0, m1 = epi
            lo, hi = spk["t0"] - GRACE_S, sat_ts + max_mount
            if not (lo <= m0 <= hi):
                why.append("mount %d at ts=%.0f outside spike %d's window [%.0f..%.0f] "
                           "(saturated at ts=%.0f, deadline %gs after it)"
                           % (i, m0, i, lo, hi, sat_ts, max_mount))
            if m1 is not None and m1 < spk["t1"] - GRACE_S:
                why.append("episode %d released MID-spike (ts=%.0f < spike end %.0f)"
                           % (i, m1, spk["t1"]))
            nxt = spikes[i]["t0"] if i < len(spikes) else last_ts + GRACE_S
            if m1 is None and i < len(spikes):
                why.append("episode %d never released before spike %d" % (i, i + 1))
            elif m1 is not None and m1 > nxt + GRACE_S:
                why.append("episode %d released too late (ts=%.0f, next spike at %.0f)"
                           % (i, m1, nxt))
        if eps and not released_last:
            why.append("cache never released after the tail (traffic below the knee must release it)")
    else:
        # canonical single-spike law
        sat_ts, peak = saturation(rows, sat_p99)
        if sat_ts is None:
            return VOID, ["load never saturated the store (p99 max %.0fms < %gms) — "
                          "the host's knee is above the load; raise [load] top" % (peak, sat_p99)]
        if not eps:
            why.append("cache never mounted under load")
        if len(eps) > 1:
            why.append("cache flapped (%d mount episodes; the policy must hold through the sustained spike)" % len(eps))
        if eps:
            if eps[0][0] < sat_ts - GRACE_S:
                why.append("cache mounted BEFORE saturation (ts=%.0f — a blip must be rejected)" % eps[0][0])
            elif eps[0][0] - sat_ts > max_mount:
                why.append("mount too late: saturation->mount %.0fs > %gs" % (eps[0][0] - sat_ts, max_mount))
        if eps and not released_last:
            why.append("cache never released after the tail (traffic below the knee must release it)")

    if err_pct > max_pct:
        why.append("load errors %d/%d = %.1f%% > %g%% (the policy must serve every request)"
                   % (errors, total, err_pct, max_pct))
    return ("true" if not why else "false"), why


def main() -> None:
    trace, events = sys.argv[1], sys.argv[2]
    total = int(sys.argv[3] or 0)
    errors = int(sys.argv[4] or 0)
    sched_path = sys.argv[5] if len(sys.argv) > 5 else ""

    ev = json.load(open(events))
    sched = None
    if sched_path and os.path.isfile(sched_path):
        sched = json.load(open(sched_path))
    verdict, why = judge(
        read_trace(trace), ev, total, errors, sched,
        sat_p99=float(os.environ.get("VERIFY_SAT_P99_MS", "150")),
        max_mount=float(os.environ.get("VERIFY_MAX_MOUNT_S", "30")),
        max_pct=float(os.environ.get("VERIFY_MAX_LOAD_ERR_PCT", "0")))
    print(verdict + "|" + "; ".join(why))


if __name__ == "__main__":
    main()
