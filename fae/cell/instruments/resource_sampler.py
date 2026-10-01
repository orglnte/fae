#!/usr/bin/env python3
"""resource_sampler.py — sample the RUNNING footprint of a deployed artifact
across three planes, so a run can report BOTH how many components it takes and
what they cost (mem/cpu). Runs for a bounded window (alongside the load spike),
records every sample to a CSV, and writes a peak-summary JSON.

Why three planes (fairness): different variants run their pieces in different
places, and counting only one plane would rig the comparison.
  * docker  — the containers the cell owns (--own): the store, a sidecar, a
              daemon, a cluster node.
  * k8s     — the pods in the namespaces named (--namespaces), an autoscaler's
              own pods among them. Per-pod mem/cpu needs metrics-server (kubectl top).
  * host    — plain processes the bring-up started beside the sampler (the
              service, a control plane), found through their pidfiles; without
              this plane a variant whose pieces are processes would count as
              zero and look artificially free.

Each running unit is tagged app|substrate. "component count" = app-tagged units
(the thing under comparison); substrate (a cluster node, an autoscaler's operator,
metrics-server) is reported separately as platform overhead, never hidden but not
charged to the variant's authored surface.

Double-count guard: the kind node container's mem ~= the sum of the pods it hosts,
so the kind node is tagged substrate and EXCLUDED from the app mem/cpu totals (the
pods represent that memory). Report it as overhead context only.

stdlib only. Every external command is best-effort: a plane that isn't present
(no pods, no metrics-server, no pidfile) contributes nothing rather than failing.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time

_KIND_INFRA = re.compile(r"(control-plane|worker)")


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def _mem_to_mb(s: str) -> float:
    """Parse a docker/k8s memory token (e.g. '123.4MiB', '1.2GiB', '512Ki',
    '256Mi', '900m'... ) to MB. Docker uses '<used> / <limit>'; caller passes the
    used side only."""
    s = s.strip()
    m = re.match(r"([0-9.]+)\s*([A-Za-z]*)", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).lower()
    # binary and decimal treated alike at this granularity
    if unit.startswith("gi") or unit.startswith("g"):
        return val * 1024.0
    if unit.startswith("mi") or unit.startswith("m") and unit != "m":
        return val
    if unit.startswith("ki") or unit.startswith("k"):
        return val / 1024.0
    if unit == "b" or unit == "":
        return val / (1024.0 * 1024.0)
    # kubectl 'top pods' reports bytes without a unit sometimes; and CPU 'm'
    return val


def _cpu_to_pct(s: str) -> float:
    """docker '12.34%' -> 12.34 ; kubectl top cpu '15m' (millicores) -> 1.5 (%/core-ish).
    We keep docker as %-of-core and convert millicores to the same scale (1000m=100%)."""
    s = s.strip()
    if s.endswith("%"):
        try:
            return float(s[:-1])
        except ValueError:
            return 0.0
    if s.endswith("m"):
        try:
            return float(s[:-1]) / 10.0  # 1000m = 100%
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def sample_docker(own: set[str] | None = None) -> list[dict]:
    """docker stats one-shot. Tag kind infra as substrate, the rest as app.

    `own` = the container names that belong to the cell being measured (its
    dind sidecar, its verify cluster's node, the harness store). docker stats
    is machine-wide, and other cells' sidecars and sandbox clusters run
    beside this one; without the filter a cell is charged its neighbours.
    None = no filter (the single-tenant reference runs)."""
    out = _run(
        ["docker", "stats", "--no-stream", "--format",
         "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"]
    )
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, cpu, mem = parts[0], parts[1], parts[2]
        if own is not None and name not in own:
            continue
        used = mem.split("/")[0]
        tag = "substrate" if _KIND_INFRA.search(name) else "app"
        rows.append({
            "plane": "docker", "name": name, "tag": tag,
            "cpu_pct": _cpu_to_pct(cpu), "mem_mb": _mem_to_mb(used),
        })
    return rows


def _cgroup_mem_mb(container: str) -> float | None:
    """The container's memory as its own cgroup reports it — a second,
    independent source for what docker stats said. Same definition as docker
    stats: usage minus the inactive file cache (memory.current -
    inactive_file on cgroup v2; usage_in_bytes - total_inactive_file on v1),
    or a kind node reads 2-3x its stats figure from page cache alone."""
    out = _run(["docker", "exec", container, "sh", "-c",
                "if [ -f /sys/fs/cgroup/memory.current ]; then"
                "  cat /sys/fs/cgroup/memory.current;"
                "  grep '^inactive_file ' /sys/fs/cgroup/memory.stat;"
                " else"
                "  cat /sys/fs/cgroup/memory/memory.usage_in_bytes;"
                "  grep '^total_inactive_file ' /sys/fs/cgroup/memory/memory.stat;"
                " fi"], timeout=6.0)
    return cgroup_mem_mb_from(out)


def cgroup_mem_mb_from(text: str) -> float | None:
    """Pure form of the parse above, for tests."""
    lines = text.split()
    try:
        usage = int(lines[0])
    except (IndexError, ValueError):
        return None
    inactive = 0
    for i, tok in enumerate(lines):
        if tok in ("inactive_file", "total_inactive_file") and i + 1 < len(lines):
            try:
                inactive = int(lines[i + 1])
            except ValueError:
                inactive = 0
            break
    return max(usage - inactive, 0) / (1024.0 * 1024.0)


def cross_check(rows: list[dict], tolerance_pct: float,
                cgroup=None) -> tuple[bool, list[str]]:
    """Every docker component measured twice (stats vs its cgroup); the pods
    of a kind node must fit inside that node. A disagreement beyond the
    tolerance marks the sample unreliable — the number is not trusted, it is
    reported as such. Returns (reliable, findings)."""
    cgroup = cgroup or _cgroup_mem_mb
    findings = []
    for r in rows:
        if r["plane"] != "docker":
            continue
        other = cgroup(r["name"])
        if other is None:
            findings.append(f"{r['name']}: cgroup memory unreadable")
            continue
        base = max(r["mem_mb"], other, 1.0)
        if abs(r["mem_mb"] - other) / base * 100.0 > tolerance_pct:
            findings.append(f"{r['name']}: docker stats {r['mem_mb']:.0f} MB vs "
                            f"cgroup {other:.0f} MB")
    node_mem = sum(r["mem_mb"] for r in rows
                   if r["plane"] == "docker" and _KIND_INFRA.search(r["name"]))
    pods_mem = sum(r["mem_mb"] for r in rows if r["plane"] == "k8s")
    if pods_mem and node_mem and pods_mem > node_mem * (1 + tolerance_pct / 100.0):
        findings.append(f"pods {pods_mem:.0f} MB exceed their node {node_mem:.0f} MB")
    return not findings, findings


def sample_k8s(namespaces: list[str], substrate_ns: set[str]) -> list[dict]:
    rows = []
    for ns in namespaces:
        out = _run(["kubectl", "top", "pods", "-n", ns, "--no-headers"])
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, cpu, mem = parts[0], parts[1], parts[2]
            tag = "substrate" if ns in substrate_ns else "app"
            rows.append({
                "plane": "k8s", "name": f"{ns}/{name}", "tag": tag,
                "cpu_pct": _cpu_to_pct(cpu), "mem_mb": _mem_to_mb(mem),
            })
    return rows


def _read_pid(pidfile: str) -> int | None:
    try:
        with open(pidfile) as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def sample_host(pidfiles: list[str]) -> list[dict]:
    """Plain processes named by pidfile (the service, a control plane the
    bring-up started): RSS via ps, instantaneous %cpu via top."""
    rows = []
    pids_and_labels = []
    for pf in pidfiles:
        pid = _read_pid(pf)
        if pid is not None:
            label = os.path.basename(pf).lstrip(".").replace(".pid", "")
            pids_and_labels.append((pid, label))
            
    if not pids_and_labels:
        return rows

    # 1. Get memory (rss) via ps (fast and reliable parsing)
    mem_by_pid = {}
    for pid, _ in pids_and_labels:
        out = _run(["ps", "-o", "rss=", "-p", str(pid)], timeout=4.0)
        rss_kb = float(out.strip()) if out.strip().replace(".", "").isdigit() else 0.0
        mem_by_pid[pid] = rss_kb / 1024.0

    # 2. Get instantaneous CPU via top (l=2 iterations, we parse the second one). 
    # macOS ps pcpu is a decaying average; top -l 2 gives instantaneous on the 2nd iteration.
    # We fallback to ps pcpu if top fails (e.g. on Linux where args differ).
    top_cmd = ["top", "-l", "2", "-stats", "pid,cpu"]
    for pid, _ in pids_and_labels:
        top_cmd.extend(["-pid", str(pid)])
    
    out = _run(top_cmd, timeout=4.0)
    cpu_by_pid = {}
    
    # top output prints multiple iterations. The second iteration (instantaneous) is at the end.
    # By simply iterating over all lines, the later occurrences will overwrite the earlier ones.
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].isdigit():
            parsed_pid = int(parts[0])
            if parsed_pid in mem_by_pid:  # we only care about the pids we queried
                cpu_by_pid[parsed_pid] = _cpu_to_pct(parts[1])
                
    # Fallback to ps for any pid that top didn't capture (or if top failed entirely)
    for pid, _ in pids_and_labels:
        if pid not in cpu_by_pid:
            out = _run(["ps", "-o", "pcpu=", "-p", str(pid)], timeout=4.0)
            cpu_by_pid[pid] = float(out.strip()) if out.strip().replace(".", "").isdigit() else 0.0

    for pid, label in pids_and_labels:
        rows.append({
            "plane": "host", "name": label, "tag": "app",
            "cpu_pct": cpu_by_pid[pid], "mem_mb": mem_by_pid[pid],
        })
    return rows


def _machine_totals(rows: list[dict]) -> dict:
    """The honest machine footprint. The kind-node docker container already
    CONTAINS every pod, so the machine total = all docker + host processes; the
    per-pod k8s rows are the breakdown INSIDE that node, never re-added (that would
    double-count). This is what makes a cluster-backed variant's true cost
    visible: it drags a whole Kubernetes node (~GB) another variant does not need."""
    docker = [r for r in rows if r["plane"] == "docker"]
    pods = [r for r in rows if r["plane"] == "k8s"]
    host = [r for r in rows if r["plane"] == "host"]
    total_mem = sum(r["mem_mb"] for r in docker + host)
    total_cpu = sum(r["cpu_pct"] for r in docker + host)
    kind_nodes = [r for r in docker if _KIND_INFRA.search(r["name"])]
    non_kind_docker = [r for r in docker if not _KIND_INFRA.search(r["name"])]
    # deployed COMPONENTS = the app-tagged logical units this treatment runs
    # (non-kind docker + app pods + host processes). Substrate pods (an
    # autoscaler's operators, metrics-server) are NOT components — they are reported
    # separately in substrate_pods so an observer can re-attribute them (for a
    # cluster-backed arm they are mechanism cost; for the others idle
    # platform). The kind node is the cluster host (its mem contains the
    # pods') — separate again. EVERYTHING observed lands in exactly one group:
    #   app components | substrate pods | cluster host   -> auditable totals.
    components = [r for r in non_kind_docker + pods + host if r["tag"] == "app"]
    sub_pods = [r for r in pods if r["tag"] != "app"]

    def _tot(rs):
        return (round(sum(r["mem_mb"] for r in rs), 1),
                round(sum(r["cpu_pct"] for r in rs), 1))

    d_mem, d_cpu = _tot([r for r in non_kind_docker if r["tag"] == "app"])
    k_mem, k_cpu = _tot([r for r in pods if r["tag"] == "app"])
    h_mem, h_cpu = _tot(host)
    a_mem, a_cpu = _tot(components)
    s_mem, s_cpu = _tot(sub_pods)
    n_mem, n_cpu = _tot(kind_nodes)
    return {
        "total_mem_mb": round(total_mem, 1),
        "total_cpu_pct": round(total_cpu, 1),
        "pods_mem_mb": round(sum(r["mem_mb"] for r in pods), 1),
        "pods_cpu_pct": round(sum(r["cpu_pct"] for r in pods), 1),
        "app_mem_mb": a_mem, "app_cpu_pct": a_cpu,
        "by_group": {
            "docker_app":     {"mem_mb": d_mem, "cpu_pct": d_cpu},
            "k8s_app":        {"mem_mb": k_mem, "cpu_pct": k_cpu},
            "host_app":       {"mem_mb": h_mem, "cpu_pct": h_cpu},
            "substrate_pods": {"mem_mb": s_mem, "cpu_pct": s_cpu},
            "kind_node":      {"mem_mb": n_mem, "cpu_pct": n_cpu},
        },
        "component_count": len(components),
        "components_rows": components,
        "substrate_rows": sub_pods,
        "cluster_host_rows": kind_nodes,
    }


def main(argv=None) -> int:
    # argv[0] is the script path (subprocess convention); None = sys.argv, so
    # script mode is unchanged. An explicit argv lets an in-process caller run
    # this concurrently without touching the process-global sys.argv.
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-s", type=float, required=True)
    ap.add_argument("--interval-s", type=float, default=3.0)
    ap.add_argument("--namespaces", default="",
                    help="k8s namespaces to sample (comma-separated); none = no k8s plane")
    ap.add_argument("--substrate-namespaces", default="",
                    help="namespaces counted as platform, not workload (comma-separated)")
    ap.add_argument("--pidfile", action="append", default=[],
                    help="pidfile of a process the bring-up started (repeatable)")
    ap.add_argument("--csv-output", required=True)
    ap.add_argument("--json-output", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--own", action="append", default=[],
                    help="a docker container that belongs to the measured cell "
                         "(repeatable); given => only these are sampled")
    ap.add_argument("--tolerance-pct", type=float, default=15.0,
                    help="max disagreement between the two sources of a number")
    ap.add_argument("--no-cross-check", action="store_true")
    args = ap.parse_args(argv[1:] if argv is not None else None)

    namespaces = [n for n in args.namespaces.split(",") if n]
    substrate_ns = {n for n in args.substrate_namespaces.split(",") if n}
    own = set(args.own) if args.own else None
    unreliable_samples, findings_seen = 0, []

    deadline = time.monotonic() + args.duration_s
    peak = None  # sample dict with the highest machine memory
    max_cpu_pct = 0.0
    max_components = 0
    n_samples = 0
    # cpu-second (= core-second) integral over the window: sum of the instantaneous
    # total cpu (in cores) times the sample interval. This is the honest COMPUTE
    # COST — an idle-but-running kind cluster still burns ~1 core the whole window,
    # which a peak-% snapshot understates and a cost story must capture.
    cpu_core_seconds = 0.0
    mem_mb_seconds = 0.0
    last_sample_t = None

    with open(args.csv_output, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "plane", "name", "tag", "cpu_pct", "mem_mb"])
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            ts = round(time.monotonic() - t0, 1)
            rows = sample_docker(own) + sample_k8s(namespaces, substrate_ns) \
                + sample_host(args.pidfile)
            if not args.no_cross_check:
                ok, findings = cross_check(rows, args.tolerance_pct)
                if not ok:
                    unreliable_samples += 1
                    findings_seen += [f for f in findings if f not in findings_seen]
            for r in rows:
                w.writerow([ts, r["plane"], r["name"], r["tag"],
                            round(r["cpu_pct"], 2), round(r["mem_mb"], 1)])
            fh.flush()
            n_samples += 1
            tot = _machine_totals(rows)
            # integrate over the ACTUAL elapsed time since the previous sample —
            # the sampling commands (docker stats, top) take 1-3s themselves, so
            # assuming interval_s would undercount the window by ~5-10%.
            now_t = time.monotonic() - t0
            dt = now_t - last_sample_t if last_sample_t is not None else args.interval_s
            last_sample_t = now_t
            cpu_core_seconds += (tot["total_cpu_pct"] / 100.0) * dt
            mem_mb_seconds += tot["total_mem_mb"] * dt
            
            if tot["total_cpu_pct"] > max_cpu_pct:
                max_cpu_pct = tot["total_cpu_pct"]
                
            if tot["component_count"] > max_components:
                max_components = tot["component_count"]
                
            # peak = the sample with the highest machine memory (all components up;
            # a 0<->1 cache pod is only present during the mounted spike).
            if peak is None or tot["total_mem_mb"] > peak["total_mem_mb"]:
                _round = lambda r: {k: (round(v, 1) if isinstance(v, float) else v)
                                    for k, v in r.items()}
                peak = {
                    "ts": ts,
                    # count == app-tagged components only; substrate pods and
                    # the kind node are reported in their own groups below.
                    "component_count": tot["component_count"],
                    "total_mem_mb": tot["total_mem_mb"],
                    "total_cpu_pct": tot["total_cpu_pct"],
                    "pods_mem_mb": tot["pods_mem_mb"],
                    "pods_cpu_pct": tot["pods_cpu_pct"],
                    "app_mem_mb": tot["app_mem_mb"],
                    "app_cpu_pct": tot["app_cpu_pct"],
                    "by_group": tot["by_group"],
                    "components": [_round(r) for r in tot["components_rows"]],
                    "substrate_pods": [_round(r) for r in tot["substrate_rows"]],
                    "cluster_host": [_round(r) for r in tot["cluster_host_rows"]],
                }
            time.sleep(max(0.0, args.interval_s - (time.monotonic() - t0 - ts)))

    # window = actual wall time covered by the samples (sampling commands take
    # 1-3s themselves, so n_samples * interval_s would misstate it).
    window_s = round(last_sample_t if last_sample_t else n_samples * args.interval_s, 1)
    summary = {
        "label": args.label,
        "samples": n_samples,
        "window_s": window_s,
        # Trust flags: a footprint is scoped to the cell only when told which
        # containers are its own, and reliable only when every sample's two
        # sources agreed. A reader must check these before quoting a number.
        "scope": "cell" if own is not None else "machine",
        "reliable": own is not None and not args.no_cross_check
                    and n_samples > 0 and unreliable_samples == 0,
        "unreliable_samples": unreliable_samples,
        "cross_check_findings": findings_seen[:20],
        # integrated compute/memory cost over the window (captures steady idle draw
        # a peak snapshot misses — e.g. a running-but-idle k8s cluster).
        "cpu_core_seconds": round(cpu_core_seconds, 1),
        "avg_cores": round(cpu_core_seconds / window_s, 3) if window_s else 0.0,
        "mem_mb_seconds": round(mem_mb_seconds, 0),
        "max_cpu_pct": round(max_cpu_pct, 1),
        "max_components": max_components,
        "peak": peak or {"component_count": 0, "total_mem_mb": 0.0,
                         "total_cpu_pct": 0.0, "pods_mem_mb": 0.0,
                         "pods_cpu_pct": 0.0, "app_mem_mb": 0.0,
                         "components": [], "cluster_host": []},
    }
    with open(args.json_output, "w") as fh:
        json.dump(summary, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
