"""`cli.py experiment check`: whether the experiment this root runs is ready,
in the order an author builds it, each step with why it matters and how to
fix what it finds.

Every check reuses the code a real run goes through: the definition is
loaded by `experiment.load`, the authoring surface and the liveness probe
by the preflight's own predicates, the seeds by `prepare()` itself into a
temporary workspace root, the infra by `experiment infra`'s probe.
A check therefore cannot pass while the run it stands for would fail on the
same cause.

`--walk` pauses before each step and after a failure, so the command is
also the how-to: read why, run it, fix what it names, retry.
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fae import experiment as _experiment
from fae import paths as _paths

HOWTO = "HOWTO.md"


# --- the host carries the variants: their infra and verify images ---------

def _authorable_error(vid):
    from fae.cell.surface import authorable
    try:
        authorable(vid)
    except RuntimeError as e:
        return str(e)
    return ""


def probe_variants(variants=None):
    """Every variant's own preflight (its infra's ok(): the daemon, the tools)
    and the image its cells are verified in, built here when missing —
    printed one per line; the count refused."""
    from fae.cell import variants as _tr
    bad = 0
    for vid in sorted(variants or _experiment.definition().active):
        cell = _tr._ShimCell(f"infra-probe-{vid}", "/nonexistent", _experiment.current().root)
        cell.variant = vid
        infra = _tr.for_cell(cell)
        ok, note = True, ""
        if not _tr.liveness_declared(infra.variant):
            ok, note = False, f"{type(infra).__name__} declares no alive() probe"
        elif (undeclared := _authorable_error(vid)):
            ok, note = False, undeclared
        elif not infra.ok():
            ok = False
        if ok:
            try:
                note = infra.image()
            except RuntimeError as e:
                ok, note = False, f"verify image: {str(e).splitlines()[0]}"
        print(f"  [{'ok' if ok else 'HALT'}] {vid}  {note}")
        bad += not ok
    return bad


def infra(args):
    """`experiment infra`: can this host carry each variant? Every active
    variant's preflight and verify image (probe_variants), then a sweep of
    stale infra. Nothing per cell is created. Exit 1 if any is refused."""
    bad = probe_variants()
    from fae.cell import variants as _tr
    for infra_cls in {cls.INFRA for cls in _tr.registry().values()}:
        infra_cls.sweep()
    if bad:
        sys.exit(f"infra: {bad} variant(s) refused — see hooks.log lines above")


# --- the transitions replay against the model ------------------------------

TLA_DIR = _paths.ENGINE.parent / ".tla"      # the engine repo's model
CONFORMANCE_SINCE = TLA_DIR / "conformance-since"
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def tla_verify_path():
    """The TLA+ trace checker: $FAE_TLA_VERIFY, else the one fae ships
    (fae/utils/tla_verify.py); None when the variable names no file."""
    p = os.environ.get("FAE_TLA_VERIFY") or str(_paths.ENGINE / "utils" / "tla_verify.py")
    return p if Path(p).is_file() else None


def conformance_since():
    """['--since', <instant>] for tla_verify, or [] when no cutoff is declared.

    A cutoff in the future would judge nothing, which is a green check that
    proves nothing — refused rather than honoured.
    """
    from datetime import datetime, timezone
    try:
        body = CONFORMANCE_SINCE.read_text()
    except OSError:
        return []
    vals = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
    if not vals:
        return []
    since = vals[-1]
    if not _ISO_Z.match(since):
        print(f"  (ignoring {CONFORMANCE_SINCE.name}: '{since}' is not "
              f"YYYY-MM-DDTHH:MM:SSZ — judging the whole trace)")
        return []
    if since > f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}":
        print(f"  (ignoring {CONFORMANCE_SINCE.name}: {since} is in the "
              f"future, which would judge nothing — judging the whole trace)")
        return []
    return ["--since", since]


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
    variants: tuple = ()
    task: str = "T1"
    static: bool = False
    _definition: object = field(default=None, repr=False)

    def definition(self):
        if self._definition is None:
            self._definition = _experiment.definition()
        return self._definition

    def forget(self):
        """Drop the loaded definition, so a retry sees the files as they are now."""
        _experiment.unload()
        self._definition = None

    def selected(self):
        d = self.definition()
        return [v for v in d.active if not self.variants or v in self.variants]


def _last_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[-1]


# --- the steps ------------------------------------------------------------

def _prerequisites(ctx):
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
    if not ctx.static:
        try:
            ok = subprocess.run(["docker", "info"], capture_output=True,
                                timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        out.append(Finding(ok, "docker daemon " + ("answers" if ok else "unreachable"),
                           "start Docker, then `docker info` must succeed"))
    from fae import mutex as _mutex
    local, fstype = _mutex.fs_is_local(_experiment.workspace().path)
    if local is False:
        out.append(Finding(True, f"WARNING: workspaces on {fstype}, not a local disk: ledger "
                                 f"appends from conduct and a cell can interleave"))
    return out


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
    from fae.cell.variants.files import DIR
    d = ctx.definition()
    out = []
    try:
        ids = d.ids
    except Exception as e:      # a variant file that cannot be read is the finding
        return [Finding(False, f"reading the variant files raised {type(e).__name__}: "
                               f"{_last_line(e)}",
                        f"fix the file it names ({DIR}/<id>.toml, HOWTO §5)")]
    out.append(Finding(bool(d.active), f"{len(d.active)} active variant(s): "
                                       f"{', '.join(d.active) or 'none'}"
                       + (f"; {len(ids) - len(d.active)} retired" if len(ids) > len(d.active) else ""),
                       f"write {d.path / DIR}/<id>.toml, one file per variant (HOWTO §5)"))
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
    for vid in ctx.selected():
        cls = d.variant(vid)
        if cls is None:
            continue
        where = cls.SOURCE.name if cls.SOURCE else vid
        try:
            exact, prefixes = authorable(vid)
            out.append(Finding(True, f"{vid}: authoring surface "
                                     f"{list(exact) + list(prefixes)}"))
        except RuntimeError as e:
            out.append(Finding(False, str(e),
                               f"declare [authoring] surface = {{ files = [...], prefixes = [...] }} "
                               f"in {where}: what the agent may write"))
        out.append(Finding(liveness_declared(cls), f"{vid}: an infra liveness probe",
                           f"give {where} an [infra] class with alive(), or a "
                           f"[verify.run] image: asked before every arrangement"))
        out.append(Finding("TODO.md" in cls.INPUTS, f"{vid}: TODO.md among its inputs",
                           f"add \"TODO.md\" = \"task/<T>.PROMPT.md\" to [authoring.inputs] in "
                           f"{where}: the engine's prompt tells the agent to read it"))
    return out


def _seeds(ctx):
    from fae.cell import config as _cfg
    from fae.cell import prepare as _prepare
    from fae.cell.surface import authorable
    d = ctx.definition()
    cfg = _cfg.load(ctx.root)
    out = []
    with tempfile.TemporaryDirectory(prefix="fae-check-") as tmp:
        for vid in ctx.selected():
            try:
                authorable(vid)
            except RuntimeError:
                out.append(Finding(False, f"{vid}: not seeded, it has no authoring surface",
                                   "declare it (the variants step names the file)"))
                continue
            cls = d.variant(vid)
            for reference in (False, True):
                if reference and cls.REFERENCE is None:
                    continue
                what = f"{vid}{' (reference)' if reference else ''}"
                cid = _experiment.cell_id("check", vid, 2 if reference else 1, ctx.task)
                try:
                    _prepare.prepare(cid, ctx.task, vid, 1, workspaces=tmp, root=ctx.root,
                                     reference=reference, cfg=cfg)
                    out.append(Finding(True, f"{what} seeds"))
                except (OSError, RuntimeError) as e:
                    out.append(Finding(False, f"{what}: {_last_line(e)}",
                                       "write the file or directory it names, or fix the "
                                       "path in the variant file (HOWTO §4, §5)"))
    return out


def _infra(ctx):
    bad = probe_variants(ctx.selected())
    return [Finding(not bad, "every variant's infra and verify image" if not bad
                    else f"{bad} variant(s) refused this host (the lines above name why)",
                    "fix what the refused variant's line names; then "
                    "python3 cli.py experiment infra")]


def _invariants(ctx):
    from fae import mutex
    from fae.cell import config as _config, prepare as _prep, rig as _rig, verify as _verify
    from fae.cell.variants import files as _files
    from fae import host
    out = []
    missing = [f"{mod.__name__}.{name}"
               for mod, names in ((_rig, ("fp", "free_port_from")),
                                  (_verify, ("run_verifier", "call", "run_in_thread")),
                                  (mutex, ("open_lock", "try_fd", "wait_fds")),
                                  (_prep, ("prepare", "seed", "safe_wipe")),
                                  (_config, ("load", "opencode_key_file", "stage_agent")))
               for name in names if not callable(getattr(mod, name, None))]
    out.append(Finding(not missing, "the engine's own functions are all there" if not missing
                       else "engine functions missing: " + ", ".join(missing),
                       "restore them: every verify depends on them"))
    effort, smoke = os.environ.get("EFFORT", "high"), os.environ.get("SMOKE")
    out.append(Finding(effort == "high" and not smoke,
                       f"EFFORT={effort!r} SMOKE={smoke!r} in this shell",
                       "unset EFFORT and SMOKE: the scheduler names cells as effort 'high' and "
                       "not smoke, a cell started from this shell would name itself otherwise"))
    d = ctx.definition()
    hook = d.verbs.get("selftest")
    problems = list(hook(_experiment.workspace().path)) if hook else []
    out += [Finding(False, f"the experiment's own check: {p}",
                    "what it names (the experiment's selftest hook)") for p in problems]
    if not problems:
        out.append(Finding(True, "the experiment's own checks" + ("" if hook else ": it declares none")))
    for vid, cls in sorted(d.variants.items()):
        where = cls.SOURCE.name if cls.SOURCE else vid
        out += [Finding(False, f"variant {vid}: {p}", f"fix {where}") for p in _files.problems(cls)]
    disagree = []
    if _experiment.workspace().path.is_dir():
        for ws in sorted(_experiment.workspace().path.iterdir()):
            if not ws.is_dir() or not _experiment.parse_cell_id(ws.name):
                continue
            st = host.cell_state(ws)
            if st and st["state"] == "DONE" and st["why"] in ("green", "failed", "revoked"):
                if _experiment.current().cell(ws.name, workspaces=ws.parent).read_ledger()["verdict"] != st["why"]:
                    disagree.append(ws.name)
    out.append(Finding(not disagree, "every finished cell's ledger agrees with its state" if not disagree
                       else f"ledger and state disagree: {', '.join(disagree[:5])}",
                       "read those cells' iterations.log; a cell's record is never edited"))
    return out


def _leftovers(ctx):
    from fae.conduct import Conduct
    found = Conduct.find_zombies()
    if not found:
        return [Finding(True, "no leftovers of dead cells")]
    return [Finding(False, f"{kind} {ident} (owner {owner}): {note}",
                    "python3 cli.py experiment repair (it reaps them)")
            for kind, ident, owner, note in found]


def _trace(ctx):
    from fae.cell import config as _config
    tool = tla_verify_path()
    if tool is None:
        return [Finding(False, "no TLA+ trace checker",
                        "unset FAE_TLA_VERIFY to use fae/utils/tla_verify.py, or point it at a file")]
    log = _experiment.workspace().transitions
    if not (log.exists() and log.stat().st_size):
        return [Finding(True, "no transitions recorded yet: nothing to replay")]
    spec = sorted(TLA_DIR.glob("*.tla"))
    # the checker's constants are the fleet's, not this shell's
    slots = str(_config.load(ctx.root).values.get("WORK_SLOTS") or 8)
    r = subprocess.run(["python3", tool, "--live-trace", str(log)]
                       + ([str(spec[0])] if spec else []) + conformance_since(),
                       cwd=ctx.root, capture_output=True, text=True,
                       env=dict(os.environ, WORK_SLOTS=slots))
    judged = [l for l in r.stdout.splitlines()
              if "judging" in l or l.startswith("tla_verify --live-trace OK")]
    if r.returncode != 0:
        return [Finding(False, "the transitions break the model: "
                        + _last_line(RuntimeError(r.stdout + r.stderr)),
                        f"read `python3 {tool} --live-trace {log}`; "
                        "`python3 cli.py rig trace-reset` only at a moment the fleet is idle")]
    return [Finding(True, "the transitions replay against the model"
                    + (f" ({judged[0].strip()})" if judged else ""))]


def _pipeline(ctx):
    exp = _experiment.current()
    try:
        exp.smoke(variants=",".join(v for v in exp.smoke_variants() if v in ctx.selected()))
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    return [Finding(rc == 0, "every variant's reference is green through the gate",
                    "read the VERDICT line of the failing variant and the log it names")]


STEPS = (
    Step("prerequisites", "prerequisites",
         "python >= 3.11, typer, ujson; docker daemon (skipped with --static)",
         "§1", _prerequisites),
    Step("config", "config",
         "fae.toml; the experiment definition loads",
         "§3, §7", _config, needs=("prerequisites",)),
    Step("definition", "definition",
         "variants/*.toml, verifier class and image, gate",
         "§3, §6", _definition, needs=("config",)),
    Step("variants", "variants",
         "authoring surface, infra liveness probe, inputs include TODO.md",
         "§5", _variants, needs=("definition",)),
    Step("seeds", "seeds",
         "prepare() of every variant and its reference into a temp root",
         "§4, §5", _seeds, needs=("definition",)),
    Step("invariants", "invariants",
         "engine functions, cell-id derivation, the experiment's selftest, sealed cells' records",
         "§12", _invariants, needs=("definition",)),
    Step("infra", "infra",
         "infra ok() per variant; verify image, built if missing",
         "§7", _infra, needs=("seeds", "prerequisites"), docker=True),
    Step("leftovers", "leftovers",
         "containers, clusters, heartbeats of dead cells",
         "§11", _leftovers, needs=("prerequisites",), docker=True),
    Step("tla-trace", "tla-trace",
         "transitions.log replayed against the TLA+ cell-lifecycle model",
         "§11", _trace, needs=("config",), opt_in="--tla-trace"),
    Step("pipeline", "pipeline",
         "one reference cell per judging path, one arrangement, no agent",
         "§8", _pipeline, needs=("infra",), docker=True, opt_in="--smoke"),
)

NEXT = (
    ("reference, all arrangements", "python3 cli.py experiment smoke --full-gate"),
    ("scripted agent (fail, then green)",
     "TESTAGENT_PLAN=fail,green python3 cli.py cell spawn testagent <variant> --rep 1"),
    ("one agent, one variant", "python3 cli.py cell spawn <agent> <variant> --rep 1"),
    ("queue every variant for an agent", "python3 cli.py queue add <agent> --matrix --reps 3"),
    ("run what is queued", "python3 cli.py experiment run -n 2 --per-agent 1"),
)


# --- the runner -----------------------------------------------------------

def _show(findings, out, failed_only=False):
    for f in findings:
        if failed_only and f.ok:
            continue
        out(f"  {'ok  ' if f.ok else 'FAIL'} {f.text}")
        if not f.ok and f.fix:
            out(f"       fix: {f.fix}")


def run(ctx, steps=STEPS, walk=False, smoke=False, tla_trace=False, ask=input, out=print):
    """Run `steps` in order; returns 0 when none failed, 1 otherwise.
    `walk` pauses before each step and after a failure (`ask` reads the answer)."""
    status = {}
    enabled = {"--smoke": smoke, "--tla-trace": tla_trace}
    active = [s for s in steps if not s.opt_in or enabled.get(s.opt_in)]
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
            out(f"\n{head}\n{step.why}")
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
        out("\nREADY. next:")
        for what, cmd in NEXT:
            out(f"  {what}:\n    {cmd}")
    else:
        out("\nNo failure; the skipped steps are not checked.")
    return 0


def main(args):
    if args.walk and not sys.stdin.isatty():
        sys.exit("check: --walk asks before each step and needs a terminal; "
                 "run without --walk for the checklist")
    ctx = Ctx(root=_experiment.current().root, variants=tuple(v for v in (args.variants or "").split(",") if v),
              task=args.task, static=args.static)
    sys.exit(run(ctx, walk=args.walk, smoke=args.smoke, tla_trace=getattr(args, "tla_trace", False)))
