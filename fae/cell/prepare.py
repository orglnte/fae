"""Seed one cell's workspace: skeleton, task materials, manifest, agent repo.

Idempotent. It does not clobber the seed or the agent's authored files, and it
does not wipe iterations.log, unless `fresh` is passed — which never deletes
either: safe_wipe MOVES the old workspace aside for review.

One implementation, three callers: Cell.prepare() at every run start,
`cli.py rig prepare` over the matrix, and main() for the
operator (`python3 -m fae.cell.prepare TASK TREATMENT CONDITION [REP]`).
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


def task_dir(root, cfg=None):
    """The shared task: skeleton/ (every variant's common files) and the prompts."""
    return Path((cfg or {}).get("TASK_DIR") or Path(root) / _config.DEFAULT_EXPERIMENT_DIR / "task")


def seed_dir(treatment):
    """The variant's own seed tree (Variant.seed_root)."""
    cls = _experiment.current().variant(treatment)
    if cls is None:
        raise FileNotFoundError(f"no variant named {treatment!r} in the experiment")
    return cls.seed_root()


def tech_for(treatment):
    """The arm's tech, which names its skeleton overlay and the fallback
    project layout; an undeclared arm is its own tech."""
    from . import experiment as _experiment
    return _experiment.current().tech_of(treatment)


def docs_for(treatment):
    """The name the arm's api docs carry (`any.<docs>[.<condition>].api.md`):
    the variant's DOCS, else its tech."""
    from . import experiment as _experiment
    return _experiment.current().docs_of(treatment)


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


def seed_skeleton(task, treatment, artifacts, condition, cfg):
    """Copy the task's skeleton + the variant's overlay + the docs, then
    record the manifest.

    The overlay (the variant's seed/overlay, optional) wins on collision.
    Docs are the variant's, named <task|any>.<tech|docs|treatment>.<kind>:
    the prompt is the task's, project_layout is per-treatment falling back to
    the tech's, and the api doc is per-docs-name (Variant.DOCS, else the
    tech) with an optional condition.
    """
    artifacts = Path(artifacts)
    common = task_dir(ROOT, cfg) / "skeleton"
    tech = tech_for(treatment)
    docs = docs_for(treatment)
    seed = seed_dir(treatment)
    overlay = seed / "overlay"
    if not common.is_dir():
        raise FileNotFoundError(f"missing skeleton dir {common}")

    prompt = task_dir(ROOT, cfg) / f"{task}.PROMPT.md"
    layout = seed / f"{task}.{treatment}.project_layout.md"
    if not layout.is_file():
        layout = seed / f"{task}.{tech}.project_layout.md"
    api = seed / f"any.{docs}.api.md"
    cond_doc = seed / f"any.{docs}.{condition}.api.md"
    if cond_doc.is_file():
        api = cond_doc
    for f in (prompt, layout, api):
        if not f.is_file():
            raise FileNotFoundError(f"missing task material {f}")

    artifacts.mkdir(parents=True, exist_ok=True)
    _copy_tree(common, artifacts)
    if overlay.is_dir():             # a tech with nothing of its own seeds common alone
        _copy_tree(overlay, artifacts)
    shutil.copy2(prompt, artifacts / "TODO.md")
    (artifacts / "docs").mkdir(exist_ok=True)
    shutil.copy2(layout, artifacts / "docs" / "project.md")
    shutil.copy2(api, artifacts / "docs" / f"{tech}.md")

    surface = Surface(artifacts, treatment)
    surface.seal(surface.record())
    _seed_repo(artifacts, task, treatment)
    return api


def _seed_repo(artifacts, task, treatment):
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
    git("commit", "-q", "-m", f"seed: task {task}, {treatment}")


_runs_cache = {}


def runs_module(root=None):
    """The engine's fae/driver/common.py, loaded by path from the ENGINE's root
    (never the cell's: an experiment repo imports the engine, it does not
    contain it). cell_id has ONE definition, and re-encoding it here is how
    a cell ends up in one workspace and is read from another."""
    from fae.driver import common
    return common


def expected_seed_doc(treatment, condition, root=None):
    """The doc this combination MUST be seeded with (the experiment's
    SEED_DOCS), or None when the matrix does not define it.

    A fallback to the arm's base doc is correct for conditions that carry no
    prose of their own and CATASTROPHIC otherwise: the cell would run on the
    base doc while being labelled with the condition it never received.
    """
    from . import experiment as _experiment
    pinned = _experiment.current().seed_docs.get((treatment, condition))
    return None if pinned is None else pinned[0]


def prepare(cid, task, treatment, condition, rep, workspaces, root=ROOT,
            fresh=False, impl="bash", model_version="?", cfg=None):
    workspaces = Path(workspaces)
    cfg = cfg if cfg is not None else _config.load(root)
    ws = workspaces / cid
    if fresh:
        moved = safe_wipe(ws, workspaces)
        if moved is not None:
            _log_retire(root, cid, moved)
    (ws / "artifacts").mkdir(parents=True, exist_ok=True)
    (ws / "PROMPT.md").write_text(PROMPT)

    tech = tech_for(treatment)
    docs = docs_for(treatment)
    seed = seed_dir(treatment)
    api = seed / f"any.{docs}.api.md"
    cond_doc = seed / f"any.{docs}.{condition}.api.md"
    if condition != "reference" and cond_doc.is_file():
        api = cond_doc

    if condition != "reference":
        want = expected_seed_doc(treatment, condition, root)
        if want is None and os.environ.get("ALLOW_UNPINNED_SEED") != "1":
            raise RuntimeError(
                f"{treatment}/{condition} is not a defined matrix combination "
                f"— no seed doc is pinned, so the cell would run on "
                f"{api.name!r} while being labelled {condition!r}")
        if want is not None and api.name != want:
            raise RuntimeError(
                f"{treatment}/{condition} must be seeded with {want!r} but "
                f"resolved to {api.name!r} — the condition doc is missing or "
                f"renamed, and the cell would silently get the base doc")

    surface = Surface(ws / "artifacts", treatment)
    if surface.has_manifest():
        # A resumed cell keeps its tree; only the seal is brought up to date.
        surface.seal()
    else:
        seed_skeleton(task, treatment, ws / "artifacts", condition, cfg)
        if condition == "reference":
            ref = seed / "reference" / "overlay"
            if not ref.is_dir():
                raise FileNotFoundError(f"no reference impl for {tech} ({ref})")
            for p in sorted(ref.rglob("*")):
                dst = ws / "artifacts" / p.relative_to(ref)
                if p.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        dst.chmod(0o644)
                    shutil.copy2(p, dst)

    doc_lines = api.read_text(errors="replace").count("\n")
    (ws / "cell.env").write_text(
        f"CELL_ID={cid}\nTASK={task}\nTREATMENT={treatment}\n"
        f"CONDITION={condition}\nREPEAT={rep}\nDOC_LINES={doc_lines}\n"
        f"ATTEMPT_BUDGET={cfg.get('ATTEMPT_BUDGET', 10)}\nIMPL={impl}\n"
        f"MODEL_VERSION={model_version}\n")

    ledger = ws / "iterations.log"
    if fresh or not ledger.exists():
        ledger.write_text(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t"
                          f"PREPARED\t{cid}\tby=prepare_cell\n")
    return ws


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if len(a) < 3:
        print("usage: prepare TASK TREATMENT CONDITION [REPEAT]", file=sys.stderr)
        return 1
    task, treatment, condition = a[0], a[1], a[2]
    rep = a[3] if len(a) > 3 else "1"
    root = _paths.root()
    cfg = _config.load(root)
    common = runs_module(root)
    cid = common.cell_id(os.environ.get("MODEL", "?"), treatment, condition, rep,
                         task, effort=os.environ.get("EFFORT", "high"),
                         smoke=bool(os.environ.get("SMOKE")))
    ws = prepare(cid, task, treatment, condition, rep,
                 workspaces=cfg.get("WORKSPACES_DIR"), root=root,
                 fresh=bool(os.environ.get("FRESH")),
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
