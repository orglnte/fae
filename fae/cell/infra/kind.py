"""A named kind cluster: create or resume, cap its node, load images, hand
out its kubeconfigs, join a container to its network, delete; and a sweep
of stale clusters by name prefix. What a variant installs into it (an
autoscaler, metrics-server, a namespace) is the variant's.

`kind` itself runs through `run`/`ok` — in this process where the image
carries it (the verify container), or through a variant's `tool` from
the driver on a host that carries only docker; the docker calls are
always the host client's."""
from __future__ import annotations

import os
from pathlib import Path

from fae.cell.infra import base
from fae.cell.infra.base import HookFailure


class Cluster:
    def __init__(self, name, log=lambda m: None, run=None, ok=None):
        self.name = name
        self.node = f"{name}-control-plane"
        self.log = log
        # resolved at call time: the module's seam a test patches
        self.run = run or (lambda argv, **kw: base._run(argv, **kw))
        self.ok = ok or (lambda argv, **kw: base._ok(argv, **kw))

    def exists(self):
        return self.name in self.run(["kind", "get", "clusters"]).stdout.split()

    def running(self):
        return self.node in base._run(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()

    def create_or_resume(self, hostcfg, wait="120s"):
        """A new cluster, or a stopped one started again; the host kubeconfig
        lands at `hostcfg` either way."""
        hostcfg = Path(hostcfg)
        old = os.umask(0o077)
        try:
            if not self.exists():
                self.log(f"kind: creating cluster {self.name}")
                if not self.ok(["kind", "create", "cluster", "--name", self.name,
                                "--wait", wait, "--kubeconfig", str(hostcfg)]):
                    raise HookFailure(f"kind create cluster {self.name} failed")
                return
            stopped = base._run(["docker", "ps", "-aq", "--filter",
                                 f"label=io.x-k8s.kind.cluster={self.name}",
                                 "--filter", "status=exited"]).stdout.split()
            if stopped:
                base._run(["docker", "start", *stopped])
            self.log(f"kind: reusing cluster {self.name}")
            hostcfg.write_text(self.kubeconfig())
        finally:
            os.umask(old)

    def cap(self, cpus, mem, cpuset=None):
        pin = ["--cpuset-cpus", cpuset] if cpuset else []
        base._run(["docker", "update", "--cpus", str(cpus), *pin, "--memory", mem,
                   "--memory-swap", mem, self.node])

    def load_images(self, images):
        for img in sorted(images):
            if not base._ok(["docker", "image", "inspect", img]) and \
                    not base._ok(["docker", "pull", "-q", img]):
                raise HookFailure(f"cannot pull {img}")
            self.run(["kind", "load", "docker-image", img, "--name", self.name])

    def kubeconfig(self, internal=False):
        argv = ["kind", "get", "kubeconfig", "--name", self.name]
        if internal:
            argv.insert(3, "--internal")
        return self.run(argv).stdout

    def network(self):
        if not self.running():
            raise HookFailure(f"{self.node} not running (provision first)")
        out = base._run(["docker", "inspect", self.node, "-f",
                         "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"]).stdout.split()
        return out[0] if out else "kind"

    def join(self, container):
        """Put a running container on the cluster's network, so it reaches
        the API server by the node's name and the cluster reaches it by
        its own. Idempotent."""
        net = self.network()
        r = base._run(["docker", "network", "connect", net, container])
        if r.returncode and "already exists" not in r.stderr:
            raise HookFailure(f"could not connect {container} to {net}: {r.stderr.strip()}")
        return net

    def delete(self):
        if self.exists():
            self.log(f"kind: deleting cluster {self.name}")
            self.run(["kind", "delete", "cluster", "--name", self.name])

    @staticmethod
    def sweep(prefix, run=None):
        run = run or base._run
        stale = [c for c in run(["kind", "get", "clusters"]).stdout.split()
                 if c.startswith(prefix)]
        if not stale:
            print(f"sweep: no stale {prefix}* clusters")
            return
        print(f"sweep: deleting {len(stale)} stale cluster(s)")
        for c in stale:
            run(["kind", "delete", "cluster", "--name", c])
