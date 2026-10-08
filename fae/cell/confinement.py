"""A claude agent runs confined: it reaches no MCP server (the account's
claude.ai connectors among them), none of the skills that account syncs, and
no web. Claude Code's built-in skills stay: they are the CLI's, the same for
every cell on its version.

Three layers say so, and each is checked: the credential is a `claude
setup-token` token, which can only make model requests and syncs nothing;
the agent's home carries settings that deny the rest; its command line drops
MCP and the web too. The agent's own init line is the proof: a session that
shows an MCP server or a denied tool is a breach, and a breach interrupts the
cell. Its skills are not judged there (built-in and synced ones look alike);
the init line keeps the list in the transcript.
"""
from __future__ import annotations

import json
from pathlib import Path

TOKEN_FILE = ".oauth_token"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_PREFIX = "sk-ant-oat01-"
DENIED_TOOLS = ("WebSearch", "WebFetch")
SETTINGS = {"permissions": {"deny": list(DENIED_TOOLS)},
            "disableClaudeAiConnectors": True,
            "syncClaudeAiSkills": False}
# --disallowedTools takes a list, so a flag must follow it on the command line.
CLI_LIMITS = ("--strict-mcp-config", "--disallowedTools", ",".join(DENIED_TOOLS))


class Breach(RuntimeError):
    """The agent is, or would run, unconfined."""


def token(home):
    """The setup-token token in the credentials home `home`; Breach when it
    is missing or is not one."""
    p = Path(home) / TOKEN_FILE
    value = p.read_text().strip() if p.is_file() else ""
    if not value:
        raise Breach(f"no {TOKEN_FILE} under {home}: a claude agent runs on a "
                     f"`claude setup-token` token, never a login")
    if not value.startswith(TOKEN_PREFIX):
        raise Breach(f"{p} is not a `claude setup-token` token")
    return value


def stage(dest):
    """Write the settings into the cell's agent home `dest`."""
    (Path(dest) / "settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")


def check_home(home):
    """Breach unless the staged home `home` holds exactly the settings and
    no login."""
    if (Path(home) / ".credentials.json").exists():
        raise Breach(f"{home} holds a login (.credentials.json)")
    try:
        staged = json.loads((Path(home) / "settings.json").read_text())
    except (OSError, ValueError):
        staged = None
    if staged != SETTINGS:
        raise Breach(f"{home}/settings.json is not the confinement settings")


class Watch:
    """Reads a stream-json transcript as it grows, for the session's init
    event; each poll reads only the complete lines written since the last."""

    def __init__(self, log):
        self.log, self.offset, self.init = Path(log), 0, None

    def poll(self):
        """The init event once written (a malformed one reads as {}), else None."""
        if self.init is not None:
            return self.init
        try:
            with self.log.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return None
        end = chunk.rfind(b"\n")
        if end < 0:
            return None
        self.offset += end + 1
        for line in chunk[:end].split(b"\n"):
            if b'"subtype":"init"' in line.replace(b" ", b""):
                try:
                    self.init = json.loads(line)
                except ValueError:
                    self.init = {}
                break
        return self.init


def breach(init):
    """What the init event shows that confinement denies, or that it lacks
    to prove it; empty when nothing."""
    missing = [k for k in ("mcp_servers", "tools") if not isinstance(init.get(k), list)]
    if missing:
        return f"init line without {missing}"
    found = []
    mcp = [s.get("name", "?") for s in init.get("mcp_servers") or []]
    if mcp:
        found.append(f"mcp servers {mcp}")
    tools = [t for t in init.get("tools") or [] if t in DENIED_TOOLS or t.startswith("mcp__")]
    if tools:
        found.append(f"tools {tools}")
    return "; ".join(found)
