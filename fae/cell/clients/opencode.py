"""opencode. Its home's opencode.json denies webfetch, but nothing the agent
prints proves which tools and MCP servers its session had: `opencode run`
prints the agent's text only. So it does not provide `breach`, and cannot
start, until a proof is found in what it writes (a JSON event stream, a log
of its tool set).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .base import AgentClient, Breach, refuse_dest, write_secret

KEY_FILE = "opencode.key"
KEY_ENV = "OPENCODE_API_KEY"


def key_file(home):
    """The opencode key path in the agent's credentials home."""
    return Path(home) / KEY_FILE


class Opencode(AgentClient):
    NAME = "opencode"
    HOME = ".agent-opencode"
    MOUNT = "/home/node/.config/opencode"
    SETUP_IN_IMAGE = False

    def credential_missing(self, home):
        key = key_file(home)
        return "" if key.is_file() and key.read_text().strip() else f"no API key in {key}"

    def setup_credential(self, home, image, ask, run, say):
        key = ask(f"{self.NAME}: the opencode API key (hidden): ").strip()
        if not key:
            return "no key given; nothing written"
        write_secret(key_file(home), key)
        return ""

    def stage_home(self, conf, dest, root):
        """opencode.json only: the model at temperature 0, sharing and
        autoupdate off, webfetch denied. The key reaches the agent by
        environment."""
        dest = str(dest)
        refuse_dest(dest, "/" + self.HOME)
        missing = self.credential_missing(conf.get("AGENT_HOME", ""))
        if missing:
            raise RuntimeError(f"{missing} (cli.py experiment credentials AGENT)")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        model = conf.get("AGENT_MODEL", "")
        prov, _, mod = model.partition("/")
        (Path(dest) / "opencode.json").write_text(
            '{\n  "$schema": "https://opencode.ai/config.json",\n'
            f'  "model": "{model}",\n  "share": "disabled",\n  "autoupdate": false,\n'
            f'  "provider": {{\n    "{prov}": {{ "models": {{ "{mod}": '
            '{ "options": { "temperature": 0 } } } }\n  },\n'
            '  "permission": { "edit": "allow", "bash": "allow", "webfetch": "deny",\n'
            '                  "external_directory": "allow" }\n}\n')

    def check_home(self, home):
        try:
            conf = json.loads((Path(home) / "opencode.json").read_text())
        except (OSError, ValueError):
            conf = None
        if not conf or (conf.get("permission") or {}).get("webfetch") != "deny":
            raise Breach(f"{home}/opencode.json does not deny webfetch")

    def env(self, conf):
        return {KEY_ENV: key_file(conf.get("AGENT_HOME", "")).read_text().strip()}

    def docker_args(self, home):
        return ["-v", f"{home}:{self.MOUNT}", "-e", KEY_ENV]

    def command(self, conf, prompt, feedback):
        return ["opencode", "run", "--print-logs", "--log-level", "ERROR",
                "--model", conf.get("AGENT_MODEL", ""), prompt]

    @classmethod
    def usage(cls, log):
        """opencode prints the agent's text only: no figures."""
        return None

    @classmethod
    def client_seconds(cls, log):
        return None
