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
host judges in the same environment. The driver holds the arena, the
verify lock and the verifier's exclusive lock on open fds; the container
inherits none, and a verifier exception, hang or crash cannot touch the
cell loop: the container is removed when the child returns or times out,
and everything it started goes with it.

What a verifier owes the rig, `verify(ctx)` being the whole interface:
the program under judgment NEVER EXECUTES ON THE HOST, and neither does
the verifier. It builds and runs the program inside the variant's
substrate — a container of the variant's runtime image, the cell's dind
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
import traceback
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import config as _config

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
    """Load <instruments>/<name>.py once, by path — the same way ledger.py and
    mutex.py are loaded, because the instruments dir is a directory of tools,
    not a package. Cached: the import cost is paid once per cell."""
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



def _now():
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def _mutex_module():
    """THE filesystem mutex (fae/mutex.py) — one implementation for every holder."""
    from fae import mutex
    return mutex


# --- the boundary --------------------------------------------------------------

@dataclass
class Ctx:
    """What the verifier is handed: everything about the run, nothing about
    the rig's locks."""

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

    def to_json(self):
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text))


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
    build and run it inside the variant's substrate (a container of the
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
    SUBSTRATE_PREFIXES = {}   # {kind: name prefix} of what a verify provisions, for the reaper
    # A charged fail at one of these stages is voided when the variant's
    # substrate is found dead afterwards; None: any charged fail.
    MEASURED_STAGES = None

    @classmethod
    def substrate_identities(cls, cid):
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


def _log_line(out, text):
    with (Path(out) / "verifier.log").open("a") as log:
        log.write(f"{_now()}  {text}\n")


def verify_argv(ctx, image, conf=None, environ=None, cpus=None):
    """The `docker run` that is one arrangement's child: the variant's
    image, the roots mounted at their own paths, the cell's network, the
    daemon's socket, the driver's environment minus the host's own, the
    verifier's CPU cap."""
    from . import image as _image
    home = Path(ctx.out) / WORKDIR / "home"
    # the ctx's roots override the config's exported copies of the same names
    extra = dict(getattr(conf, "exported", None) or {})
    extra.update(REPO_ROOT=ctx.root, EXPERIMENT_DIR=ctx.experiment_dir,
                 PYTHONPATH=os.pathsep.join([ctx.root, str(HARNESS.parent)]),
                 HOME=str(home), USER=CONTAINER_USER,
                 FAE_VERIFY_CONTAINER=_image.verify_container(ctx.cid),
                 FAE_VERIFY_IMAGE=image,
                 FAE_CELL_NET=_image.cell_network(ctx.cid))
    env = _image.child_env(environ if environ is not None else os.environ, **extra)
    mounts = _image.mounts_for(ctx.root, ctx.experiment_dir, HARNESS.parent,
                               ctx.workspace, ctx.out)
    return _image.run_argv(image, _image.verify_container(ctx.cid),
                           CHILD_ARGV + ["--ctx", str(Path(ctx.out) / WORKDIR / "ctx.json"),
                                         "--out", str(Path(ctx.out) / WORKDIR / "verdict.json")],
                           mounts=mounts, env=env, workdir=ctx.root,
                           network=_image.cell_network(ctx.cid),
                           labels=(("fae-cell", ctx.cid),), extra=HOST_ALIAS,
                           cpus=cpus)


from .image import CONTAINER_USER, HOST_ALIAS  # noqa: E402


def teardown_argv(ctx, image, conf=None, environ=None):
    """A fresh container of the variant's image running its
    `verify_teardown` alone: the verify's own container is gone (killed,
    timed out) and what it provisioned outside itself is not."""
    from . import image as _image
    home = Path(ctx.out) / WORKDIR / "home"
    extra = dict(getattr(conf, "exported", None) or {})
    extra.update(REPO_ROOT=ctx.root, EXPERIMENT_DIR=ctx.experiment_dir,
                 PYTHONPATH=os.pathsep.join([ctx.root, str(HARNESS.parent)]),
                 HOME=str(home), USER=CONTAINER_USER)
    env = _image.child_env(environ if environ is not None else os.environ, **extra)
    mounts = _image.mounts_for(ctx.root, ctx.experiment_dir, HARNESS.parent,
                               ctx.workspace, ctx.out)
    return _image.run_argv(image, f"{_image.verify_container(ctx.cid)}-teardown",
                           CHILD_ARGV + ["--teardown", "--ctx",
                                         str(Path(ctx.out) / WORKDIR / "teardown.ctx.json")],
                           mounts=mounts, env=env, workdir=ctx.root,
                           network=_image.cell_network(ctx.cid),
                           labels=(("fae-cell", ctx.cid),), extra=HOST_ALIAS)


