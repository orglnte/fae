"""Claude Code. Its agent reaches no MCP server (the account's claude.ai
connectors among them), none of the skills that account syncs, and no web.
Claude Code's built-in skills stay: they are the CLI's, the same for every
cell on its version.

Three layers say so, and each is checked: the credential is a `claude
setup-token` token, which can only make model requests and syncs nothing;
the agent's home carries settings that deny the rest; its command line drops
MCP and the web too. The agent's own init line is the proof: a session that
shows an MCP server or a denied tool is a breach. Its skills are not judged
there (built-in and synced ones look alike); the init line keeps the list in
the transcript.

Its stream-json carries every transcript figure. One run can close with
several `result` events: a background task that finishes resumes the
session, and each resumption closes again. `duration_api_ms` and
`total_cost_usd` are running totals across them; `num_turns` and `usage`
count one resumption each.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from .base import AgentClient, Breach, events, refuse_dest, sum_ints, write_secret

TOKEN_FILE = ".oauth_token"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_PREFIX = "sk-ant-oat01-"
DENIED_TOOLS = ("WebSearch", "WebFetch")
SETTINGS = {"permissions": {"deny": list(DENIED_TOOLS)},
            "disableClaudeAiConnectors": True,
            "syncClaudeAiSkills": False}
# --disallowedTools takes a list, so a flag must follow it on the command line.
CLI_LIMITS = ("--strict-mcp-config", "--disallowedTools", ",".join(DENIED_TOOLS))
_INPUT = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


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


class Claude(AgentClient):
    NAME = "claude"
    HOME = ".agent-claude"
    MOUNT = "/home/node/.claude"

    def credential_missing(self, home):
        try:
            token(home)
        except Breach as e:
            return str(e)
        return ""

    def setup_credential(self, home, image, ask, run, say):
        home = Path(home)
        say(f"{self.NAME}: a browser sign-in follows; copy the token it prints.")
        run(["docker", "run", "-it", "--rm", image, "claude", "setup-token"])
        value = ask("Paste the token (hidden): ").strip()
        if not value.startswith(TOKEN_PREFIX):
            return "that is not a `claude setup-token` token; nothing written"
        write_secret(home / TOKEN_FILE, value)
        login = home / ".credentials.json"
        if login.exists():
            aside = home / ".to_be_deleted" / datetime.now().strftime("%Y%m%d-%H%M%S")
            aside.mkdir(parents=True)
            shutil.move(str(login), aside / login.name)
            say(f"{self.NAME}: the old login moved to {aside}")
        return ""

    def stage_home(self, conf, dest, root):
        """The settings only: the token stays out of the cell's home and
        reaches the agent by environment."""
        dest = str(dest)
        refuse_dest(dest, "/" + self.HOME)
        try:
            token(conf.get("AGENT_HOME", ""))
        except Breach as e:
            raise RuntimeError(f"{e} (cli.py experiment credentials AGENT)") from e
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        (Path(dest) / "settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n")

    def check_home(self, home):
        if (Path(home) / ".credentials.json").exists():
            raise Breach(f"{home} holds a login (.credentials.json)")
        try:
            staged = json.loads((Path(home) / "settings.json").read_text())
        except (OSError, ValueError):
            staged = None
        if staged != SETTINGS:
            raise Breach(f"{home}/settings.json is not the confinement settings")

    def breach(self, init, tools_used):
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

    def env(self, conf):
        return {TOKEN_ENV: token(conf.get("AGENT_HOME", ""))}

    def docker_args(self, home):
        return ["-v", f"{home}:{self.MOUNT}", "-e", TOKEN_ENV]

    def command(self, conf, prompt, feedback):
        tail = ["claude", "-p", "--dangerously-skip-permissions", *CLI_LIMITS,
                "--model", conf.get("AGENT_MODEL", "")]
        if feedback:
            tail += ["--add-dir", "/feedback"]
        if conf.get("EFFORT", ""):
            tail += ["--effort", conf.get("EFFORT", "")]
        if conf.get("STREAM_AGENT", ""):
            tail += ["--output-format", "stream-json", "--verbose"]
        return tail + [prompt]

    @classmethod
    def usage(cls, log):
        """retries counts the client's API retries and retry_wait_s the
        backoff it announced before them; limited counts rate-limit events
        whose status is not "allowed"."""
        results, tools, retries, retry_ms, limited = [], set(), 0, 0, 0
        for e in events(log):
            kind = e.get("type")
            if kind == "assistant":
                content = (e.get("message") or {}).get("content")
                if isinstance(content, list):
                    tools.update(b.get("id") for b in content
                                 if isinstance(b, dict) and b.get("type") == "tool_use")
            elif kind == "system" and e.get("subtype") == "api_retry":
                retries += 1
                delay = e.get("retry_delay_ms")
                retry_ms += delay if isinstance(delay, (int, float)) else 0
            elif kind == "rate_limit_event":
                if (e.get("rate_limit_info") or {}).get("status") not in (None, "allowed"):
                    limited += 1
            elif kind == "result":
                results.append(e)
        if not results:
            return None
        last = results[-1]
        us = [r.get("usage") or {} for r in results]
        api_ms = last.get("duration_api_ms")
        return {
            "api_s": round(api_ms / 1000, 1) if isinstance(api_ms, (int, float)) else None,
            "turns": sum_ints([r.get("num_turns") for r in results]),
            "tool_calls": len(tools),
            "out_tok": sum_ints([u.get("output_tokens") for u in us]),
            "think_tok": sum_ints([(u.get("output_tokens_details") or {}).get("thinking_tokens")
                                   for u in us]),
            "in_tok": sum_ints([sum_ints([u.get(k) for k in _INPUT]) for u in us]),
            "cost_usd": last.get("total_cost_usd"),
            "retries": retries,
            "retry_wait_s": round(retry_ms / 1000, 1),
            "limited": limited,
        }

    @classmethod
    def client_seconds(cls, log):
        """A finished background task resumes the session and it closes
        again, so a resumed run's seconds are the sum of its closings and
        leave out the waits between them."""
        ms = [e.get("duration_ms") for e in events(log) if e.get("type") == "result"]
        if not ms or not all(isinstance(v, (int, float)) for v in ms):
            return None
        return sum(ms) / 1000, len(ms)
