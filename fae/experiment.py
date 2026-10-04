"""The experiment: its definition, its workspace, and the verbs that act on
it as a whole.

    Definition   the experiment as declared, as the engine reads it
    Workspace    the cells' folders of a root, and the scheduling plane beside them
    Experiment   the definition and the workspace together, and the operator's
                 verbs on the experiment: prepare, init, smoke, verb, reset_trace

THE DEFINITION

One root runs one experiment: the directory `EXPERIMENT_DIR` names (config,
default `<root>/experiment`) is a Python package the engine loads BY PATH and
registers as `experiment`, so the definition's own modules import each other
relatively and the frozen bring-ups reach it as `-m experiment.variants`
whatever directory it lives in. Loading a second definition into the same
process is refused; tests that need one call `unload()` first.

The experiment's variants are files, one per variant:
`<experiment>/variants/<id>.toml` (fae/cell/variants/files.py). A variant
is one complete set of what the agent is given and how its work is judged;
the set of files is the set of variants.

The definition's `__init__.py` declares, all optional:

    NAME                 a short name
    GATE                 a Gate (default: one arrangement)
    verifier_class()     -> the Verifier subclass (fae/cell/verify.py: one
                            verify(ctx) -> Verdict, EXCLUSIVE — the lock the
                            engine holds around every run — and FILES; what it
                            owes is on the base class; a function: `verifier`
                            is the package)
    verbs()              -> {name: callable} hooks the engine's own verbs call
                            ("reference_cell" for smoke, "selftest")
    commands()           -> {name: callable(argv) -> exit code} the
                            experiment's own operator commands, run as
                            `cli.py experiment verb NAME [ARGS...]`; each
                            parses its own arguments
    taint_rules          rules(cell, workspace, metrics, it_text, v_text,
                            rc_text, verdict) -> (taints, warns, fields):
                            what a rig fault looks like in this experiment's
                            evidence, read through the Cell; `workspace` (a
                            Workspace) holds its sibling cells
                            (fae/scoring/validate.py runs the engine's own
                            rules beside it)
    report_text(cell)    -> the verifier's per-attempt reports, concatenated,
                            for the taint rules ("" by default)
    POOLED_MODELS        {model id: scoreboard row label} for the results table
    report_summary(cells, metrics_of, delta, metrics) -> {key: value} the
                            experiment adds to the aggregate's summary (its
                            own gaps between variants, its reading notes); the
                            engine hands it every scored cell, its per-group
                            metric function, its None-safe delta and the
                            metric names
    CONFIG               {KEY: (toml section, name, default, "path"|"str")}
                            — machine-local settings the experiment needs,
                            read from fae.toml / the environment into
                            the config and exported to every child; a path
                            default may name "{experiment}"
    fingerprint_trees(conf)  -> directories whose *.py are hashed into the
                            verify fingerprint beside the experiment tree
    (agents' images: the base is the experiment root's Dockerfile.agent-base,
    else the engine's; each variant's layer over it is its [authoring] tools
    directory — fae/cell/image.py)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "experiment"

AGENTS_FILE = "agents.toml"
AGENT_CLIS = ("claude", "agy", "opencode", "testagent")
AGENT_KEYS = {"cli", "model", "effort"}
BUILTIN_AGENTS = {"testagent": {"cli": "testagent", "model": "testagent"}}


def load_agents(path):
    """{tag: entry} from an agents file's `[agents.<tag>]` tables; none when
    the file is absent. A malformed entry is refused, naming the tag."""
    try:
        import tomllib
    except ModuleNotFoundError:          # Python < 3.11
        import tomli as tomllib
    path = Path(path)
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        doc = tomllib.load(f)
    extra = set(doc) - {"agents"}
    if extra:
        raise ValueError(f"{path}: unknown top-level key(s) {sorted(extra)}; agents go under [agents.<tag>]")
    out = {}
    for tag, entry in (doc.get("agents") or {}).items():
        unknown = set(entry) - AGENT_KEYS
        if unknown:
            raise ValueError(f"{path}: agent {tag!r}: unknown key(s) {sorted(unknown)}")
        if entry.get("cli") not in AGENT_CLIS:
            raise ValueError(f"{path}: agent {tag!r}: cli must be one of {AGENT_CLIS}")
        if not entry.get("model"):
            raise ValueError(f"{path}: agent {tag!r}: no model")
        out[tag] = dict(entry)
    return out


@dataclass(frozen=True)
class Gate:
    """The arrangements one attempt must pass, all of them, in the order the
    engine seeds (`rotate`: the first one moves with the attempt number so an
    agent never sees the same first timeline twice in a row)."""

    arrangements: tuple = (None,)
    rotate: bool = True
    feedback_note: str = ""

    @property
    def arity(self):
        return len(self.arrangements)


class Definition:
    """Typed view of a loaded definition module, with the engine's defaults."""

    def __init__(self, module, path):
        self.module = module
        self.path = Path(path)
        self.name = getattr(module, "NAME", self.path.name)
        self._subjects = None
        self._agents = None

    @property
    def agents(self):
        """{tag: {"cli", "model"[, "effort"]}}: the agents the experiment
        compares, from `<experiment>/agents.toml`, plus the engine's scripted
        agent. The tag names every cell id."""
        if self._agents is None:
            self._agents = {**BUILTIN_AGENTS, **load_agents(self.path / AGENTS_FILE)}
        return self._agents

    @property
    def variants(self):
        """{id: class}, read from the variant files on first use."""
        if self._subjects is None:
            from fae.cell.variants import files
            self._subjects = files.load(self.path)
        return self._subjects

    @property
    def ids(self):
        """Every variant, the retired included (their cells stay readable)."""
        return tuple(self.variants)

    @property
    def active(self):
        """The variants cells are scheduled for: every one not retired."""
        return tuple(i for i, c in self.variants.items() if not c.RETIRED)

    def variant(self, vid):
        return self.variants.get(vid)

    def label_of(self, vid):
        s = self.variant(vid)
        return s.LABEL if s else vid

    def lock_of(self, vid):
        """The exclusive lock a variant's cells hold for their lifetime, or None."""
        s = self.variant(vid)
        return s.LOCK if s else None

    @property
    def gate(self):
        return getattr(self.module, "GATE", None) or Gate()

    @property
    def config_keys(self):
        return dict(getattr(self.module, "CONFIG", {}) or {})

    def fingerprint_trees(self, conf):
        fn = getattr(self.module, "fingerprint_trees", None)
        return [str(p) for p in (fn(conf) if fn else [])]

    @property
    def taint_rules(self):
        return getattr(self.module, "taint_rules", None)

    def report_text(self, cell):
        fn = getattr(self.module, "report_text", None)
        return fn(cell) if fn else ""

    @property
    def pooled_models(self):
        return dict(getattr(self.module, "POOLED_MODELS", {}) or {})

    def report_summary(self, cells, metrics_of, delta, metrics):
        """What the experiment adds to the aggregate's summary block."""
        fn = getattr(self.module, "report_summary", None)
        return dict(fn(cells, metrics_of, delta, metrics) or {}) if fn else {}

    def verifier_class(self):
        """The experiment's Verifier subclass, from `verifier_class()`."""
        from fae.cell.verify import Verifier
        fn = getattr(self.module, "verifier_class", None)
        if fn is None:
            raise RuntimeError(f"FATAL: {self.path} declares no verifier_class()")
        cls = fn()
        if not (isinstance(cls, type) and issubclass(cls, Verifier)):
            raise TypeError(f"{self.path}: verifier() must return a Verifier subclass, got {cls!r}")
        return cls

    @property
    def exclusive(self):
        """The verifier's EXCLUSIVE; None when no verifier is declared."""
        if getattr(self.module, "verifier_class", None) is None:
            return None
        return self.verifier_class().EXCLUSIVE

    @property
    def verbs(self):
        fn = getattr(self.module, "verbs", None)
        return dict(fn()) if fn else {}

    @property
    def commands(self):
        fn = getattr(self.module, "commands", None)
        return dict(fn()) if fn else {}


