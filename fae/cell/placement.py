"""Which of Docker's CPUs each phase's containers run on: the verify's
(measured) and the agents' (authoring), as docker --cpuset-cpus sets.

The count is read from Docker at every placement, so a Docker restart with
another CPU count is followed at the next one. The verify gets the larger
half when the count is odd. The sets are written where every container start
reads them, CPUSET_MEASURED and CPUSET_AGENT; a value the experiment's config
names wins over them.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Placement:
    measured: str
    agent: str


def _cores(lo, hi):
    return str(lo) if lo == hi else f"{lo}-{hi}"


def split(n):
    """Docker's `n` CPUs: the first ceil(n/2) to the verify, the rest to the
    agents. One CPU is shared."""
    if n < 2:
        return Placement("0", "0")
    v = (n + 1) // 2
    return Placement(_cores(0, v - 1), _cores(v, n - 1))


def docker_ncpu():
    """Docker's CPU count, or None when the daemon does not answer."""
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.NCPU}}"],
                           capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def current():
    n = docker_ncpu()
    return split(n) if n else None


def apply(environ=None):
    """Reads Docker's CPU count and writes both sets into `environ` (this
    process's environment by default), which every container start of this
    process and of its children reads. Returns the placement, or None when
    Docker did not answer and the previous sets stay."""
    environ = os.environ if environ is None else environ
    p = current()
    if p is not None:
        environ["CPUSET_MEASURED"] = p.measured
        environ["CPUSET_AGENT"] = p.agent
    return p
