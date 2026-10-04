"""The rig utilities: the store, the cache-backend guards, the fingerprint,
the venv manifest, the load shape — what lib.sh did for the verify, native.

Each function was ported from its lib.sh namesake and held byte-identical
to it until the bash rig retired (2026-08-29): same knobs (read from the env
mapping the caller passes, the way the shell helpers read their exported
config), same outputs, same failure shapes. The goldens that equality was
proven against are tests/test_rig_equivalence.py, and they are now the
contract; every recorded cell was measured by these numbers.
"""
from __future__ import annotations

import hashlib
import json
import locale
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from fae import paths as _paths  # noqa: E402

HARNESS = _paths.ENGINE
ROOT = _paths.ROOT


def _run(argv, input_text=None, timeout=None, env=None):
    p = subprocess.run(argv, input=input_text, capture_output=True,
                       text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collated(names):
    """Sorted the way a bash glob expands: locale collation from the
    environment, not byte order — the fingerprint depends on this order."""
    try:
        locale.setlocale(locale.LC_COLLATE, "")
        return sorted(names, key=locale.strxfrm)
    except locale.Error:
        return sorted(names)


def _shasum_lines(paths):
    """The `shasum -a 256 <files>` text: one "<hex>  <path>" line per readable
    file, unreadable ones skipped (the shell pipeline sends those to
    /dev/null and hashes what remains)."""
    out = []
    for p in paths:
        try:
            out.append(f"{_sha256_file(p)}  {p}\n")
        except OSError:
            pass
    return "".join(out)


# --- identity / mapping ------------------------------------------------------

def fp(root, env):
    """The verify-surface fingerprint: sha256 over the `shasum -a 256` text of
    every file in the experiment tree (EXPERIMENT_DIR: task package,
    variants, contracts, bring-ups, load profiles, instruments, daemon
    config), then the FP_EXTRA_FILES the config names (the SDK/daemon
    sources, every module of fae/cell and fae/experiment), in that order. Raises
    RuntimeError when the experiment tree is empty or an FP_EXTRA_FILES
    entry is missing — a silently skipped file would quietly shrink the
    guarded surface."""
    root = Path(root)
    extras = str(env.get("FP_EXTRA_FILES", "")).split()
    for f in extras:
        if not Path(f).is_file():
            raise RuntimeError(f"FATAL _fp: FP_EXTRA_FILES entry missing: {f}")
    exp = Path(env.get("EXPERIMENT_DIR") or root / "experiment")
    tree = _collated(str(p) for p in exp.rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts
                     and p.suffix != ".pyc" and p.name != ".DS_Store")
    if not tree:
        raise RuntimeError(f"FATAL _fp: no experiment tree under {exp}")
    return hashlib.sha256(_shasum_lines(tree + extras).encode()).hexdigest()


def free_port_from(port, span=200):
    """First port at/after `port` with no listener, or None. The hash picks
    the start so a cell keeps a stable port across resumes when it can."""
    port = int(port)
    for _ in range(span):
        if _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])[0] != 0:
            return port
        port += 1
    return None


def _mutex():
    from fae import mutex
    return mutex
