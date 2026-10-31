"""What an agent's transcript says about its own run: time spent in the
provider's API, turns, tool calls, tokens, retries, rate-limit pressure.

claude's stream-json carries all of them. One run can close with several
`result` events: a background task that finishes resumes the session, and
each resumption closes again. `duration_api_ms` and `total_cost_usd` are
running totals across them; `num_turns` and `usage` count one resumption each.

agy's stream-json (`"event"` lines) carries tokens and its steps, not its API
time, cost, retries or rate limits: those read None, unknown rather than none.
Its `num_turns` counts the user's turns, so the agent's turns are its
`agent_response` steps and its tool calls its `tool` steps, each counted once
by `step_index`. opencode prints the agent's text only and reads as None.
"""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = ("api_s", "turns", "tool_calls", "out_tok", "think_tok", "in_tok",
          "cost_usd", "retries", "retry_wait_s", "limited")
_INPUT = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _events(log):
    try:
        text = Path(log).read_text(errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            yield e


def _tool_use_ids(event):
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b.get("id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def _sum(values):
    return sum(values) if all(isinstance(v, int) for v in values) else None


def usage(cli, log) -> dict | None:
    """The run's figures (FIELDS), or None when `cli` records none or the
    transcript has no result event."""
    if cli == "claude":
        return _claude(log)
    if cli == "agy":
        return _agy(log)
    return None


def _claude(log):
    """retries counts the client's API retries and retry_wait_s the backoff it
    announced before them; limited counts rate-limit events whose status is
    not "allowed"."""
    results, tools, retries, retry_ms, limited = [], set(), 0, 0, 0
    for e in _events(log):
        kind = e.get("type")
        if kind == "assistant":
            tools.update(_tool_use_ids(e))
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
        "turns": _sum([r.get("num_turns") for r in results]),
        "tool_calls": len(tools),
        "out_tok": _sum([u.get("output_tokens") for u in us]),
        "think_tok": _sum([(u.get("output_tokens_details") or {}).get("thinking_tokens")
                           for u in us]),
        "in_tok": _sum([_sum([u.get(k) for k in _INPUT]) for u in us]),
        "cost_usd": last.get("total_cost_usd"),
        "retries": retries,
        "retry_wait_s": round(retry_ms / 1000, 1),
        "limited": limited,
    }


def _agy(log):
    results, steps = [], {"agent_response": set(), "tool": set()}
    for e in _events(log):
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
        "out_tok": _sum([u.get("output_tokens") for u in us]),
        "think_tok": _sum([u.get("thinking_tokens") for u in us]),
        "in_tok": _sum([_sum([u.get("input_tokens"), u.get("cache_read_tokens")]) for u in us]),
        "cost_usd": None,
        "retries": None,
        "retry_wait_s": None,
        "limited": None,
    }
