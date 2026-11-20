"""Docker-in-Docker substrate: `DockerContext` (the typed handle handed to
verifiers on `ctx.substrate`) and `DockerSubstrate` (the `SubstrateInfra`
driving cell, verify, and split lifecycles)."""
from __future__ import annotations

import json
import os
import subprocess
import time

from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess, HookFailure, cksum, free_port_from
from fae.cell.infra.dind import DindSidecar, VerifyDind
from .base import ImageSpec, SubstrateInfra

DEFAULT_NETWORKS = frozenset({"bridge", "host", "none"})
DIND_API_PORT = 2375
CELL_API_BASE = 24000
CELL_EXPOSE_BASE = 25000
PORT_SPAN = 800


def _allocate_ports(cid, expose_ports):
    """Deterministic starting loopback ports for one cell's DinD API and
    exposed inner ports, stepped past listeners via free_port_from."""
    slot = cksum(cid) % PORT_SPAN
    stride = max(1, len(expose_ports))
    api_seed = CELL_API_BASE + slot
    api = free_port_from(api_seed)
    if api is None:
        raise HookFailure(f"HALT[infra]: no free Docker API port near {api_seed} for {cid}")
    exposed = {}
    cursor = CELL_EXPOSE_BASE + (slot * stride) % PORT_SPAN
    for inner in expose_ports:
        p = free_port_from(cursor)
        if p is None:
            raise HookFailure(f"HALT[infra]: no free host port near {cursor} for {cid}:{inner}")
        exposed[int(inner)] = p
        cursor = p + 1
    return api, exposed


def _read_published_port(container, inner_port):
    p = infra_base.run(["docker", "port", container, f"{inner_port}/tcp"])
    if p.returncode != 0 or not p.stdout.strip():
        return None
    line = p.stdout.strip().splitlines()[0]
    _, _, port = line.rpartition(":")
    return int(port) if port.isdigit() else None


