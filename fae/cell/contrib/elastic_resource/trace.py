#!/usr/bin/env python3
"""Rich-trace sidecar — the per-second sensor beside the load generator.

Runs alongside k6 (k6 owns load generation; this owns observation) and writes:

  1. a per-second CSV trace  (--csv-output) — offered load, latency percentiles,
     cache/pool state, HTTP response mix;
  2. a small events JSON      (--events-output) — the load start_epoch, the
     saturation_epoch (the run's first tick whose p99 reaches --sat-p99-ms,
     a blip's included; the law and k6.py take each spike's own saturation
     from the trace and the schedule), the substrate-gated mount_epoch, the
     release_epoch.

CONCURRENCY: the k6 JSONL tailer, the /health poller, and the substrate probe
each run on their OWN thread, so a slow /health (up to 1s under saturation) or a
slow substrate probe (a docker/kubectl subprocess) can NEVER starve the tailer.
An earlier single-threaded version stalled during the load burst and dropped
thousands of k6 points (the response-mix columns undercounted by ~50x); the
tailer must keep up in real time. Authoritative totals still come from k6's
--summary-export (k6.py reads that); this trace is the shape.

The mount timestamp is GROUND-TRUTH gated: mount_epoch is the first moment the
app reports the resource mounted (--mounted-field, default cache_mounted) AND
the substrate probe confirms it is serving — so no variant can post a mount
before its resource is provably up. The decision epoch is read off another
health field (--decision-field/--decision-value, default scaler_state ==
acquiring) where the variant exposes one.

Stdlib only (threading + urllib + subprocess) — the harness python has no httpx.

Usage (the verifier wires this up):
  python3 trace.py \
      --base-url http://127.0.0.1:8080 --k6-jsonl <ws>/k6_results.jsonl \
      --duration-s 65 --cache-probe-cmd "<command; exit 0 == resource serving>" \
      --ceiling-qps 55 --sat-p99-ms 150 \
      --csv-output <ws>/trace.csv --events-output <ws>/trace_events.json
"""
from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

_HEALTH_POLL_S = 0.5      # /health cadence
_SUBSTRATE_POLL_S = 1.0   # substrate probe cadence (subprocess — kept off the tick path)
_LAT_WINDOW_S = 1.0       # sliding window for p90/p99


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(q * len(s))))
    return s[idx]


def _bucket(status: str) -> str:
    """k6 http_reqs status tag -> coarse bucket. 404 (app-state "no such record")
    and 429 (admission shed) are split out; status "0" is a k6 transport error
    (DNS/connect/read-timeout — this is how the 20s request timeouts land)."""
    if not status or status == "0":
        return "err"
    try:
        c = int(status)
    except ValueError:
        return "err"
    if c == 404:
        return "http_404"
    if c == 429:
        return "http_429"
    if 200 <= c < 400:
        return "http_2xx"
    if 500 <= c < 600:
        return "http_5xx"
    return "err"


