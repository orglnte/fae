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
                 reports it mounted AND the substrate probe confirms it). A
                 scaler that exposes no decision (an external autoscaler) is
                 measured from the store's saturation (saturation_epoch, the
                 signal it reacts to) instead. "" when the cache never
                 mounted or nothing marks where to measure from.

Usage: k6.py <k6_summary.json> <trace.csv> <trace_events.json>
"""
from __future__ import annotations

import json
import sys


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


def mount_delay_s(csv_path: str, events_path: str) -> str:
    """decision -> mount, or saturation -> mount for a scaler with no
    observable decision (see the module docstring)."""
    try:
        ev = json.load(open(events_path))
    except (OSError, json.JSONDecodeError):
        return ""
    t1 = ev.get("mount_epoch")
    if t1 is None:
        return ""  # cache never mounted / never confirmed by the substrate
    t0 = ev.get("decision_epoch")
    if t0 is None:
        t0 = ev.get("saturation_epoch")
    if t0 is None:
        return ""
    return str(round(max(float(t1) - float(t0), 0.0), 2))


def main() -> None:
    k6_summary = sys.argv[1]
    trace_csv = sys.argv[2] if len(sys.argv) > 2 else ""
    events = sys.argv[3] if len(sys.argv) > 3 else ""
    total, errors, dropped, content = k6_metrics(k6_summary)
    delay = mount_delay_s(trace_csv, events) if (trace_csv and events) else ""
    print(total, errors, dropped, content, delay)


if __name__ == "__main__":
    main()
