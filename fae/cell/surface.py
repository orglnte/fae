"""The authorable surface of a cell's artifacts.

The seeded files are recorded in the workspace's .skeleton_manifest
(relative path, line count, sha256), beside artifacts/ rather than in it:
the agent's container mounts only artifacts/, so it cannot rewrite the
record it is checked against. The seal marks fixed files read-only, which is
a deterrent only: the agent is root in its container. The guarantee is
heal-then-check before every verdict — heal restores a changed fixed file
from the skeleton and re-baselines its row, evict moves every file that is
neither seeded nor authorable out of artifacts/, and check asserts that
nothing outside the surface is left. Scoring reads the same manifest to
separate the authored lines from the seed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

MANIFEST = ".skeleton_manifest"
# Written by the rig into artifacts/ (.git, the root .gitignore) or left by
# the agent's own tools; none of them can change what a build does.
RIG_OWNED = frozenset({".git", ".gitignore"})
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def authorable(vid):
    """(exact relpaths, dir prefixes) the agent may write for this variant:
    its [authoring] surface. A variant that is not one of the experiment's,
    or one that declares no surface, raises: a default would let one
    experiment's layout decide what another's agents may write."""
    from fae import experiment as _experiment
    s = _experiment.definition().variant(vid)
    if s is None:
        raise RuntimeError(f"no variant {vid!r}: it has no authorable surface")
    if s.AUTHORING_SURFACE is None:
        raise RuntimeError(f"variant {vid!r} declares no [authoring] surface: "
                           f"the files the agent may write must be declared")
    exact, prefixes = s.AUTHORING_SURFACE
    return tuple(exact), tuple(prefixes)


def skeleton_shas(artifacts):
    """{relpath: sha256} of every fixed file seeded into `artifacts`, from its
    manifest; {} when there is none."""
    manifest = Path(artifacts).parent / MANIFEST
    out = {}
    if manifest.is_file():
        for line in manifest.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[parts[0]] = parts[2]
    return out


class Surface:

    def __init__(self, artifacts, vid):
        self.artifacts = Path(artifacts)
        self.exact, self.prefixes = authorable(vid)
        self.manifest = self.artifacts.parent / MANIFEST

    def is_authorable(self, rel):
        return rel in self.exact or rel.startswith(self.prefixes)

    def has_manifest(self):
        return self.manifest.exists()

    def record(self):
        """Write the manifest from the tree as seeded: (rel, lines, sha) rows."""
        rows = []
        for p in sorted(self.artifacts.rglob("*")):
            if not p.is_file() or self._ignored(p):
                continue
            data = p.read_bytes()
            rows.append((p.relative_to(self.artifacts).as_posix(),
                         data.count(b"\n"), hashlib.sha256(data).hexdigest()))
        self._write(rows)
        return rows

    def rows(self):
        self.has_manifest()
        out = []
        for line in self.manifest.read_text().splitlines():
            rel, n, sha = line.split("\t")
            out.append((rel, int(n), sha))
        return out

    def _write(self, rows):
        self.manifest.write_text("".join(f"{r}\t{n}\t{s}\n" for r, n, s in rows))

    def seal(self, rows=None):
        """Fixed files read-only; the exact authorable files opened (an earlier
        seal may have left one read-only)."""
        for rel, _n, _sha in (self.rows() if rows is None else rows):
            if self.is_authorable(rel):
                continue
            try:
                (self.artifacts / rel).chmod(0o444)
            except OSError:
                pass
        for rel in self.exact:
            p = self.artifacts / rel
            if p.is_file():
                p.chmod(0o644)

    def heal(self, sources):
        """Restore every fixed file that differs from its row, from the current
        seed (`sources`: workspace path -> the file it is seeded from), re-seal
        it and re-baseline its row. Returns the restored relpaths."""
        rows, restored = [], []
        for rel, lines, sha in self.rows():
            if not self.is_authorable(rel):
                dst = self.artifacts / rel
                ok = dst.is_file() and hashlib.sha256(dst.read_bytes()).hexdigest() == sha
                if not ok:
                    for src in (sources.get(rel),):
                        if src is not None and Path(src).is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            if dst.exists():
                                dst.chmod(0o644)      # the agent may have chmod'ed it odd
                            shutil.copy2(src, dst)
                            data = dst.read_bytes()
                            sha = hashlib.sha256(data).hexdigest()
                            lines = data.decode(errors="ignore").count("\n")
                            mode = os.stat(dst).st_mode
                            os.chmod(dst, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
                            restored.append(rel)
                            break
            rows.append((rel, lines, sha))
        if restored:
            self._write(rows)
        return restored

    def _ignored(self, p):
        """Rig-owned at the root, or a tool cache anywhere."""
        rel = p.relative_to(self.artifacts)
        return (rel.parts[0] in RIG_OWNED or p.suffix == ".pyc"
                or any(part in CACHE_DIRS for part in rel.parts[:-1]))

    def strays(self):
        """Files neither seeded nor authorable, as relpaths."""
        seeded = {rel for rel, _n, _sha in self.rows()}
        out = []
        for p in sorted(self.artifacts.rglob("*")):
            if p.is_dir() and not p.is_symlink():
                continue
            if self._ignored(p):
                continue
            rel = p.relative_to(self.artifacts).as_posix()
            if rel not in seeded and not self.is_authorable(rel):
                out.append(rel)
        return out

    def evict(self, dest):
        """Move every stray into dest (kept as evidence, never deleted) and
        drop the directories that leaves empty. Returns the moved relpaths."""
        if not self.has_manifest():
            return []
        moved = self.strays()
        for rel in moved:
            src, to = self.artifacts / rel, Path(dest) / rel
            to.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, to)
            d = src.parent
            while d != self.artifacts and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        return moved

    def check(self):
        """What is outside the surface after heal and evict: 'rel (modified)',
        'rel (deleted)' for fixed files, 'rel (outside surface)' for a stray.
        Empty when the tree has no manifest."""
        if not self.has_manifest():
            return []
        out = []
        for rel, _n, sha in self.rows():
            if self.is_authorable(rel):
                continue
            p = self.artifacts / rel
            if not p.is_file():
                out.append(f"{rel} (deleted)")
            elif hashlib.sha256(p.read_bytes()).hexdigest() != sha:
                out.append(f"{rel} (modified)")
        return out + [f"{rel} (outside surface)" for rel in self.strays()]
