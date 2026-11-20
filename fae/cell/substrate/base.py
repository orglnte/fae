"""Declarative substrate base types: ImageSpec, SubstrateSpec, DomainOverlay,
NoneContext, and SubstrateInfra.

A variant's `[substrate]` TOML table compiles into a `SubstrateSpec` and a
bound `SubstrateInfra` subclass (an `Infra`), so the rest of the engine
interacts with it through the standard Infra contract while verifiers receive
a typed substrate context (`ctx.substrate`)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess, DefaultInfra, Infra
from fae.cell.verify_host import VerifyHost

KINDS = frozenset({"none", "docker", "k8s", "custom"})
LIFECYCLES = frozenset({"cell", "verify", "split"})


@dataclass(frozen=True)
class ImageSpec:
    """An image preloaded into a substrate: either a registry tag (`source`)
    or a local Dockerfile directory (`dir`) optionally re-tagged as `as_tag`."""

    source: str = ""
    dir: Path | None = None
    as_tag: str = ""
    name: str = ""

    @property
    def is_build(self):
        return self.dir is not None

    @property
    def target_tag(self):
        return self.as_tag or self.source

    def build_name(self):
        if self.name:
            return self.name
        if self.as_tag:
            return self.as_tag.split(":", 1)[0]
        return Path(self.dir).name if self.dir else ""

    def host_tag(self, build=False, log=print):
        """Tag on the host daemon; builds when `build=True` and `self.dir` is set."""
        if not self.is_build:
            return self.source
        from fae.cell import image as _image
        if build:
            return _image.ensure(self.build_name(), self.dir, log=log)
        return _image.tag(self.build_name(), self.dir)


@dataclass(frozen=True)
class SubstrateSpec:
    """Normalized representation of a variant file's `[substrate]` table."""

    kind: str = "none"
    lifecycle: str = "cell"
    access: bool = False
    images: tuple = ()          # tuple[ImageSpec, ...]
    expose_ports: tuple = ()    # tuple[int, ...]
    cpus: float = 1.0
    memory: str = "1g"
    dind_image: str = "docker:27-dind"
    cluster_prefix: str = "fae-k8s-"
    overlay: type | None = None
    params: tuple = ()          # tuple[(key, value), ...] for hashability
    lock: str | None = None
    lock_slots: int = 1

    def params_dict(self):
        return dict(self.params)


class DomainOverlay:
    """Optional experiment-specific layer that rides on top of a FAE-managed
    substrate (for example, a domain control-plane daemon or cluster operator).
    Methods are no-ops by default."""

    PREFIXES = {}
    ENV_FILE = None
    VERIFY_ADOPTS = None
    RUN_DIR = None

    def ok(self, infra):
        return True

    def on_cell_setup(self, substrate, infra):
        return None

    def on_cell_teardown(self, substrate, infra):
        pass

    def on_verify_setup(self, substrate, ctx, env):
        return None

    def on_verify_teardown(self, substrate, ctx, env):
        pass

    def shared_containers(self, infra):
        return []

    @classmethod
    def identities(cls, cid):
        return []

    @classmethod
    def stray(cls, live, workspaces):
        return []

    @classmethod
    def image_context(cls, conf):
        return []

    @classmethod
    def agent_image_context(cls, conf):
        return []


class NoneContext:
    """Substrate context for `kind = "none"` variants."""

    kind = "none"

    def env(self):
        return {}

    def up(self):
        return self

    def ensure(self):
        return self

    def reset(self):
        pass

    def down(self, force=False):
        pass

    def __enter__(self):
        return self.up()

    def __exit__(self, *_exc):
        self.down()


def _merge_prefixes(base_prefixes, extra_prefixes):
    out = {}
    for src in (base_prefixes, extra_prefixes):
        for kind, pfxs in (src or {}).items():
            vals = (pfxs,) if isinstance(pfxs, str) else tuple(pfxs)
            cur = list(out.get(kind, ()))
            for v in vals:
                if v not in cur:
                    cur.append(v)
            out[kind] = tuple(cur)
    return out


