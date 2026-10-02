"""Seed one cell's workspace from its variant: the template directories, the
inputs the agent reads, the manifest, the agent's repo.

Idempotent. It does not clobber the seed or the agent's authored files, and it
does not wipe iterations.log, unless `fresh` is passed — which never deletes
either: safe_wipe MOVES the old workspace aside for review.

One implementation, three callers: Cell.prepare() at every run start,
`cli.py experiment prepare` over the matrix, and main() for the
operator (`python3 -m fae.cell.prepare TASK VARIANT [REP]`).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as _config
from . import experiment as _experiment
from .surface import Surface

from fae import paths as _paths  # noqa: E402

ROOT = _paths.ROOT

PROMPT = """You are a software engineer working in the repository at your current
directory. Read TODO.md and complete the task it describes. The project
documentation is under docs/.
"""

GITIGNORE = "__pycache__/\n*.pyc\n.skeleton_manifest\n*.log\n"

# A cell id ends in _r<N>; safe_wipe refuses anything else.
CELL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*_r[0-9]+$")


def variant_of(vid):
    cls = _experiment.current().variant(vid)
    if cls is None:
        raise FileNotFoundError(f"no variant named {vid!r} in the experiment")
    return cls


def safe_wipe(target, workspaces):
    """Two-stage delete: MOVE the workspace under .to_be_deleted/<ts>/.

    Refuses anything that is not an absolute path to a cell directory directly
    under `workspaces` — an empty or wrong variable must die here rather than
    expand into a tree wipe.
    """
    target, workspaces = Path(target), Path(workspaces)
    if not target.is_absolute():
        raise ValueError(f"safe_wipe: refusing {target!r} — not absolute")
    if target.parent != workspaces:
        raise ValueError(f"safe_wipe: refusing {target!r} — not directly under "
                         f"{workspaces}")
    if not CELL_ID_RE.match(target.name):
        raise ValueError(f"safe_wipe: refusing {target!r} — "
                         f"{target.name!r} is not a cell id")
    if not target.exists():
        return None
    tbd = workspaces / ".to_be_deleted" / f"{datetime.now():%Y%m%d-%H%M%S}"
    tbd.mkdir(parents=True, exist_ok=True)
    dest = tbd / target.name
    shutil.move(str(target), str(dest))
    return dest


def _transitions_log(root):
    return Path(os.environ.get("TRANSITIONS_LOG")
                or Path(root) / "workspaces.nosync" / ".orch" / "transitions.log")


def _log_retire(root, cid, moved):
    """A wiped workspace ends the cell under that id; the id's next events are
    a new cell. The conformance replay needs the boundary, or it judges the new
    cell's Spawn against the old cell's verdict."""
    log = _transitions_log(root)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\tRetire\t{cid}\tmoved={moved}\n")


def _copy_tree(src, dst):
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _lay(src, dst):
    """Copy the tree `src` over `dst`, replacing read-only files."""
    for p in sorted(Path(src).rglob("*")):
        d = Path(dst) / p.relative_to(src)
        if p.is_dir():
            d.mkdir(parents=True, exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.exists():
                d.chmod(0o644)
            shutil.copy2(p, d)


def seed(task, vid, artifacts):
    """The variant's template directories merged in order, then its inputs at
    their workspace paths; then the manifest and the seal."""
    from .variants import files
    cls = variant_of(vid)
    problems = files.problems(cls)
    if problems:
        raise FileNotFoundError(f"{vid}: " + "; ".join(problems))
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    for d in cls.TEMPLATE:
        _copy_tree(d, artifacts)
    for rel, src in sorted(cls.INPUTS.items()):
        dst = artifacts / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    surface = Surface(artifacts, vid)
    surface.seal(surface.record())
    _seed_repo(artifacts, task, vid)


def _seed_repo(artifacts, task, vid):
    """The workspace is the AGENT's own git repo: its commits land here, never
    in the study repo, and the seed is the initial commit."""
    (artifacts / ".gitignore").write_text(GITIGNORE)

    def git(*args, check=False):
        return subprocess.run(["git", "-C", str(artifacts), *args],
                              capture_output=True, text=True, check=check)

    if git("init", "-q", "-b", f"task/{task}").returncode != 0:
        git("init", "-q")
    git("config", "user.email", "agent@fae.local")
    git("config", "user.name", "fae agent")
    git("add", "-A")
    git("commit", "-q", "-m", f"seed: task {task}, {vid}")


_runs_cache = {}


def runs_module(root=None):
    """The engine's fae/driver/common.py, loaded by path from the ENGINE's root
    (never the cell's: an experiment repo imports the engine, it does not
    contain it). cell_id has ONE definition, and re-encoding it here is how
    a cell ends up in one workspace and is read from another."""
    from fae.driver import common
    return common


def prepare(cid, task, vid, rep, workspaces, root=ROOT, fresh=False, reference=False,
            impl="bash", model_version="?", cfg=None):
    """The cell's workspace, seeded from variant `vid`; with `reference` the
    variant's known answer is laid over the template (a smoke cell)."""
    workspaces = Path(workspaces)
    cfg = cfg if cfg is not None else _config.load(root)
    cls = variant_of(vid)
    ws = workspaces / cid
    if fresh:
        moved = safe_wipe(ws, workspaces)
        if moved is not None:
            _log_retire(root, cid, moved)
    (ws / "artifacts").mkdir(parents=True, exist_ok=True)
    (ws / "PROMPT.md").write_text(PROMPT)

    surface = Surface(ws / "artifacts", vid)
    if surface.has_manifest():
        # A resumed cell keeps its tree; only the seal is brought up to date.
        surface.seal()
    else:
        seed(task, vid, ws / "artifacts")
        if reference:
            if cls.REFERENCE is None or not Path(cls.REFERENCE).is_dir():
                raise FileNotFoundError(f"{vid} declares no reference ([verify] reference)")
            _lay(cls.REFERENCE, ws / "artifacts")

    (ws / "cell.env").write_text(
        f"CELL_ID={cid}\nTASK={task}\nVARIANT={vid}\nREPEAT={rep}\n"
        + ("REFERENCE=1\n" if reference else "")
        + f"ATTEMPT_BUDGET={cfg.get('ATTEMPT_BUDGET', 10)}\nIMPL={impl}\n"
        f"MODEL_VERSION={model_version}\n")

    ledger = ws / "iterations.log"
    if fresh or not ledger.exists():
        ledger.write_text(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t"
                          f"PREPARED\t{cid}\tby=prepare_cell\n")
    return ws


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if len(a) < 2:
        print("usage: prepare TASK VARIANT [REPEAT]   (REFERENCE=1: the variant's known answer)",
              file=sys.stderr)
        return 1
    task, vid = a[0], a[1]
    rep = a[2] if len(a) > 2 else "1"
    root = _paths.root()
    cfg = _config.load(root)
    common = runs_module(root)
    cid = common.cell_id(os.environ.get("MODEL", "?"), vid, rep,
                         task, effort=os.environ.get("EFFORT", "high"),
                         smoke=bool(os.environ.get("SMOKE")))
    ws = prepare(cid, task, vid, rep,
                 workspaces=cfg.get("WORKSPACES_DIR"), root=root,
                 fresh=bool(os.environ.get("FRESH")),
                 reference=os.environ.get("REFERENCE") == "1",
                 impl=os.environ.get("CELL_IMPL", "bash"),
                 # AGENT_MODEL is config, not exported env: the environment
                 # alone yields the lane name ("opus") rather than the pinned
                 # version ("claude-opus-5") this field records.
                 model_version=cfg.get("AGENT_MODEL")
                 or os.environ.get("MODEL", "?"), cfg=cfg)
    print(ws)
    return 0


if __name__ == "__main__":
    sys.exit(main())
