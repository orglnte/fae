"""The judged program, served from a container of its own.

A verifier that has to measure a running program (an HTTP service under
load) starts it here instead of as a child of the verify container. The
verify container holds the rig: the repo and workspace mounts, the host's
Docker socket, the lock plane. The runner holds only what the program needs
to run:

- `/workspace`: a fresh copy of the artifacts (`sandbox.fresh_copy`), never
  the judged tree;
- `/scratch`: a writable directory for the program's own state (HOME, a
  Pulumi backend, temp files), inside the verify's own output directory;
- the networks the verifier names, and a stable name on them
  (`fae-secrun-<cid>`) that the verifier and the cell's substrate reach it by.

No other mount, no Docker socket, the operator's uid, every capability
dropped and no privilege escalation, with memory, pid and CPU ceilings. What
the program prints goes to the verifier's log for it, on `stop` or on
`dump_logs`.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..image import CONTAINER_USER, RUN_PREFIX

MEMORY = "2g"
PIDS = 512
NOFILE = 65536          # a service holds a store pool, a cache pool and hundreds of sockets


def name_for(cid):
    return f"{RUN_PREFIX}{cid}"


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@dataclass
class SecRunner:
    image: str
    cid: str
    workdir: Path
    scratch: Path
    argv: tuple
    networks: tuple = ()
    env: dict = field(default_factory=dict)
    log: Path | None = None
    memory: str = MEMORY
    pids: int = PIDS
    cpus: float | None = None
    nofile: int | None = NOFILE
    cpuset: str | None = None
    cwd: str = "/workspace"

    @property
    def name(self):
        return name_for(self.cid)

    def run_argv(self):
        """The `docker run -d` that starts the program. Only the first network
        is given here; the others are connected after it starts."""
        out = ["docker", "run", "-d", "--name", self.name,
               "--label", f"fae-cell={self.cid}",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--memory", self.memory, "--pids-limit", str(self.pids)]
        if self.cpus is not None:
            out += [f"--cpus={self.cpus}"]
        if self.cpuset:
            out += [f"--cpuset-cpus={self.cpuset}"]
        if self.nofile is not None:
            out += ["--ulimit", f"nofile={self.nofile}:{self.nofile}"]
        out += ["--network", self.networks[0] if self.networks else "none",
                "-v", f"{self.workdir}:/workspace", "-v", f"{self.scratch}:/scratch",
                "-w", self.cwd, "-e", "HOME=/scratch",
                # the operator's uid has no passwd entry in the image; a tool
                # that asks who it runs as (Go's user.Current) reads USER
                "-e", f"USER={CONTAINER_USER}"]
        for k, v in sorted(self.env.items()):
            out += ["-e", f"{k}={v}"]
        return out + [self.image, *self.argv]

    def start(self):
        """Start it; returns None, or the reason it could not start."""
        self.scratch.mkdir(parents=True, exist_ok=True)
        _docker("rm", "-f", self.name)
        try:
            p = subprocess.run(self.run_argv(), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"docker run: {e}"
        if p.returncode != 0:
            return f"docker run exited {p.returncode}: {p.stderr.strip()[:300]}"
        for net in self.networks[1:]:
            c = _docker("network", "connect", net, self.name)
            if c.returncode != 0:
                return f"network connect {net}: {c.stderr.strip()[:300]}"
        return None

    def alive(self):
        try:
            p = _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return p.returncode == 0 and p.stdout.strip() == "true"

    def output(self):
        """What the program printed so far, stdout and stderr in order."""
        try:
            p = subprocess.run(["docker", "logs", self.name], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    def dump_logs(self):
        """What the program printed so far, into `log` (replaced)."""
        if self.log is None:
            return
        out = self.output()
        if out is not None:
            Path(self.log).write_text(out)

    def run_to_end(self, timeout_s):
        """A program that ends rather than serves (a CLI): run it to its end
        and return (exit code, what it printed). The code is None when it
        never started or ran past `timeout_s`; the container is removed
        either way."""
        why = self.start()
        if why:
            return None, why
        rc = None
        try:
            p = subprocess.run(["docker", "wait", self.name], capture_output=True,
                               text=True, timeout=timeout_s)
            if p.returncode == 0 and p.stdout.strip().lstrip("-").isdigit():
                rc = int(p.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
        out = self.output() or ""
        self.stop()
        return rc, out

    def stop(self):
        """Keep its logs, then remove it. Safe to call when it never started."""
        self.dump_logs()
        try:
            _docker("rm", "-f", self.name)
        except (OSError, subprocess.TimeoutExpired):
            pass
