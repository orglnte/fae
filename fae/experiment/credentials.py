"""Each agent's credential: one per agent (a CLI), shared by every model it
runs, kept in the agent's credentials home on this machine.

`experiment credentials AGENT` sets one up, the secret read from a hidden
prompt and written owner-only; `experiment run` checks every agent's before
it admits anything. What each CLI needs:

claude    .oauth_token   a `claude setup-token` token, never a login (see
                         fae/cell/confinement.py); a login found there is
                         moved aside
opencode  opencode.key   the API key
agy       the home       a signed-in agy home (agy writes it at sign-in)
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fae.cell import confinement
from fae.experiment import config as _config


def _write_secret(path, value):
    """`value` into `path`, owner-only before a byte is written, in an
    owner-only directory; a symlink at `path` is refused."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.ftruncate(fd, 0)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip() + "\n")


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
            return self._missing()
        except OSError as e:
            return f"unreadable: {e}"

    def _missing(self):
        if self.cli == "claude":
            try:
                confinement.token(self.home)
            except confinement.Breach as e:
                return str(e)
            return ""
        if self.cli == "opencode":
            key = _config.opencode_key_file(self.home)
            return "" if key.is_file() and key.read_text().strip() else f"no API key in {key}"
        if self.cli == "agy":
            signed_in = self.home.is_dir() and any(self.home.iterdir())
            return "" if signed_in else f"no signed-in agy home at {self.home}"
        return ""

    def setup(self, ask=getpass.getpass, run=subprocess.run, say=print):
        """Set the credential up interactively; returns why it is still not
        usable, empty when it is."""
        if self.cli in ("claude", "agy"):
            absent = _image_absent(self.image)
            if absent:
                return absent
        if self.cli == "claude":
            say(f"{self.name}: a browser sign-in follows; copy the token it prints.")
            run(["docker", "run", "-it", "--rm", self.image, "claude", "setup-token"])
            token = ask("Paste the token (hidden): ").strip()
            if not token.startswith(confinement.TOKEN_PREFIX):
                return "that is not a `claude setup-token` token; nothing written"
            _write_secret(self.home / confinement.TOKEN_FILE, token)
            login = self.home / ".credentials.json"
            if login.exists():
                aside = self.home / ".to_be_deleted" / datetime.now().strftime("%Y%m%d-%H%M%S")
                aside.mkdir(parents=True)
                shutil.move(str(login), aside / login.name)
                say(f"{self.name}: the old login moved to {aside}")
        elif self.cli == "opencode":
            key = ask(f"{self.name}: the opencode API key (hidden): ").strip()
            if not key:
                return "no key given; nothing written"
            _write_secret(_config.opencode_key_file(self.home), key)
        elif self.cli == "agy":
            self.home.mkdir(parents=True, exist_ok=True)
            os.chmod(self.home, 0o700)
            say(f"{self.name}: sign in inside the container, then exit it.")
            run(["docker", "run", "-it", "--rm", "-v", f"{self.home}:/home/node/.gemini",
                 self.image, "agy"])
        return self.missing()

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
