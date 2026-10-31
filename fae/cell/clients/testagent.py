"""The scripted test agent: no model and no network tool, it replays a plan
(TESTAGENT_PLAN) or lays a variant's reference over the artifacts. It exists
for rig tests, so it has no credential and nothing to confine, and writes no
transcript to prove it by.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .base import AgentClient, refuse_dest


class Testagent(AgentClient):
    NAME = "testagent"
    HOME = ".agent-testagent"
    MOUNT = "/home/node/.testagent"
    PROVEN_BY_TRANSCRIPT = False
    SETUP_IN_IMAGE = False

    def credential_missing(self, home):
        return ""

    def setup_credential(self, home, image, ask, run, say):
        return ""

    def stage_home(self, conf, dest, root):
        """The engine's script and each variant's reference."""
        import fae.experiment
        from fae import paths
        dest = str(dest)
        refuse_dest(dest, "/" + self.HOME)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        shutil.copy(paths.ENGINE / "testagent.py", Path(dest) / "testagent.py")
        for vid, cls in fae.experiment.exp().definition.variants.items():
            if cls.REFERENCE is not None and Path(cls.REFERENCE).is_dir():
                shutil.copytree(cls.REFERENCE, Path(dest) / "reference" / vid)
        # the container runs as uid 1000 and writes findings.json into the home
        for r, dirs, files in os.walk(dest):
            for n in dirs + files:
                try:
                    os.chmod(os.path.join(r, n), 0o777)
                except OSError:
                    pass
        os.chmod(dest, 0o777)

    def check_home(self, home):
        return None

    def breach(self, init, tools_used):
        return ""

    def env(self, conf):
        return {}

    def docker_args(self, home):
        return ["-v", f"{home}:{self.MOUNT}",
                "-e", "CELL_ID", "-e", "VARIANT", "-e", "TESTAGENT_PLAN", "-e", "SERVICE_PORT"]

    def command(self, conf, prompt, feedback):
        return ["python3", f"{self.MOUNT}/testagent.py", prompt]

    @classmethod
    def usage(cls, log):
        return None

    @classmethod
    def client_seconds(cls, log):
        return None
