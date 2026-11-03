"""Host load while a verify holds the fleet-wide verify lock.

A verify judges timings (p99, mount time) on a host the other live cells
share, so a verdict is only as comparable as the load it was judged under.
While the lock is held this samples the host's CPU and load, its memory, and
every container's CPU and memory with the cell that owns it, and writes:

  <conduct>/hostload/<cid>.a<attempt>.csv  every sample of that verify
  <conduct>/hostload/summary.csv           one row per arrangement: its
                                           window's host idle and load, and
                                           the CPU of its own cell, of other
                                           cells and of unowned containers

Recording only: nothing here changes a verdict, a refund or admission.
"""
from __future__ import annotations

import csv
import os
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import fae.experiment
from fae.cell.cell import Cell

from . import _host, zombies

PROBE_S = float(os.environ.get("HOSTLOAD_PROBE_S", 5))
SAMPLE_S = float(os.environ.get("HOSTLOAD_SAMPLE_S", 10))
RAW_FIELDS = ["ts", "scope", "name", "owner", "role", "cpu_pct", "mem_mb"]
SUMMARY_FIELDS = ["ts", "cell", "attempt", "arrangement", "outcome", "samples",
                  "idle_min", "idle_median", "load1_max", "mem_used_gb_max",
                  "own_cpu_median", "other_cells_cpu_median", "unowned_cpu_median",
                  "top_other"]
_KIND_NODE = re.compile(r"^(?P<cluster>.+)-(?:control-plane|worker\d*)$")
_MARK = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\t(VERIFY_READY|SHAPE)\t")
_SHAPE = re.compile(r"\b([BS]{3}) (pass|FAIL)\b")


