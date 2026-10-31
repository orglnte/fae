"""The agent clients a cell can run, one AgentClient subclass per CLI
(fae/cell/clients/base.py says what each must provide). `of` gives the
client a cell starts and refuses one that does not provide everything;
`reader` gives the class that reads a finished attempt's transcript, which
works for any known client.
"""
from __future__ import annotations

from .agy import Agy
from .base import AgentClient, Breach, Incomplete, Watch
from .claude import Claude
from .opencode import Opencode
from .testagent import Testagent

_CLASSES = {c.NAME: c for c in (Claude, Agy, Opencode, Testagent)}
NAMES = tuple(_CLASSES)

__all__ = ["AgentClient", "Breach", "Incomplete", "Watch", "NAMES", "of", "reader"]


def missing(cls):
    """What the client class `cls` leaves out of AgentClient."""
    out = sorted(cls.__abstractmethods__)
    return out + [a for a in ("NAME", "HOME", "MOUNT") if not getattr(cls, a, "")]


def of(cli) -> AgentClient:
    """The client `cli` a cell starts. Incomplete when it does not provide
    every guarantee, ValueError when there is no such client."""
    if cli not in _CLASSES:
        raise ValueError(f"no agent client {cli!r}; the clients are {', '.join(NAMES)}")
    cls = _CLASSES[cli]
    left = missing(cls)
    if left:
        raise Incomplete(f"the {cli} client cannot start: it does not provide "
                         f"{', '.join(left)} (fae/cell/clients/base.py)")
    return cls()


def reader(cli):
    """The class of client `cli`, for its classmethods `usage` and
    `client_seconds`; None for a client this engine does not know."""
    return _CLASSES.get(cli)
