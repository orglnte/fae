"""What an agent's transcript says about its own run: time spent in the
provider's API, turns, tool calls, tokens, retries, rate-limit pressure.
Each client reads its own transcript (AgentClient.usage, fae/cell/clients/);
a figure it does not record reads None, unknown rather than none.
"""
from __future__ import annotations

FIELDS = ("api_s", "turns", "tool_calls", "out_tok", "think_tok", "in_tok",
          "cost_usd", "retries", "retry_wait_s", "limited")
