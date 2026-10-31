"""What FAE needs of an agent client (a CLI: claude, agy, ...) to run it in a
cell, as one abstract class. A client is a subclass that provides every
method; one that leaves any out cannot be started (`clients.of` refuses it,
naming what is missing), so no cell runs on a client that skips a guarantee.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path


class Breach(RuntimeError):
    """The agent is, or would run, unconfined."""


class Incomplete(RuntimeError):
    """A client that does not provide every guarantee: it cannot start."""


class AgentClient(ABC):
    """One agent CLI as a cell runs it. Five guarantees, each a method or
    two below:

    1. Credential. The sign-in or key lives in the agent's credentials home
       on this machine, shared by every model the agent runs, and never
       enters a cell's home; the agent gets it by environment or a copy of
       exactly the files the CLI signs in with.
    2. Home. Each cell has its own home, staged fresh before every attempt
       from an allowlist: what the CLI needs to start and its confinement,
       nothing a sign-in session or an earlier agent left (memory,
       transcripts, settings). A shared or inherited home lets one agent
       read another's work, invisible in the results.
    3. Confinement. The agent reaches no web, no browser, no MCP server or
       plugin, and no tool beyond those the client keeps, by what the home
       and the command line say; and its own transcript proves it as the
       session runs, so a client update that widens the agent interrupts
       the cell instead of passing unseen.
    4. Invocation. The container command: the home mounted at `MOUNT`, the
       secret named on the command line and never its value, the model and
       the prompt.
    5. Accounting. What the client's transcript records of its run (turns,
       tokens, API time) and its own clock of the run, None where it records
       none, never zero.
    """

    NAME: str = ""
    HOME: str = ""    # the cell home's directory, under the workspace
    MOUNT: str = ""   # where the agent's container sees that home
    # The session proves its confinement in a stream-json transcript (method
    # `breach`), so it runs with stream_agent on, the transcript watched.
    PROVEN_BY_TRANSCRIPT: bool = True
    # Its credential is set up by running the agent image (setup_credential).
    SETUP_IN_IMAGE: bool = True

    # 1. Credential

    @abstractmethod
    def credential_missing(self, home) -> str:
        """Why the credentials home `home` cannot start this client; empty
        when it can."""

    @abstractmethod
    def setup_credential(self, home, image, ask, run, say) -> str:
        """Set the credential up in `home` interactively (`ask` reads a
        secret, `run` runs a command, `say` prints), the agent image `image`
        present; empty when done, else why nothing was written."""

    # 2. Home

    @abstractmethod
    def stage_home(self, conf, dest, root) -> None:
        """Make the cell's home `dest` afresh from the allowlist: what was
        there before goes. Raise RuntimeError when the credentials home
        (conf AGENT_HOME) cannot start the client."""

    @abstractmethod
    def check_home(self, home) -> None:
        """Raise Breach unless the staged home `home` holds the confinement
        and nothing that defeats it."""

    # 3. Confinement

    @abstractmethod
    def breach(self, init, tools_used) -> str:
        """What the session's transcript shows that confinement denies, or
        lacks to prove it: `init` is its init event, `tools_used` the tools
        its steps called so far (Watch). Empty when nothing. Called on every
        poll until the agent exits."""

    # 4. Invocation

    @abstractmethod
    def env(self, conf) -> dict:
        """The docker client's environment for the secret the container
        names (`docker_args`); raise Breach when the credential is not one
        that keeps the agent confined."""

    @abstractmethod
    def docker_args(self, home) -> list:
        """The `docker run` arguments before the image: the home mounted at
        MOUNT and the environment names the container takes."""

    @abstractmethod
    def command(self, conf, prompt, feedback) -> list:
        """The command the container runs, after the image: the CLI, its
        limits, the model (conf AGENT_MODEL), and `prompt` as one argument;
        `feedback` says /feedback is mounted for it to read."""

    # 5. Accounting

    @classmethod
    @abstractmethod
    def usage(cls, log) -> dict | None:
        """The run's figures (fae.cell.transcript.FIELDS) from the attempt
        log `log`; a field the client does not record is None. None when the
        client records none or the log has no result."""

    @classmethod
    @abstractmethod
    def client_seconds(cls, log) -> tuple | None:
        """The client's own account of how long its run spent in turns, and
        in how many turns it closed: (seconds, closings), else None."""


def refuse_dest(dest, suffix):
    """A cell home is wiped before staging, so its path is checked first."""
    if not (os.path.isabs(dest) and str(dest).endswith(suffix)):
        raise RuntimeError(f"REFUSING agent-home dest {dest!r} (not an absolute {suffix} path)")


def write_secret(path, value):
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


class Watch:
    """Reads a stream-json transcript as it grows: the session's init event,
    and the tools its steps called (agy's `step_update` events); each poll
    reads only the complete lines written since the last."""

    def __init__(self, log):
        self.log, self.offset, self.init = Path(log), 0, None
        self.tools_used = []

    def poll(self):
        """The init event once written (a malformed one reads as {}), else None."""
        try:
            with self.log.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return self.init
        end = chunk.rfind(b"\n")
        if end < 0:
            return self.init
        self.offset += end + 1
        for line in chunk[:end].split(b"\n"):
            squeezed = line.replace(b" ", b"")
            if self.init is None and (b'"subtype":"init"' in squeezed
                                      or b'"event":"init"' in squeezed):
                try:
                    self.init = json.loads(line)
                except ValueError:
                    self.init = {}
            elif b'"event":"step_update"' in squeezed:
                try:
                    step = json.loads(line).get("step_update") or {}
                except (ValueError, AttributeError):
                    continue
                tool = step.get("tool_name")
                if tool and tool not in self.tools_used:
                    self.tools_used.append(tool)
        return self.init


def events(log):
    """The JSON objects of a stream-json transcript, one per line; nothing
    when it cannot be read."""
    try:
        text = Path(log).read_text(errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            yield e


def sum_ints(values):
    """The sum, or None when any value is missing or not an int."""
    return sum(values) if all(isinstance(v, int) for v in values) else None
