"""The infra interface: what exists around a variant's program for one cell —
provisioned for the cell's life and for each arrangement — and the probes
that say whether it is there; plus the process/cksum helpers every infra
shares.

A variant file's `[infra] class` names a subclass of `Infra`; a variant
without one gets `DefaultInfra`. The engine instantiates it with the
variant (the file's data, fae/experiment/variants) and the cell; it reads what
the file says of it (`ACCESS_INFRA`, `PARAMS`) from `self.variant`.
"""
from __future__ import annotations

import itertools
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import fae.experiment
from fae import paths as _paths


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


def host_ports(cid, ranges):
    """{role: port} on the host's loopback for one cell: the same slot, by
    the hash of its id, in each of `ranges` ({role: range}). Two cells can
    hash to one slot."""
    slot = cksum(cid) % min(len(r) for r in ranges.values())
    return {role: r[slot] for role, r in ranges.items()}


def free_port_from(port, span=200):
    """First port at/after `port` with no listener on this host, or None. The
    hash picks the start so a cell keeps a stable port across resumes when
    it can."""
    port = int(port)
    for _ in range(span):
        if not _ok(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]):
            return port
        port += 1
    return None


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


def write_env(path, pairs):
    Path(path).write_text("".join(f"{k}={v}\n" for k, v in pairs))


