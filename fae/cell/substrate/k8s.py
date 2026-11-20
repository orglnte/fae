"""Kubernetes (`kind`) substrate: `K8sContext` (the typed handle handed to
verifiers on `ctx.substrate`) and `K8sSubstrate` (the `SubstrateInfra`
driving cell, verify, and split lifecycles, with Docker-only host fallback)."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import fae.experiment
from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess, HookFailure, cksum
from fae.cell.infra.kind import Cluster
from .base import ImageSpec, SubstrateInfra


def cluster_name_for(cid, prefix="fae-k8s-"):
    return f"{prefix}{cksum(cid)}"


class K8sContext:
    """Typed handle to a cell-lifetime or verify-lifetime `kind` cluster."""

    kind = "k8s"

    def __init__(self, cid, *, scope="verify", kubeconfig_dir, cluster_prefix="fae-k8s-",
                 images=(), cpus=1.0, memory="4g", infra=None, via_image=None,
                 log=print):
        self.cid = cid
        self.scope = scope          # "cell" or "verify"
        self.cluster_cid = cid if scope == "cell" else f"{cid}-verify"
        self.cluster_name = cluster_name_for(self.cluster_cid, cluster_prefix)
        self.kubeconfig_dir = Path(kubeconfig_dir)
        self.kubeconfig = self.kubeconfig_dir / ("kubeconfig" if scope == "cell" else ".kubecfg")
        self.host_kubeconfig = Path(f"{self.kubeconfig}.host")
        self.images = tuple(images)
        self.cpus = cpus
        self.memory = memory
        self._infra = infra
        self.log = log
        # Fall back to running kind/kubectl in the variant's image when host lacks kind
        self.via_image = (shutil.which("kind") is None) if via_image is None else bool(via_image)
        if self.via_image and self._infra is not None:
            self.cluster = Cluster(self.cluster_name, log=log,
                                   run=self._tool_run, ok=self._tool_ok)
        else:
            self.cluster = Cluster(self.cluster_name, log=log)
        self.node = self.cluster.node

    def _tool_run(self, argv, env=None, **kw):
        net = self.cluster.network() if self.cluster.running() else None
        return self._infra.tool(argv, env=env, network=net)

    def _tool_ok(self, argv, env=None, **kw):
        return self._tool_run(argv, env=env).returncode == 0

    @property
    def network(self):
        return self.cluster.network()

    def env(self):
        return {"KUBECONFIG": str(self.kubeconfig)}

    def containers(self):
        return [self.node] if self.cluster.running() else []

    def kubectl(self, *argv, env=None, timeout=120):
        kenv = dict(os.environ, KUBECONFIG=str(self.kubeconfig), **(env or {}))
        return self.cluster.run(["kubectl", *[str(a) for a in argv]], env=kenv, timeout=timeout)

    def apply(self, path, namespace=None, server_side=False):
        args = ["apply"]
        if server_side:
            args.append("--server-side")
        args += ["-f", str(path)]
        if namespace:
            args += ["-n", str(namespace)]
        return self.kubectl(*args)

    def join(self, container):
        return self.cluster.join(container)

    def load_images(self, images=None):
        specs = [i if isinstance(i, ImageSpec) else ImageSpec(source=str(i))
                 for i in (self.images if images is None else images)]
        tags = [s.host_tag(build=True, log=self.log) for s in specs]
        if tags:
            self.cluster.load_images(tags)

    def ensure(self, container=None, wait="120s"):
        """Create or resume the cluster, join `container` (if given) to its
        network, write internal/host kubeconfigs, cap node resources, and
        preload configured images."""
        self.kubeconfig_dir.mkdir(parents=True, exist_ok=True)
        self.cluster.create_or_resume(self.host_kubeconfig, wait=wait)
        if container:
            self.cluster.join(container)
        old = os.umask(0o077)
        try:
            self.kubeconfig.write_text(self.cluster.kubeconfig(internal=True))
        finally:
            os.umask(old)
        pin_key = "CPUSET_MEASURED" if self.scope == "verify" else "CPUSET_AGENT"
        cpuset = (self._infra.cfg(pin_key, "") if self._infra is not None else "") or \
            os.environ.get(pin_key) or None
        self.cluster.cap(self.cpus, str(self.memory), cpuset=cpuset)
        self.load_images()
        return self

    up = ensure

    def down(self, force=False):
        if self.scope == "verify" or force:
            self.cluster.delete()
            for f in (self.kubeconfig, self.host_kubeconfig):
                f.unlink(missing_ok=True)

    def __enter__(self):
        return self.up()

    def __exit__(self, *_exc):
        self.down()


class K8sSubstrate(SubstrateInfra):
    """Declarative `kind` Kubernetes substrate supporting `cell`, `verify`,
    and `split` lifecycles."""

    def _cell_ctx(self):
        return K8sContext(
            self.cid,
            scope="cell",
            kubeconfig_dir=self.ws,
            cluster_prefix=self.SPEC.cluster_prefix,
            images=self.SPEC.images,
            cpus=self.SPEC.cpus,
            memory=self.SPEC.memory,
            infra=self,
            via_image=True,
            log=self.log,
        )

    def _verify_ctx(self, ctx):
        if self.SPEC.lifecycle == "cell":
            return K8sContext(
                ctx.cid,
                scope="cell",
                kubeconfig_dir=self.ws,
                cluster_prefix=self.SPEC.cluster_prefix,
                images=self.SPEC.images,
                cpus=self.SPEC.cpus,
                memory=self.SPEC.memory,
                infra=self,
                via_image=False,
                log=self.log,
            )
        return K8sContext(
            ctx.cid,
            scope="verify",
            kubeconfig_dir=Path(ctx.out),
            cluster_prefix=self.SPEC.cluster_prefix,
            images=self.SPEC.images,
            cpus=self.SPEC.cpus,
            memory=self.SPEC.memory,
            infra=self,
            via_image=False,
            log=self.log,
        )

    def make_context(self, ctx=None):
        return self._verify_ctx(ctx) if ctx is not None else self._cell_ctx()

    def _needs_cell_cluster(self):
        return self.SPEC.lifecycle == "cell" or (self.SPEC.lifecycle == "split" and self.SPEC.access)

    def ok(self):
        if not self._check_remote_lifecycle():
            return False
        if not infra_base.ok(["docker", "info"]):
            self.log("HALT[infra]: docker unreachable")
            return False
        if not self._check_spec_images():
            return False
        if self.overlay and not self.overlay.ok(self):
            return False
        return True

    def alive(self):
        return self._daemon_alive()

    @classmethod
    def identities(cls, cid):
        pfx = cls.SPEC.cluster_prefix
        out = []
        if cls.SPEC.lifecycle in ("cell", "split"):
            out.append(("cluster", cluster_name_for(cid, pfx)))
        if cls.SPEC.lifecycle in ("verify", "split"):
            out.append(("cluster", cluster_name_for(f"{cid}-verify", pfx)))
        return out + super().identities(cid)

    @classmethod
    def sweep(cls):
        """Remove stale kind clusters for this substrate prefix; uses host
        `kind` when installed, else runs `kind` via the variant's tool image."""
        pfx = cls.SPEC.cluster_prefix
        if shutil.which("kind") is not None:
            Cluster.sweep(pfx)
            return
        from fae import paths
        from fae.experiment import variants as _variants
        variant = next((v for v in fae.experiment.exp().definition.variants.values()
                        if v.INFRA is cls), None)
        if variant is None:
            return
        with tempfile.TemporaryDirectory(prefix="fae-sweep-") as ws:
            inst = _variants.infra_for(variant.ID, "sweep", ws, paths.root())
            Cluster.sweep(pfx, run=lambda argv, **kw: inst.tool(argv))

    def cell_setup(self):
        sub = None
        acc = None
        if self._needs_cell_cluster():
            sub = self._cell_ctx().ensure()
            if self.SPEC.access:
                acc = AgentAccess(
                    network=sub.network,
                    mounts=((str(sub.kubeconfig), "/home/node/.kube/config", "ro"),),
                    env={"KUBECONFIG": "/home/node/.kube/config"},
                )
        if self.overlay:
            over = self.overlay.on_cell_setup(sub, self)
            if isinstance(over, AgentAccess):
                acc = acc.merge(over) if acc is not None else over
            elif over:
                return over
        return acc if acc is not None else {}

    def cell_teardown(self):
        sub = self._cell_ctx() if self._needs_cell_cluster() else None
        if self.overlay:
            self.overlay.on_cell_teardown(sub, self)
        if sub is not None:
            sub.down(force=True)

    def verify_setup(self, ctx, env):
        sub = getattr(ctx, "substrate", None) or self._verify_ctx(ctx)
        me = getattr(ctx, "verify_container", "") or os.environ.get("FAE_VERIFY_CONTAINER", "")
        sub.up(container=me or None)
        if self.overlay:
            res = self.overlay.on_verify_setup(sub, ctx, env)
            if res is not None:
                return res
        return sub.env()

    def verify_teardown(self, ctx, env):
        sub = getattr(ctx, "substrate", None) or self._verify_ctx(ctx)
        if self.overlay:
            self.overlay.on_verify_teardown(sub, ctx, env)
        if self.SPEC.lifecycle != "cell":
            sub.down()
