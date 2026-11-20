#!/usr/bin/env python3
"""THE verify boundary — the engine's side of one arrangement.

The engine judges nothing itself. It hands the experiment's verifier one
`Ctx` (the workspace, the arrangement, the variant, the fingerprint it pinned
at cell start), runs it OUT OF PROCESS, and reads back one `Verdict`:
ok or not, at which stage, whether the attempt is charged (a rig fault
refunds it), whether the cell must stand down for the operator, the metrics
to persist, the files to archive.

Out of process AND out of the host: the child is `python3 -m
fae.cell.verify_child --ctx F --out F` inside a container of the variant's
image (`fae/cell/image.py`: a Dockerfile the experiment declares, tagged
by its content, every tool pinned), named `fae-verify-<cid>`, on the
cell's network, with the roots it needs mounted at their own paths and the
daemon's socket for what it provisions. Nothing of the verify — not the
law, not the judged program, not a load tool — runs on the host, so every
host judges in the same environment. The driver holds its slots, the
verify lock and the verifier's exclusive lock on open fds; the container
inherits none, and a verifier exception, hang or crash cannot touch the
cell loop: the container is removed when the child returns or times out,
and everything it started goes with it.

What a verifier owes the rig, `verify(ctx)` being the whole interface:
the program under judgment NEVER EXECUTES ON THE HOST, and neither does
the verifier. It builds and runs the program inside the variant's
infra — a container of the variant's runtime image, the cell's dind
daemon, its kind cluster — over its own copy of the artifacts and with no
network the experiment did not declare, so a judged program cannot reach
the rig, the locks, other cells or the operator's machine.

The instrument loader below (`call`, `run_in_thread`) is engine code the
verifiers import: every instrument is Python, so it runs as a function call
in the verifier's process rather than as a spawned interpreter.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from fae.experiment import config as _config

import fae.experiment  # noqa: E402
from fae import paths as _paths  # noqa: E402

HARNESS = _paths.ENGINE
ROOT = _paths.ROOT
INSTRUMENTS = ROOT / _config.DEFAULT_EXPERIMENT_DIR / "instruments"
# The engine's own instruments: measurements every verifier gets, whatever it judges.
ENGINE_INSTRUMENTS = Path(__file__).resolve().parent / "instruments"


# --- the measurement instruments, run in this process --------------------
# Every instrument in instruments/ is Python, and the cell that drives them is
# Python too, so they run as function calls in the cell's own process rather
# than as spawned `python3 instruments/<x>.py` children. k6, docker and kubectl
# are programs in their own right and stay children; the bring-up is shell by
# design. THE INSTRUMENTS THEMSELVES ARE NOT MODIFIED: call() adapts to their
# main() by patching sys.argv/env for the duration of the call, exactly as an
# interpreter start would, and puts them back afterwards.

_inst_cache = {}
_inst_lock = threading.Lock()


def _load_instrument(name, instruments=None):
    """Load <instruments>/<name>.py once, by path, because the instruments dir
    is a directory of tools, not a package. Cached: the import cost is paid once per cell."""
    base = Path(instruments) if instruments else INSTRUMENTS
    key = (str(base), name)
    with _inst_lock:
        if key not in _inst_cache:
            path = base / f"{name}.py"
            if not path.is_file():
                raise FileNotFoundError(f"no instrument at {path}")
            spec = importlib.util.spec_from_file_location(f"_inst_{name}", path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _inst_cache[key] = m
        return _inst_cache[key]


@contextlib.contextmanager
def _as_script(argv, env):
    """Present the process to the instrument as an interpreter start would, but
    patch ONLY what it cannot receive directly, and restore on every path — a
    leaked argv would make the NEXT instrument read the previous one's args."""
    old_argv, old_env = sys.argv, dict(os.environ)
    if argv is not None:
        sys.argv = list(argv)
    try:
        if env:
            os.environ.update({k: str(v) for k, v in env.items()})
        yield
    finally:
        if argv is not None:
            sys.argv = old_argv
        if env:
            os.environ.clear()
            os.environ.update(old_env)


def call(name, args=(), env=None, capture=True, out=None, instruments=None):
    """Run an instrument's main() in this process. Returns (rc, stdout).

    rc mirrors what a subprocess returned: 0 on a clean return, the code from a
    SystemExit, 1 from an uncaught exception (whose traceback goes to stderr,
    where a crashed child's would have). `capture=False` leaves stdout alone —
    trace_sidecar streams a live per-second table an operator watches."""
    base = Path(instruments) if instruments else INSTRUMENTS
    m = _load_instrument(name, base)
    argv = [str(base / f"{name}.py"), *[str(a) for a in args]]
    buf = io.StringIO()
    rc = 0

    # Both shapes exist: some instruments read sys.argv and take nothing, others
    # accept argv explicitly (the shape that is safe to run concurrently).
    import inspect
    try:
        takes_argv = bool(inspect.signature(m.main).parameters)
    except (TypeError, ValueError):
        takes_argv = False

    def invoke():
        return m.main(argv) if takes_argv else m.main()

    try:
        with _as_script(None if takes_argv else argv, env):
            if capture:
                with contextlib.redirect_stdout(buf):
                    ret = invoke()
            else:
                ret = invoke()
            if isinstance(ret, int):
                rc = ret
    except SystemExit as e:
        rc = 0 if e.code in (None, 0) else (e.code if isinstance(e.code, int) else 1)
    except Exception:                        # noqa: BLE001 — mirrors a crashed child
        traceback.print_exc(file=sys.stderr)
        rc = 1
    text = buf.getvalue()
    if out is not None:
        out.write(text)
    return rc, text.strip()


