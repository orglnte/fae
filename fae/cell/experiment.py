"""The experiment definition, as the engine reads it.

One root runs one experiment: the directory `EXPERIMENT_DIR` names (config,
default `<root>/experiment`) is a Python package the engine loads BY PATH and
registers as `experiment`, so the definition's own modules import each other
relatively and the frozen bring-ups reach it as `-m experiment.variants`
whatever directory it lives in. Loading a second definition into the same
process is refused; tests that need one call `unload()` first.

The definition's `__init__.py` declares, all optional except `variant_classes`:

    NAME                 a short name
    variant_classes()    -> the Variant subclasses (a function: importing
                            them pulls the infra modules in, and the
                            config reads this file before any of that)
    MATRIX               {arm: [variants]} — derived from the variants'
                            CONDITIONS when absent
    RETIRED              (arm, ...) registered for the cells they already
                            have, never minted again: outside MATRIX, their
                            SEED_DOCS pins kept so a resumed cell still seeds
    SEED_DOCS            {(arm, condition): (doc file, min lines)}
    GATE                 a Gate (default: one arrangement)
    verifier_class()     -> the Verifier subclass (fae/cell/verify.py: one
                            verify(ctx) -> Verdict, EXCLUSIVE — the lock the
                            engine holds around every run, "rig" for a
                            singleton infra — and FILES; what it owes is
                            on the base class; a function, like
                            variant_classes: `verifier` is the package)
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
    reference_workspace(arm) -> the workspace name the grader compares a
                            green cell against, or None
    POOLED_MODELS        {model id: scoreboard row label} for the results table
    report_summary(cells, metrics_of, delta, metrics) -> {key: value} the
                            experiment adds to the aggregate's summary (its
                            own gaps between arms, its reading notes); the
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
    else the engine's; each arm's layer over it is its variant's
    AGENT_IMAGE_DIR — fae/cell/image.py)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "experiment"


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

    @property
    def variants(self):
        """{arm: class}, built on first use."""
        if self._subjects is None:
            fn = getattr(self.module, "variant_classes", None)
            classes = tuple(fn()) if fn else ()
            self._subjects = {c.ARM: c for c in classes}
        return self._subjects

    @property
    def arms(self):
        return tuple(self.variants)

    def variant(self, arm):
        return self.variants.get(arm)

    def tech_of(self, arm):
        """The variant's TECH; an unknown arm is its own tech (fixture arms)."""
        s = self.variant(arm)
        return s.TECH if s else arm

    def docs_of(self, arm):
        """The name the arm's api docs carry: the variant's DOCS, else its tech."""
        s = self.variant(arm)
        return (s.DOCS or s.TECH) if s else arm

    def lock_of(self, arm):
        """The exclusive lock a variant's cells hold for their lifetime, or None."""
        s = self.variant(arm)
        return s.LOCK if s else None

    @property
    def matrix(self):
        m = getattr(self.module, "MATRIX", None)
        if m is not None:
            return m
        return {arm: list(getattr(c, "CONDITIONS", ())) for arm, c in self.variants.items()}

    @property
    def retired(self):
        return tuple(getattr(self.module, "RETIRED", ()))

    @property
    def seed_docs(self):
        return getattr(self.module, "SEED_DOCS", {})

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

    def reference_workspace(self, arm):
        fn = getattr(self.module, "reference_workspace", None)
        return fn(arm) if fn else None

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
