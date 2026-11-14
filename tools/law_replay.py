#!/usr/bin/env python3
"""Re-judge archived elastic-resource arrangements with the law's switches
off and on, and print the verdicts that change. Read-only: it writes
nothing anywhere.

An arrangement is a directory holding trace.csv, trace_events.json,
schedule.json, metrics.json (`variant`, `load_total`, `load_errors`) and
verdict.json (the engine's archive, `arrangements/NN-...`); any other
directory is skipped. Each is judged twice with `law.judge`: switches off
(the law as shipped) and switches on, as the options below set them. A
trace from before the generator's first request was recorded
(`k6_start_epoch`) numbered its own lines from the schedule's second 0
(`ts_s`); the replay reads those numbers as its seconds (`law.second`)
and that line's time as the first request's.

    python3 tools/law_replay.py --tolerance 2 --law-stages scaling,load-shape \\
        <root>/workspaces.nosync/*/arrangements/*

Options:
    --tolerance S            seconds every spike's mount and release limits
                             widen by, for every variant (law.judge's
                             mount_tol and release_tol)
    --tolerance VARIANT=S    the same for one variant; overrides the above
    --law-stages A,B         the verdict.json stages the law decided (its
                             charged failure and its void, as the
                             experiment names them); with it, only green
                             arrangements and those stages are counted,
                             since any other stage was decided before the
                             law ran. Without it every arrangement counts.
    --sat-p99, --max-mount, --max-pct, --max-lag
                             the law's thresholds (defaults 150 ms — or the
                             trace_events.json's sat_p99_ms — 30 s, 0 %, 20 s)
    --list                   print each changed arrangement, not only the counts

Output: per variant, the arrangements counted, how many the switches-off
law judges as the recorded verdict did (green vs not green), and each
change off -> on (true = green, false = charged, void = refunded).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fae.cell.contrib.elastic_resource import law  # noqa: E402

FILES = ("trace.csv", "trace_events.json", "schedule.json", "metrics.json", "verdict.json")


def tolerance_for(specs):
    """{variant or None: seconds} from --tolerance values."""
    out = {}
    for s in specs:
        name, _, sec = s.rpartition("=")
        out[name or None] = float(sec)
    return out


def numbered_start(trace):
    """The epoch of second 0 of a trace that numbered its own lines (`ts_s`
    beside `epoch`), None for any other."""
    with open(trace) as f:
        first = next(csv.DictReader(f), None)
    if not first or first.get("ts_s") in (None, "") or first.get("epoch") in (None, ""):
        return None
    return float(first["epoch"]) - float(first["ts_s"])


def judge(d, args, switches):
    events = json.loads((d / "trace_events.json").read_text())
    metrics = json.loads((d / "metrics.json").read_text())
    sched = json.loads((d / "schedule.json").read_text())
    rows = law.read_trace(str(d / "trace.csv"), events)
    if events.get("k6_start_epoch") is None:
        events["k6_start_epoch"] = numbered_start(d / "trace.csv")
    sat = args.sat_p99 if args.sat_p99 is not None else float(events.get("sat_p99_ms") or 150)
    return law.judge(rows, events, int(metrics.get("load_total") or 0),
                     int(metrics.get("load_errors") or 0), sched, sat_p99=sat,
                     max_mount=args.max_mount, max_pct=args.max_pct, max_lag=args.max_lag,
                     **switches)


def switches_for(variant, sched, args, tol):
    s = {}
    t = tol.get(variant, tol.get(None))
    if t:
        n = sum(1 for w in sched["events"] if w["kind"] == "spike")
        s["mount_tol"] = s["release_tol"] = (t,) * n
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--tolerance", action="append", default=[])
    ap.add_argument("--law-stages", default="")
    ap.add_argument("--sat-p99", type=float, default=None)
    ap.add_argument("--max-mount", type=float, default=30.0)
    ap.add_argument("--max-pct", type=float, default=0.0)
    ap.add_argument("--max-lag", type=float, default=20.0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    tol = tolerance_for(args.tolerance)
    law_stages = {s for s in args.law_stages.split(",") if s}
    counted, agree, skipped = Counter(), Counter(), Counter()
    changes = defaultdict(Counter)
    for d in map(Path, args.dirs):
        if not all((d / f).is_file() for f in FILES):
            skipped["files missing"] += 1
            continue
        try:
            verdict = json.loads((d / "verdict.json").read_text())
            variant = json.loads((d / "metrics.json").read_text()).get("variant") or "?"
            sched = json.loads((d / "schedule.json").read_text())
        except ValueError:
            skipped["unreadable"] += 1
            continue
        if law_stages and not verdict.get("ok") and verdict.get("stage") not in law_stages:
            skipped["decided before the law"] += 1
            continue
        try:
            off, why_off = judge(d, args, {})
            on, why_on = judge(d, args, switches_for(variant, sched, args, tol))
        except (OSError, ValueError, KeyError) as e:
            skipped[f"unjudgeable ({type(e).__name__})"] += 1
            continue
        counted[variant] += 1
        agree[variant] += (off == "true") == bool(verdict.get("ok"))
        if off != on:
            changes[variant][f"{off}->{on}"] += 1
            if args.list:
                print(f"{d}\t{variant}\trecorded={'green' if verdict.get('ok') else verdict.get('stage')}"
                      f"\t{off}->{on}\toff: {'; '.join(why_off)[:200]}")
    kinds = sorted({k for c in changes.values() for k in c})
    print("variant\tcounted\toff_agrees\t" + "\t".join(kinds))
    for v in sorted(counted):
        print(f"{v}\t{counted[v]}\t{agree[v]}\t" + "\t".join(str(changes[v][k]) for k in kinds))
    print(f"total\t{sum(counted.values())}\t{sum(agree.values())}\t"
          + "\t".join(str(sum(changes[v][k] for v in changes)) for k in kinds))
    for why, n in sorted(skipped.items()):
        print(f"skipped\t{n}\t{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