class _Shared:
    """State the threads write and the main loop snapshots, under one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fired = 0
        self.dropped = 0
        self.status = {"http_2xx": 0, "http_404": 0, "http_429": 0, "http_5xx": 0, "err": 0}
        self.latencies: deque[tuple[float, float]] = deque()
        self.health: dict = {}
        self.substrate = False


def _tail_thread(path: Path, st: _Shared, stop: threading.Event) -> None:
    """Continuously tail k6's JSONL — the ONLY job of this thread, so it is never
    blocked by a slow network call. Batches updates into the shared state every
    ~0.1s to keep lock traffic low even at thousands of points/sec."""
    fh = None
    fired = dropped = 0
    status = {"http_2xx": 0, "http_404": 0, "http_429": 0, "http_5xx": 0, "err": 0}
    lat: list[tuple[float, float]] = []
    last_merge = time.monotonic()

    def merge() -> None:
        nonlocal fired, dropped, lat
        with st.lock:
            st.fired += fired
            st.dropped += dropped
            for k, v in status.items():
                st.status[k] += v
            st.latencies.extend(lat)
        fired = dropped = 0
        for k in status:
            status[k] = 0
        lat = []

    def consume(line: str) -> None:
        nonlocal fired, dropped
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if obj.get("type") != "Point":
            return
        metric = obj.get("metric") or ""
        data = obj.get("data") or {}
        if metric == "fired":
            fired += int(data.get("value") or 0)
        elif metric == "dropped_iterations":
            dropped += int(data.get("value") or 0)
        elif metric == "http_reqs":
            status[_bucket(str((data.get("tags") or {}).get("status") or "0"))] += 1
        elif metric == "http_req_duration":
            lat.append((time.monotonic(), float(data.get("value") or 0.0)))

    while not stop.is_set():
        if fh is None:
            if not path.exists():
                stop.wait(0.05)
                continue
            fh = path.open("r")
        line = fh.readline()
        if not line:
            if fired or dropped or lat or any(status.values()):
                merge()
            stop.wait(0.02)
            continue
        consume(line)
        if time.monotonic() - last_merge > 0.1:
            merge()
            last_merge = time.monotonic()
    # Final drain: read whatever k6 has already written but we haven't consumed.
    if fh is not None:
        for line in fh:
            consume(line)
    merge()


def _health_thread(url: str, st: _Shared, stop: threading.Event) -> None:
    while not stop.is_set():
        h = None
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    h = json.loads(r.read().decode())
        except Exception:  # noqa: BLE001 - a failed poll keeps the prior snapshot
            h = None
        if h is not None:
            with st.lock:
                st.health = h
        stop.wait(_HEALTH_POLL_S)


def _substrate_thread(argv: list[str] | None, st: _Shared, stop: threading.Event) -> None:
    if not argv:
        return
    while not stop.is_set():
        try:
            up = subprocess.run(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            ).returncode == 0
        except Exception:  # noqa: BLE001
            up = False
        with st.lock:
            st.substrate = up
        stop.wait(_SUBSTRATE_POLL_S)


HOST_SLEEP_GAP_S = 30.0


def host_sleep_gap(wall_dt, mono_dt):
    """Seconds the host was away during one tick: wall-clock advanced, the
    monotonic clock did not. Below the threshold is scheduler jitter, 0."""
    gap = wall_dt - mono_dt
    return gap if gap > HOST_SLEEP_GAP_S else 0.0


def main(argv=None) -> None:
    # argv[0] is the script path (subprocess convention); None = sys.argv, so
    # script mode is unchanged. An explicit argv lets an in-process caller run
    # this concurrently without touching the process-global sys.argv.
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--k6-jsonl", type=Path, required=True, action="append",
                    help="a k6 JSON output to tail; repeat for every generator (the blips')")
    ap.add_argument("--duration-s", type=int, default=65)
    ap.add_argument("--cache-probe-cmd", default="",
                    help="shell command; exit 0 == cache backend confirmed up")
    ap.add_argument("--ceiling-qps", type=float, default=0.0)
    ap.add_argument("--sat-p99-ms", type=float, default=150.0,
                    help="p99 at which the store counts as saturated (the law's anchor)")
    ap.add_argument("--csv-output", type=Path, required=True)
    ap.add_argument("--events-output", type=Path, required=True)
    ap.add_argument("--mounted-field", default="cache_mounted",
                    help="/health field that is true while the resource is mounted")
    ap.add_argument("--decision-field", default="scaler_state",
                    help="/health field that carries the scaler's decision, if any")
    ap.add_argument("--decision-value", default="acquiring",
                    help="the decision field's value at the moment of the acquire decision")
    ap.add_argument("--pressure-field", default="pool_pressure",
                    help="/health field traced as the load signal (pp column)")
    ap.add_argument("--no-stream", dest="stream", action="store_false",
                    help="disable the live per-second console table (streamed by default)")
    ap.set_defaults(stream=True)
    args = ap.parse_args(argv[1:] if argv is not None else None)

    st = _Shared()
    stop = threading.Event()
    probe_argv = shlex.split(args.cache_probe_cmd) if args.cache_probe_cmd else None
    threads = [
        *[threading.Thread(target=_tail_thread, args=(p, st, stop), daemon=True)
          for p in args.k6_jsonl],
        threading.Thread(target=_health_thread, args=(f"{args.base_url}/health", st, stop), daemon=True),
        threading.Thread(target=_substrate_thread, args=(probe_argv, st, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    start_epoch = time.time()
    mount_epoch = release_epoch = sat_ts = sat_epoch = decision_epoch = None

    args.csv_output.parent.mkdir(parents=True, exist_ok=True)
    cf = args.csv_output.open("w", newline="")
    w = csv.writer(cf)
    w.writerow([
        "epoch", "ts_s", "offered_rps", "p90_ms", "p99_ms",
        "cache_mounted", "substrate_cache_up", "pool_pressure",
        "inflight", "pool_size", "pool_waiting",
        "http_2xx", "http_404", "http_429", "http_5xx", "http_err", "k6_drop",
    ])

    _hdr = (f"{'ts':>5} {'rps':>6} {'p90':>6} {'p99':>7} {'pp':>5} "
            f"{'cch':>3} {'sub':>3} {'2xx':>7} {'404':>6} {'429':>6} "
            f"{'5xx':>5} {'err':>5} {'drp':>5}")
    if args.stream:
        print(f"# live trace  ceiling={args.ceiling_qps:g}qps  "
              f"saturation=p99>={args.sat_p99_ms:g}ms  (--no-stream to silence)",
              flush=True)
        print(_hdr, flush=True)

    started = time.monotonic()
    last_fired = 0
    last_tick_mono = started
    last_tick_wall = time.time()
    host_sleep_s = 0.0
    try:
        for tick in range(1, args.duration_s + 1):
            target = started + tick
            now = time.monotonic()
            if target > now:
                time.sleep(target - now)
            now = time.monotonic()
            elapsed = now - started
            wall = time.time()
            host_sleep_s += host_sleep_gap(wall - last_tick_wall, now - last_tick_mono)
            last_tick_wall = wall

            with st.lock:
                cutoff = now - _LAT_WINDOW_S
                while st.latencies and st.latencies[0][0] < cutoff:
                    st.latencies.popleft()
                win = [v for _, v in st.latencies]
                fired = st.fired
                dropped = st.dropped
                status = dict(st.status)
                snap = dict(st.health)
                substrate = st.substrate

            cache_self = bool(snap.get(args.mounted_field, False))
            if decision_epoch is None and str(snap.get(args.decision_field, "")) == args.decision_value:
                decision_epoch = time.time()
                if args.stream:
                    print(f"  >>> scaler DECISION: acquiring at ts={elapsed:.1f}s "
                          f"(saturation+latency sustained)", flush=True)
            if mount_epoch is None and cache_self and substrate:
                mount_epoch = time.time()
                if args.stream:
                    _prov = f" (provision {mount_epoch - decision_epoch:.1f}s)" if decision_epoch else ""
                    _md = f" — {mount_epoch - sat_epoch:.1f}s after saturation" if sat_epoch else ""
                    print(f"  >>> cache MOUNTED (substrate-confirmed) at ts={elapsed:.1f}s{_prov}{_md}", flush=True)
            if (mount_epoch is not None and release_epoch is None
                    and not cache_self and not substrate):
                release_epoch = time.time()
                if args.stream:
                    print(f"  >>> cache RELEASED at ts={elapsed:.1f}s", flush=True)

            p90 = _percentile(win, 0.90)
            p99 = _percentile(win, 0.99)
            dt = max(now - last_tick_mono, 1e-3)
            offered_rps = round((fired - last_fired) / dt)
            last_fired = fired
            last_tick_mono = now
            pp = round(float(snap.get(args.pressure_field, 0.0) or 0.0), 3)

            if sat_ts is None and p99 >= args.sat_p99_ms:
                sat_ts = round(elapsed, 1)
                sat_epoch = time.time()
                if args.stream:
                    print(f"  >>> store SATURATED at ts={sat_ts}s "
                          f"(p99 {p99:.0f}ms >= {args.sat_p99_ms:g}ms at {offered_rps} rps)", flush=True)

            w.writerow([
                round(time.time(), 3), round(elapsed, 1), offered_rps,
                round(p90, 1), round(p99, 1),
                cache_self, substrate, pp,
                int(snap.get("inflight", 0) or 0),
                int(snap.get("pool_size", 0) or 0),
                int(snap.get("pool_waiting", 0) or 0),
                status["http_2xx"], status["http_404"], status["http_429"],
                status["http_5xx"], status["err"], dropped,
            ])
            cf.flush()

            if args.stream:
                print(f"{elapsed:5.1f} {offered_rps:6d} {p90:6.0f} {p99:7.0f} "
                      f"{pp:5.2f} {'y' if cache_self else 'n':>3} "
                      f"{'y' if substrate else 'n':>3} "
                      f"{status['http_2xx']:7d} {status['http_404']:6d} "
                      f"{status['http_429']:6d} {status['http_5xx']:5d} "
                      f"{status['err']:5d} {dropped:5d}", flush=True)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=3.0)
        cf.close()
        events = {
            "start_epoch": round(start_epoch, 3),
            "decision_epoch": round(decision_epoch, 3) if decision_epoch is not None else None,
            "mount_epoch": round(mount_epoch, 3) if mount_epoch is not None else None,
            "release_epoch": round(release_epoch, 3) if release_epoch is not None else None,
            "saturation_epoch": round(sat_epoch, 3) if sat_epoch is not None else None,
            "ceiling_qps": args.ceiling_qps,
            "sat_p99_ms": args.sat_p99_ms,
            "host_sleep_s": round(host_sleep_s, 1),
        }
        args.events_output.parent.mkdir(parents=True, exist_ok=True)
        args.events_output.write_text(json.dumps(events, indent=2) + "\n")


if __name__ == "__main__":
    main()
