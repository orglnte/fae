"""Antigravity's agy. Its agent reaches no web, no browser, no MCP server or
plugin, and no subagent it defines itself, which could be given any tool.
agy has no flag that removes a built-in tool, so the cell's home carries a
custom agent, `fae`, whose `tools` list names the ones it keeps, and the
command line selects it. agy's init line lists its default tools whatever
the agent, so the proof is the session's steps: a call to a tool outside
that list is a breach, read as the transcript grows.

Its stream-json (`"event"` lines) carries tokens and its steps, not its API
time, cost, retries or rate limits: those read None, unknown rather than
none. Its `num_turns` counts the user's turns, so the agent's turns are its
`agent_response` steps and its tool calls its `tool` steps, each counted
once by `step_index`.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .base import AgentClient, Breach, events, refuse_dest, sum_ints

# What a cell's agy home holds of the sign-in home: the sign-in and the
# identity agy keeps with it. Its memory, logs and settings stay behind
# (conversation summaries, the sign-in session's log, any agentMode).
TOKEN = "antigravity-cli/antigravity-oauth-token"
LOGIN = (TOKEN, "antigravity-cli/installation_id",
         "antigravity-cli/cache/default_project_id.txt",
         "antigravity-cli/cache/onboarding.json",
         "config/projects/default-cli-project.json")
SETTINGS = "antigravity-cli/settings.json"

AGENT = "fae"
# agy moves antigravity-cli/agents/ here and links it back
AGENT_FILE = f"config/agents/{AGENT}/agent.md"
# agy 1.4.12's default tools less search_web, read_url_content, every browser
# tool, call_mcp_tool, read_resource, list_resources, search_marketplace,
# list_plugin_accounts, define_subagent and manage_subagents, and less the six
# a custom agent cannot name ("not found in registry"): delete_knowledge,
# list_permissions, manage_inbox, notebook_execution, sed_file, wait_5_seconds
TOOLS = (
    "ask_custom_permission", "ask_permission", "ask_question", "command_status",
    "find_by_name", "finish", "generate_image", "grep_search", "invoke_subagent",
    "list_dir", "manage_task", "multi_replace_file_content", "notebook_edit",
    "replace_file_content", "run_command", "run_workflow", "schedule",
    "send_command_input", "send_message", "view_file", "wait", "write_to_file")
# Tools a session may call beyond the agent file's list: agy 1.5.3 runs
# read_file though neither its init line nor the agent file names it, and it
# reads a file as view_file does, inside the same container mounts. Kept out of
# the agent file, which stays the list agy has accepted.
ALSO_ALLOWED = ("read_file",)
AGENT_MD = ("---\n"
            f"name: {AGENT}\n"
            "description: The agent of an FAE cell.\n"
            "mainAgent: true\n"
            f"tools: [{', '.join(TOOLS)}]\n"
            "---\n"
            f"# {AGENT}\n")
CLI_LIMITS = ("--agent", AGENT)


class Agy(AgentClient):
    NAME = "agy"
    HOME = ".agent-gemini"
    MOUNT = "/home/node/.gemini"

    def credential_missing(self, home):
        signed_in = (Path(home) / TOKEN).is_file()
        return "" if signed_in else f"no signed-in agy home at {home}"

    def setup_credential(self, home, image, ask, run, say):
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        say(f"{self.NAME}: sign in inside the container, then exit it.")
        run(["docker", "run", "-it", "--rm", "-v", f"{home}:{self.MOUNT}", image, "agy"])
        return ""

    def stage_home(self, conf, dest, root):
        """The sign-in files (LOGIN), a settings file of FAE's own, so the
        agent's mode comes from its argv alone, and the confined agent."""
        dest = str(dest)
        refuse_dest(dest, "/" + self.HOME)
        home = Path(conf.get("AGENT_HOME", ""))
        missing = self.credential_missing(home)
        if missing:
            raise RuntimeError(f"{missing} — python3 cli.py experiment credentials agy")
        shutil.rmtree(dest, ignore_errors=True)
        for rel in LOGIN:
            if (home / rel).is_file():
                (Path(dest) / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(home / rel, Path(dest) / rel)
        (Path(dest) / SETTINGS).write_text("{}\n")
        agent = Path(dest) / AGENT_FILE
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(AGENT_MD)

    def check_home(self, home):
        p = Path(home) / AGENT_FILE
        if not p.is_file() or p.read_text() != AGENT_MD:
            raise Breach(f"{p} is not the confined agent")

    def breach(self, init, tools_used):
        if not isinstance(init.get("init"), dict):
            return "init line without ['init']"
        denied = [t for t in tools_used if t not in TOOLS + ALSO_ALLOWED]
        return f"tools called {denied}" if denied else ""

    def env(self, conf):
        return {}

    def docker_args(self, home):
        return ["-v", f"{home}:{self.MOUNT}"]

    def command(self, conf, prompt, feedback):
        dirs = ["--add-dir", "/workspace"] + (["--add-dir", "/feedback"] if feedback else [])
        stream = ["--output-format", "stream-json"] if conf.get("STREAM_AGENT", "") else []
        return ["agy", "--sandbox", "--dangerously-skip-permissions", *CLI_LIMITS, *dirs,
                "--model", conf.get("AGENT_MODEL", ""), *stream,
                "--print-timeout", "60m", "--print", prompt]

    @classmethod
    def usage(cls, log):
        results, steps = [], {"agent_response": set(), "tool": set()}
        for e in events(log):
            if e.get("event") == "step_update":
                st = e.get("step_update") or {}
                if st.get("step_type") in steps:
                    steps[st["step_type"]].add(st.get("step_index"))
            elif e.get("event") == "result":
                results.append(e.get("result") or {})
        if not results:
            return None
        us = [r.get("usage") or {} for r in results]
        return {
            "api_s": None,
            "turns": len(steps["agent_response"]),
            "tool_calls": len(steps["tool"]),
            "out_tok": sum_ints([u.get("output_tokens") for u in us]),
            "think_tok": sum_ints([u.get("thinking_tokens") for u in us]),
            "in_tok": sum_ints([sum_ints([u.get("input_tokens"), u.get("cache_read_tokens")])
                                for u in us]),
            "cost_usd": None,
            "retries": None,
            "retry_wait_s": None,
            "limited": None,
        }

    @classmethod
    def client_seconds(cls, log):
        secs = [(e.get("result") or {}).get("duration_seconds")
                for e in events(log) if e.get("event") == "result"]
        if not secs or not all(isinstance(v, (int, float)) for v in secs):
            return None
        return sum(secs), len(secs)
