"""The Docker daemon a verify runs on, and how its files get there.

`VERIFY_DOCKER_HOST` names it as Docker does (`ssh://user@host`,
`tcp://...`), or several, one verify on each at a time (`VerifyHost.pool`);
empty is the daemon the authoring runs on, which shares this
host's filesystem. Any other daemon is a host of its own: the paths a verify
reads are copied to it at the same absolute paths before the verify, and its
output directory is copied back after, so the ctx, the mounts and the
evidence mean the same on both sides.

A verify host serves verifies only: its cores are not split with agents, so
nothing the verify starts is pinned there.

`VERIFY_HOST_UP` / `VERIFY_HOST_DOWN` are shell commands that start and stop
the machine behind a remote daemon (a cloud VM): up when a run starts or the
daemon does not answer before a verify, down when the run stops.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

RSYNC_EXCLUDES = (".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv",
                  "*.pyc")


class VerifyHost:
    def __init__(self, docker_host="", up_cmd="", down_cmd=""):
        self.docker_host = (docker_host or "").strip()
        self.up_cmd = (up_cmd or "").strip()
        self.down_cmd = (down_cmd or "").strip()
        self._identity = None

    @classmethod
    def pool(cls, conf):
        """Every verify host the config names, in order: VERIFY_DOCKER_HOST
        lists one or more daemons, comma- or space-separated; the local
        daemon alone when it names none. Each serves one verify at a time
        (Cell.verify_lock_acquire). VERIFY_HOST_UP/DOWN cover them all."""
        get = (lambda k: conf.get(k) or "") if conf is not None else (lambda k: os.environ.get(k, ""))
        names = [n for n in re.split(r"[,\s]+", get("VERIFY_DOCKER_HOST")) if n]
        return [cls(n, get("VERIFY_HOST_UP"), get("VERIFY_HOST_DOWN")) for n in names or [""]]

    @classmethod
    def from_conf(cls, conf):
        """The one host a verify runs on. A config that lists several names
        none of them: the cell's verify lock picks one and narrows the
        cell's config to it while held."""
        hosts = cls.pool(conf)
        if len(hosts) > 1:
            raise ValueError(f"VERIFY_DOCKER_HOST lists {len(hosts)} hosts; a verify runs on "
                             f"the one its verify lock names")
        return hosts[0]

    @property
    def remote(self):
        return bool(self.docker_host)

    @property
    def ssh_target(self):
        """`user@host` of an ssh:// daemon, for rsync and plain ssh; None for
        any other scheme (no file copy is possible through it)."""
        u = urlparse(self.docker_host)
        if u.scheme != "ssh" or not u.hostname:
            return None
        return f"{u.username}@{u.hostname}" if u.username else u.hostname

    def env(self, base=None):
        """The environment a host-side docker call of the verify runs with."""
        env = dict(os.environ if base is None else base)
        if self.remote:
            env["DOCKER_HOST"] = self.docker_host
        else:
            env.pop("DOCKER_HOST", None)
        return env

    def docker(self, *args, timeout=120, **kw):
        return subprocess.run(["docker", *args], env=self.env(), capture_output=True,
                              text=True, timeout=timeout, **kw)

    def answers(self):
        try:
            return self.docker("version", "--format", "{{.Server.Version}}", timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def ensure_up(self, log=print, wait_s=600):
        """The daemon answers, starting its machine with VERIFY_HOST_UP when
        it does not. False when it still does not answer after `wait_s`."""
        if self.answers():
            return True
        if not self.up_cmd:
            return False
        log(f"verify host: {self.docker_host} does not answer; running VERIFY_HOST_UP")
        run_command(self.up_cmd, log)
        self._identity = None
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.answers():
                log(f"verify host: {self.docker_host} answers")
                return True
            time.sleep(10)
        return False

    def down(self, log=print):
        if self.down_cmd:
            log("verify host: running VERIFY_HOST_DOWN")
            run_command(self.down_cmd, log)

    def identity(self):
        """(uid, gid, socket gid) a container on this daemon runs with, so
        what it writes in the copied tree is the copying user's; None on the
        local daemon (image.run_argv's own)."""
        if not self.remote:
            return None
        if self._identity is None:
            target = self.ssh_target
            if not target:
                raise RuntimeError(f"VERIFY_DOCKER_HOST {self.docker_host}: only ssh:// "
                                   f"daemons can be a remote verify host")
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", target,
                                "id -u; id -g; stat -c %g /var/run/docker.sock"],
                               capture_output=True, text=True, timeout=60)
            parts = r.stdout.split()
            if r.returncode or len(parts) != 3:
                raise RuntimeError(f"verify host {target}: identity unreadable: "
                                   f"{(r.stderr or r.stdout).strip()[-300:]}")
            self._identity = tuple(parts)
        return self._identity

    def push(self, paths, log=print):
        """Copy each path to the same absolute path on the verify host,
        replacing what is there (a stale file must not survive)."""
        if not self.remote:
            return
        target = self.ssh_target
        for p in sorted({str(Path(p)) for p in paths if p and Path(p).exists()}):
            src = p + "/" if Path(p).is_dir() else p
            dest = p + "/" if Path(p).is_dir() else p
            _rsync([src, f"{target}:{dest}"], log, delete=Path(p).is_dir())

    def pull(self, path, log=print, keep=("verifier.log",)):
        """Copy the verify host's copy of `path` back over the local one;
        `keep` names files this host writes there itself (the docker client's
        log), which the copy neither overwrites nor deletes."""
        if not self.remote:
            return
        p = str(Path(path))
        _rsync([f"{self.ssh_target}:{p}/", p + "/"], log, delete=True, keep=keep)

    def network_up(self, name, cid):
        if self.docker("network", "inspect", name).returncode:
            r = self.docker("network", "create", "--label", f"fae-cell={cid}", name)
            if r.returncode and "already exists" not in r.stderr:
                raise RuntimeError(f"could not create {name} on {self.docker_host or 'the local daemon'}: "
                                   f"{r.stderr.strip()}")


class VerifyHostProbe:
    """What one verify host can do, in the numbers the experiment's verdicts
    depend on (a store's throughput, a baseline latency). The experiment
    declares its subclass with `verify_host_probe()`; a host whose numbers
    differ judges the same build differently."""

    def measure(self, host, conf, log=print):
        """{name: number} measured on `host` (a VerifyHost) now, with the
        experiment's config `conf`. Raises when the host cannot be measured:
        a number that was never taken is not a slow host."""
        raise NotImplementedError


def _rsync(args, log, delete=False, keep=()):
    argv = ["rsync", "-a", "--mkpath", "-e", "ssh -o BatchMode=yes"]
    if delete:
        argv.append("--delete")
    for x in (*RSYNC_EXCLUDES, *(f"/{k}" for k in keep)):
        argv += ["--exclude", x]
    r = subprocess.run(argv + args, capture_output=True, text=True, timeout=1800)
    if r.returncode not in (0, 24):     # 24: a source file vanished mid-copy
        raise RuntimeError(f"rsync {' '.join(args)} failed ({r.returncode}): "
                           f"{(r.stderr or r.stdout).strip()[-500:]}")


def run_command(cmd, log=print, timeout=900):
    r = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    log(f"verify host: `{cmd}` exited {r.returncode}" + (": " + " | ".join(tail) if tail else ""))
    return r.returncode
