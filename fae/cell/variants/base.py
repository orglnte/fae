"""The treatment interface, the process/cksum helpers every tech shares, and
the fixture seam (NoopVariant)."""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fae import paths as _paths

HARNESS = _paths.ENGINE
ROOT = _paths.ROOT


class HookFailure(RuntimeError):
    """Provisioning could not give the cell its substrate. A rig fault: the
    driver halts the cell for review without spending an attempt."""


def _run(argv, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(argv, **kw)


def _ok(argv, **kw):
    return _run(argv, **kw).returncode == 0


_DAEMON_GONE = ("cannot connect", "connection refused", "no such file",
                "error during connect", "is the docker daemon running")


def daemon_answers(argv, timeout=15):
    """Whether a docker daemon answers `argv` (an `info`/`version` call).
    Only a hard connect failure is a dead daemon; a slow one (timeout) or
    any other error is alive — a build that fails under a slow daemon is a
    fail, a build that fails under a gone daemon is a rig fault."""
    try:
        p = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return True
    if p.returncode == 0:
        return True
    err = (p.stderr or "").lower()
    return not any(m in err for m in _DAEMON_GONE)


def cksum(text):
    """POSIX cksum of the text, the number the shell hooks hashed cell ids
    with — ports and cluster names must not move under a cell that resumes,
    and every recorded cell's were derived this way."""
    data = text.encode()
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CKSUM_TABLE[((crc >> 24) ^ b) & 0xFF]) & 0xFFFFFFFF
    n = len(data)
    while n:
        crc = ((crc << 8) ^ _CKSUM_TABLE[((crc >> 24) ^ (n & 0xFF)) & 0xFF]) & 0xFFFFFFFF
        n >>= 8
    return (~crc) & 0xFFFFFFFF


def _make_cksum_table():
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


_CKSUM_TABLE = _make_cksum_table()


