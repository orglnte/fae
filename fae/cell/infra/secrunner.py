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
  (`fae-secrun-<cid>`) that the verifier and the cell's infra reach it by.

No other mount but the read-only extras a caller names, no Docker socket,
the operator's uid, every capability dropped and no privilege escalation,
with memory, pid and CPU ceilings.

One lifecycle for every program: `start`, then either `wait` for it to end
(a CLI, one test case with its input on stdin) or use it while it is
`alive` (a service), then `stop`, which keeps what it printed and removes
the container. `run` is start-wait-stop in one call.

`for_variant` builds the runner of a variant's program from its file's
[verify.run].
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


def stop_by_name(cid, workdir, log=None):
    """Stop the cell's runner, addressed by its name alone — from any
    container of the verify, a fresh one after a kill included — keeping
    what it printed in `log`. Idempotent."""
    SecRunner(image="", cid=cid, workdir=Path(workdir), argv=(), log=log).stop()


# --- a variant's program, as its file declares it ([verify.run]) ---------------

RUN_MEMORY = "256m"     # a program that runs once per case
RUN_PIDS = 128


def run_image_name(variant_cls):
    """The tag name of an image built from the variant's [verify.run] image_dir."""
    from fae import experiment as _experiment
    from .. import image as _image
    return _image.dir_tag_name(_experiment.definition(), variant_cls.RUN["image_dir"])


def run_image(variant_cls):
    """The image the variant's program runs in: [verify.run] image (a pinned
    tag), the image built from its image_dir (tagged by content, so it
    resolves without a daemon), or the verify image this code runs in."""
    from .. import image as _image
    run = variant_cls.RUN
    if run.get("image_dir"):
        return _image.tag(run_image_name(variant_cls), run["image_dir"])
    if run.get("image"):
        return run["image"]
    image = os.environ.get("FAE_VERIFY_IMAGE")
    if not image:
        raise RuntimeError(f"{variant_cls.ID} runs in the verify image, and this is "
                           f"not a verify container (FAE_VERIFY_IMAGE unset)")
    return image


def ensure_run_image(variant_cls, log=print):
    """The variant's own run image present: built when it is a Dockerfile
    directory, required when it is a pinned tag."""
    from .. import image as _image
    run = variant_cls.RUN
    if run.get("image_dir"):
        return _image.ensure(run_image_name(variant_cls), run["image_dir"], log=log)
    if run.get("image") and not _image.present(run["image"]):
        raise RuntimeError(f"image {run['image']} not present (docker pull {run['image']})")
    return run.get("image")


def for_variant(variant_cls, cid, workdir, argv=None, env=None, networks=(),
                scratch=None, log=None, cpus=None, cpuset=None):
    """The runner of a variant's program, from its [verify.run]: `command`
    (or `argv`, e.g. its `build`) over `workdir`. A program that `serves`
    is kept running on the cell's network (and `networks`) with room for a
    service; any other runs once with no network and the tight caps."""
    run = variant_cls.RUN
    argv = tuple(argv if argv is not None else run.get("command") or ())
    if run.get("serves"):
        cell_net = os.environ.get("FAE_CELL_NET")
        if not cell_net:
            raise RuntimeError(f"{variant_cls.ID} serves on the cell network, and this is "
                               f"not a verify container (FAE_CELL_NET unset)")
        return SecRunner(image=run_image(variant_cls), cid=cid, workdir=Path(workdir),
                         argv=argv, scratch=scratch, networks=(cell_net, *networks),
                         env=dict(env or {}), log=log, cpus=cpus, cpuset=cpuset)
    return SecRunner(image=run_image(variant_cls), cid=cid, workdir=Path(workdir), argv=argv,
                     env=dict(env or {}), log=log, memory=RUN_MEMORY, pids=RUN_PIDS,
                     cpus=1 if cpus is None else cpus, nofile=None, cpuset=cpuset)