def run_in_thread(name, args=(), capture=False, instruments=None):
    """Run an instrument concurrently, in a THREAD rather than a child process.

    Only the resource sampler needs this: it measures the footprint across the
    same window the load runs in, so it has to overlap the trace sidecar. It is
    IO-bound (shells out to docker/kubectl and waits), so a thread is the right
    shape and keeps one process per cell. Takes NO env: os.environ is
    process-global, and a per-thread patch interleaved with the main thread's
    would resurrect the other context's variables. Returns a handle with
    .join(timeout)/.rc/.alive — enough of Popen's surface for the call site."""
    holder = {}

    def _run():
        holder["rc"], holder["out"] = call(name, args, capture=capture,
                                           instruments=instruments)

    t = threading.Thread(target=_run, name=f"instrument:{name}", daemon=True)
    t.start()

    class Handle:
        def join(self, timeout=None):
            t.join(timeout)
            return holder.get("rc")

        @property
        def rc(self):
            return holder.get("rc")

        @property
        def alive(self):
            return t.is_alive()

    return Handle()



def now():
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


_now = now


def _mutex_module():
    """THE filesystem mutex (fae/mutex.py) — one implementation for every holder."""
    from fae import mutex
    return mutex


# --- the boundary --------------------------------------------------------------

class Fixture:
    """Handle to a detached auxiliary container started via `ctx.start_fixture`.
    Tagged `fae-cell=<cid>` and `fae-role=fixture` so crash teardown and the
    reaper can clean it up without touching cell-lifetime infra."""

    def __init__(self, name, cid):
        self.name = name
        self.cid = cid

    def alive(self):
        try:
            p = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.name],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return p.returncode == 0 and p.stdout.strip() == "true"

    def exec(self, *argv, timeout=60, env=None, user=None, input=None):
        cmd = ["docker", "exec"]
        if user:
            cmd += ["-u", str(user)]
        for k, v in sorted((env or {}).items()):
            cmd += ["-e", f"{k}={v}"]
        if input is not None:
            cmd.append("-i")
        cmd += [self.name, *argv]
        return subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)

    def logs(self, tail=None, timeout=30):
        cmd = ["docker", "logs"]
        if tail is not None:
            cmd += ["--tail", str(tail)]
        cmd.append(self.name)
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""

    def stop(self):
        try:
            subprocess.run(["docker", "rm", "-f", "-v", self.name],
                           capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass


def stop_fixtures_for(cid):
    """Best-effort removal of every `fae-role=fixture` container owned by `cid`."""
    try:
        p = subprocess.run(
            ["docker", "ps", "-aq",
             "--filter", f"label=fae-cell={cid}",
             "--filter", "label=fae-role=fixture"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    ids = p.stdout.split()
    if ids:
        try:
            subprocess.run(["docker", "rm", "-f", "-v", *ids],
                           capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass


class RunnerHelper:
    """Bound `secrunner` facade on `ctx.runner` so verifiers run judged code
    without importing `fae.cell.infra.secrunner` or resolving the variant."""

    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def name(self):
        from .infra import secrunner
        return secrunner.name_for(self._ctx.cid)

    def fresh_copy(self, name="run"):
        from .infra import secrunner
        return secrunner.fresh_copy(Path(self._ctx.artifacts), Path(self._ctx.out), name=name)

    def for_variant(self, workdir, *, argv=None, env=None, networks=(),
                    scratch=None, log=None, cpus=None, cpuset=None, variant=None):
        from .infra import secrunner
        vcls = variant if variant is not None else self._ctx.variant_def
        if vcls is None:
            raise RuntimeError(f"ctx.runner: no variant definition bound for {self._ctx.variant!r}")
        return secrunner.for_variant(vcls, self._ctx.cid, workdir, argv=argv, env=env,
                                     networks=networks, scratch=scratch, log=log,
                                     cpus=cpus, cpuset=cpuset)

    def run(self, workdir, timeout_s, *, argv=None, stdin=None, split=False, **kw):
        return self.for_variant(workdir, argv=argv, **kw).run(timeout_s, stdin=stdin, split=split)

    def stop(self, log=None):
        from .infra import secrunner
        secrunner.stop_by_name(self._ctx.cid, self._ctx.out, log=log)


_CTX_WIRE_FIELDS = ("root", "experiment_dir", "workspace", "artifacts", "out",
                    "cid", "task", "variant", "arrangement", "expected_fp", "mode")


@dataclass
class Ctx:
    """What the verifier is handed: everything about the run, nothing about
    the rig's locks. Runtime helpers (`variant_def`, `conf`, `infra`,
    `substrate`, `runner`, `verify_container`, `cell_network`) are bound
    inside the verify child and excluded from JSON serialization."""

    root: str
    experiment_dir: str
    workspace: str
    artifacts: str
    out: str
    cid: str
    task: str
    variant: str
    arrangement: str | None = None
    expected_fp: str | None = None
    mode: str = "cell"
    # Runtime-only bindings attached by verify.main() inside the child container
    variant_def: object = field(default=None, repr=False, compare=False)
    conf: object = field(default=None, repr=False, compare=False)
    infra: object = field(default=None, repr=False, compare=False)
    substrate: object = field(default=None, repr=False, compare=False)
    runner: object = field(default=None, repr=False, compare=False)
    verify_container: str = field(default="", repr=False, compare=False)
    cell_network: str = field(default="", repr=False, compare=False)
    _fixtures: list = field(default_factory=list, init=False, repr=False, compare=False)

    def _fixture_argv(self, image, *, name=None, argv=(), detach=False, env=None,
                      network=None, mounts=(), ports=(), cpus=None, cpuset=None,
                      memory=None, labels=(), extra=()):
        cmd = ["docker", "run", "-d" if detach else "--rm"]
        if name:
            cmd += ["--name", name]
        cmd += ["--label", f"fae-cell={self.cid}", "--label", "fae-role=fixture"]
        for k, v in labels:
            cmd += ["--label", f"{k}={v}"]
        net = self.cell_network or os.environ.get("FAE_CELL_NET") if network is None else network
        if net:
            cmd += ["--network", net]
        if cpus is not None:
            cmd += [f"--cpus={cpus}"]
        if cpuset:
            cmd += [f"--cpuset-cpus={cpuset}"]
        if memory:
            cmd += ["--memory", str(memory)]
        for spec in ports:
            cmd += ["-p", str(spec)]
        for m in mounts:
            if isinstance(m, tuple):
                mode = m[2] if len(m) == 3 else "ro"
                cmd += ["-v", f"{m[0]}:{m[1]}:{mode}"]
            else:
                cmd += ["-v", str(m)]
        for k, v in sorted((env or {}).items()):
            cmd += ["-e", f"{k}={v}"]
        cmd += list(extra)
        return cmd + [image, *[str(a) for a in argv]]

    def start_fixture(self, name, image, *, argv=(), env=None, network=None,
                      mounts=(), ports=(), cpus=None, cpuset=None, memory=None,
                      labels=(), extra=(), timeout=120):
        """Start a detached auxiliary container tagged `fae-cell=<cid>` and
        `fae-role=fixture`, tracked for automatic cleanup on verifier exit."""
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, text=True, timeout=60)
        cmd = self._fixture_argv(image, name=name, argv=argv, detach=True, env=env,
                                 network=network, mounts=mounts, ports=ports, cpus=cpus,
                                 cpuset=cpuset, memory=memory, labels=labels, extra=extra)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError(f"fixture {name} ({image}) failed to start: "
                               f"{(p.stderr or p.stdout).strip()[-300:]}")
        fx = Fixture(name, self.cid)
        self._fixtures.append(fx)
        return fx

    def run_fixture(self, image, *, argv=(), name=None, env=None, network=None,
                    mounts=(), cpus=None, cpuset=None, memory=None,
                    labels=(), extra=(), timeout=300):
        """Run a one-shot auxiliary container (`--rm`) tagged `fae-cell=<cid>`
        and `fae-role=fixture`; returns CompletedProcess."""
        cmd = self._fixture_argv(image, name=name, argv=argv, detach=False, env=env,
                                 network=network, mounts=mounts, cpus=cpus,
                                 cpuset=cpuset, memory=memory, labels=labels, extra=extra)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def stop_fixtures(self):
        """Stop every fixture started via `start_fixture` (LIFO, idempotent)."""
        while self._fixtures:
            self._fixtures.pop().stop()

    def to_json(self):
        return json.dumps({k: getattr(self, k) for k in _CTX_WIRE_FIELDS}, indent=2) + "\n"

    @classmethod
    def from_json(cls, text):
        raw = json.loads(text)
        return cls(**{k: raw[k] for k in _CTX_WIRE_FIELDS if k in raw})


@dataclass
class Verdict:
    """What the verifier hands back. `charge=False` refunds the attempt (a
    rig fault, not a build verdict); a non-empty `stand_down` pauses the cell
    for the operator; `metrics` is persisted as metrics.json; `files` are
    archived under arrangements/."""

    ok: bool
    stage: str = ""
    why: str = ""
    charge: bool = True
    stand_down: tuple = ()
    metrics: dict = field(default_factory=dict)
    arrangement: str | None = None
    seconds: float = 0.0
    files: tuple = ()

    def to_json(self):
        d = asdict(self)
        d["stand_down"], d["files"] = list(self.stand_down), list(self.files)
        return json.dumps(d, indent=2) + "\n"

    @classmethod
    def from_json(cls, text):
        d = json.loads(text)
        d["stand_down"], d["files"] = tuple(d.get("stand_down", ())), tuple(d.get("files", ()))
        return cls(**d)


class Verifier(ABC):
    """One experiment's verification. The definition's `verifier_class()`
    names the subclass; the engine instantiates it inside a container of
    the variant's image, hands it one Ctx and reads one Verdict from
    `verify`.

    What it owes: the program under judgment never executes on the host —
    build and run it inside the variant's infra (a container of the
    variant's runtime image, the cell's dind daemon, its kind cluster), over
    its own copy of the artifacts, with no network the experiment did not
    declare. This code itself runs in the verify container, never on the
    host: `IMAGE_DIR` holds the Dockerfile of the environment it needs (the
    engine's runtime, its load tools, the judged app's runtime), every
    version pinned; a Variant may layer its own on top. It takes no lock:
    the engine holds `EXCLUSIVE` on the cell's own fd around the run, and
    the container is removed when this returns."""

    IMAGE_DIR = None    # the directory holding the Dockerfile the verify runs in (required)
    CPUS = None         # a CPU cap on the verify container (docker --cpus); None: the host's
    EXCLUSIVE = None    # a lock name the engine holds around every run; None: none
    FILES = ()          # archived under arrangements/ when the Verdict names no files
    FEEDBACK_LOGS = ("verify.log", "deploy.log")   # copied to /feedback/ for the next attempt; a name may be a directory
    ROOT_READS = ()     # paths under the experiment root the verify reads, besides the root's files
    # Files every verify of this experiment must leave: always copied up, and
    # one missing after a verify that ran is a rig defect (Cell.verify).
    REQUIRED_OUTPUTS = ()
    # The stages at which the verifier never ran to its end (no image, a
    # crash, a timeout): its outputs are not expected there.
    NOT_RUN_STAGES = frozenset({"verifier", "verifier-timeout", "verifier-image", "verifier-host",
                                "host-busy"})
    PREFIXES = {}   # {kind: name prefix} of what a verify provisions, for the reaper
    RUN_LOG = "run.log"   # under the verify's out: what the variant's program printed
    # A charged fail at one of these stages is voided when the variant's
    # infra is found dead afterwards; None: any charged fail.
    MEASURED_STAGES = None
    # Calibration (`experiment calibrate`): the config keys a host is
    # calibrated with, recorded in its section of fae_verify_timings.toml.
    CALIBRATION_KEYS = ()
    # A negative control the calibration runs once per host: {"variant": id
    # or None for the first, "env": {key: value}, "stage": the stage its one
    # arrangement must be refunded at}. None: no control.
    CALIBRATION_GUARD = None

    @classmethod
    def calibration_values(cls, arrangement_dir):
        """{name: number} one reference arrangement measured, read from its
        archived files (`arrangements/NN-...`). Nothing by default."""
        return {}

    @classmethod
    def estimated_spread(cls, name, value):
        """The spread of `name` when only one run measured it (`value`)."""
        return 0.0

    @classmethod
    def identities(cls, cid):
        """[(kind, name)] this verifier provisions for a cell, named as it
        names them — what a reaper may look for after the cell is gone."""
        return []

    @classmethod
    def image_context(cls, conf):
        """[(host path, name)] copied beside the Dockerfile at build time
        (sources the image installs); hashed into the tag."""
        return []

    @abstractmethod
    def verify(self, ctx) -> Verdict:
        """One arrangement on `ctx`; every failure of the rig rather than
        of the build answers `charge=False`."""

    def teardown(self, ctx):
        """Remove what this verifier provisioned for `ctx` outside its own
        container (a store), derivable from `ctx` alone: it runs in a fresh
        container when a verify ended without finishing (killed, timed out,
        crashed). Idempotent. Nothing by default."""


CHILD_ARGV = ["python3", "-m", "fae.cell.verify_child"]
WORKDIR = ".verifier"
# The verify's own directory under the workspace: the only path its container
# writes. The host copies the verifier's declared outputs up into the
# workspace after the child exits (Cell.verify).
RUN_OUT = ".verify-out"
# A verify does not write the ledger: it records its events here and the host
# appends the ones it allows, after the child exits.
EVENTS = "ledger-events.tsv"
LEDGER_EVENTS = frozenset({"ALERT", "VERIFY_READY"})


def record_event(out, event, *fields):
    """Called inside the verify: one ledger event for the host to append,
    stamped now. Fields are free text; the host sanitises them."""
    clean = (str(x).replace("\t", " ").replace("\n", " ").replace("\r", " ")
             for x in (event, *fields))
    with (Path(out) / EVENTS).open("a") as f:
        f.write("\t".join((f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}", *clean)) + "\n")


def take_events(out):
    """The host's side: the events a verify recorded under `out`, as
    (stamp, event, fields) for the allowed event names only, and the file
    removed so the next verify starts empty."""
    f = Path(out) / EVENTS
    try:
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return []
    f.unlink(missing_ok=True)
    events = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] in LEDGER_EVENTS:
            events.append((parts[0], parts[1], tuple(parts[2:])))
    return events


class _Steps:
    """The seconds each step before the verifier took, for its start line."""

    def __init__(self, out):
        self.out, self.took = out, []

    def timed(self, name, fn):
        t0 = time.monotonic()
        try:
            return fn()
        finally:
            self.took.append((name, time.monotonic() - t0))

    def __str__(self):
        return " ".join(f"{n}={s:.1f}s" for n, s in self.took) or "-"


def _log_line(out, text):
    with (Path(out) / "verifier.log").open("a") as log:
        log.write(f"{_now()}  {text}\n")


def verify_mounts(ctx, root_reads=None):
    """What a verify container sees: the experiment, the engine, the root's
    files and the paths the verifier declares (`ROOT_READS`), and each entry
    of the workspace, all read-only; its own directory `ctx.out` writable.
    Whatever runs in it can change nothing of the cell's record or the
    experiment, and sees no other cell.

    No mount sits inside another: a writable bind nested in a read-only one
    vanishes from the container within a second on Docker Desktop, and the
    verify then writes into the read-only parent. So the root and the
    workspace are not mounted whole; the workspace entry that holds `ctx.out`
    is left out, and so is what the cell keeps for itself and the agent
    (`Cell.VERIFY_UNREAD`): no verify reads it."""
    from fnmatch import fnmatch
    from . import image as _image
    from .cell import Cell
    if root_reads is None:
        root_reads = fae.experiment.exp().definition.verifier_class().ROOT_READS
    out = Path(ctx.out)
    root, ws = Path(ctx.root), Path(ctx.workspace)

    def entries(d, files_only=False, unread=()):
        try:
            found = sorted(d.iterdir())
        except OSError:
            return []
        return [p for p in found
                if not p.is_symlink() and p != out and p not in out.parents
                and not (files_only and not p.is_file())
                and not any(fnmatch(p.name, u) for u in unread)]
    ro = [ctx.experiment_dir, HARNESS.parent, *entries(root, files_only=True),
          *[root / r for r in root_reads if (root / r).exists()],
          *entries(ws, unread=Cell.VERIFY_UNREAD)]
    return ([(p, "ro") for p in _image.mounts_for(*ro) if p != str(out)]
            + [(str(out), "rw")])


def _host_args(host, extra):
    """What a container on a dedicated verify host differs by: no core
    pinning (its cores are the verify's alone, CPUSET_MEASURED emptied for
    what it starts) and the remote user's identity. (cpuset override or
    False to keep the config's, identity)."""
    if host is None or not host.remote:
        return False, None
    extra["CPUSET_MEASURED"] = ""
    return None, host.identity()


def verify_argv(ctx, image, conf=None, environ=None, cpus=None, host=None, reference=None):
    """The `docker run` that is one arrangement's child: the variant's
    image, the roots mounted at their own paths, the cell's network, the
    daemon's socket, the driver's environment minus the host's own, the
    verifier's CPU cap, and the verify host's calibration `reference` for
    this variant and arrangement (verify_timings.Reference) as JSON in
    FAE_CALIBRATION."""
    from . import image as _image
    home = Path(ctx.out) / WORKDIR / "home"
    # the ctx's roots override the config's exported copies of the same names
    extra = dict(getattr(conf, "exported", None) or {})
    extra.update(REPO_ROOT=ctx.root, EXPERIMENT_DIR=ctx.experiment_dir,
                 PYTHONPATH=os.pathsep.join([ctx.root, str(HARNESS.parent)]),
                 HOME=str(home), USER=CONTAINER_USER,
                 FAE_VERIFY_CONTAINER=_image.verify_container(ctx.cid),
                 FAE_VERIFY_IMAGE=image, FAE_VERIFY_OUT=ctx.out,
                 FAE_CELL_NET=_image.cell_network(ctx.cid))
    if reference is not None:
        extra["FAE_CALIBRATION"] = json.dumps(asdict(reference), sort_keys=True)
    cpuset, identity = _host_args(host, extra)
    if cpuset is False:
        cpuset = conf.get("CPUSET_MEASURED") if conf else None
    env = _image.child_env(environ if environ is not None else os.environ, **extra)
    mounts = verify_mounts(ctx)
    return _image.run_argv(image, _image.verify_container(ctx.cid),
                           CHILD_ARGV + ["--ctx", str(Path(ctx.out) / WORKDIR / "ctx.json"),
                                         "--out", str(Path(ctx.out) / WORKDIR / "verdict.json")],
                           mounts=mounts, env=env, workdir=ctx.root,
                           network=_image.cell_network(ctx.cid),
                           labels=(("fae-cell", ctx.cid),), extra=HOST_ALIAS,
                           cpus=cpus, cpuset=cpuset, identity=identity)


from .image import CONTAINER_USER, HOST_ALIAS  # noqa: E402


def teardown_argv(ctx, image, conf=None, environ=None, host=None):
    """A fresh container of the variant's image running its
    `verify_teardown` alone: the verify's own container is gone (killed,
    timed out) and what it provisioned outside itself is not."""
    from . import image as _image
    home = Path(ctx.out) / WORKDIR / "home"
    extra = dict(getattr(conf, "exported", None) or {})
    extra.update(REPO_ROOT=ctx.root, EXPERIMENT_DIR=ctx.experiment_dir,
                 PYTHONPATH=os.pathsep.join([ctx.root, str(HARNESS.parent)]),
                 HOME=str(home), USER=CONTAINER_USER,
                 FAE_VERIFY_CONTAINER=_image.verify_container(ctx.cid),
                 FAE_VERIFY_IMAGE=image, FAE_VERIFY_OUT=ctx.out,
                 FAE_CELL_NET=_image.cell_network(ctx.cid))
    cpuset, identity = _host_args(host, extra)
    env = _image.child_env(environ if environ is not None else os.environ, **extra)
    if cpuset is False:
        cpuset = (conf.get("CPUSET_MEASURED") if conf else None) or env.get("CPUSET_MEASURED") or None
    mounts = verify_mounts(ctx)
    return _image.run_argv(image, f"{_image.verify_container(ctx.cid)}-teardown",
                           CHILD_ARGV + ["--teardown", "--ctx",
                                         str(Path(ctx.out) / WORKDIR / "teardown.ctx.json")],
                           mounts=mounts, env=env, workdir=ctx.root,
                           network=_image.cell_network(ctx.cid),
                           labels=(("fae-cell", ctx.cid),), extra=HOST_ALIAS,
                           cpuset=cpuset, identity=identity)


def run_teardown(ctx, infra, timeout_s=900, log_dir=None, host=None):
    """Run the teardown for `ctx` — the artifacts' runner stopped, its
    infra's `verify_teardown`, then the verifier's own `teardown` — in a fresh container of
    its image (see `teardown_argv`), on the verify host; returns the child's
    exit code, or None when no container could run (no image, no daemon).
    Best effort: the reaper covers what this leaves. `log_dir` holds
    verifier.log (the host's record of the run), default `ctx.out`; `host`
    is the verify host, default the config's one (VerifyHost.from_conf)."""
    from . import image as _image
    from .verify_host import VerifyHost
    work = Path(ctx.out) / WORKDIR
    out = Path(log_dir or ctx.out)
    (work / "home").mkdir(parents=True, exist_ok=True)
    (work / "teardown.ctx.json").write_text(ctx.to_json())
    conf = getattr(infra, "conf", None)
    host = host or VerifyHost.from_conf(conf)
    denv = host.env()
    log = lambda m: _log_line(out, m)  # noqa: E731
    try:
        image = _image.for_variant(infra.variant, fae.experiment.exp().definition, conf,
                                   log=log, env=denv)
        if host.remote:
            host.network_up(_image.cell_network(ctx.cid), ctx.cid)
            host.push([p for p, _ in verify_mounts(ctx)], log)
        argv = teardown_argv(ctx, image, conf, host=host)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        _log_line(out, f"teardown image: {e}")
        if host.remote:
            _drop_network(host, ctx.cid)
        return None
    name = f"{_image.verify_container(ctx.cid)}-teardown"
    _image.remove_container(name, env=denv)
    _log_line(out, f"teardown start image={image} container={name}"
                   + (f" host={host.docker_host}" if host.remote else ""))
    try:
        with (out / "verifier.log").open("a") as lf:
            p = subprocess.Popen(argv, cwd=ctx.root, env=denv,
                                 stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                return p.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _log_line(out, f"teardown ran past {timeout_s}s; its container was removed")
                return None
            finally:
                _end(p, name, env=denv)
    finally:
        _pull(host, ctx, out)


def _pull(host, ctx, out, drop_network=True):
    """The verify host's output directory back over the local one, and (when
    `drop_network`) the cell network there removed: nothing of the cell stays
    on a verify host between verifies."""
    if not host.remote:
        return
    try:
        host.pull(ctx.out, lambda m: _log_line(out, m))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        _log_line(out, f"verify host: could not copy {ctx.out} back: {e}")
    if drop_network:
        _drop_network(host, ctx.cid)


def _drop_network(host, cid):
    from . import image as _image
    try:
        host.docker("network", "rm", _image.cell_network(cid))
    except subprocess.TimeoutExpired:
        pass


def run_verifier(ctx, infra, timeout_s=7200, log_dir=None, host=None):
    """Run the experiment's verifier on `ctx` inside a container of the
    image of `infra`'s variant, on the verify host (verify_host.py), and
    return its Verdict. A verifier that hangs past `timeout_s`, crashes,
    exits without writing a verdict, or a verify host that cannot be reached
    is a rig fault: `charge=False`, the attempt is retried. The container is
    removed on every path; when the verify did not end on its own (a timeout,
    a signal, any exception) or ended without a readable verdict (its
    container killed under it, a crash), the full teardown runs too
    (`run_teardown`), since what it started beside its container (the
    artifacts' runner, the infra, a store) does not die with it. `log_dir`
    holds verifier.log (the host's record of the run), default `ctx.out`;
    `host` is the verify host, default the config's one (VerifyHost.from_conf)."""
    from . import image as _image
    from .verify_host import VerifyHost
    from .verify_timings import calibration_of
    work = Path(ctx.out) / WORKDIR
    out = Path(log_dir or ctx.out)
    (work / "home").mkdir(parents=True, exist_ok=True)
    ctx_path, verdict_path = work / "ctx.json", work / "verdict.json"
    verdict_path.unlink(missing_ok=True)
    ctx_path.write_text(ctx.to_json())
    conf = getattr(infra, "conf", None)
    host = host or VerifyHost.from_conf(conf)
    denv = host.env()
    log = lambda m: _log_line(out, m)  # noqa: E731

    def host_fault(why):
        _log_line(out, f"verify host: {why}")
        return Verdict(ok=False, stage="verifier-host", charge=False,
                       why=f"verify host {host.docker_host}: {why}", arrangement=ctx.arrangement)

    steps = _Steps(out)
    if host.remote and not steps.timed("host up", lambda: host.ensure_up(log)):
        return host_fault("the daemon does not answer")
    # A smoke cell (a reference pipeline check: host tuning, calibration's own
    # runs) is judged with the config's load, on any host; a scored verify
    # only on a calibrated host, with the load its section recorded, and
    # only once a remote host is idle.
    scored = not (conf is not None and conf.get("SMOKE"))
    reference = None
    if scored:
        timings, uncalibrated = calibration_of(fae.experiment.exp().root, host)
        if uncalibrated:
            _log_line(out, f"verify host: {uncalibrated}")
            return Verdict(ok=False, stage="verifier-host", charge=False, why=uncalibrated,
                           stand_down=("uncalibrated-verify-host",), arrangement=ctx.arrangement)
        if conf is not None and timings.load:
            conf = conf.with_values(timings.load)
        reference = timings.reference(ctx.variant, ctx.arrangement or "seed")
    if scored and host.remote:
        try:
            idle, seen = steps.timed("idle", lambda: _wait_idle(host, conf, log))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return host_fault(f"its CPU busy share is unreadable: {str(e).splitlines()[0]}")
        if not idle:
            readings = " ".join(f"{b:.2f}" for b in seen)
            record_event(ctx.out, "ALERT", f"HOST-BUSY {ctx.arrangement or 'seed'} "
                                           f"busy={readings} on {host.docker_host}")
            return Verdict(ok=False, stage="host-busy", charge=False,
                           why=f"verify host {host.docker_host} stayed busy: CPU busy share "
                               f"{readings} over {len(seen)} readings",
                           arrangement=ctx.arrangement)
    try:
        image = steps.timed("image", lambda: _image.for_variant(
            infra.variant, fae.experiment.exp().definition, conf, log=log, env=denv))
    except RuntimeError as e:
        _log_line(out, f"verifier image: {e}")
        return Verdict(ok=False, stage="verifier-image", charge=False,
                       why=f"no verify image: {str(e).splitlines()[0]}", arrangement=ctx.arrangement)
    name = _image.verify_container(ctx.cid)
    cls = fae.experiment.exp().definition.verifier_class()
    try:
        steps.timed("stale container", lambda: _image.remove_container(name, env=denv))
        if host.remote:
            steps.timed("network", lambda: host.network_up(_image.cell_network(ctx.cid), ctx.cid))
            steps.timed("push", lambda: host.push([p for p, _ in verify_mounts(ctx)], log))
        argv = verify_argv(ctx, image, conf, cpus=cls.CPUS, host=host, reference=reference)
    except BaseException as e:
        # nothing has started on the host: what was set up for it goes, a
        # signal included, so no cell network outlives the verify there
        if host.remote:
            _drop_network(host, ctx.cid)
        if not isinstance(e, (RuntimeError, OSError, subprocess.TimeoutExpired)):
            raise
        return host_fault(str(e).splitlines()[0] if str(e) else type(e).__name__)
    _log_line(out, f"verifier start arrangement={ctx.arrangement or 'seed'} "
                   f"image={image} container={name}"
                   + (f" cpus={cls.CPUS}" if cls.CPUS is not None else "")
                   + (f" host={host.docker_host}" if host.remote else "")
                   + f" before: {steps}")
    ended = False
    from .cell import Cell
    stop_sampling = host.sample_load(out / Cell.HOST_LOAD)
    with (out / "verifier.log").open("a") as lf:
        p = subprocess.Popen(argv, cwd=ctx.root, env=denv, stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=timeout_s)
            ended = True
        except subprocess.TimeoutExpired:
            return Verdict(ok=False, stage="verifier-timeout", charge=False,
                           why=f"the verifier ran past {timeout_s}s; its container was removed",
                           arrangement=ctx.arrangement)
        finally:
            stop_sampling()
            _end(p, name, env=denv)
            _pull(host, ctx, out, drop_network=False)
            if not ended:
                _log_line(out, "verify did not end on its own: running its teardown")
                run_teardown(ctx, infra, log_dir=out, host=host)
    if verdict_path.is_file():
        try:
            verdict = Verdict.from_json(verdict_path.read_text())
        except (ValueError, TypeError) as e:
            why = f"unreadable verdict: {e}"
        else:
            if host.remote:
                _drop_network(host, ctx.cid)
            return verdict
    else:
        why = f"the verifier exited {rc} without a verdict (see verifier.log)"
    _log_line(out, "verify left no verdict: running its teardown")
    run_teardown(ctx, infra, log_dir=out, host=host)
    return Verdict(ok=False, stage="verifier", charge=False, why=why,
                   arrangement=ctx.arrangement)


def _wait_idle(host, conf, log):
    """(idle, readings) of `host` against the config's VERIFY_IDLE_* keys."""
    get = (lambda k, d: (conf.get(k) if conf is not None else None) or d)
    return host.wait_idle(float(get("VERIFY_IDLE_MAX", 0.10)),
                          float(get("VERIFY_IDLE_WAIT_S", 60)),
                          int(get("VERIFY_IDLE_RETRIES", 3)), log=log)


def _end(p, name, env=None):
    """End the verify: remove its container (everything it started goes
    with it) and reap the client."""
    from . import image as _image
    _image.remove_container(name, env=env)
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _bind_runtime(ctx, definition):
    """Attach runtime helpers onto `ctx` inside the verify child container."""
    from fae.experiment import variants as _variants
    vcls = definition.variant(ctx.variant)
    ctx.variant_def = vcls
    ctx.verify_container = os.environ.get("FAE_VERIFY_CONTAINER", "")
    ctx.cell_network = os.environ.get("FAE_CELL_NET", "")
    if vcls is not None:
        infra = _variants.infra_for(ctx.variant, ctx.cid, ctx.workspace, ctx.root,
                                    definition=definition)
        ctx.infra = infra
        ctx.conf = infra.conf
    else:
        ctx.infra = None
        ctx.conf = _config.load(ctx.root)
    ctx.runner = RunnerHelper(ctx)
    return vcls


def main(argv=None):
    """The child: load the definition the ctx names, call its verifier,
    write the Verdict."""
    import argparse
    ap = argparse.ArgumentParser(description="run the experiment's verifier on one ctx.json")
    ap.add_argument("--ctx", required=True)
    ap.add_argument("--out", help="where the Verdict lands (a verify)")
    ap.add_argument("--teardown", action="store_true",
                    help="stop the artifacts' runner and run the infra's verify_teardown "
                         "for the ctx instead of a verify")
    a = ap.parse_args(argv)
    if not (a.out or a.teardown):
        ap.error("--out is required for a verify")
    ctx = Ctx.from_json(Path(a.ctx).read_text())
    if ctx.root not in sys.path:
        sys.path.insert(0, ctx.root)
    definition = fae.experiment._experiment.load(ctx.experiment_dir)
    vcls = _bind_runtime(ctx, definition)
    if a.teardown:
        if vcls is None:
            return 2
        from .infra import secrunner
        secrunner.stop_by_name(ctx.cid, ctx.out,
                               log=Path(ctx.out) / definition.verifier_class().RUN_LOG)
        stop_fixtures_for(ctx.cid)
        try:
            ctx.infra.verify_teardown(ctx, dict(os.environ))
        finally:
            try:
                down = getattr(ctx.infra, "verify_down", None)
                if callable(down):
                    down(ctx)
            finally:
                definition.verifier_class()().teardown(ctx)
        return 0
    cls = definition.verifier_class()
    try:
        verdict = cls().verify(ctx)
    finally:
        ctx.stop_fixtures()
    if not verdict.files and cls.FILES:
        verdict = replace(verdict, files=tuple(cls.FILES))
    Path(a.out).write_text(verdict.to_json())
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