class DockerContext:
    """Typed handle to a cell-lifetime (`fae-dind-<cid>`) or verify-lifetime
    (`fae-vdind-<cid>`) Docker-in-Docker daemon."""

    kind = "docker"

    def __init__(self, cid, *, scope="verify", network="", dind_image="docker:27-dind",
                 images=(), expose_ports=(), cpus=1.0, memory="1g",
                 in_verify=True, log=print, api_port=None, exposed_ports=None):
        self.cid = cid
        self.scope = scope          # "cell" or "verify"
        self.network = network
        self.dind_image = dind_image
        self.images = tuple(images)
        self.expose_ports = tuple(int(p) for p in expose_ports)
        self.cpus = cpus
        self.memory = memory
        self.in_verify = in_verify
        self.log = log
        self.container = DindSidecar.name_for(cid) if scope == "cell" else VerifyDind.name_for(cid)
        self.api_port = api_port
        self.exposed_ports = dict(exposed_ports or {})

    @property
    def host(self):
        """DNS name of the DinD container on the cell network."""
        return self.container

    @property
    def docker_host(self):
        if self.in_verify or self.scope == "verify":
            return f"tcp://{self.container}:{DIND_API_PORT}"
        if self.api_port is None:
            self.api_port = _read_published_port(self.container, DIND_API_PORT)
        if self.api_port is None:
            slot = cksum(self.cid) % PORT_SPAN
            return f"tcp://127.0.0.1:{CELL_API_BASE + slot}"
        return f"tcp://127.0.0.1:{self.api_port}"

    def env(self):
        return {"DOCKER_HOST": self.docker_host}

    def docker(self, *argv, timeout=120, stdin=None):
        return subprocess.run(["docker", "-H", self.docker_host, *[str(a) for a in argv]],
                              capture_output=True, text=True, timeout=timeout, input=stdin)

    def exists(self):
        p = infra_base.run(["docker", "ps", "-a", "--format", "{{.Names}}"])
        return self.container in p.stdout.split()

    def alive(self):
        return infra_base.ok(["docker", "-H", self.docker_host, "info"])

    def answers(self):
        return infra_base.daemon_answers(["docker", "-H", self.docker_host, "version"])

    def containers(self, all=False):
        """Parsed `docker inspect` JSON dicts for inner containers."""
        args = ["ps", "-a", "-q"] if all else ["ps", "-q"]
        ids = self.docker(*args).stdout.split()
        if not ids:
            return []
        raw = self.docker("inspect", *ids).stdout or "[]"
        try:
            return json.loads(raw)
        except ValueError:
            return []

    def container_names(self, all=False):
        args = ["ps", "-a", "--format", "{{.Names}}"] if all else ["ps", "--format", "{{.Names}}"]
        return self.docker(*args).stdout.split()

    def residue(self):
        """(container_ids, volume_names, non_default_networks) inside the daemon."""
        cs = self.docker("ps", "-aq").stdout.split()
        vs = self.docker("volume", "ls", "-q").stdout.split()
        ns = [n for n in self.docker("network", "ls", "--format", "{{.Name}}").stdout.split()
              if n not in DEFAULT_NETWORKS]
        return cs, vs, ns

    def snapshot(self):
        """Human-readable status of inner containers and log tails of stopped ones."""
        rows = self.docker("ps", "-a", "--format", "{{.Names}}\t{{.Status}}").stdout.strip()
        lines = [f"  {r}" for r in rows.splitlines()] or ["  (no containers)"]
        for row in rows.splitlines():
            name, _, status = row.partition("\t")
            if not status.startswith("Up"):
                tail = self.docker("logs", "--tail", "15", name)
                lines.append(f"  --- {name} log:")
                lines += [f"    {line}" for line in (tail.stdout + tail.stderr).strip().splitlines()]
        return "\n".join(lines)

    def reset(self):
        """Remove every inner container, volume, and non-default network while
        keeping preloaded images intact."""
        cs, vs, ns = self.residue()
        if cs:
            self.docker("rm", "-f", "-v", *cs)
        if vs:
            self.docker("volume", "rm", "-f", *vs)
        if ns:
            self.docker("network", "rm", *ns)
        if cs or vs or ns:
            self.log(f"substrate[docker]: cleared {len(cs)} container(s), "
                     f"{len(vs)} volume(s), {len(ns)} network(s) in {self.container}")

    def _ensure_host_image(self, spec):
        tag = spec.host_tag(build=False)
        if spec.is_build:
            if not infra_base.ok(["docker", "image", "inspect", tag]):
                tag = spec.host_tag(build=True, log=self.log)
            return tag
        if not infra_base.ok(["docker", "image", "inspect", tag]):
            if not infra_base.ok(["docker", "pull", "-q", tag]):
                raise HookFailure(f"HALT[infra]: image '{tag}' not present and not pullable")
        return tag

    def load_images(self, images=None):
        """Stream missing images from the host daemon into this DinD daemon
        and apply any `as_tag` aliases."""
        specs = [i if isinstance(i, ImageSpec) else ImageSpec(source=str(i))
                 for i in (self.images if images is None else images)]
        if not specs:
            return
        resolved = [(spec, self._ensure_host_image(spec)) for spec in specs]
        have = set(self.docker("images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split())
        missing = sorted({host_tag for _, host_tag in resolved if host_tag not in have})
        if missing:
            save = subprocess.Popen(["docker", "save", *missing],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            load = subprocess.run(["docker", "-H", self.docker_host, "load"],
                                  stdin=save.stdout, capture_output=True)
            save.stdout.close()
            save.wait()
            if save.returncode != 0 or load.returncode != 0:
                raise HookFailure(f"HALT[infra]: loading {missing} into {self.container} failed")
            self.log(f"substrate[docker]: loaded {' '.join(missing)} into {self.container}")
        for spec, host_tag in resolved:
            if spec.as_tag and spec.as_tag != host_tag:
                if self.docker("tag", host_tag, spec.as_tag).returncode != 0:
                    raise HookFailure(f"HALT[infra]: tagging {spec.as_tag} in {self.container} failed")

    def _start_verify(self):
        infra_base.run(["docker", "rm", "-f", "-v", self.container])
        if not infra_base.ok(["docker", "image", "inspect", self.dind_image]) and \
                not infra_base.ok(["docker", "pull", "-q", self.dind_image]):
            raise HookFailure(f"verify dind: image {self.dind_image} not present and not pullable")
        cpuset = os.environ.get("CPUSET_MEASURED")
        self.log(f"substrate[docker]: starting {self.container} on {self.network}")
        argv = ["docker", "run", "-d", "--privileged", "--name", self.container,
                "--label", f"fae-cell={self.cid}", "--label", "fae-role=substrate",
                *(["--network", self.network] if self.network else []),
                "-e", "DOCKER_TLS_CERTDIR=",
                "--memory", str(self.memory), "--cpus", str(self.cpus),
                *(["--cpuset-cpus", cpuset] if cpuset else []),
                self.dind_image]
        if not infra_base.ok(argv):
            raise HookFailure(f"verify dind: start failed for {self.cid}")
        for _ in range(45):
            if self.alive():
                break
            time.sleep(2)
        else:
            infra_base.run(["docker", "rm", "-f", "-v", self.container])
            raise HookFailure(f"verify dind: API never came up for {self.cid}")
        self.load_images()
        return self

    def _start_cell(self):
        if self.exists():
            running_api = _read_published_port(self.container, DIND_API_PORT)
            if running_api is not None:
                self.api_port = running_api
                self.exposed_ports = {
                    p: (_read_published_port(self.container, p) or self.exposed_ports.get(p))
                    for p in self.expose_ports
                }
            if self.alive():
                self.log(f"substrate[docker]: reusing live sidecar {self.container} (API :{self.api_port})")
                self.load_images()
                return self
            self.log(f"substrate[docker]: removing wedged sidecar {self.container}")
            infra_base.run(["docker", "rm", "-f", "-v", self.container])
        for attempt in range(1, 6):
            self.api_port, self.exposed_ports = _allocate_ports(self.cid, self.expose_ports)
            port_flags = ["-p", f"127.0.0.1:{self.api_port}:{DIND_API_PORT}"]
            for inner, host_p in sorted(self.exposed_ports.items()):
                port_flags += ["-p", f"127.0.0.1:{host_p}:{inner}"]
            cpuset = os.environ.get("CPUSET_AGENT")
            argv = ["docker", "run", "-d", "--privileged", "--name", self.container,
                    "--label", f"fae-cell={self.cid}", "--label", "fae-role=substrate",
                    *(["--network", self.network] if self.network else []),
                    *port_flags, "-e", "DOCKER_TLS_CERTDIR=",
                    "--memory", str(self.memory), "--cpus", str(self.cpus),
                    *(["--cpuset-cpus", cpuset] if cpuset else []),
                    self.dind_image]
            r = infra_base.run(argv)
            if r.returncode == 0:
                break
            infra_base.run(["docker", "rm", "-f", "-v", self.container])
            self.log(f"substrate[docker]: bind failed on attempt {attempt}/5: {r.stderr.strip()[:160]}")
        else:
            raise HookFailure(f"HALT[infra]: dind start failed for {self.cid}")
        for _ in range(30):
            if self.alive():
                break
            time.sleep(2)
        if not self.alive():
            infra_base.run(["docker", "rm", "-f", "-v", self.container])
            raise HookFailure(f"HALT[infra]: dind API never came up for {self.cid}")
        self.load_images()
        return self

    def ensure(self):
        """Start or reuse this DinD daemon and load its configured images."""
        if self.scope == "verify":
            return self._start_verify()
        if self.in_verify:
            if not self.answers():
                raise HookFailure(f"HALT[infra]: cell daemon {self.docker_host} does not answer")
            self.load_images()
            return self
        return self._start_cell()

    up = ensure

    def down(self, force=False):
        """Remove a verify-lifetime daemon (or a cell-lifetime one when `force=True`)."""
        if self.scope == "verify" or force:
            infra_base.run(["docker", "rm", "-f", "-v", self.container])

    def __enter__(self):
        return self.up()

    def __exit__(self, *_exc):
        self.down()


class DockerSubstrate(SubstrateInfra):
    """Declarative Docker-in-Docker substrate supporting `cell`, `verify`,
    and `split` lifecycles."""

    PREFIXES = {"container": (DindSidecar.PREFIX, VerifyDind.PREFIX)}

    def _dind_image(self):
        return self.cfg("DIND_IMAGE", self.SPEC.dind_image) or self.SPEC.dind_image

    def _cell_ctx(self, in_verify=False):
        return DockerContext(
            self.cid,
            scope="cell",
            network=self.network(),
            dind_image=self._dind_image(),
            images=self.SPEC.images,
            expose_ports=self.SPEC.expose_ports,
            cpus=self.SPEC.cpus,
            memory=self.SPEC.memory,
            in_verify=in_verify,
            log=self.log,
        )

    def _verify_ctx(self, ctx):
        net = getattr(ctx, "cell_network", "") or os.environ.get("FAE_CELL_NET") or self.network()
        if self.SPEC.lifecycle == "cell":
            return self._cell_ctx(in_verify=True)
        return DockerContext(
            ctx.cid,
            scope="verify",
            network=net,
            dind_image=self._dind_image(),
            images=self.SPEC.images,
            cpus=self.SPEC.cpus,
            memory=self.SPEC.memory,
            in_verify=True,
            log=self.log,
        )

    def make_context(self, ctx=None):
        return self._verify_ctx(ctx) if ctx is not None else self._cell_ctx(in_verify=False)

    def _needs_cell_daemon(self):
        return self.SPEC.lifecycle == "cell" or (self.SPEC.lifecycle == "split" and self.SPEC.access)

    def ok(self):
        if not self._check_remote_lifecycle():
            return False
        if not infra_base.ok(["docker", "info"]):
            self.log("HALT[infra]: docker unreachable")
            return False
        dind = self._dind_image()
        if not infra_base.ok(["docker", "image", "inspect", dind]):
            self.log(f"HALT[infra]: image '{dind}' not present — docker pull {dind}")
            return False
        if not self._check_spec_images():
            return False
        if self.overlay and not self.overlay.ok(self):
            return False
        return True

    def alive(self):
        if self.SPEC.lifecycle == "cell":
            return self._cell_ctx(in_verify=False).answers()
        if self.SPEC.lifecycle == "split" and self.SPEC.access and not self._remote_verify_host_configured():
            return self._cell_ctx(in_verify=False).answers()
        return self._daemon_alive()

    def shared_containers(self):
        out = [DindSidecar.name_for(self.cid)] if self.SPEC.lifecycle == "cell" else []
        return out + super().shared_containers()

    @classmethod
    def identities(cls, cid):
        out = []
        if cls.SPEC.lifecycle in ("cell", "split"):
            out.append(("container", DindSidecar.name_for(cid)))
        if cls.SPEC.lifecycle in ("verify", "split"):
            out.append(("container", VerifyDind.name_for(cid)))
        return out + super().identities(cid)

    def cell_setup(self):
        sub = None
        acc = None
        if self._needs_cell_daemon():
            sub = self._cell_ctx(in_verify=False).ensure()
            if self.SPEC.access:
                acc = AgentAccess(
                    env={"DOCKER_HOST": f"tcp://host.docker.internal:{sub.api_port}"},
                    extra_hosts=("host.docker.internal:host-gateway",),
                )
        if self.overlay:
            over = self.overlay.on_cell_setup(sub, self)
            if isinstance(over, AgentAccess):
                acc = acc.merge(over) if acc is not None else over
            elif over:
                return over
        return acc if acc is not None else {}

    def cell_teardown(self):
        sub = self._cell_ctx(in_verify=False) if self._needs_cell_daemon() else None
        if self.overlay:
            self.overlay.on_cell_teardown(sub, self)
        if sub is not None:
            sub.down(force=True)

    def verify_setup(self, ctx, env):
        sub = getattr(ctx, "substrate", None) or self._verify_ctx(ctx)
        if self.SPEC.lifecycle == "cell":
            sub.ensure()
            sub.reset()
        else:
            sub.up()
        if self.overlay:
            res = self.overlay.on_verify_setup(sub, ctx, env)
            if res is not None:
                return res
        return sub.env()

    def verify_teardown(self, ctx, env):
        sub = getattr(ctx, "substrate", None) or self._verify_ctx(ctx)
        if self.overlay:
            self.overlay.on_verify_teardown(sub, ctx, env)
        if self.SPEC.lifecycle == "cell" and sub.answers():
            sub.reset()