class SubstrateInfra(Infra):
    """Infra adapter backed by a declarative `SubstrateSpec` and optional
    `DomainOverlay`."""

    SPEC = SubstrateSpec()

    def __init__(self, variant, cell=None, *, cid=None, ws=None, root=None, conf=None):
        super().__init__(variant, cell, cid=cid, ws=ws, root=root, conf=conf)
        self.overlay = self.SPEC.overlay() if self.SPEC.overlay is not None else None

    def _remote_verify_host_configured(self):
        return any(h.remote for h in VerifyHost.pool(self.conf))

    def _check_remote_lifecycle(self):
        if self.SPEC.lifecycle == "cell" and self._remote_verify_host_configured():
            self.log("HALT[infra]: [substrate] lifecycle='cell' is incompatible with "
                     "remote VERIFY_DOCKER_HOST (use lifecycle='verify' or 'split')")
            return False
        return True

    def _check_spec_images(self):
        if not self.SPEC.images:
            return True
        if not infra_base.ok(["docker", "info"]):
            self.log("HALT[infra]: docker unreachable")
            return False
        for img in self.SPEC.images:
            if img.is_build:
                try:
                    img.host_tag(build=True, log=self.log)
                except RuntimeError as e:
                    self.log(f"HALT[infra]: {e}")
                    return False
            elif not infra_base.ok(["docker", "image", "inspect", img.source]):
                self.log(f"HALT[infra]: image '{img.source}' not present — docker pull {img.source}")
                return False
        return True

    def _daemon_alive(self):
        """Liveness check for the daemon a verify runs on: remote verify host
        when a single remote host is bound, else the local Docker daemon."""
        hosts = VerifyHost.pool(self.conf)
        if len(hosts) == 1 and hosts[0].remote:
            return hosts[0].answers()
        return infra_base.daemon_answers(["docker", "version"])

    def make_context(self, ctx=None):
        return NoneContext()

    def shared_containers(self):
        return list(self.overlay.shared_containers(self)) if self.overlay else []

    def not_ready(self, ctx, env):
        fn = getattr(self.overlay, "not_ready", None) if self.overlay else None
        if callable(fn):
            fn(getattr(ctx, "substrate", None), ctx, env)

    def verify_down(self, ctx):
        sub = getattr(ctx, "substrate", None)
        if sub is not None:
            sub.down()

    @classmethod
    def identities(cls, cid):
        return list(cls.SPEC.overlay.identities(cid)) if cls.SPEC.overlay else []

    @classmethod
    def stray(cls, live, workspaces):
        return list(cls.SPEC.overlay.stray(live, workspaces)) if cls.SPEC.overlay else []

    @classmethod
    def image_context(cls, conf):
        return list(cls.SPEC.overlay.image_context(conf)) if cls.SPEC.overlay else []

    @classmethod
    def agent_image_context(cls, conf):
        return list(cls.SPEC.overlay.agent_image_context(conf)) if cls.SPEC.overlay else []


class NoneSubstrate(SubstrateInfra):
    """Declarative `kind = "none"` substrate preserving DefaultInfra's
    `[verify.run]` Docker preflight and liveness checks."""

    def ok(self):
        if not self._check_spec_images():
            return False
        if self.variant.runs_own_image():
            if subprocess.run(["docker", "info"], capture_output=True).returncode:
                self.log("HALT[infra]: docker unreachable")
                return False
            from fae.cell.infra import secrunner
            try:
                secrunner.ensure_run_image(self.variant, log=self.log)
            except RuntimeError as e:
                self.log(f"HALT[infra]: {e}")
                return False
        if self.overlay and not self.overlay.ok(self):
            return False
        return True

    def alive(self):
        return self.variant.runs_own_image() and self._daemon_alive()

    def cell_setup(self):
        if self.overlay:
            acc = self.overlay.on_cell_setup(NoneContext(), self)
            return acc if acc is not None else {}
        return {}

    def cell_teardown(self):
        if self.overlay:
            self.overlay.on_cell_teardown(NoneContext(), self)

    def verify_setup(self, ctx, env):
        sub = ctx.substrate or NoneContext()
        if self.overlay:
            res = self.overlay.on_verify_setup(sub, ctx, env)
            return res if res is not None else {}
        return {}

    def verify_teardown(self, ctx, env):
        if self.overlay:
            self.overlay.on_verify_teardown(ctx.substrate or NoneContext(), ctx, env)
