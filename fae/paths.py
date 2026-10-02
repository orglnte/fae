"""Where the engine and the experiment root are.

ENGINE is this package's own directory: its instruments, its testagent, its
agent-container context live here. ROOT is the experiment root the engine
runs for — the checkout holding fae.toml, experiment/, workspaces — one root,
one experiment: REPO_ROOT in the environment, else the working directory.
"""
from __future__ import annotations

import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parent


def root():
    """The experiment root: REPO_ROOT in the environment, else the working
    directory — `fae experiment run` is run from the experiment repo's root."""
    return Path(os.environ.get("REPO_ROOT") or Path.cwd()).resolve()


ROOT = root()
