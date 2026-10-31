"""Each agent's credential: one per agent (a CLI), shared by every model it
runs, kept in the agent's credentials home on this machine.

`experiment credentials AGENT` sets one up, the secret read from a hidden
prompt and written owner-only; `experiment run` checks every agent's before
it admits anything. What each CLI needs is its client's
(fae/cell/clients/: credential_missing, setup_credential); a client that
cannot start (clients.Incomplete) reads as a missing credential, so the run
names it before any cell.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fae.cell import clients
from fae.experiment import config as _config


def _image_absent(image):
    """Why `image` cannot be run here; empty when it is present."""
    r = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return "" if r.returncode == 0 else f"the agent image {image} is not built (cli.py experiment check)"


class Credential:
    """The credential of the agent `name` (CLI `cli`) in its home."""

    def __init__(self, name, cli, home, image):
        self.name, self.cli, self.home, self.image = name, cli, Path(home), image

    def missing(self):
        """Why the credential is not usable; empty when it is."""
        try:
            return clients.of(self.cli).credential_missing(self.home)
        except clients.Incomplete as e:
            return str(e)
        except OSError as e:
            return f"unreadable: {e}"

    def setup(self, ask=getpass.getpass, run=subprocess.run, say=print):
        """Set the credential up interactively; returns why it is still not
        usable, empty when it is."""
        try:
            client = clients.of(self.cli)
        except clients.Incomplete as e:
            return str(e)
        why = ((client.SETUP_IN_IMAGE and _image_absent(self.image))
               or client.setup_credential(self.home, self.image, ask, run, say))
        return why or self.missing()

    def proven(self, model, log_dir):
        """Start one claude agent on this credential with the cell's limits and
        read its init line: empty when it ran confined, else why not. Only
        claude carries a proof; for another CLI it reports what it lacks."""
        if self.cli != "claude":
            return self.missing()
        from fae.cell.cell import Cell
        why = self.missing() or _image_absent(self.image)
        if why:
            return why
        conf = _config.Config({"AGENT_CLI": "claude", "AGENT_MODEL": model,
                               "AGENT_HOME": str(self.home), "AGENT_IMAGE": self.image,
                               "STREAM_AGENT": "1", "AGENT_TIMEOUT_S": "300"}, {})
        d = Path(tempfile.mkdtemp(dir=log_dir))
        why = Cell.trial(conf, f"credcheck-{self.name}", d)
        if not why:
            shutil.rmtree(d, ignore_errors=True)
        return why


def of(definition, root, toml, image):
    """{name: Credential} of every agent the experiment declares."""
    out = {}
    for name, agent in definition.agents.items():
        if agent["cli"] == "testagent":
            continue
        home = _config.agent_home(name, definition, toml)
        home = home if os.path.isabs(home) else f"{root}/{home}"
        out[name] = Credential(name, agent["cli"], home, image)
    return out


def ensure(creds, exclude=(), interactive=None, ask=getpass.getpass, run=subprocess.run,
           say=print, confirm=input):
    """Check every credential but the excluded; when one is not usable and a
    person is at the terminal, offer to set it up. Returns {name: why} of
    those still not usable."""
    interactive = sys.stdin.isatty() if interactive is None else interactive
    unknown = sorted(set(exclude) - set(creds))
    if unknown:
        raise ValueError(f"--exclude-agent: no agent {unknown}; the agents are {sorted(creds)}")
    left = {}
    for name, cred in creds.items():
        if name in exclude:
            continue
        why = cred.missing()
        if why and interactive and confirm(f"{name}: {why}. Set it up now? [y/N] ").strip().lower() == "y":
            why = cred.setup(ask=ask, run=run, say=say)
        if why:
            left[name] = why
    return left