_loaded: dict = {"def": None}
_lock = threading.Lock()


def load(path):
    """The definition under `path`, loaded once. A different path while one
    is loaded is an error, not a swap."""
    path = Path(path).resolve()
    with _lock:
        cur = _loaded["def"]
        if cur is not None:
            if cur.path == path:
                return cur
            raise RuntimeError(f"one root, one experiment: {cur.path} is loaded, "
                               f"refusing {path}")
        init = path / "__init__.py"
        if not init.is_file():
            raise FileNotFoundError(f"FATAL: no experiment definition at {init}")
        module = sys.modules.get(PACKAGE)
        if module is None or Path(getattr(module, "__file__", "") or "").resolve() != init:
            # by path, registered under the package name so the definition's
            # relative imports and `-m experiment...` see one object
            spec = importlib.util.spec_from_file_location(
                PACKAGE, init, submodule_search_locations=[str(path)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[PACKAGE] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(PACKAGE, None)
                raise
        _loaded["def"] = Definition(module, path)
        return _loaded["def"]


def for_config(path):
    """The definition the config builds against: the loaded one, else `path`."""
    return _loaded["def"] if _loaded["def"] is not None else load(path)


_current: dict = {"x": None}


def current():
    """The experiment this process runs, built on first use from the
    environment: REPO_ROOT (else the working directory) and WORKSPACES_DIR."""
    if _current["x"] is None:
        _current["x"] = Experiment()
    return _current["x"]


def workspace():
    """The workspace this process runs on: current().workspace."""
    return current().workspace


def definition():
    """The definition this process runs: current().definition."""
    return current().definition


def set_current(experiment):
    """Make `experiment` the one this process runs (tests only); returns the
    previous one."""
    prev, _current["x"] = _current["x"], experiment
    return prev


def unload():
    """Forget the loaded definition (tests only: a process runs one experiment)."""
    with _lock:
        _loaded["def"] = None
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
            del sys.modules[name]


# --- cell ids -------------------------------------------------------------------

def cell_id(agent, variant, rep, task="T1", effort="high", smoke=False):
    """<agent>[_<effort>][_smoke]_<variant>_<task>_r<rep>; effort="" omits it."""
    prefix = agent + (f"_{effort}" if effort else "") + ("_smoke" if smoke else "")
    return f"{prefix}_{variant}_{task}_r{rep}"


_AGENT_RE = re.compile(r"^[a-zA-Z0-9-]+$")
_TASK_RE = re.compile(r"^T\d$")
_REP_RE = re.compile(r"^r(\d+)$")


def parse_cell_id(cid):
    """<agent>_<effort>[_smoke]_<variant>_<task>_r<rep> as (agent, variant,
    task, rep), or None.

    Agent and effort carry no underscore; the variant may, so it is whatever
    lies between the effort and the last two tokens — and it must be one of
    the experiment's variants: a name from another experiment is not a cell
    of this one."""
    t = cid.split("_")
    if len(t) < 5:
        return None
    rep = _REP_RE.match(t[-1])
    if not rep or not _TASK_RE.match(t[-2]) or not _AGENT_RE.match(t[0]):
        return None
    i = 3 if t[2] == "smoke" else 2
    variant = "_".join(t[i:-2])
    if not variant or variant not in definition().variants:
        return None
    return t[0], variant, t[-2], rep.group(1)


# --- the workspace ------------------------------------------------------------

def matches(cid, sel):
    """The one selector language: `all`, a whole cid, or a run of whole
    `_`-separated tokens (`sonnet`, an arm, `..._r1`). Anchored at token
    boundaries, so `r1` never matches `r10` and `son` matches nothing."""
    if sel in ("all", "*") or cid == sel:
        return True
    return f"_{cid}_".find(f"_{sel}_") >= 0


def is_blanket(selectors):
    """A selection naming `all`: standing operator decisions (roster or manual
    pauses, a cancel) survive it, and yield only to a cell or agent named."""
    return any(s in ("all", "*") for s in selectors)


class Workspace:
    """The cells' folders of one root (`path`; WORKSPACES_DIR names another
    tree) and the scheduling plane (fae/plane.py: queues, conduct, locks,
    transitions) under `plane`, the root's whatever `path` is.
    `parse(cid)` says whether a folder is a cell of this experiment."""

    def __init__(self, root, path=None, *, plane=None, parse=None):
        from fae import plane as _plane
        self.root = Path(root)
        if path is None:
            path = os.environ.get("WORKSPACES_DIR") or _plane.base(self.root)
        self.path = Path(path)
        self.plane = Path(plane) if plane else _plane.base(self.root)
        self._parse = parse
        self.conduct = self.plane / _plane.CONDUCT
        self.locks = self.plane / _plane.LOCKS
        self.transitions = self.plane / _plane.TRANSITIONS

    def parse(self, cid):
        return (self._parse or parse_cell_id)(cid)

    @property
    def queues(self):
        """The queues (fae/queues.py) the plane holds: the cell-id grammar
        names their specs, and a sealed cell is refused."""
        from fae import plane as _plane
        from fae.queues import Queues
        return Queues(self.plane / _plane.QUEUES, locks=self.locks, cell_id=cell_id,
                      refuse=self._refusal)

    def _refusal(self, cid):
        # a sealed cell's spec could only ever be refused: a stuck queue entry
        c = self.cell(cid)
        return f"SEALED — {c.seal_record().replace(chr(9), ' ') or 'sealed'}" if c.sealed else None

    def cells(self):
        """Every cell with a folder here, sorted."""
        try:
            return sorted(p.name for p in self.path.iterdir() if p.is_dir() and self.parse(p.name))
        except OSError:
            return []

    def select(self, *selectors):
        """The cells any selector matches."""
        return [c for c in self.cells() if any(matches(c, s) for s in selectors)]

    def queued_cells(self, *selectors):
        """The cells with a pending spec that any selector matches, with a
        folder here or not."""
        return self.queues.pending_cids(lambda c: any(matches(c, s) for s in selectors))

    def cell(self, cid, task=None, variant=None, rep=1, agent=None, reference=False):
        """The cell `cid` on this workspace's plane. Named by what it runs
        (`variant`), it may not exist yet: what a start prepares."""
        from fae.cell.cell import Cell
        plane = dict(workspaces=self.path, root=self.root, queues=self.queues,
                     locks=self.locks, transitions=self.transitions)
        if variant is None:
            return Cell(cid, **plane)
        return Cell.new(cid, task or "T1", variant, rep, agent=agent, reference=reference,
                        **plane)

    def named_cell(self, cid, variant=None):
        """The cell `cid`, its identity read from its id where its folder does
        not record it."""
        p = self.parse(cid)
        return self.cell(cid, p[2] if p else "T1", variant or (p[1] if p else ""),
                         p[3] if p else 1)


# --- the experiment -------------------------------------------------------------

class Experiment:
    """The experiment one root runs: its definition and its workspace, and the
    operator's verbs on it as a whole. The scheduling (admitting cells,
    supervising them) is the Conduct's; one cell's verbs are its Cell's."""

    SMOKE_DIR = "ws-smoke.nosync"

    def __init__(self, root=None, workspace=None):
        from fae import paths
        self.root = Path(root or paths.root())
        self.workspace = workspace or Workspace(self.root)

    def cell(self, cid, task=None, variant=None, rep=1, agent=None, reference=False,
             workspaces=None):
        """The cell `cid` on this experiment's workspace, or on the root
        `workspaces` names (on the same plane)."""
        ws = self.beside(workspaces) if workspaces else self.workspace
        return ws.cell(cid, task, variant, rep, agent=agent, reference=reference)

    @property
    def definition(self):
        """The definition this process runs: the one loaded, else this root's
        (fae/cell/config.py: EXPERIMENT_DIR). One process, one experiment."""
        if _loaded["def"] is not None:
            return _loaded["def"]
        from fae.cell import config as _config
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        return load(_config.experiment_dir(self.root))

    def prepare(self, agent, reps=1, task="T1", fresh=False):
        """Seed the matrix's workspaces for `agent` (`reps` reps of every
        active variant) and launch nothing: each cell prepares itself
        (Cell.prepare), as at every start. `fresh` moves an existing workspace
        aside first (safe_wipe; never a delete). Returns how many."""
        from fae.cell.cell import Busy
        n = 0
        for rep in range(1, reps + 1):
            for vid in self.definition.active:
                cid = cell_id(agent, vid, rep, task)
                try:
                    ws = self.workspace.cell(cid, task, vid, rep, agent=agent).prepare(fresh=fresh)
                except Busy:
                    print(f"  skipped {cid}: held by another process")
                    continue
                print(f"  prepared {ws}")
                n += 1
        print(f"Prepared every active variant: {n} workspace(s).")
        return n

    def init(self, experiment=""):
        """Write fae.toml at the root with every key at its default (the
        engine's keys, the caps for the locks the variants declare, the
        machine-local keys the experiment declares), then the operator edits
        it. Refuses to overwrite one. Returns the file written."""
        from fae.cell import config as _config
        target = self.root / _config.TOML
        if target.exists():
            sys.exit(f"init: {target} exists — set [paths] experiment_dir there (or "
                     f"EXPERIMENT_DIR in the environment), or move it aside first")
        if experiment:
            # the file is rendered for THAT experiment: its locks, its CONFIG keys
            os.environ["EXPERIMENT_DIR"] = experiment
        target.write_text(_config.render_default_toml(self.definition, experiment or None))
        print(f"wrote {target} — edit [paths] if the siblings are elsewhere")
        return target

    def verb(self, name, argv):
        """One of the experiment's own commands (its definition's commands()),
        `argv` passed through; no name lists them. Returns its exit code."""
        try:
            commands = self.definition.commands
        except FileNotFoundError as e:
            sys.exit(f"verb: {e} — `python3 cli.py experiment check` names what is missing")
        if not name:
            if not commands:
                print("this experiment defines no commands (commands() in its definition)")
            for n, fn in sorted(commands.items()):
                doc = (fn.__doc__ or "").strip().splitlines()
                print(f"  {n:16s} {doc[0] if doc else ''}")
            return 0
        if name not in commands:
            sys.exit(f"verb: {name!r} is not a command of this experiment "
                     f"({', '.join(sorted(commands)) or 'it defines none'})")
        return commands[name](list(argv))

    # --- smoke: the reference through the pipeline -------------------------

    @property
    def smoke_workspaces(self):
        """Where every smoke cell lives: `experiment smoke`'s and the SMOKE=1
        cells an experiment's own verbs run; never among scored cells."""
        return self.root / self.SMOKE_DIR

    def smoke_variants(self):
        """One active variant per distinct way of being judged: variants that
        differ only in what the agent reads share a reference, a run, an infra
        and a verify image, so one smoke covers them all."""
        out, seen = [], set()
        d = self.definition
        for vid in d.active:
            cls = d.variant(vid)
            key = (str(cls.REFERENCE), repr(sorted(cls.RUN.items())), cls.INFRA,
                   cls.ACCESS_INFRA, repr(sorted(cls.PARAMS.items())), str(cls.IMAGE_DIR),
                   cls.LOCK)
            if key not in seen:
                seen.add(key)
                out.append(vid)
        return out

    def beside(self, path):
        """Another workspace root on this experiment's plane (the smoke cells')."""
        return Workspace(self.root, path, plane=self.workspace.plane, parse=self.workspace._parse)

    def reference_cell(self, cid, vid, rep, workspaces):
        """A Cell over a REFERENCE workspace (the variant's template and inputs
        with its known answer laid over, no agent), constructed only:
        prepare() seeds it."""
        return self.beside(workspaces).cell(cid, "T1", vid, rep, reference=True)

    @staticmethod
    def smoke_verdict(cell):
        """(green, one-line verdict): the ledger decides, as for any cell;
        metrics.json only localizes the break (the stage, the log to read)
        when the verifier's metrics carry the fields read here."""
        ws = cell.ws
        L = cell.read_ledger() if cell.has_ledger else {}
        m = cell.read_metrics() or None
        e2e = f"{(m or {}).get('e2e_pass')}/{(m or {}).get('e2e_total')}"
        # no ledger verdict: the verifier's own green, when its metrics carry one
        green = L.get("verdict") == "green" if L.get("verdict") else \
            bool(m and m.get("e2e_green") and m.get("scaling_ok") is not False)
        if green:
            if m and "k6_available" in m and not m["k6_available"]:
                return True, (f"GREEN e2e {e2e} — but K6 MISSING, load + scaling gate "
                              f"not measured (install k6)")
            return True, (f"GREEN at attempt {L['green_at']}" if L.get("green_at")
                          else f"GREEN e2e {e2e}")
        if L.get("halt_cause"):
            return False, f"HALT — {L['halt_cause']}. See {ws}/verifier.log"
        if m is None:
            return False, "NO METRICS — the verify never wrote metrics.json (harness break)"
        if m.get("e2e_green") and m.get("scaling_ok") is False:
            return False, (f"SCALING FAILED — e2e {e2e} passed but the policy did not "
                           f"scale ({m.get('scaling_why') or ''}). See {ws}/verify.log")
        stage = m.get("stage_failed") or ""
        where = {"deploy": f"DEPLOY FAILED — deploy.sh non-zero. See {ws}/deploy.log",
                 "nostart": f"NEVER STARTED — infra/readiness. See {ws}/deploy.log",
                 "e2e": f"E2E FAILED ({e2e}). See {ws}/verify.log",
                 "k6": f"K6 STAGE ISSUE. See {ws}/k6.log"}
        return False, where.get(stage, f"NOT GREEN (stage={stage or '?'}). See {ws}/verify.log")

    def smoke(self, variants="", only="", rep=1, full_gate=False):
        """Pipeline check, NOT a scored run: one REFERENCE cell per variant
        through the driver's own entrypoint — prepare fresh (the reference laid
        over the template), then `python3 -m fae.cell T1 <variant> <rep> --stub
        <empty>`: no agent, one attempt, the gate. Exercises the bring-ups, the
        contract, the probes and the sampler exactly as a scored cell would.

        Cells are tagged ref_high_smoke_* and live in smoke_workspaces, so
        nothing under the scored tree is touched. Without `variants`, one
        variant per distinct way of being judged (smoke_variants). One
        canonical arrangement per variant by default (a pipeline check);
        `full_gate` runs the whole gate. Exits 0 iff every variant is green;
        the per-variant verdict names the log to read.
        """
        import tempfile
        import time
        from fae.driver import check
        from fae.driver.conduct import Conduct
        chosen = [v for v in (variants.split(",") if variants else self.smoke_variants())
                  if not only or only in v]
        if not chosen:
            sys.exit(f"smoke: no variant matches --only {only!r}")
        smoke_ws = self.smoke_workspaces
        print("=== SMOKE MODE: agent=ref — pipeline check, NOT a scored run "
              f"(cells tagged ref_high_smoke_*, in {smoke_ws.name}) ===")
        # each variant's own preflight, so a missing daemon, tool or image is
        # named before any infra is spent
        if check.probe_variants(chosen):
            sys.exit("SMOKE ABORTED: a variant refused this host — see hooks.log lines above")
        env = dict(os.environ, WORKSPACES_DIR=str(smoke_ws), AGENT="ref",
                   SMOKE="1", REFERENCE="1",
                   PYTHONPATH=str(self.root) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        env.setdefault("EFFORT", "high")
        if not full_gate:
            # gate_shapes reads SHAPE_GATE off the loaded config's raw values
            # (fae/cell/config.py seeds it from this env var); anything but
            # "all" is the single canonical arrangement.
            env["SHAPE_GATE"] = "one"
        empty = Path(tempfile.mkdtemp(prefix="stub-empty-"))
        results = []
        for vid in chosen:
            cid = cell_id("ref", vid, rep, "T1", effort=env["EFFORT"], smoke=True)
            print(f"\n=== CELL {cid} — prepare -> verify", flush=True)
            t0 = time.time()
            try:
                verbs = self.definition.verbs
                c = (verbs["reference_cell"](cid, vid, rep, smoke_ws)
                     if "reference_cell" in verbs else
                     self.reference_cell(cid, vid, rep, smoke_ws))
                c.prepare(fresh=True)
            except (OSError, RuntimeError, FileNotFoundError) as e:
                print(f"    VERDICT: PREPARE FAILED — {e}")
                results.append((vid, False, f"PREPARE FAILED — {e}"))
                continue
            print(f"  prepared: {c.ws}", flush=True)
            from fae.cell.cell import Cell
            slots, why = Conduct().slots_for(c, wait=True)
            if slots is None:
                print(f"    VERDICT: NOT ADMITTED — {why}")
                results.append((vid, False, f"NOT ADMITTED — {why}"))
                continue
            try:
                p = subprocess.run(c.process_argv() + ["--stub", str(empty)], cwd=str(self.root),
                                   env=dict(env, **{Cell.SLOT_FDS_ENV: slots.handover()}),
                                   pass_fds=tuple(slots.fds()))
            finally:
                slots.close()
            green, verdict = self.smoke_verdict(self.beside(c.ws.parent).cell(c.ws.name))
            green = green and p.returncode == 0
            if p.returncode != 0:
                verdict += f" [driver rc={p.returncode}]"
            print(f"    VERDICT: {verdict}  ({time.time() - t0:.0f}s)", flush=True)
            results.append((vid, green, verdict))
        print("\n=== SMOKE SUMMARY ===")
        for vid, green, verdict in results:
            print(f"  {'ok  ' if green else 'FAIL'} {vid:24s} {verdict}")
        if all(g for _, g, _ in results):
            print("  PIPELINE OK on every variant.")
            sys.exit(0)
        print("  PIPELINE BROKE — see the per-variant VERDICT above; each names the "
              "log to read.", file=sys.stderr)
        sys.exit(1)

    # --- results: actions on one workspace ----------------------------------

    def _workspace_of(self, cell):
        root = Path(cell.ws).parent
        return self.workspace if root == self.workspace.path else self.beside(root)

    def validate_cell(self, cell):
        """Validate one DONE `cell` against the engine's and this experiment's
        taint rules (fae/scoring/validate.py); the doc is also written as its
        validation.json. Raises Busy while the cell is held."""
        from fae.scoring import validate as _validate
        return _validate.validate_cell(cell, self._workspace_of(cell))

    def finished(self, cell, workspace=None):
        """`cell`'s outcome (green, failed, revoked) when it is DONE and not
        cancelled, else None. Status ranks a recorded outcome above liveness,
        so the host's facts (heartbeat, process table, queue) never change it."""
        parsed = (workspace or self._workspace_of(cell)).parse(cell.cid)
        if not parsed or not cell.has_ledger:
            return None
        st = cell.status(parsed, self.definition.gate.arity, None,
                         looping=lambda: False, queued=lambda: False)
        return st["why"] if st["state"] == "DONE" and st["why"] != "cancelled" else None

    def validate(self, selector="all", workspace=None):
        """Validate the finished cells of `workspace` (this experiment's by
        default) that `selector` matches: [(cid, outcome, doc)], doc None for
        a cell another process holds."""
        from fae.cell.cell import Busy
        from fae.scoring import validate as _validate
        ws = workspace or self.workspace
        out = []
        for cid in ws.select(selector or "all"):
            c = ws.cell(cid)
            why = self.finished(c, ws)
            if why is None:
                continue
            try:
                doc = _validate.validate_cell(c, ws)
            except Busy:
                doc = None
            out.append((cid, why, doc))
        return out

    def score(self, selector=None, workspace=None, on_validated=None, on_cell=None,
              on_failed=None):
        """Validate, then score the finished cells of `workspace` that
        `selector` matches (all by default), each into its score.json
        (fae/scoring/score_cell.py, in-process). `on_validated(results)` gets
        validate()'s results before any cell is scored, `on_cell(i, n, cid)`
        runs before each cell, `on_failed(cid, line)` after each failure.
        Returns (scored, [(cid, one-line error)])."""
        from fae.scoring import score_cell as _sc
        ws = workspace or self.workspace
        validated = self.validate(selector or "all", ws)
        if on_validated:
            on_validated(validated)
        todo = [cid for cid, _why, _doc in validated]
        n_ok, failed = 0, []
        for i, cid in enumerate(todo):
            if on_cell:
                on_cell(i, len(todo), cid)
            err = io.StringIO()
            # one cell's scoring failure is that cell's, never the sweep's
            try:
                with contextlib.redirect_stderr(err):
                    rc = _sc.score_one(cid, cell=ws.cell(cid))
            except Exception as e:
                rc = 1
                err.write(f"{type(e).__name__}: {e}")
            if rc == 0:
                n_ok += 1
                continue
            tail = err.getvalue().strip().splitlines()
            failed.append((cid, tail[-1][:100] if tail else "(no output)"))
            if on_failed:
                on_failed(*failed[-1])
        return n_ok, failed

    AGGREGATE_SWITCHES = ("include_tainted", "tainted_cells_details", "sort_discrepancy",
                          "sort_significant")

    def aggregate(self, workspace=None, variant=None, where=(), impl=None,
                  allow_stale=False, **switches):
        """Print the scoreboard of `workspace`'s scored cells
        (fae/scoring/aggregate.py, a process of its own on that workspace).
        `switches` are AGGREGATE_SWITCHES by name. Raises CalledProcessError
        when aggregate refuses (a stale score.json, without allow_stale)."""
        unknown = set(switches) - set(self.AGGREGATE_SWITCHES)
        if unknown:
            raise TypeError(f"unknown aggregate option(s): {', '.join(sorted(unknown))}")
        ws = workspace or self.workspace
        argv = [sys.executable, "-m", "fae.scoring.aggregate"]
        if allow_stale:
            argv.append("--allow-stale")
        if variant:
            argv += ["--variant", variant]
        for w in where or ():
            argv += ["--where", w]
        if impl:
            argv += ["--impl", impl]
        argv += ["--" + s.replace("_", "-") for s in self.AGGREGATE_SWITCHES if switches.get(s)]
        return subprocess.run(argv, check=True, env=dict(os.environ, WORKSPACES_DIR=str(ws.path)))

    # --- the transitions log, re-anchored -----------------------------------

    def observed_epoch(self):
        """Per-cell state as it is RIGHT NOW, in .tla/Runs.tla's own vocabulary.

        outcome  none | green | failed | revoked      (the ledger's verdict)
        intent   run  | paused | killed               (.paused / .cancelled)
        loop     none | idle | agent | verify         (live loop + heartbeat phase)
        attempts 0..Budget                            (judged attempts)
        slot     true | false                         (holds a work slot)
        verify   true | false                         (holds the verify lock)

        Every variable the replay needs, or a cell caught mid-flight by a reset
        is seeded into a state it cannot legally leave.
        """
        from fae.driver.conduct import Conduct
        loops = Conduct.loop_parents()
        slot_holders, verify_holder = self.slot_holders(), self.verify_holder()
        out = []
        for cid in self.workspace.cells():
            c = self.workspace.cell(cid)
            L = c.read_ledger()
            if c.cancelled:
                intent = "killed"
            elif (c.pause_request() or (None,))[0]:
                intent = "paused"
            else:
                intent = "run"
            slot = cid in slot_holders
            if cid in loops:
                hb = Conduct.heartbeat(c.ws, c)
                loop = _loop_of_phase((hb or {}).get("phase") or "", slot)
            else:
                loop = "none"
            out.append(dict(cid=cid, outcome=L["verdict"] or "none", intent=intent,
                            loop=loop, attempts=len(L["iters"]),
                            slot=slot, verify=cid == verify_holder))
        return out

    def slot_holders(self):
        """The cells holding a work slot now: the kernel says held, the note says who."""
        q = self.workspace.queues
        return {n[0] for n in (q.slot_note(s) for s in q.slot_files() if q.slot_held(s)) if n}

    def verify_holder(self):
        """The cell holding the verify lock now, or ''."""
        from fae.cell.cell import Cell
        locks = self.workspace.locks
        return Cell.shared_lock_holder(locks, "verify") if Cell.shared_lock_held(locks, "verify") \
            else ""

    def reset_trace(self, dry_run=False):
        """Re-anchor transitions.log on a RECORDED state.

        The live-trace check replays this log against .tla/Runs.tla from the
        spec's Init — every cell idle, intent "run", no outcome. That is only
        true of a genuinely cold fleet; replaying from Init at any other moment
        judges real events against a starting state that never existed. The
        state is therefore RECORDED here, at a moment the operator chose, as
        EPOCH lines a human can read and diff against the workspaces:

            <ts>  EPOCH  <cid>  outcome=green intent=paused loop=none attempts=1

        One line per cell, appended as one block (Cell.reanchor): the log is
        never rewritten, and the replay starts from its last EPOCH block. Not
        inferred by the replayer at read time: inference would silently absorb
        the mismatches the check exists to find.
        """
        cells = self.observed_epoch()
        default = dict(outcome="none", intent="run", loop="none", attempts=0,
                       slot=False, verify=False)
        differing = [c for c in cells if any(c[k] != v for k, v in default.items())]
        if dry_run:
            print(f"would append {len(cells)} EPOCH line(s) to {self.workspace.transitions.name}, "
                  f"{len(differing)} of them away from Init:")
            for c in differing[:10]:
                print(f"  {c['cid']}  {_epoch_fields(c)}")
            if len(differing) > 10:
                print(f"  ... and {len(differing) - 10} more")
            return
        if not cells:
            print("no cells: nothing to re-anchor")
            return
        from fae.cell.cell import Cell
        Cell.reanchor([(c["cid"], _epoch_fields(c)) for c in cells], root=self.root,
                      transitions=self.workspace.transitions)
        print(f"appended {len(cells)} EPOCH line(s) ({len(cells) - len(differing)} "
              f"cell(s) at Init)")
        live = [c for c in differing if c["loop"] != "none"]
        if live:
            print(f"NOTE: {len(live)} cell(s) have a LIVE loop — their in-flight "
                  f"attempt straddles the re-anchor:")
            for c in live:
                print(f"  {c['cid']} (loop={c['loop']})")


def _loop_of_phase(phase, slot_held):
    """Heartbeat phase -> the model's loop state.

    A cell is admitted holding its slot and starts in `agent`, so a slot in
    hand means Admit has happened: the cell is `agent`, whatever phase it is
    waiting in. Seeded `idle` with its slot held, it could never legally
    reach `agent`, and every verify it went on to run would replay as illegal.
    """
    if phase == "verify":
        return "verify"
    if not slot_held:
        return "idle"
    return "agent"


def _epoch_fields(c):
    return (f"outcome={c['outcome']} intent={c['intent']} loop={c['loop']} "
            f"attempts={c['attempts']} slot={str(c['slot']).lower()} "
            f"verify={str(c['verify']).lower()}")
