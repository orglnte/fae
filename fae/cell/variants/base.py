"""The variant interface, the process/cksum helpers every infra shares, and
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
    """Provisioning could not give the cell its infra. A rig fault: the
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
    """One variant of the experiment, for one cell. The data is the variant
    file's (fae/cell/variants/files.py builds a subclass per file); the
    methods are the variant's infra: what is provisioned around its program
    for the cell's life and for each arrangement, and the probes that say
    whether it is there. `cell` is the Cell (cid, ws, root, conf).

    A variant's program runs only inside its infra (a container of an
    image, a dind daemon, a kind cluster), never on the host
    (fae/cell/verify.py)."""

    ID = ""
    SOURCE = None           # the variant file
    LABEL = ""              # what reports show; the id when the file names none
    RETIRED = False         # keeps its cells, never scheduled again
    FACTORS = {}            # the experimental factors it is a level of
    # [authoring]: what the agent gets
    TEMPLATE = ()           # directories merged into the workspace, in order
    INPUTS = {}             # workspace path -> source file the agent reads
    # (exact relpaths, directory prefixes) the agent may write; every other
    # seeded file is fixed and healed before a verdict. Required: a cell of
    # a variant that leaves it None is refused.
    AUTHORING_SURFACE = None
    AGENT_IMAGE_DIR = None  # the agent's image layer (a Dockerfile `FROM $BASE`)
    ACCESS_INFRA = False    # the agent's container is connected to the cell's infra
    # [verify]: how the work is judged
    IMAGE_DIR = None        # the verify container's layer, FROM the verifier's image
    REFERENCE = None        # the known answer, laid over the template for smoke
    RUN = {}                # how the verifier runs the artifacts (secrunner.for_variant)
    # [infra]
    LOCK = None             # the lock a cell holds setup to teardown; None: none
    LOCK_SLOTS = 1          # its default cap when the config names none
    PARAMS = {}             # the infra class's own settings
    # {kind: name prefix} — the infra a reaper may discover by scanning
    # (containers, clusters) for a cell that left no live loop.
    INFRA_PREFIXES = {}

    @classmethod
    def agent_image_context(cls, conf):
        """[(host path, name)] staged beside AGENT_IMAGE_DIR's Dockerfile at
        build time (an SDK's sources)."""
        return []

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
        agent-container env (DOCKER_NET/KUBE_MOUNT), empty when the agent is not connected to the cell's infra."""
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

    @classmethod
    def runs_own_image(cls):
        """Whether its program runs in an image of its own ([verify.run]
        image or image_dir) rather than the verify image."""
        return bool(cls.RUN.get("image") or cls.RUN.get("image_dir"))

    def infra_ok(self):
        """The host can carry this variant at all. False halts the cell
        without spending an attempt. A variant whose program runs in an image
        of its own needs the daemon and that image, built here when it is a
        Dockerfile directory."""
        if not self.runs_own_image():
            return True
        if subprocess.run(["docker", "info"], capture_output=True).returncode:
            self.log("HALT[infra]: docker unreachable")
            return False
        from ..infra import secrunner
        try:
            secrunner.ensure_run_image(type(self), log=self.log)
        except RuntimeError as e:
            self.log(f"HALT[infra]: {e}")
            return False
        return True

    def infra_alive(self):
        """The variant's infra answers RIGHT NOW: asked before every
        arrangement and again after a charged fail, so infra that died under
        the measurement voids the arrangement instead of scoring as a build
        verdict. A variant whose program runs in an image of its own needs
        the daemon; any other declares its own probe, or the preflight
        (`liveness_declared`) halts its cells before an attempt is spent."""
        return self.runs_own_image() and daemon_answers(["docker", "version"])

    @classmethod
    def stray(cls, live, workspaces):
        """Infra of this variant that only a scan can find (no name carries
        the cid): [(kind, ident, owner cid)] whose owner is not in `live`,
        anchored at the workspace root so nothing outside the rig is ours."""
        return []

    @classmethod
    def sweep(cls):
        """Operator preflight: remove this variant's stale infra left by dead
        cells (`cli.py experiment infra`). Nothing by default."""

    @classmethod
    def infra_identities(cls, cid):
        """[(kind, name)] of the cell-lifetime infra a cell of this variant
        provisions, named as this class names it — what a reaper may look
        for after the cell is gone. Unnamed infra (found by scanning)
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
    """Whether `cls` has a liveness probe: its own infra_alive, or the
    engine's for a program in an image of its own. Without one every
    arrangement would be void, refunded, forever; a definition error,
    caught before the first attempt."""
    return cls.infra_alive is not Variant.infra_alive or cls.runs_own_image()


class NoopVariant(Variant):
    """The seam a fixture root uses: no provisioning, no infra demand,
    no network."""
    ID = "noop"

    def infra_alive(self):
        return True

    def network_up(self):
        pass

    def network_down(self):
        pass


# --- shared infra pieces ---------------------------------------------------


def write_env(path, pairs):
    Path(path).write_text("".join(f"{k}={v}\n" for k, v in pairs))
