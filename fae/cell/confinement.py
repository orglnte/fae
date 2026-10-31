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

An agy agent runs confined the same way: no web, no browser, no MCP server
or plugin, and no subagent it defines itself, which could be given any tool.
agy has no flag that removes a built-in tool, so the cell's home carries a
custom agent, `fae`, whose `tools` list names the ones it keeps, and the
command line selects it. agy's init line lists its default tools whatever
the agent, so the proof is the session's steps: a call to a tool outside
that list is a breach, read as the transcript grows.
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

AGY_AGENT = "fae"
# agy moves antigravity-cli/agents/ here and links it back
AGY_AGENT_FILE = f"config/agents/{AGY_AGENT}/agent.md"
# agy 1.4.12's default tools less search_web, read_url_content, every browser
# tool, call_mcp_tool, read_resource, list_resources, search_marketplace,
# list_plugin_accounts, define_subagent and manage_subagents, and less the six
# a custom agent cannot name ("not found in registry"): delete_knowledge,
# list_permissions, manage_inbox, notebook_execution, sed_file, wait_5_seconds
AGY_TOOLS = (
    "ask_custom_permission", "ask_permission", "ask_question", "command_status",
    "find_by_name", "finish", "generate_image", "grep_search", "invoke_subagent",
    "list_dir", "manage_task", "multi_replace_file_content", "notebook_edit",
    "replace_file_content", "run_command", "run_workflow", "schedule",
    "send_command_input", "send_message", "view_file", "wait", "write_to_file")
AGY_AGENT_MD = ("---\n"
                f"name: {AGY_AGENT}\n"
                "description: The agent of an FAE cell.\n"
                "mainAgent: true\n"
                f"tools: [{', '.join(AGY_TOOLS)}]\n"
                "---\n"
                f"# {AGY_AGENT}\n")
AGY_CLI_LIMITS = ("--agent", AGY_AGENT)


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


def stage_agy(dest):
    """Write the confined custom agent into the cell's agy home `dest`."""
    p = Path(dest) / AGY_AGENT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(AGY_AGENT_MD)


def check_agy_home(home):
    """Breach unless the staged agy home `home` holds the confined agent."""
    p = Path(home) / AGY_AGENT_FILE
    if not p.is_file() or p.read_text() != AGY_AGENT_MD:
        raise Breach(f"{p} is not the confined agent")


class Watch:
    """Reads a stream-json transcript as it grows: the session's init event,
    and the tools its steps called (agy's `step_update` events); each poll
    reads only the complete lines written since the last."""

    def __init__(self, log):
        self.log, self.offset, self.init = Path(log), 0, None
        self.tools_used = []

    def poll(self):
        """The init event once written (a malformed one reads as {}), else None."""
        try:
            with self.log.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return self.init
        end = chunk.rfind(b"\n")
        if end < 0:
            return self.init
        self.offset += end + 1
        for line in chunk[:end].split(b"\n"):
            squeezed = line.replace(b" ", b"")
            if self.init is None and (b'"subtype":"init"' in squeezed
                                      or b'"event":"init"' in squeezed):
                try:
                    self.init = json.loads(line)
                except ValueError:
                    self.init = {}
            elif b'"event":"step_update"' in squeezed:
                try:
                    step = json.loads(line).get("step_update") or {}
                except (ValueError, AttributeError):
                    continue
                tool = step.get("tool_name")
                if tool and tool not in self.tools_used:
                    self.tools_used.append(tool)
        return self.init


def breach(init, cli, tools_used=()):
    """What `cli`'s session shows that confinement denies, or that it lacks
    to prove it; empty when nothing. claude's init event lists its tools and
    MCP servers. agy's lists its default tools whatever the agent, so an agy
    session is judged by the tools its steps called (`tools_used`)."""
    if cli == "agy":
        if not isinstance(init.get("init"), dict):
            return "init line without ['init']"
        denied = [t for t in tools_used if t not in AGY_TOOLS]
        return f"tools called {denied}" if denied else ""
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
