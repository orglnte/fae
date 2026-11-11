#!/usr/bin/env python3
"""The SCALING gate: GREEN means the scaling behavior matches the law, not
merely that e2e assertions pass.

The deadline is anchored on what the policy can observe — the store's
SATURATION, the first trace tick inside a spike whose p99 crosses
VERIFY_SAT_P99_MS — never on the load generator's clock: the moment a
capped store tips over under a ramp is a property of the host, and a
policy cannot be judged on a clock it cannot see. A spike that never
saturates the store is a rig fault (the ramp did not reach the host's
knee), reported as VOID, not as a verdict against the policy — unless the
load errors exceed the threshold: a service that stops answering completes
no request, so its ticks carry no p99 and read as an idle store, and that
is the policy's failure, charged. Nor when a cache mounted for an earlier
spike is still up as the spike begins: the policy's own cache absorbed it,
and the release it missed is charged.

The release bound runs from the OBSERVED end of a spike's traffic, not its
scheduled end: the generator stops sending on schedule, but on a slow host
its requests are still being answered afterwards, and a policy that holds
its cache until they are is not late. The end is the first tick at which
the main generator's answered count (k6_done) reaches what it had sent by
the scheduled end (k6_sent), at most VERIFY_MAX_LAG_S later (the client's
request timeout). A delay under the clock slack (GRACE_S) is not one: it
is the trace's one-second grain. Both counts are the generator's own. A
trace without them is judged on the scheduled end.

Time is the clock's. The trace stamps each line with the time it was
taken and does not number seconds; its events file holds the time of the
load generator's first request (k6_start_epoch). The schedule counts its
seconds from that request, so a line's second is its time less that one:
lines taken before the traffic began are negative. A run whose generator
start was not recorded cannot be placed against its schedule: void.

Two modes:

* SCHEDULE-AWARE (a schedule.json exists next to the metrics — written by
  load_shape.py): judges ANY arrangement of blips and spikes, including
  multi-spike re-acquisition. Checks:
    1. every spike saturates the store (else: void — unless an earlier
       spike's episode is still up at its start, which is check 4's failure);
    2. exactly one mount episode per SPIKE window, starting inside it —
       from the spike's start (grace) to VERIFY_MAX_MOUNT_S after its
       saturation;
    3. no mount episode starting anywhere else (blips and baselines rejected);
    4. each episode releases before the NEXT spike begins, later by however
       long its own spike's traffic outlasted the schedule (and the last one
       by trace end) — and never before its own spike's hold ends (no
       mid-spike drop). A release inside that extra time that reaches into
       the next spike is the host's delay: void — unless requests failed,
       which stays the policy's failure;
    5. load errors <= VERIFY_MAX_LOAD_ERR_PCT (default 0: a correct policy
       serves every request; the references achieve exactly 0).

* CANONICAL (no schedule.json): the original single-spike law — no mount
  before saturation, mount within VERIFY_MAX_MOUNT_S of it, exactly one
  episode, released by trace end, errors within threshold.

Usage: law.py <trace.csv> <trace_events.json> <load_total> <load_errors> [schedule.json]
Env:   VERIFY_SAT_P99_MS, VERIFY_MAX_MOUNT_S, VERIFY_MAX_LOAD_ERR_PCT, VERIFY_MAX_LAG_S
Output: one line, "true|", "false|<reason>; <reason>..." or "void|<reason>"
"""
from __future__ import annotations

import csv
import json
import os
import sys

GRACE_S = 3.0   # slack on every bound: the trace has one line per second
VOID = "void"


def _count(v) -> int | None:
    return int(v) if v not in (None, "") else None


def second(row: dict, events: dict | None) -> float:
    """A trace line's second on the schedule's clock: its own time less the
    generator's first request. A trace that numbered its own lines (ts_s)
    and has no such request recorded keeps its numbers."""
    zero = (events or {}).get("k6_start_epoch")
    if zero is not None and row.get("epoch") not in (None, ""):
        return round(float(row["epoch"]) - float(zero), 3)
    return float(row.get("ts_s") or 0)


