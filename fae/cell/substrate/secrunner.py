"""The judged program, run in a container of its own.

A verifier never runs the agent's program itself: it runs it here. The
verify container holds the rig (the repo and workspace mounts, the host's
Docker socket, the lock plane); the runner holds only what the program
needs:

- `/workspace`: a fresh copy of the artifacts (`fresh_copy`), never the
  judged tree;
- `/scratch`, when given: a writable directory for the program's own state
  (HOME, a Pulumi backend, temp files), inside the verify's own directory;
- the networks the verifier names, or none, and a stable name on them
  (`fae-secrun-<cid>`) that the verifier and the cell's substrate reach it by.

No other mount but the read-only extras a caller names, no Docker socket,
the operator's uid, every capability dropped and no privilege escalation,
with memory, pid and CPU ceilings.

One lifecycle for every program: `start`, then either `wait` for it to end
(a CLI, one test case with its input on stdin) or use it while it is
`alive` (a service), then `stop`, which keeps what it printed and removes
the container. `run` is start-wait-stop in one call.

`SecRunnerVariant` is the variant of an experiment whose program needs
nothing but a runtime image: it declares the image and the commands, and
the engine supplies the substrate checks and the runner.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..image import CONTAINER_USER, RUN_PREFIX

MEMORY = "2g"
PIDS = 512
NOFILE = 65536          # a service holds a store pool, a cache pool and hundreds of sockets
ATTACHED_GRACE_S = 2


def name_for(cid):
    return f"{RUN_PREFIX}{cid}"


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def fresh_copy(artifacts, out, name="run"):
    """`<out>/<name>`, a copy of `artifacts` rebuilt for every verify."""
    workdir = Path(out) / name
    if workdir.is_dir():
        shutil.rmtree(workdir)
    shutil.copytree(artifacts, workdir)
    return workdir


@dataclass
class SecRunner:
    image: str
    cid: str
    workdir: Path
    argv: tuple
    scratch: Path | None = None
    networks: tuple = ()
    env: dict = field(default_factory=dict)
    mounts: tuple = ()      # (host path, container path), read-only
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

    def create_argv(self, stdin=False):
        """The `docker create` of the program. Only the first network is given
        here; the others are connected before it starts. With `stdin` the
        container keeps stdin open for an attached start and closes it at EOF."""
        out = ["docker", "create", "--pull", "never", "--name", self.name,
               "--label", f"fae-cell={self.cid}",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--memory", self.memory, "--pids-limit", str(self.pids)]
        if stdin:
            out += ["-i", "-a", "stdin"]
        if self.cpus is not None:
            out += [f"--cpus={self.cpus}"]
        if self.cpuset:
            out += [f"--cpuset-cpus={self.cpuset}"]
        if self.nofile is not None:
            out += ["--ulimit", f"nofile={self.nofile}:{self.nofile}"]
        home = "/scratch" if self.scratch is not None else "/workspace"
        out += ["--network", self.networks[0] if self.networks else "none",
                "-v", f"{self.workdir}:/workspace"]
        if self.scratch is not None:
            out += ["-v", f"{self.scratch}:/scratch"]
        for host, inner in self.mounts:
            out += ["-v", f"{host}:{inner}:ro"]
        out += ["-w", self.cwd, "-e", f"HOME={home}",
                # the operator's uid has no passwd entry in the image; a tool
                # that asks who it runs as (Go's user.Current) reads USER
                "-e", f"USER={CONTAINER_USER}"]
        for k, v in sorted(self.env.items()):
            out += ["-e", f"{k}={v}"]
        return out + [self.image, *self.argv]

    def start(self, stdin=None, timeout_s=None):
        """Start it; returns None, or the reason it could not start. With
        `stdin` the program is fed it and this returns when the program has
        ended (or after `timeout_s`, the program then still running)."""
        if self.scratch is not None:
            Path(self.scratch).mkdir(parents=True, exist_ok=True)
        _docker("rm", "-f", self.name)
        try:
            p = subprocess.run(self.create_argv(stdin is not None),
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"docker create: {e}"
        if p.returncode != 0:
            return f"docker create exited {p.returncode}: {p.stderr.strip()[:300]}"
        for net in self.networks[1:]:
            c = _docker("network", "connect", net, self.name)
            if c.returncode != 0:
                return f"network connect {net}: {c.stderr.strip()[:300]}"
        if stdin is None:
            s = _docker("start", self.name, timeout=120)
            return None if s.returncode == 0 else \
                f"docker start exited {s.returncode}: {s.stderr.strip()[:300]}"
        try:
            subprocess.run(["docker", "start", "-a", "-i", self.name], input=stdin,
                           capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            pass
        except OSError as e:
            return f"docker start: {e}"
        return None

    def wait(self, timeout_s):
        """Its exit code once it has ended; None when it runs past `timeout_s`."""
        try:
            p = subprocess.run(["docker", "wait", self.name], capture_output=True,
                               text=True, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = p.stdout.strip()
        return int(out) if p.returncode == 0 and out.lstrip("-").isdigit() else None

    def alive(self):
        try:
            p = _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return p.returncode == 0 and p.stdout.strip() == "true"

    def output(self, split=False):
        """What the program printed so far: stdout and stderr in order, or
        with `split` the pair (stdout, stderr). None when it cannot be read."""
        try:
            p = subprocess.run(["docker", "logs", self.name], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE if split else subprocess.STDOUT,
                               text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0:
            return None
        return (p.stdout, p.stderr) if split else p.stdout

    def dump_logs(self):
        """What the program printed so far, into `log` (replaced)."""
        if self.log is None:
            return
        out = self.output()
        if out is not None:
            Path(self.log).write_text(out)

    def run(self, timeout_s, stdin=None, split=False):
        """Start it, let it end, stop it: (exit code, what it printed), or with
        `split` (exit code, stdout, stderr). The code is None when it never
        started or ran past `timeout_s`; the output is then the reason or what
        it printed by then. The container is removed either way."""
        why = self.start(stdin=stdin, timeout_s=timeout_s)
        if why:
            return (None, "", why) if split else (None, why)
        # an attached start has already waited for the program
        rc = self.wait(timeout_s if stdin is None else ATTACHED_GRACE_S)
        out = self.output(split=split)
        self.stop()
        if split:
            stdout, stderr = out or ("", "")
            return rc, stdout, (stderr if rc is not None else f"timed out after {timeout_s}s")
        return rc, out or ""

    def stop(self):
        """Keep its logs, then remove it. Safe to call when it never started."""
        self.dump_logs()
        try:
            _docker("rm", "-f", self.name)
        except (OSError, subprocess.TimeoutExpired):
            pass


# --- the variant whose program needs only a runtime image -----------------

from ..variants.base import Variant, daemon_answers  # noqa: E402

RUN_MEMORY = "256m"
RUN_PIDS = 128


class SecRunnerVariant(Variant):
    """A program the verifier builds and runs in a container of the variant's
    runtime image, with no network and one directory: the verifier's copy of
    the artifacts. The runtime is a pinned registry tag (IMAGE, pulled by the
    operator) or a Dockerfile directory the engine builds and tags by content
    (RUNTIME_DIR). A subclass declares ARM, TECH, CONDITIONS,
    AUTHORING_SURFACE, the runtime, RUN and optionally BUILD."""

    IMAGE = ""          # a pinned registry tag the program runs in, or
    RUNTIME_DIR = None  # the Dockerfile directory the engine builds it from
    BUILD = None        # argv inside the container, or None
    RUN = ()            # argv inside the container

    @classmethod
    def runtime_name(cls):
        from .. import experiment as _experiment
        return f"{_experiment.current().name}-{cls.TECH}-runtime"

    @classmethod
    def runtime_image(cls):
        """The tag the program runs in. A built runtime's tag is its content
        hash, so it resolves without a daemon."""
        from .. import image as _image
        if cls.RUNTIME_DIR:
            return _image.tag(cls.runtime_name(), cls.RUNTIME_DIR)
        return cls.IMAGE

    @classmethod
    def runner(cls, cid, workdir, argv):
        return SecRunner(image=cls.runtime_image(), cid=cid, workdir=Path(workdir),
                         argv=tuple(argv), memory=RUN_MEMORY, pids=RUN_PIDS, cpus=1,
                         nofile=None)

    @classmethod
    def run(cls, cid, workdir, argv, stdin, timeout_s):
        """(stdout, stderr, exit code, error) of `argv` in the runtime; `error`
        names what kept it from running to its end (it never started, or
        it timed out), and the exit code is then None."""
        rc, out, err = cls.runner(cid, workdir, argv).run(timeout_s, stdin=stdin, split=True)
        return (out, err, rc, None) if rc is not None else ("", "", None, err)

    def substrate_alive(self):
        return daemon_answers(["docker", "version"])

    def substrate_ok(self):
        from .. import image as _image
        if subprocess.run(["docker", "info"], capture_output=True).returncode:
            self.log("HALT[substrate]: docker unreachable")
            return False
        if self.RUNTIME_DIR:
            try:
                _image.ensure(self.runtime_name(), self.RUNTIME_DIR, log=self.log)
            except RuntimeError as e:
                self.log(f"HALT[substrate]: {e}")
                return False
        elif subprocess.run(["docker", "image", "inspect", self.IMAGE],
                            capture_output=True).returncode:
            self.log(f"HALT[substrate]: image {self.IMAGE} not present "
                     f"(docker pull {self.IMAGE})")
            return False
        return True