def run_teardown(ctx, variant, timeout_s=900, log_dir=None):
    """Run the variant's `verify_teardown` for `ctx` in a fresh container of
    its image (see `teardown_argv`); returns the child's exit code, or None
    when no container could run (no image, no daemon). Best effort: the
    reaper covers what this leaves. `log_dir` holds verifier.log (the host's
    record of the run), default `ctx.out`."""
    from . import experiment as _experiment
    from . import image as _image
    work = Path(ctx.out) / WORKDIR
    out = Path(log_dir or ctx.out)
    (work / "home").mkdir(parents=True, exist_ok=True)
    (work / "teardown.ctx.json").write_text(ctx.to_json())
    conf = getattr(variant, "conf", None)
    try:
        image = _image.for_variant(type(variant), _experiment.current(), conf,
                                   log=lambda m: _log_line(out, m))
    except RuntimeError as e:
        _log_line(out, f"teardown image: {e}")
        return None
    name = f"{_image.verify_container(ctx.cid)}-teardown"
    _image.remove_container(name)
    _log_line(out, f"teardown start image={image} container={name}")
    with (out / "verifier.log").open("a") as log:
        p = subprocess.Popen(teardown_argv(ctx, image, conf), cwd=ctx.root,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _log_line(out, f"teardown ran past {timeout_s}s; its container was removed")
            return None
        finally:
            _end(p, name)


def run_verifier(ctx, variant, timeout_s=7200, log_dir=None):
    """Run the experiment's verifier on `ctx` inside a container of
    `variant`'s image and return its Verdict. A verifier that hangs past
    `timeout_s`, crashes, or exits without writing a verdict is a rig
    fault: `charge=False`, the attempt is retried. The container is removed
    on every path, so nothing the verifier started outlives it. `log_dir`
    holds verifier.log (the host's record of the run), default `ctx.out`."""
    from . import experiment as _experiment
    from . import image as _image
    work = Path(ctx.out) / WORKDIR
    out = Path(log_dir or ctx.out)
    (work / "home").mkdir(parents=True, exist_ok=True)
    ctx_path, verdict_path = work / "ctx.json", work / "verdict.json"
    verdict_path.unlink(missing_ok=True)
    ctx_path.write_text(ctx.to_json())
    conf = getattr(variant, "conf", None)
    try:
        image = _image.for_variant(type(variant), _experiment.current(), conf,
                                   log=lambda m: _log_line(out, m))
    except RuntimeError as e:
        _log_line(out, f"verifier image: {e}")
        return Verdict(ok=False, stage="verifier-image", charge=False,
                       why=f"no verify image: {str(e).splitlines()[0]}", arrangement=ctx.arrangement)
    name = _image.verify_container(ctx.cid)
    _image.remove_container(name)                 # a previous run's, if any
    cls = _experiment.current().verifier_class()
    argv = verify_argv(ctx, image, conf, cpus=cls.CPUS)
    _log_line(out, f"verifier start arrangement={ctx.arrangement or 'seed'} "
                   f"image={image} container={name}"
                   + (f" cpus={cls.CPUS}" if cls.CPUS is not None else ""))
    with (out / "verifier.log").open("a") as log:
        p = subprocess.Popen(argv, cwd=ctx.root, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _end(p, name)
            return Verdict(ok=False, stage="verifier-timeout", charge=False,
                           why=f"the verifier ran past {timeout_s}s; its container was removed",
                           arrangement=ctx.arrangement)
        finally:
            _end(p, name)
    if verdict_path.is_file():
        try:
            return Verdict.from_json(verdict_path.read_text())
        except (ValueError, TypeError) as e:
            return Verdict(ok=False, stage="verifier", charge=False,
                           why=f"unreadable verdict: {e}", arrangement=ctx.arrangement)
    return Verdict(ok=False, stage="verifier", charge=False,
                   why=f"the verifier exited {rc} without a verdict (see verifier.log)",
                   arrangement=ctx.arrangement)


def _end(p, name):
    """End the verify: remove its container (everything it started goes
    with it) and reap the client."""
    from . import image as _image
    _image.remove_container(name)
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def main(argv=None):
    """The child: load the definition the ctx names, call its verifier,
    write the Verdict."""
    import argparse
    ap = argparse.ArgumentParser(description="run the experiment's verifier on one ctx.json")
    ap.add_argument("--ctx", required=True)
    ap.add_argument("--out", help="where the Verdict lands (a verify)")
    ap.add_argument("--teardown", action="store_true",
                    help="run the variant's verify_teardown for the ctx instead of a verify")
    a = ap.parse_args(argv)
    if not (a.out or a.teardown):
        ap.error("--out is required for a verify")
    ctx = Ctx.from_json(Path(a.ctx).read_text())
    if ctx.root not in sys.path:
        sys.path.insert(0, ctx.root)
    from . import experiment as _experiment
    definition = _experiment.load(ctx.experiment_dir)
    if a.teardown:
        from .variants import _ShimCell
        vcls = definition.variant(ctx.variant)
        if vcls is None:
            return 2
        cell = _ShimCell(ctx.cid, ctx.workspace, ctx.root)
        cell.treatment = ctx.variant
        vcls(cell).verify_teardown(ctx, dict(os.environ))
        return 0
    cls = definition.verifier_class()
    verdict = cls().verify(ctx)
    if not verdict.files and cls.FILES:
        verdict = replace(verdict, files=tuple(cls.FILES))
    Path(a.out).write_text(verdict.to_json())
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
