"""The run's regime log, `.conduct/regimes.jsonl`: one JSON line per change in
the conditions cells are measured under — a run started (its caps, Docker's
CPUs and their split, the engine's and the experiment's commits), a drain
begun, a drain complete — so an analysis splits its cells by regime from one
file instead of reconstructing it from the narration."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fae.cell import placement as _placement

FILE = "regimes.jsonl"


def _head(path):
    """The short commit checked out at `path`, or None."""
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None


def record(conduct_dir, event, **fields):
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    d = Path(conduct_dir)
    d.mkdir(parents=True, exist_ok=True)
    with (d / FILE).open("a") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")
    return line


def run_started(conduct_dir, *, cap, per_agent, overrides, excluded, engine_dir,
                experiment_root):
    n = _placement.docker_ncpu()
    p = _placement.split(n) if n else None
    return record(conduct_dir, "run-start", cap=cap, per_agent=per_agent,
                  overrides=dict(overrides or {}), excluded=sorted(excluded or ()),
                  docker_cpus=n, cpus_measured=p.measured if p else None,
                  cpus_agent=p.agent if p else None,
                  engine=_head(engine_dir), experiment=_head(experiment_root))


def read(conduct_dir):
    p = Path(conduct_dir) / FILE
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
