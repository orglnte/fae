"""The per-cell docker-in-docker sidecar a variant provisions into (an
engine block a variant composes)."""
from __future__ import annotations

import subprocess
import time

from fae.cell.variants import base
from fae.cell.variants.base import HookFailure


class DindSidecar:
    """A per-cell docker-in-docker sidecar: the cell's own daemon on a stable
    host port pair, seeded with the cache image so no attempt pays a pull."""

    def __init__(self, cid, api_port, cache_port, inner_cache_port, image,
                 cache_image, log, tag, network=None):
        self.cid = cid
        self.name = self.name_for(cid)
        self.api_port = api_port
        self.cache_port = cache_port
        self.inner_cache_port = inner_cache_port
        self.image = image
        self.cache_image = cache_image
        self.log = log
        self.tag = tag
        # the cell network: on it the sidecar's daemon and what it publishes
        # answer by the sidecar's name (<name>:2375, <name>:<inner port>)
        self.network = network

    PREFIX = "fae-dind-"

    @classmethod
    def name_for(cls, cid):
        return f"{cls.PREFIX}{cid}"

    @property
    def api(self):
        return f"tcp://127.0.0.1:{self.api_port}"

    def exists(self):
        p = base._run(["docker", "ps", "-a", "--format", "{{.Names}}"])
        return self.name in p.stdout.split()

    def alive(self):
        return base._ok(["docker", "-H", self.api, "info"])

    def answers(self):
        """The sidecar's daemon answers now; only a hard connect failure is
        no (base.daemon_answers)."""
        return base.daemon_answers(["docker", "-H", self.api, "version"])

    def remove(self):
        if self.exists():
            base._run(["docker", "rm", "-f", "-v", self.name])

    def ensure(self):
        """True when a live sidecar was reused, False when one was started.
        Raises HookFailure when neither is possible."""
        if self.exists():
            if self.alive():
                self.log(f"{self.tag}: reusing live sidecar {self.name} "
                         f"(API :{self.api_port} cache :{self.cache_port})")
                return True
            self.log(f"{self.tag}: removing wedged sidecar {self.name}")
            self.remove()
        self.log(f"{self.tag}: starting sidecar {self.name} "
                 f"(API 127.0.0.1:{self.api_port}, cache 127.0.0.1:{self.cache_port})")
        net = ["--network", self.network] if self.network else []
        if not base._ok(["docker", "run", "-d", "--privileged", "--name", self.name,
                    "--label", f"fae-cell={self.cid}", *net,
                    "-p", f"127.0.0.1:{self.api_port}:2375",
                    "-p", f"127.0.0.1:{self.cache_port}:{self.inner_cache_port}",
                    "-e", "DOCKER_TLS_CERTDIR=",
                    "--memory", "1g", "--cpus", "1", self.image]):
            raise HookFailure(f"HALT[substrate]: dind start failed for {self.cid}")
        for _ in range(30):
            if self.alive():
                break
            time.sleep(2)
        if not self.alive():
            self.remove()
            raise HookFailure(f"HALT[substrate]: dind API never came up for {self.cid}")
        self.seed()
        return False

    def seed(self):
        """docker save on the host, docker load inside the sidecar — a real
        streamed pipe between the two daemons."""
        save = subprocess.Popen(["docker", "save", self.cache_image],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        load = subprocess.run(["docker", "-H", self.api, "load"],
                              stdin=save.stdout, capture_output=True)
        save.stdout.close()
        save.wait()
        if save.returncode != 0 or load.returncode != 0:
            raise HookFailure(f"HALT[substrate]: seeding {self.cache_image} "
                              f"into {self.name} failed")