def read_trace(trace: str, events: dict | None = None) -> list[dict]:
    rows = []
    for r in csv.DictReader(open(trace)):
        rows.append({
            "ts": second(r, events),
            "p99": float(r.get("p99_ms") or 0),
            "up": str(r.get("infra_cache_up", "")).strip() == "True",
            "sent": _count(r.get("k6_sent")),
            "done": _count(r.get("k6_done")),
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


def traffic_lag(rows: list[dict], t1: float, max_lag: float) -> float:
    """Seconds past the scheduled end `t1` until the generator had an answer
    (or a timeout) for everything it sent by `t1`; 0 under the clock slack
    (a per-second trace reads a request in flight as a second's delay) and
    for a trace that does not carry the generator's counts, `max_lag` when
    it never did."""
    sent = None
    for r in rows:
        if r["ts"] <= t1 and r.get("sent") is not None:
            sent = r["sent"]
    if sent is None:
        return 0.0
    for r in rows:
        if t1 <= r["ts"] <= t1 + max_lag and r.get("done") is not None and r["done"] >= sent:
            lag = r["ts"] - t1
            return lag if lag >= GRACE_S else 0.0
    return max_lag


def judge(rows: list[dict], events: dict, total: int, errors: int, sched: dict | None,
          *, sat_p99: float, max_mount: float, max_pct: float,
          max_lag: float = 20.0) -> tuple[str, list[str]]:
    eps, last_ts = episodes(rows)
    released_last = (not eps) or eps[-1][1] is not None or (events.get("release_epoch") is not None)
    err_pct = (100.0 * errors / total) if total else 100.0
    why: list[str] = []
    errors_why = ("load errors %d/%d = %.1f%% > %g%% (the policy must serve every request)"
                  % (errors, total, err_pct, max_pct))

    def unsaturated(what: str) -> tuple[str, list[str]]:
        if err_pct > max_pct:
            return "false", [what + " while requests failed — the service did not answer",
                             errors_why]
        return VOID, [what + " — the host's knee is above the ramp; raise [load] top"]

    if sched:
        if events.get("k6_start_epoch") is None:
            return VOID, ["the load generator's start was not recorded — its schedule "
                          "cannot be placed on the trace"]
        spikes = [w for w in sched["events"] if w["kind"] == "spike"]
        lags = [traffic_lag(rows, w["t1"], max_lag) for w in spikes]
        sats = []
        for i, spk in enumerate(spikes, 1):
            held = next((n for n, (m0, m1) in enumerate(eps, 1)
                         if m0 < spk["t0"] - GRACE_S
                         and (m1 is None or m1 > spk["t0"] + GRACE_S)), None)
            lag = lags[i - 2] if i > 1 else 0.0
            if held is not None and lag and err_pct <= max_pct and eps[held - 1][1] is not None \
                    and eps[held - 1][1] <= spk["t0"] + lag + GRACE_S:
                return VOID, ["spike %d's traffic was answered %.0fs past its schedule and "
                              "episode %d was released within that time, inside spike %d "
                              "(ts=%.0f) — the host is too slow for this schedule"
                              % (i - 1, lag, held, i, eps[held - 1][1])]
            sat_ts, peak = saturation(rows, sat_p99, spk["t0"], spk["t1"])
            if sat_ts is None:
                if held is not None:
                    why.append("episode %d never released before spike %d (its cache "
                               "absorbed the spike: p99 max %.0fms < %gms over [%.0f..%.0f])"
                               % (held, i, peak, sat_p99, spk["t0"], spk["t1"]))
                    if err_pct > max_pct:
                        why.append(errors_why)
                    return "false", why
                return unsaturated(
                    "spike %d never saturated the store (p99 max %.0fms < %gms over "
                    "[%.0f..%.0f])" % (i, peak, sat_p99, spk["t0"], spk["t1"]))
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
            elif m1 is not None and m1 > nxt + lags[i - 1] + GRACE_S:
                why.append("episode %d released too late (ts=%.0f, next spike at %.0f%s)"
                           % (i, m1, nxt, ", traffic %.0fs past schedule" % lags[i - 1]
                              if lags[i - 1] else ""))
        if eps and not released_last:
            why.append("cache never released after the tail (traffic below the knee must release it)")
    else:
        # canonical single-spike law
        sat_ts, peak = saturation(rows, sat_p99)
        if sat_ts is None:
            return unsaturated("load never saturated the store (p99 max %.0fms < %gms)"
                               % (peak, sat_p99))
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
        why.append(errors_why)
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
        read_trace(trace, ev), ev, total, errors, sched,
        sat_p99=float(os.environ.get("VERIFY_SAT_P99_MS", "150")),
        max_mount=float(os.environ.get("VERIFY_MAX_MOUNT_S", "30")),
        max_pct=float(os.environ.get("VERIFY_MAX_LOAD_ERR_PCT", "0")),
        max_lag=float(os.environ.get("VERIFY_MAX_LAG_S", "20")))
    print(verdict + "|" + "; ".join(why))


if __name__ == "__main__":
    main()
