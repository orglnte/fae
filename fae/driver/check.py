"""`cli.py experiment check`: whether the experiment this root runs is ready,
in the order an author builds it, each step with why it matters and how to
fix what it finds.

Every check reuses the code a real run goes through: the definition is
loaded by `experiment.load`, the authoring surface and the liveness probe
by the preflight's own predicates, the seeds by `prepare()` itself into a
temporary workspace root, the substrate by `experiment substrate`'s probe.
A check therefore cannot pass while the run it stands for would fail on the
same cause.

`--walk` pauses before each step and after a failure, so the command is
also the how-to: read why, run it, fix what it names, retry.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from fae.driver import common

HOWTO = "HOWTO.md"


@dataclass
class Finding:
    ok: bool
    text: str
    fix: str = ""


@dataclass
class Step:
    key: str
    title: str
    why: str
    howto: str                      # the HOWTO section that explains it
    run: Callable                   # (Ctx) -> [Finding]
    needs: tuple = ()               # steps that must pass first
    docker: bool = False            # skipped under --static
    opt_in: str = ""                # the option that enables it; "" = always


@dataclass
class Ctx:
    root: Path
    arms: tuple = ()
    task: str = "T1"
    static: bool = False
    _definition: object = field(default=None, repr=False)

    def definition(self):
        if self._definition is None:
            self._definition = common.definition()
        return self._definition

    def forget(self):
        """Drop the loaded definition, so a retry sees the files as they are now."""
        from fae.cell import experiment as _experiment
        _experiment.unload()
        self._definition = None

    def selected(self):
        d = self.definition()
        return [a for a in d.matrix if not self.arms or a in self.arms]


def _last_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[-1]


# --- the steps ------------------------------------------------------------

def _host(ctx):
    out = []
    v = sys.version_info
    out.append(Finding(v >= (3, 11), f"Python {v.major}.{v.minor}",
                       "use Python 3.11 or newer"))
    for mod in ("typer", "ujson"):
        try:
            importlib.import_module(mod)
            out.append(Finding(True, f"{mod} importable"))
        except ImportError:
            out.append(Finding(False, f"{mod} not installed",
                               f"python3 -m pip install {mod}"))
    return out


def _docker(ctx):
    try:
        ok = subprocess.run(["docker", "info"], capture_output=True,
                            timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return [Finding(ok, "docker daemon " + ("answers" if ok else "unreachable"),
                    "start Docker, then `docker info` must succeed")]


def _config(ctx):
    from fae.cell import config as _cfg
    out = []
    toml = ctx.root / _cfg.TOML
    exp_dir = _cfg.experiment_dir(ctx.root)
    rel = os.path.relpath(exp_dir, ctx.root)
    out.append(Finding(toml.is_file(), f"{_cfg.TOML} at {ctx.root}",
                       f"python3 cli.py experiment init --experiment {rel}"))
    init = exp_dir / "__init__.py"
    if not init.is_file():
        out.append(Finding(False, f"no experiment definition at {init}",
                           f"write {init} (HOWTO §3), or point [paths] experiment_dir "
                           f"in {_cfg.TOML} at the experiment"))
        return out
    try:
        d = ctx.definition()
        out.append(Finding(True, f"definition {d.name!r} loads from {exp_dir}"))
    except Exception as e:      # whatever the author's module raises is the finding
        where = traceback.extract_tb(e.__traceback__)[-1]
        out.append(Finding(False, f"loading {init} raised {type(e).__name__}: {_last_line(e)}",
                           f"fix {where.filename}:{where.lineno}"))
    return out


def _definition(ctx):
    from fae.cell.verify import Verifier
    d = ctx.definition()
    out = []
    try:
        arms = d.arms
    except Exception as e:
        return [Finding(False, f"variant_classes() raised {type(e).__name__}: {_last_line(e)}",
                        "variant_classes() returns the Variant subclasses (HOWTO §3)")]
    out.append(Finding(bool(arms), f"{len(arms)} variant(s): {', '.join(arms) or 'none'}",
                       "variant_classes() must return at least one Variant subclass"))
    for arm, conditions in d.matrix.items():
        out.append(Finding(arm in arms, f"matrix arm {arm!r} has a variant",
                           f"add {arm!r}'s class to variant_classes(), or drop it from MATRIX"))
        out.append(Finding(bool(conditions), f"matrix arm {arm!r}: conditions {list(conditions)}",
                           f"give {arm!r} at least one condition (MATRIX, or the "
                           f"variant's CONDITIONS)"))
    try:
        v = d.verifier_class()
        dockerfile = Path(v.IMAGE_DIR or "") / "Dockerfile"
        out.append(Finding(True, f"verifier {v.__name__}"))
        out.append(Finding(bool(v.IMAGE_DIR) and dockerfile.is_file(),
                           f"verifier image: {dockerfile if v.IMAGE_DIR else 'IMAGE_DIR not set'}",
                           f"set {v.__name__}.IMAGE_DIR to a directory holding its "
                           f"Dockerfile (HOWTO §6)"))
    except Exception as e:
        out.append(Finding(False, f"verifier_class(): {_last_line(e)}",
                           f"verifier_class() returns a {Verifier.__name__} subclass (HOWTO §6)"))
    out.append(Finding(d.gate.arity >= 1, f"gate: {d.gate.arity} arrangement(s)",
                       "GATE needs at least one arrangement"))
    return out


def _variants(ctx):
    from fae.cell.surface import authorable
    from fae.cell.variants import liveness_declared
    d = ctx.definition()
    out = []
    for arm in ctx.selected():
        cls = d.variant(arm)
        if cls is None:
            continue
        name = cls.__name__
        out.append(Finding(bool(cls.TECH), f"{name}: TECH {cls.TECH!r}",
                           f"set {name}.TECH: it names the seed docs"))
        try:
            exact, prefixes = authorable(arm)
            out.append(Finding(True, f"{name}: authoring surface "
                                     f"{list(exact) + list(prefixes)}"))
        except RuntimeError as e:
            out.append(Finding(False, str(e),
                               f"declare {name}.AUTHORING_SURFACE = ((files), (dir prefixes)): "
                               f"what the agent may write"))
        out.append(Finding(liveness_declared(cls), f"{name}: substrate_alive probe",
                           f"implement {name}.substrate_alive(): asked before every "
                           f"arrangement; the base answers dead"))
        seed = cls.seed_root()
        out.append(Finding(seed.is_dir(), f"{name}: seed tree {seed}",
                           f"create {seed}/ with overlay/, the docs and reference/overlay/ "
                           f"(HOWTO §5)"))
    return out


def _seeds(ctx):
    from fae.cell import config as _cfg
    from fae.cell import prepare as _prepare
    from fae.cell.surface import authorable
    d = ctx.definition()
    cfg = _cfg.load(ctx.root)
    out = []
    with tempfile.TemporaryDirectory(prefix="fae-check-") as tmp:
        for arm in ctx.selected():
            try:
                authorable(arm)
            except RuntimeError:
                out.append(Finding(False, f"{arm}: not seeded, it has no authoring surface",
                                   "declare it (the variants step names the class)"))
                continue
            for condition in [*d.matrix[arm], "reference"]:
                cid = common.cell_id("check", arm, condition, 1, ctx.task)
                try:
                    _prepare.prepare(cid, ctx.task, arm, condition, 1, workspaces=tmp,
                                     root=ctx.root, cfg=cfg)
                    out.append(Finding(True, f"{arm}/{condition} seeds"))
                except (OSError, RuntimeError) as e:
                    out.append(Finding(False, f"{arm}/{condition}: {_last_line(e)}",
                                       "write the file it names (HOWTO §4 task, §5 seed); "
                                       "a condition's api doc is also pinned in SEED_DOCS"))
    return out


def _substrate(ctx):
    from fae.driver import rig
    bad = rig._probe_arms(ctx.selected())
    return [Finding(not bad, "every arm's substrate and verify image" if not bad
                    else f"{bad} arm(s) refused this host (the lines above name why)",
                    "fix what the refused arm's line names; then "
                    "python3 cli.py experiment substrate")]


def _pipeline(ctx):
    from fae.driver import rig
    try:
        rig.smoke(SimpleNamespace(arms=",".join(ctx.selected()), only="", rep=1,
                                  full_gate=False))
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    return [Finding(rc == 0, "every arm's reference is green through the gate",
                    "read the VERDICT line of the failing arm and the log it names")]


STEPS = (
    Step("host", "This machine",
         "The engine needs Python 3.11+ and two packages; nothing else runs on\n"
         "the host.",
         "§1", _host),
    Step("config", "The config and the definition",
         "fae.toml is machine-local and says where the experiment is; the engine\n"
         "loads that directory's __init__.py as the experiment's definition.",
         "§3, §7", _config, needs=("host",)),
    Step("definition", "What the definition declares",
         "The arms (variant classes), the matrix of arms × conditions, the\n"
         "verifier and the image it runs in, and the gate every attempt must pass.",
         "§3, §6", _definition, needs=("config",)),
    Step("variants", "Each variant",
         "What the agent may write (AUTHORING_SURFACE), how the engine tells the\n"
         "substrate is alive, and the seed tree the agent starts from.",
         "§5", _variants, needs=("definition",)),
    Step("seeds", "Seeding every cell of the matrix",
         "Prepares each arm × condition, and each arm's reference, into a\n"
         "throwaway workspace root: the same prepare() a real cell runs.",
         "§4, §5", _seeds, needs=("definition",)),
    Step("docker", "The docker daemon",
         "Every agent, verifier and program under test runs in a container of\n"
         "this daemon.",
         "§1", _docker, docker=True),
    Step("substrate", "This host can carry each arm",
         "Each variant's own preflight (substrate_ok) and the verify image,\n"
         "built now if missing, so no cell pays for the build.",
         "§7", _substrate, needs=("seeds", "docker"), docker=True),
    Step("pipeline", "The reference passes the gate",
         "One reference cell per arm, no agent, one arrangement: proves the\n"
         "verifier judges the known answer green before any agent runs.",
         "§8", _pipeline, needs=("substrate",), docker=True, opt_in="--smoke"),
)

NEXT = (
    ("the reference, every arrangement", "python3 cli.py experiment smoke --full-gate"),
    ("a scripted agent (fail, then green)",
     "TESTAGENT_PLAN=fail,green python3 cli.py cell spawn testagent <arm> <condition> --rep 1"),
    ("one real agent", "python3 cli.py cell spawn <model> <arm> <condition> --rep 1"),
    ("the fleet", "python3 cli.py conduct queue-add <model> --matrix --reps 3 && "
                  "python3 cli.py conduct run -n 2 --per-model 1"),
)


# --- the runner -----------------------------------------------------------

def _show(findings, out, failed_only=False):
    for f in findings:
        if failed_only and f.ok:
            continue
        out(f"  {'ok  ' if f.ok else 'FAIL'} {f.text}")
        if not f.ok and f.fix:
            out(f"       fix: {f.fix}")


def run(ctx, steps=STEPS, walk=False, smoke=False, ask=input, out=print):
    """Run `steps` in order; returns 0 when none failed, 1 otherwise.
    `walk` pauses before each step and after a failure (`ask` reads the answer)."""
    status = {}
    active = [s for s in steps if not (s.opt_in and not smoke)]
    for n, step in enumerate(active, 1):
        head = f"[{n}/{len(active)}] {step.title}"
        blocked = [k for k in step.needs if status.get(k) != "ok"]
        if step.docker and ctx.static:
            status[step.key] = "skip"
            out(f"{head}: skip (--static)")
            continue
        if blocked:
            status[step.key] = "skip"
            out(f"{head}: skip (needs {', '.join(blocked)})")
            continue
        if walk:
            out(f"\n{head}  (HOWTO {step.howto})\n{step.why}")
            a = ask("[Enter] run · s skip · q quit > ").strip().lower()
            if a == "q":
                break
            if a == "s":
                status[step.key] = "skip"
                continue
        while True:
            try:
                findings = step.run(ctx)
            except Exception as e:      # a check that crashes is a finding, not the end
                findings = [Finding(False, f"{step.key} check raised {type(e).__name__}: "
                                           f"{_last_line(e)}", "")]
            ok = all(f.ok for f in findings)
            if walk or not ok:
                if not walk:
                    out(head)
                _show(findings, out, failed_only=not walk)
            status[step.key] = "ok" if ok else "FAIL"
            if ok or not walk:
                break
            a = ask("fix it, then [r] retry · Enter go on · q quit > ").strip().lower()
            if a == "q":
                return _summary(active, status, out)
            if a != "r":
                break
            ctx.forget()
        if not walk and ok:
            out(f"{head}: ok")
    return _summary(active, status, out)


def _summary(steps, status, out):
    failed = [s for s in steps if status.get(s.key) == "FAIL"]
    out("")
    for s in steps:
        out(f"  {status.get(s.key, 'not run'):7s} {s.title}")
    if failed:
        out(f"\nNOT READY: {len(failed)} step(s) failed; see `fix:` above "
            f"({HOWTO} {', '.join(sorted({h.strip() for s in failed for h in s.howto.split(',')}))}).")
        return 1
    if all(status.get(s.key) == "ok" for s in steps):
        out("\nREADY. Next:")
        for what, cmd in NEXT:
            out(f"  {what}:\n    {cmd}")
    else:
        out("\nNo failure; the skipped steps are not checked.")
    return 0


def main(args):
    if args.walk and not sys.stdin.isatty():
        sys.exit("check: --walk asks before each step and needs a terminal; "
                 "run without --walk for the checklist")
    ctx = Ctx(root=common.ROOT, arms=tuple(a for a in (args.arms or "").split(",") if a),
              task=args.task, static=args.static)
    sys.exit(run(ctx, walk=args.walk, smoke=args.smoke))
