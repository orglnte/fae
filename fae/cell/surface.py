"""The authorable surface of a cell's artifacts.

The seeded files the agent may not change are recorded in .skeleton_manifest
(relative path, line count, sha256). The seal marks them read-only, which is
a deterrent only: the agent is root in its container. The guarantee is
heal-then-check before every verdict — heal restores a changed fixed file
from the skeleton and re-baselines its row, check asserts that nothing
outside the surface still differs. Scoring reads the same manifest to
separate the authored lines from the seed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

MANIFEST = ".skeleton_manifest"


def authorable(treatment):
    """(exact relpaths, dir prefixes) the agent may write for this arm — the
    variant's AUTHORABLE, or the engine's default for an arm no variant
    declares."""
    from . import experiment as _experiment
    from .variants.base import Variant
    s = _experiment.current().variant(treatment)
    exact, prefixes = (s or Variant).AUTHORABLE
    return tuple(exact), tuple(prefixes)


class Surface:

    def __init__(self, artifacts, treatment):
        self.artifacts = Path(artifacts)
        self.tech = str(treatment).split("_")[0]
        self.exact, self.prefixes = authorable(treatment)
        self.manifest = self.artifacts / MANIFEST

    def is_authorable(self, rel):
        return rel in self.exact or rel.startswith(self.prefixes)

    def record(self):
        """Write the manifest from the tree as seeded: (rel, lines, sha) rows."""
        rows = []
        for p in sorted(self.artifacts.rglob("*")):
            if not p.is_file() or p.name == MANIFEST:
                continue
            data = p.read_bytes()
            rows.append((p.relative_to(self.artifacts).as_posix(),
                         data.count(b"\n"), hashlib.sha256(data).hexdigest()))
        self._write(rows)
        return rows

    def rows(self):
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

    def heal(self, common, overlay):
        """Restore every fixed file that differs from its row, from the current
        seed (the variant's overlay wins over the task's skeleton), re-seal
        it and re-baseline its row. Returns the restored relpaths."""
        common, overlay = Path(common), Path(overlay)
        rows, restored = [], []
        for rel, lines, sha in self.rows():
            if not self.is_authorable(rel):
                dst = self.artifacts / rel
                ok = dst.is_file() and hashlib.sha256(dst.read_bytes()).hexdigest() == sha
                if not ok:
                    for src in (overlay / rel, common / rel):
                        if src.is_file():
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

    def check(self):
        """Fixed files that still differ from their row: 'rel (modified)' /
        'rel (deleted)'. Empty when the tree has no manifest."""
        if not self.manifest.exists():
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
        return out
