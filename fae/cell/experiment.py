"""The experiment definition, as the engine reads it.

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
    taint_rules          rules(ws, workspaces, metrics, it_text, v_text,
                            rc_text, verdict) -> (taints, warns, fields):
                            what a rig fault looks like in this experiment's
                            evidence (fae/driver/validate.py runs the engine's
                            own rules beside it)
    report_text(ws)      -> the verifier's per-attempt reports, concatenated,
                            for the taint rules ("" by default)
    reference_workspace(variant) -> the workspace name the grader compares a
                            green cell against, or None
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

import importlib.util
import os
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
            from .variants import files
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

    def report_text(self, ws):
        fn = getattr(self.module, "report_text", None)
        return fn(ws) if fn else ""

    def reference_workspace(self, vid):
        fn = getattr(self.module, "reference_workspace", None)
        return fn(vid) if fn else None

    @property
    def pooled_models(self):
        return dict(getattr(self.module, "POOLED_MODELS", {}) or {})

    def report_summary(self, cells, metrics_of, delta, metrics):
        """What the experiment adds to the aggregate's summary block."""
        fn = getattr(self.module, "report_summary", None)
        return dict(fn(cells, metrics_of, delta, metrics) or {}) if fn else {}

    def verifier_class(self):
        """The experiment's Verifier subclass, from `verifier_class()`."""
        from .verify import Verifier
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


def current(root=None, env=None):
    """The definition this process runs: the one already loaded, else the
    root's (REPO_ROOT, else the engine's own) through the config's
    EXPERIMENT_DIR rule."""
    from . import config as _config
    if _loaded["def"] is not None:
        return _loaded["def"]
    env = os.environ if env is None else env
    from fae import paths
    root = Path(root or env.get("REPO_ROOT") or paths.root())
    return load(_config.experiment_dir(root, env))


def unload():
    """Forget the loaded definition (tests only: a process runs one experiment)."""
    with _lock:
        _loaded["def"] = None
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
            del sys.modules[name]