def stamp(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t,
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


# --- parsing ---------------------------------------------------------------

def parse_top(text):
    """{cpu_user, cpu_sys, cpu_idle, load1} from `top -l 2` output: the LAST
    sample, since the first one top prints is averaged since boot."""
    out = {}
    cpu = re.findall(r"CPU usage: ([\d.]+)% user, ([\d.]+)% sys, ([\d.]+)% idle", text)
    if cpu:
        out.update(zip(("cpu_user", "cpu_sys", "cpu_idle"), map(float, cpu[-1])))
    load = re.findall(r"Load Avg: ([\d.]+)", text)
    if load:
        out["load1"] = float(load[-1])
    return out


def mem_mb(text):
    """MB from a docker stats usage figure such as '512.3MiB' or '1.2GiB'."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?)i?B", text or "")
    if not m:
        return None
    scale = {"": 1e-6, "K": 1e-3, "M": 1, "G": 1e3, "T": 1e6}[m.group(2)]
    return round(float(m.group(1)) * scale, 1)


def parse_stats(text):
    """[(name, cpu_pct, mem_mb)] from `docker stats --no-stream` lines in the
    format Name<TAB>CPUPerc<TAB>MemUsage."""
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, cpu, mem = parts
        try:
            pct = float(cpu.strip().rstrip("%"))
        except ValueError:
            continue
        rows.append((name.strip(), pct, mem_mb(mem.split("/")[0])))
    return rows


def owner_of(name, cids, clusters):
    """(owning cell, role) of a container: the cell whose id its name
    carries, or the cell that owns the kind cluster it is a node of
    (`clusters`: cluster name -> cid); ("", role) when no cell claims it."""
    for cid in cids:
        if name.endswith(cid) or f"-{cid}-" in name:
            return cid, name[:name.index(cid)].rstrip("-") or "cell"
    m = _KIND_NODE.match(name)
    if m:
        return clusters.get(m.group("cluster"), ""), "kind"
    return "", "other"


# --- the per-arrangement summary --------------------------------------------

def _median(xs):
    return round(statistics.median(xs), 1) if xs else ""


def summarize(cid, attempt, raw_rows, ledger_text):
    """One summary row per arrangement the ledger closed (VERIFY_READY up to
    its SHAPE) inside the span the raw samples cover."""
    if not raw_rows:
        return []
    first, last = _epoch(raw_rows[0]["ts"]), _epoch(raw_rows[-1]["ts"])
    by_ts = {}
    for r in raw_rows:
        by_ts.setdefault(_epoch(r["ts"]), []).append(r)
    out, ready = [], None
    for line in ledger_text.splitlines():
        m = _MARK.match(line)
        if not m:
            continue
        t = _epoch(m.group(1))
        if m.group(2) == "VERIFY_READY":
            ready = t
            continue
        shape = _SHAPE.search(line)
        if ready is None or shape is None or t < first or ready > last:
            ready = None
            continue
        window = [rows for ts, rows in by_ts.items() if ready <= ts <= t]
        host = lambda k: [float(r["cpu_pct"]) for rows in window for r in rows
                          if r["scope"] == "host" and r["name"] == k and r["cpu_pct"] != ""]
        def cpu_of(pick):
            per = [sum(float(r["cpu_pct"]) for r in rows
                       if r["scope"] == "container" and r["cpu_pct"] != "" and pick(r))
                   for rows in window]
            return _median(per)
        others = {}
        for rows in window:
            for r in rows:
                if r["scope"] == "container" and r["owner"] not in ("", cid) and r["cpu_pct"] != "":
                    k = f"{r['role']}:{r['owner']}"
                    others[k] = others.get(k, 0.0) + float(r["cpu_pct"]) / len(window)
        top = max(others.items(), key=lambda kv: kv[1]) if others else None
        idle, load, mem = host("cpu_idle"), host("load1"), host("mem_used_gb")
        out.append({
            "ts": m.group(1), "cell": cid, "attempt": attempt,
            "arrangement": shape.group(1), "outcome": shape.group(2),
            "samples": len(window),
            "idle_min": min(idle) if idle else "", "idle_median": _median(idle),
            "load1_max": max(load) if load else "",
            "mem_used_gb_max": max(mem) if mem else "",
            "own_cpu_median": cpu_of(lambda r: r["owner"] == cid),
            "other_cells_cpu_median": cpu_of(lambda r: r["owner"] not in ("", cid)),
            "unowned_cpu_median": cpu_of(lambda r: r["owner"] == ""),
            "top_other": f"{top[0]}={top[1]:.0f}" if top else "",
        })
        ready = None
    return out


# --- the sampler -------------------------------------------------------------

class HostLoad:
    """Watches the verify lock and records the host while it is held."""

    def __init__(self, workspace=None, run=_host.sh):
        self.workspace = workspace or fae.experiment.exp().workspace
        self.dir = Path(self.workspace.conduct) / "hostload"
        self.run = run
        self._stop = threading.Event()
        self._errors = set()

    def holder(self):
        """The cell holding the verify lock now, or ""."""
        locks = self.workspace.locks
        if not Cell.shared_lock_held(locks, "verify"):
            return ""
        return Cell.shared_lock_holder(locks, "verify")

    def attempt(self, cid):
        text = self.workspace.cell(cid).ledger_text()
        found = re.findall(rf"\tSTART\t{re.escape(cid)}\tattempt=(\d+)", text)
        return int(found[-1]) if found else 0

    def sample(self):
        """[raw row] of one sample: host CPU, load and memory, then every
        container with its owner and role."""
        ts = stamp()
        rows = [dict(ts=ts, scope="host", name=k, owner="", role="", cpu_pct=v, mem_mb="")
                for k, v in parse_top(self.run(["top", "-l", "2", "-s", "1", "-n", "0"])).items()]
        mem = _host.mem_pressure()
        if mem.get("used_gb"):
            rows.append(dict(ts=ts, scope="host", name="mem_used_gb", owner="", role="",
                             cpu_pct=round(mem["used_gb"], 2), mem_mb=""))
        stats = self.run(["docker", "stats", "--no-stream", "--format",
                          "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"])
        cids = sorted(self.workspace.cells(), key=len, reverse=True)
        clusters = {ident: c for c, ident in zombies._cluster_map().items()}
        for name, cpu, mem_used in parse_stats(stats):
            owner, role = owner_of(name, cids, clusters)
            rows.append(dict(ts=ts, scope="container", name=name, owner=owner, role=role,
                             cpu_pct=cpu, mem_mb="" if mem_used is None else mem_used))
        return rows

    def _append(self, path, fields, rows):
        if not rows:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            if new:
                w.writeheader()
            w.writerows(rows)

    def raw_path(self, cid, attempt):
        return self.dir / f"{cid}.a{attempt}.csv"

    def close_verify(self, cid, attempt):
        """Write the summary rows of the verify that just released the lock."""
        path = self.raw_path(cid, attempt)
        try:
            with path.open() as fh:
                raw = list(csv.DictReader(fh))
        except OSError:
            return
        done = set()
        summary = self.dir / "summary.csv"
        if summary.exists():
            with summary.open() as fh:
                done = {(r["cell"], r["attempt"], r["ts"]) for r in csv.DictReader(fh)}
        rows = [r for r in summarize(cid, attempt, raw, self.workspace.cell(cid).ledger_text())
                if (r["cell"], str(r["attempt"]), r["ts"]) not in done]
        self._append(summary, SUMMARY_FIELDS, rows)

    def _note(self, what, err):
        if what not in self._errors:
            self._errors.add(what)
            print(f"  [{datetime.now():%H:%M:%S}] hostload: {what} failed: {err}",
                  file=sys.stderr, flush=True)

    def loop(self):
        current, next_sample = None, 0.0
        while not self._stop.is_set():
            try:
                cid = self.holder()
            except Exception as e:          # the sampler must never take the run down
                self._note("lock probe", e)
                cid = ""
            if current and (not cid or cid != current[0]):
                try:
                    self.close_verify(*current)
                except Exception as e:
                    self._note("summary", e)
                current = None
            if cid:
                if current is None:
                    try:
                        current = (cid, self.attempt(cid))
                    except Exception as e:
                        self._note("attempt", e)
                        current = (cid, 0)
                if time.time() >= next_sample:
                    next_sample = time.time() + SAMPLE_S
                    try:
                        self._append(self.raw_path(*current), RAW_FIELDS, self.sample())
                    except Exception as e:
                        self._note("sample", e)
            self._stop.wait(PROBE_S)

    def start(self):
        t = threading.Thread(target=self.loop, daemon=True, name="hostload")
        t.start()
        return t

    def stop(self):
        self._stop.set()