class Variant:
    """The interface every arm answers. `cell` is the Cell (cid, ws, root,
    conf, condition); the class reads config through cell.conf.

    A variant's substrate is where the agent's program runs — for the
    verifier as much as for the agent: a judged program executes only
    inside it (a container of the variant's image, a dind daemon, a kind
    cluster), never on the host (fae/cell/verify.py)."""

    ARM = ""
    TECH = ""
    # The name the variant's api docs carry (`any.<DOCS>[.<condition>].api.md`),
    # when arms of one tech are told different things; empty = TECH.
    DOCS = ""
    # The name a reader sees for the arm's family (status, reports); empty =
    # TECH. Arms that share a TECH (substrate, image, lock) are told apart by it.
    LABEL = ""
    # The variant's own trees, beside its module unless declared: seed/ (what
    # the agent is handed — overlay/, its docs, reference/overlay/) and
    # verify/ (its bring-up scripts and contract), see seed_root/verify_root.
    SEED = None
    VERIFY = None
    # The exclusive lock a cell of this arm holds from setup to teardown
    # (None: bounded by the work slots alone), and the default cap on its
    # holders when the config names none (ARM_SLOTS_<LOCK>).
    LOCK = None
    LOCK_SLOTS = 1
    # The doc variants this arm is run under (the matrix, when the
    # definition declares no MATRIX of its own).
    CONDITIONS = ()
    # {kind: name prefix} — the substrate a reaper may discover by scanning
    # (containers, clusters) for a cell that left no live loop.
    SUBSTRATE_PREFIXES = {}
    # The directory holding this variant's Dockerfile: the tools its cells
    # are verified with, layered FROM the verifier's image; None = the
    # verifier's image as it is.
    IMAGE_DIR = None
    # The directory holding this arm's agent layer (a Dockerfile `FROM $BASE`,
    # the agents' base image): the tools and SDK its agents author with, and
    # only those; None = the base as it is. Arms of one TECH share it.
    AGENT_IMAGE_DIR = None

    @classmethod
    def agent_image_context(cls, conf):
        """[(host path, name)] staged beside AGENT_IMAGE_DIR's Dockerfile at
        build time (an SDK's sources)."""
        return []
    # The authorable surface: (exact relpaths, directory prefixes) the agent
    # may write; every other seeded file is fixed and healed before a verdict.
    # Required: a cell of a variant that leaves it None is refused.
    AUTHORING_SURFACE = None

    def __init__(self, cell):
        self.cell = cell
        self.cid = cell.cid
        self.ws = Path(cell.ws)
        self.root = Path(cell.root)
        self.conf = cell.conf

    def cfg(self, key, default=None):
        v = self.conf.get(key) if self.conf is not None else None
        if v in (None, ""):
            v = os.environ.get(key, default)
        return v

    def log(self, msg):
        line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}  {msg}\n"
        # inside a verify the workspace is read-only; its own directory is not
        where = Path(os.environ.get("FAE_VERIFY_OUT") or self.ws)
        try:
            with (where / "hooks.log").open("a") as f:
                f.write(line)
        except OSError:
            pass
        sys.stderr.write(line)

    def fail(self, msg):
        self.log(msg)
        raise HookFailure(msg)

    # the interface: two phases, one pair each
    def author_setup(self):
        """What the agent needs while it authors, kept for every attempt.
        Called by the driver before the first attempt. Returns the
        agent-container env (DOCKER_NET/KUBE_MOUNT), empty for a sealed arm."""
        return {}

    def author_teardown(self):
        """Best-effort, idempotent, derivable from the cid alone: it runs even
        when author_setup died halfway."""

    def verify_setup(self, ctx, env):
        """What one arrangement of the judged artifacts runs on, brought up
        fresh. Called by the experiment's Verifier inside the verify
        container, over the daemon's socket; `env` is the arrangement's
        environment the verifier composed (its store, its ports, the cell's
        endpoint files). Raises on a rig fault; returns what the later
        stages must know (a kubeconfig path), or nothing."""
        return {}

    def verify_teardown(self, ctx, env):
        """The world reset after the arrangement: idempotent, runs whether
        verify_setup finished or not — in the same container, or in a fresh
        one of the same image when the verify was killed, so everything it
        needs is derivable from `ctx` alone."""

    def substrate_ok(self):
        """The host can carry this arm at all. False halts the cell without
        spending an attempt."""
        return True

    @classmethod
    def _own_dir(cls):
        import inspect
        return Path(inspect.getfile(cls)).resolve().parent

    @classmethod
    def seed_root(cls):
        """The agent-visible tree: overlay/ (over the task's skeleton), the
        docs (`T*.<tech|arm>.project_layout.md`, `any.<tech>[.<condition>].api.md`)
        and reference/overlay/ (the stub answer)."""
        return Path(cls.SEED) if cls.SEED else cls._own_dir() / "seed"

    @classmethod
    def verify_root(cls):
        """The verify side: the contract, and whatever verify_setup reads."""
        return Path(cls.VERIFY) if cls.VERIFY else cls._own_dir() / "verify"

    def substrate_alive(self):
        """The arm's provisioning substrate answers RIGHT NOW: asked before
        every arrangement and again after a charged fail, so a substrate
        that died under the measurement voids the arrangement instead of
        scoring as a build verdict. Every variant declares its own: the
        preflight (`liveness_declared`) halts a cell of one that does not,
        before an attempt is spent."""
        return False

    @classmethod
    def stray(cls, live, workspaces):
        """Substrate of this arm that only a scan can find (no name carries
        the cid): [(kind, ident, owner cid)] whose owner is not in `live`,
        anchored at the workspace root so nothing outside the rig is ours."""
        return []

    @classmethod
    def sweep(cls):
        """Operator preflight: remove this arm's stale substrate left by dead
        cells (`cli.py rig substrate`). Nothing by default."""

    @classmethod
    def substrate_identities(cls, cid):
        """[(kind, name)] of the cell-lifetime substrate a cell of this arm
        provisions, named as this class names it — what a reaper may look
        for after the cell is gone. Unnamed substrate (found by scanning)
        is not listed."""
        return []

    @classmethod
    def image_context(cls, conf):
        """[(host path, name)] copied beside the Dockerfile at build time;
        hashed into the image tag."""
        return []

    def image(self):
        """The image this cell is verified in, built when missing."""
        from .. import experiment as _experiment
        from .. import image as _image
        return _image.for_variant(type(self), _experiment.current(), self.conf, self.log)

    def network(self):
        """The cell's docker network, the engine's: every container of the
        cell — the verify, the store, a sidecar — is a DNS name on it."""
        from .. import image as _image
        return _image.cell_network(self.cid)

    def network_up(self):
        """Create the cell network before `setup` provisions onto it."""
        from .. import image as _image
        _image.network_up(self.cid)

    def network_down(self):
        """Remove the verify container and the cell network after
        `teardown`; idempotent."""
        from .. import image as _image
        _image.remove_container(_image.verify_container(self.cid))
        _image.network_down(self.cid)

    _tool_seq = itertools.count(1)

    def tool(self, argv, env=None, network=None, timeout=900):
        """Run `argv` in a throwaway container of this variant's image —
        a tool the host does not carry (kind, kubectl), called by the
        driver — over the daemon's socket, with the root and the workspace
        at their own paths and `env` on top of the config's exported keys.
        Returns the CompletedProcess (text)."""
        from .. import image as _image
        name = f"{_image.TOOL_PREFIX}{self.cid}-{os.getpid()}-{next(self._tool_seq)}"
        home = self.ws / ".tool-home"
        home.mkdir(parents=True, exist_ok=True)
        extra = dict(getattr(self.conf, "exported", None) or {})
        extra.update(env or {})
        extra.update(HOME=str(home), USER=_image.CONTAINER_USER)
        penv = _image.child_env(os.environ, **extra)
        mounts = _image.mounts_for(self.root, _paths.ENGINE.parent, self.ws)
        cmd = _image.run_argv(self.image(), name, argv, mounts=mounts, env=penv,
                              workdir=str(self.ws), network=network,
                              labels=(("fae-cell", self.cid),), extra=_image.HOST_ALIAS)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _image.remove_container(name)
            raise


def liveness_declared(cls):
    """Whether `cls` answers substrate_alive itself. The base answer is
    "dead", which would void every arrangement of the cell, refunded,
    forever; a variant that never declared a probe is a definition error,
    caught before the first attempt."""
    return cls.substrate_alive is not Variant.substrate_alive


class NoopVariant(Variant):
    """The seam a fixture root uses: no provisioning, no substrate demand,
    no network."""
    ARM = "noop"

    def substrate_alive(self):
        return True

    def network_up(self):
        pass

    def network_down(self):
        pass


# --- shared substrate pieces ---------------------------------------------------


def write_env(path, pairs):
    Path(path).write_text("".join(f"{k}={v}\n" for k, v in pairs))
