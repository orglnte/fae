"""Per-attempt git checkpoints for one cell — the provenance of what the agent
authored, attempt by attempt. Everything here returns a TREE hash: commit SHAs
carry a timestamp and are not comparable across runs; identical content hashes
identically, always.

WHY a repo beside the work tree, never inside it:
  The agent owns artifacts/ and may keep its own .git there. Writing the
  harness's checkpoints into that repo would touch the agent's history and
  index, and any git write inside artifacts/ used to defeat the mtime no-edit
  guard. So the checkpoint repo lives beside the work tree and never writes a
  byte inside it:

      GIT_DIR         = <ws>/.attempts.git
      GIT_WORK_TREE   = <ws>/artifacts
      GIT_INDEX_FILE  = <ws>/.attempts.index
      info/exclude    = .git/   (the agent's own repo is not our content)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Fixed identity: the harness authors every checkpoint, and a commit must never
# depend on the operator's global git config (a fresh machine may lack one).
_AUTHOR = ("fae", "harness@fae.local")


def _git_env(ws):
    ws = Path(ws).resolve()
    return dict(
        os.environ,
        GIT_DIR=str(ws / ".attempts.git"),
        GIT_WORK_TREE=str(ws / "artifacts"),
        GIT_INDEX_FILE=str(ws / ".attempts.index"),
        GIT_AUTHOR_NAME=_AUTHOR[0], GIT_AUTHOR_EMAIL=_AUTHOR[1],
        GIT_COMMITTER_NAME=_AUTHOR[0], GIT_COMMITTER_EMAIL=_AUTHOR[1],
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
    )


class Checkpoints:
    """The checkpoint chain for one cell's work tree. One implementation, one
    tree hash — the key every recorded cell's provenance is stored under."""

    def __init__(self, ws, root=None):
        self.ws = Path(ws)

    def _git(self, *args, check=True):
        p = subprocess.run(["git", *args], env=_git_env(self.ws),
                           cwd=str(self.ws.resolve()), capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({p.returncode}): "
                               f"{p.stderr.strip()}")
        return p.stdout.strip()

    def init(self):
        """Create the checkpoint repo if absent. Idempotent."""
        gitdir = self.ws.resolve() / ".attempts.git"
        if not (gitdir / "HEAD").exists():
            gitdir.parent.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "--initial-branch=attempts")
        # Written every time: an exclude file lost (or a repo restored from a
        # partial copy) would silently start checkpointing the agent's objects.
        info = gitdir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text(".git/\n")

    def tree(self):
        """Tree hash of the work tree as it stands — no commit, no HEAD move.
        This is the no-edit oracle: two identical trees have the same hash, and
        nothing else does. Staging into our own index leaves the agent's alone."""
        self.init()
        self._git("add", "-A", ".")
        return self._git("write-tree")

    def commit(self, label):
        """Checkpoint the work tree. An unchanged tree is still committed: the
        chain records that an attempt happened, and a missing commit would make
        "the agent changed nothing" indistinguishable from "the harness failed
        to checkpoint"."""
        t = self.tree()
        parent = self._git("rev-parse", "-q", "--verify", "HEAD", check=False)
        args = ["commit-tree", t, "-m", label]
        if parent:
            args += ["-p", parent]
        sha = self._git(*args)
        self._git("update-ref", "HEAD", sha)
        return t

    def restore(self, tree):
        """Put the work tree back to `tree` (full or abbreviated hash from this
        chain). The index is first set to the tree as it stands (so every file
        the agent added is tracked and therefore removed), then read-tree
        rewrites the work tree. Ignored files and the agent's own .git are never
        in the index and are left alone. Returns the tree hash afterwards — the
        caller checks it."""
        self.tree()
        self._git("read-tree", "--reset", "-u", f"{tree}^{{tree}}")
        return self.tree()

    @property
    def judged(self):
        """The last tree a verdict was CHARGED on. A usage-limit respawn inside
        an attempt and a voided verify both leave trees that were never judged;
        the cell writes this only at the charge."""
        try:
            return (self.ws / ".judged_tree").read_text().strip()
        except OSError:
            return ""

    @judged.setter
    def judged(self, sha):
        (self.ws / ".judged_tree").write_text(sha + "\n")

    def noedit(self):
        """True iff this attempt rebuilds byte-for-byte what was already judged.
        No reference at all means nothing has been judged yet, so the claim
        cannot be made and the attempt must not be burned."""
        ref = self.judged
        return bool(ref) and self.tree() == ref
