#!/usr/bin/env python3
"""Derive the load + mount-delay numbers for one run, from the rich trace.

Reads three artifacts written by the load run and prints five space-separated
values on one line:

    load_total load_errors dropped content mount_delay_s

  load_total     completed HTTP requests (k6 http_reqs.count).
  load_errors    requests that did NOT respond within the 20s client timeout
                 (k6 http_req_failed) PLUS fake-200s the loadgen flagged
                 (content_errors). Queueing in the admission gate during the
                 cache warm-up is NOT an error as long as the response lands
                 under the timeout.
  dropped        k6 dropped_iterations (a client VU-pool limit, diagnostic only —
                 NOT a server failure).
  content        content_errors alone (anti-gaming: hot 200s missing real data).
  mount_delay_s  PROVISIONING latency: seconds from the scaler's decision to
                 the cache becoming provably ready (mount_epoch: the app
                 reports it mounted AND the infra probe confirms it). A
                 scaler that exposes no decision (an external autoscaler) is
                 measured from the store's saturation, the signal it reacts
                 to, instead: with a schedule, the first SPIKE's saturation
                 (the law's, a tick inside the spike's window) to the mount
                 that starts in that spike; without one, the run's first
                 saturated tick. "" when the cache never mounted or nothing
                 marks where to measure from.

Usage: k6.py <k6_summary.json> <trace.csv> <trace_events.json> [schedule.json]
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
from pathlib import Path


def _law():
    path = Path(__file__).with_name("law.py")
    spec = importlib.util.spec_from_file_location("_elastic_resource_law", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def k6_metrics(path: str) -> tuple[int, int, int, int]:
    try:
        m = json.load(open(path)).get("metrics", {})
        total = int((m.get("http_reqs") or {}).get("count", 0))
        rate = float((m.get("http_req_failed") or {}).get("value", 0.0))
        content = int((m.get("content_errors") or {}).get("count", 0))
        dropped = int((m.get("dropped_iterations") or {}).get("count", 0))
        return total, round(rate * total) + content, dropped, content
    except Exception:  # noqa: BLE001 - a missing/partial summary yields zeros
        return 0, 0, 0, 0


def read_mounts(csv_path: str) -> list[dict]:
    """Per tick: ts, p99, and mounted as mount_epoch defines it (the app
    reports the resource mounted AND the infra confirms it)."""
    out = []
    for r in csv.DictReader(open(csv_path)):
        out.append({"ts": float(r.get("ts_s") or 0), "p99": float(r.get("p99_ms") or 0),
                    "mounted": (str(r.get("cache_mounted", "")).strip() == "True"
                                and str(r.get("infra_cache_up", "")).strip() == "True")})
    return out


def spike_mount_delays(rows: list[dict], spikes: list[dict], sat_p99: float) -> list:
    """Per spike: seconds from its saturation (the law's: the first tick
    inside its window at sat_p99) to the mount that starts in it (from its
    start, less the law's grace, to the next spike's); None when either is
    missing. A mount that began before the spike is not this spike's."""
    law = _law()
    onsets = [r["ts"] for prev, r in zip([{"mounted": False}] + rows, rows)
              if r["mounted"] and not prev["mounted"]]
    out = []
    for i, spk in enumerate(spikes):
        sat, _ = law.saturation(rows, sat_p99, spk["t0"], spk["t1"])
        end = spikes[i + 1]["t0"] - law.GRACE_S if i + 1 < len(spikes) else float("inf")
        mount = next((t for t in onsets if spk["t0"] - law.GRACE_S <= t < end), None)
        out.append(None if sat is None or mount is None else round(max(mount - sat, 0.0), 2))
    return out


def mount_delay_s(csv_path: str, events_path: str, schedule_path: str = "") -> str:
    """decision -> mount, or saturation -> mount for a scaler with no
    observable decision (see the module docstring)."""
    try:
        ev = json.load(open(events_path))
    except (OSError, json.JSONDecodeError):
        return ""
    t1 = ev.get("mount_epoch")
    if t1 is None:
        return ""  # cache never mounted / never confirmed by the infra
    t0 = ev.get("decision_epoch")
    if t0 is not None:
        return str(round(max(float(t1) - float(t0), 0.0), 2))
    if schedule_path and os.path.isfile(schedule_path):
        try:
            spikes = [w for w in json.load(open(schedule_path))["events"] if w["kind"] == "spike"]
            sat_p99 = float(ev.get("sat_p99_ms") or os.environ.get("VERIFY_SAT_P99_MS") or 150)
            delays = spike_mount_delays(read_mounts(csv_path), spikes, sat_p99)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return ""
        return "" if not delays or delays[0] is None else str(delays[0])
    t0 = ev.get("saturation_epoch")
    if t0 is None:
        return ""
    return str(round(max(float(t1) - float(t0), 0.0), 2))


def main() -> None:
    k6_summary = sys.argv[1]
    trace_csv = sys.argv[2] if len(sys.argv) > 2 else ""
    events = sys.argv[3] if len(sys.argv) > 3 else ""
    schedule = sys.argv[4] if len(sys.argv) > 4 else ""
    total, errors, dropped, content = k6_metrics(k6_summary)
    delay = mount_delay_s(trace_csv, events, schedule) if (trace_csv and events) else ""
    print(total, errors, dropped, content, delay)


if __name__ == "__main__":
    main()
