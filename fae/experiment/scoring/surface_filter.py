#!/usr/bin/env python3
"""THE definition of "does this file count as agent-authored surface",
used by score_cell.py for the authored-lines metric.

Names alone cannot decide it: an agent may download and unpack a vendor tree
into its workspace at runtime, and a compiled binary counted by its newline
bytes scores as a million authored lines. No name list catches a directory
the agent invents; binary content must be detected as binary.
"""
from __future__ import annotations

from pathlib import Path

from fae.cell.surface import MANIFEST

# Directory names that are never authored surface, whatever they contain.
IGNORE_PARTS = frozenset({
    "__pycache__", "node_modules", ".venv", "venv", "env", ".terraform",
    ".git", ".pulumi", ".claude", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

# Extensions that are never authored surface.
IGNORE_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".a", ".o", ".class",
    ".jar", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2",
    ".log", ".pid", ".bin", ".exe", ".wasm",
})

# Names that are rig bookkeeping, not artifacts.
IGNORE_NAMES = frozenset({MANIFEST})

_BINARY_SNIFF_BYTES = 8192


def is_binary(path: Path) -> bool:
    """True if the file looks binary: a NUL byte in the first 8 KiB, or a head
    that is not decodable as UTF-8. This is the check that catches a
    downloaded executable no extension or directory name would reveal."""
    try:
        head = path.open("rb").read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True          # unreadable => certainly not countable surface
    if b"\x00" in head:
        return True
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def countable(path: Path, root: Path, rel: str | None = None) -> bool:
    """Does `path` count as agent-authorable surface within `root`?

    Cheap checks first (name/suffix/parts), then the content sniff, so the
    common case never opens the file. A caller that already walked the tree
    may pass `rel` (the "/"-separated path relative to root) precomputed:
    deriving it here via Path.relative_to, per file, was the single largest
    CPU cost of a warm scoring sweep.
    """
    if rel is None:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            return False
    if not path.is_file():
        return False
    name = rel.rsplit("/", 1)[-1]
    if name in IGNORE_NAMES or path.name in IGNORE_NAMES:
        return False
    if any(part in IGNORE_PARTS for part in rel.split("/")):
        return False
    if path.suffix.lower() in IGNORE_SUFFIXES:
        return False
    return not is_binary(path)
