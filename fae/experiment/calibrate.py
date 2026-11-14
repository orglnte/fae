"""`experiment calibrate --host H [--repeats N]`: measure one verify host and
write its section of fae_verify_timings.toml.

1. The host answers (VERIFY_HOST_UP when it does not) and is idle: its CPUs
   busy under IDLE_MAX, so the references measure the host, not a neighbour.
2. The experiment's probe (`verify_host_probe()`) measures it.
3. Every active variant's reference runs the whole gate on that host alone,
   `repeats` times; each green arrangement's numbers are the verifier's
   `calibration_values()`.
4. The verifier's CALIBRATION_GUARD, a run that must be refunded at a named
   stage, is refunded there.
5. The section is written: the config values the verifier names in
   CALIBRATION_KEYS, the probe's numbers, and per (variant, arrangement) the
   mean of each value (the verifier's; `arrangement_s`, its seconds from
   start to verdict; and on a remote host `host_busy_p95`, the 95th
   percentile of the host's CPU busy share while it ran) with its spread, measured from `repeats` >= 2 runs,
   else the verifier's `estimated_spread()`.

Nothing is written unless the host was idle, every reference was green and
the guard held.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fae.cell.cell import Cell
from fae.cell.verify_host import IDLE_SAMPLE_S, VerifyHost, busy_p95, cpu_busy  # noqa: F401
from fae.cell.verify_timings import (ARRANGEMENT_S, HOST_BUSY_P95, HostTimings, Reference,
                                     VerifyTimings, host_key)

IDLE_MAX = 0.10


class CalibrationError(RuntimeError):
    pass


def busy_fraction(host, sample_s=IDLE_SAMPLE_S):
    """How busy the host's CPUs are, 0..1, over `sample_s`: /proc/stat read
    twice over ssh for a remote host, the load average per CPU for this one."""
    if not host.remote:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    try:
        return host.busy_share(sample_s)
    except RuntimeError as e:
        raise CalibrationError(str(e)) from e


def arrangement_dirs(cell):
    """{arrangement: archived dir} of a cell's green verify runs."""
    return {label: d for label, _, end, d, _ in cell.archived_runs() if end == "green"}


def arrangement_seconds(cell):
    """{arrangement: seconds from its start to its verdict} of a cell's green
    runs: what one arrangement takes on the host it ran on."""
    out = {}
    for label, _, end, _, v in cell.archived_runs():
        try:
            t0, t1 = (datetime.strptime(v[k], "%Y-%m-%dT%H:%M:%SZ") for k in ("started", "ended"))
        except (KeyError, TypeError, ValueError):
            continue
        if end == "green":
            out[label] = (t1 - t0).total_seconds()
    return out


def aggregate(runs, estimated_spread):
    """[{name: value}, ...] of one (variant, arrangement) over its runs ->
    (values, spreads, spread kind, n). Only the names every run measured."""
    n = len(runs)
    names = sorted(set.intersection(*(set(r) for r in runs))) if runs else []
    values = {k: statistics.fmean(r[k] for r in runs) for k in names}
    if n >= 2:
        return values, {k: statistics.stdev(r[k] for r in runs) for k in names}, "measured", n
    return values, {k: float(estimated_spread(k, values[k])) for k in names}, "estimated", n


def commit_of(path):
    """`git rev-parse --short HEAD` of the tree holding `path`, `-dirty` when it
    has uncommitted changes; `unknown` outside git."""
    def git(*a):
        return subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True)
    head = git("rev-parse", "--short", "HEAD")
    if head.returncode:
        return "unknown"
    return head.stdout.strip() + ("-dirty" if git("status", "--porcelain").stdout.strip() else "")


def resolve_host(conf, name):
    """The pool's host named `name` (its VERIFY_DOCKER_HOST entry, `local`
    for the local daemon)."""
    pool = VerifyHost.pool(conf)
    for h in pool:
        if host_key(h) == name:
            return h
    raise CalibrationError(f"{name!r} is not a verify host of this config; "
                           f"VERIFY_DOCKER_HOST names: {', '.join(host_key(h) for h in pool)}")