class Infra:
    """The infra of one variant for one cell. `variant` is the variant class
    (its file's data), `cell` the Cell (cid, ws, root, conf).

    A variant's program runs only inside its infra (a container of an
    image, a dind daemon, a kind cluster), never on the host
    (fae/cell/verify.py)."""

    # {kind: name prefix} — the infra a reaper may discover by scanning
    # (containers, clusters) for a cell that left no live loop.
    PREFIXES = {}
    # The infra's own files in the cell's folder: ENV_FILE, the KEY=value
    # endpoints its setup records (the verify adopts the keys VERIFY_ADOPTS
    # names, every key when None); RUN_DIR, the state it keeps for the cell.
    ENV_FILE = None
    VERIFY_ADOPTS = None
    RUN_DIR = None

    def __init__(self, variant, cell):
        self.variant = variant
        self.cell = cell
        self.cid = cell.cid
        self.ws = Path(cell.ws)
        self.root = Path(cell.root)
        self.conf = cell.conf

    @property
    def env_file(self):
        return self.ws / self.ENV_FILE if self.ENV_FILE else None

    @property
    def run_dir(self):
        return self.ws / self.RUN_DIR if self.RUN_DIR else None

    def record_env(self, pairs):
        """Record the endpoints the verify and the agent reach (ENV_FILE)."""
        write_env(self.env_file, pairs)

    def env(self):
        """{KEY: value} the setup recorded in ENV_FILE; {} when there is none."""
        out = {}
        try:
            text = self.env_file.read_text(errors="replace") if self.env_file else ""
        except OSError:
            return out
        for line in text.splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$", line)
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[m.group(1)] = v
        return out

    def verify_env(self):
        """The recorded endpoints a verify adopts (VERIFY_ADOPTS)."""
        keys = self.VERIFY_ADOPTS
        return {k: v for k, v in self.env().items() if keys is None or k in keys}

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

    # the interface: two phases, one pair each, and two probes
    def cell_setup(self):
        """What exists for the cell's life, kept for every attempt. Called
        by the driver before the first attempt. Returns the agent-container
        env (DOCKER_NET/KUBE_MOUNT): empty unless the variant's agent is
        connected to the cell's infra (`variant.ACCESS_INFRA`)."""
        return {}

    def cell_teardown(self):
        """Best-effort, idempotent, derivable from the cid alone: it runs even
        when cell_setup died halfway."""

    def verify_setup(self, ctx, env):
        """What one arrangement of the judged artifacts runs on, brought up
        fresh. Called by the experiment's Verifier inside the verify
        container, over the daemon's socket; `env` is the arrangement's
        environment the verifier composed (its store, its ports, the cell's
        endpoint files). Raises on a rig fault; returns what the verifier
        must know to run the artifacts on it, or nothing."""
        return {}

    def verify_teardown(self, ctx, env):
        """The world reset after the arrangement, once the verifier has
        stopped the artifacts: idempotent, runs whether verify_setup finished
        or not — in the same container, or in a fresh one of the same image
        when the verify was killed, so everything it needs is derivable from
        `ctx` alone."""

    def ok(self):
        """The host can carry this infra at all. False halts the cell
        without spending an attempt."""
        return True

    def alive(self):
        """The infra answers RIGHT NOW: asked before every arrangement and
        again after a charged fail, so infra that died under the measurement
        voids the arrangement instead of scoring as a build verdict. An infra
        class declares its own probe, or the preflight (`liveness_declared`)
        halts its cells before an attempt is spent."""
        return False

    @classmethod
    def identities(cls, cid):
        """[(kind, name)] of the cell-lifetime infra a cell provisions, named
        as this class names it — what a reaper may look for after the cell
        is gone. Unnamed infra (found by scanning) is not listed."""
        return []

    @classmethod
    def stray(cls, live, workspaces):
        """Infra of this class that only a scan can find (no name carries
        the cid): [(kind, ident, owner cid)] whose owner is not in `live`,
        anchored at the workspace root so nothing outside the rig is ours."""
        return []

    @classmethod
    def sweep(cls):
        """Operator preflight: remove this infra's stale leftovers from dead
        cells (`cli.py experiment infra`). Nothing by default."""

    @classmethod
    def image_context(cls, conf):
        """[(host path, name)] copied beside the variant's verify Dockerfile
        at build time; hashed into the image tag."""
        return []

    @classmethod
    def agent_image_context(cls, conf):
        """[(host path, name)] staged beside the variant's agent Dockerfile
        at build time (an SDK's sources)."""
        return []

    def image(self):
        """The image this cell is verified in, built when missing."""
        from .. import image as _image
        return _image.for_variant(self.variant, fae.experiment.exp().definition, self.conf, self.log)

    def network(self):
        """The cell's docker network, the engine's: every container of the
        cell — the verify, the store, a sidecar — is a DNS name on it."""
        from .. import image as _image
        return _image.cell_network(self.cid)

    def network_up(self):
        """Create the cell network before `cell_setup` provisions onto it."""
        from .. import image as _image
        _image.network_up(self.cid)

    def network_down(self):
        """Remove the verify container and the cell network after
        `cell_teardown`; idempotent."""
        from .. import image as _image
        _image.remove_container(_image.verify_container(self.cid))
        _image.network_down(self.cid)

    _tool_seq = itertools.count(1)

    def tool(self, argv, env=None, network=None, timeout=900):
        """Run `argv` in a throwaway container of the variant's verify image
        — a tool the host does not carry (kind, kubectl), called by the
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


class DefaultInfra(Infra):
    """The infra of a variant whose file names no [infra] class: a program
    in an image of its own ([verify.run] image or image_dir) needs the
    docker daemon and that image, built here when it is a Dockerfile
    directory; a program in the verify image needs nothing, and declares
    no liveness."""

    def ok(self):
        if not self.variant.runs_own_image():
            return True
        if subprocess.run(["docker", "info"], capture_output=True).returncode:
            self.log("HALT[infra]: docker unreachable")
            return False
        from . import secrunner
        try:
            secrunner.ensure_run_image(self.variant, log=self.log)
        except RuntimeError as e:
            self.log(f"HALT[infra]: {e}")
            return False
        return True

    def alive(self):
        return self.variant.runs_own_image() and daemon_answers(["docker", "version"])


class NoopInfra(Infra):
    """The seam a fixture root uses: no provisioning, no infra demand,
    no network."""

    def alive(self):
        return True

    def network_up(self):
        pass

    def network_down(self):
        pass


def liveness_declared(variant):
    """Whether the variant's infra has a liveness probe: its class's own
    `alive`, or the default's for a program in an image of its own. Without
    one every arrangement would be void, refunded, forever; a definition
    error, caught before the first attempt."""
    infra = variant.INFRA
    if infra is DefaultInfra:
        return variant.runs_own_image()
    return infra.alive is not Infra.alive