def calibrate(exp, name, repeats=1, log=None, idle_max=IDLE_MAX):
    """Measure host `name` and write its section; the written HostTimings.
    `log` defaults to a print that flushes, so a redirected log is current."""
    log = log or (lambda m: print(m, flush=True))
    if repeats < 1:
        raise CalibrationError("--repeats is at least 1")
    conf = exp.config()
    host = resolve_host(conf, name)
    d = exp.definition
    verifier = d.verifier_class()
    if not host.ensure_up(log=log):
        raise CalibrationError(f"{name}: the daemon does not answer")
    busy = busy_fraction(host)
    log(f"calibrate: {name} busy {busy:.0%} (max {idle_max:.0%})")
    if busy >= idle_max:
        raise CalibrationError(f"{name} is busy ({busy:.0%} >= {idle_max:.0%}): calibrate "
                               f"an idle host, with nothing else running on it")
    probe = d.verify_host_probe()().measure(host, conf, log=log)
    log(f"calibrate: probe {json.dumps(probe)}")

    pin = {"VERIFY_DOCKER_HOST": host.docker_host}
    runs = {}
    for r in range(1, repeats + 1):
        for vid, green, verdict, cell in exp.smoke_cells(list(d.active), rep=r, full_gate=True,
                                                         env=pin):
            if not green:
                raise CalibrationError(f"reference {vid} (run {r}) not green: {verdict}")
            dirs, took = arrangement_dirs(cell), arrangement_seconds(cell)
            labels = [a or "seed" for a in d.gate.arrangements]
            missing = [a for a in labels if a not in dirs]
            if missing:
                raise CalibrationError(f"reference {vid} (run {r}): no green {missing}")
            for label in labels:
                values = dict(verifier.calibration_values(dirs[label]))
                if label in took:
                    values[ARRANGEMENT_S] = took[label]
                p95 = busy_p95(Path(dirs[label]) / Cell.HOST_LOAD)
                if p95 is not None:
                    values[HOST_BUSY_P95] = p95
                runs.setdefault((vid, label), []).append(values)
    _guard(exp, verifier, d, pin, repeats + 1, log)

    refs = []
    for (vid, label), measured in sorted(runs.items()):
        values, spreads, kind, n = aggregate(measured, verifier.estimated_spread)
        refs.append(Reference(vid, label, values=values, spreads=spreads, spread=kind, n=n))
    import fae
    section = HostTimings(
        host=name, date=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        fae_commit=commit_of(Path(fae.__file__).resolve().parents[1]),
        experiment_commit=commit_of(exp.root),
        load={k: str(conf.get(k)) for k in verifier.CALIBRATION_KEYS if conf.get(k) is not None},
        probe=probe, references=tuple(refs))
    VerifyTimings.read(exp.root).with_host(section).write(exp.root)
    log(f"calibrate: wrote {name} ({len(refs)} references) to {exp.root}/fae_verify_timings.toml")
    return section


def _guard(exp, verifier, d, pin, rep, log):
    g = verifier.CALIBRATION_GUARD
    if not g:
        return
    vid = g.get("variant") or d.active[0]
    (_, _, verdict, cell), = exp.smoke_cells([vid], rep=rep, full_gate=False,
                                             env={**pin, **{k: str(v) for k, v in g["env"].items()}})
    got = [(end, v.get("stage")) for _, _, end, _, v in cell.archived_runs()] if cell else []
    ok = ("refunded", g["stage"]) in got
    log(f"calibrate: guard {vid} {g['env']} -> {got}")
    if not ok:
        raise CalibrationError(f"guard {vid} {g['env']} was not refunded at stage "
                               f"{g['stage']!r}: {verdict}")
