"""Provider-side agent faults: the CLI printed a wall, not a build.

One vocabulary for both readers. The driver asks whether an invocation may be
judged at all (a limit retries the same attempt; an auth or config fault
halts the cell for a human); the orchestrator asks whether a lane must cool
and until when. Two regexes in two files drifted once — the driver missed a
wording the orchestrator knew — and ten attempts burned in twenty seconds.

Stdlib only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LIMIT = "limit"      # retry the same attempt; the lane may need cooling
AUTH = "auth"        # credentials or configuration: a human, not a retry

# A quota or rate wall: the lane must cool until the provider's reset.
QUOTA_RE = re.compile(
    r"hit your \w+ limit|usage limit|session limit|weekly limit|"
    r"monthly usage limit|quota (reached|exceeded)|individual quota|"
    r"rate.?limit|too many requests|upgrade your (subscription|plan)|"
    r"limit (reached|exceeded)|insufficient_quota|insufficient credits|"
    r"resets? (in|at) |resets? (?:[A-Z][a-z]{2} \d{1,2}, )?\d{1,2}(:\d{2})?\s*[ap]m",
    re.I)

# Anything the retry loop heals by waiting: the quota walls above plus
# server-side and transport faults. Applied to a structured result's text
# only — a transcript is full of the task's own vocabulary.
LIMIT_RE = re.compile(
    QUOTA_RE.pattern + r"|overloaded|api error|internal server error|"
    r"service unavailable|bad gateway|gateway time.?out|"
    r"timeout waiting for response|econnreset|socket hang up|"
    r"connection (error|closed|refused)|unable to connect|failedtoopensocket|"
    r"provider returned error|AI_APICallError|AI_RetryError|stream error",
    re.I)

# What a plain-text CLI itself prints when walled — searched across the whole
# transcript, so nothing an agent would write into a service goes here: no
# bare "rate limit", "429", "too many requests", "connection refused".
PLAIN_LIMIT_RE = re.compile(
    r"hit your \w+ limit|usage limit|session limit|weekly limit|"
    r"monthly usage limit|quota (reached|exceeded)|individual quota|"
    r"upgrade your (subscription|plan)|limit (reached|exceeded)|"
    r"insufficient_quota|insufficient credits|resets? in \d|"
    r"API Error:|AI_APICallError|AI_RetryError|provider returned error|"
    r"rate_limit_error|overloaded_error|eligibility check failed",
    re.I)

# Walls no wait lifts. Deliberately narrow: "unauthorized" alone is what a
# tool the agent runs (kubectl on an RBAC miss) prints inside the transcript.
AUTH_RE = re.compile(
    r"authentication required\. please visit|not logged in|please run /login|"
    r"authentication_failed|invalid authentication|invalid api key|"
    r"oauth token has expired|issue with the selected model",
    re.I)

# HTTP statuses the structured result line may carry.
AUTH_STATUSES = {401, 403, 404}


@dataclass(frozen=True)
class AgentResult:
    """The last `"type":"result"` line of a stream-json transcript."""
    api_error_status: int | None
    is_error: bool
    text: str


def parse_result_line(log_text):
    """The structured verdict of a Claude CLI run, or None for a transcript
    that has none (plain-text CLIs, a run killed before it wrote one)."""
    for line in reversed(log_text.splitlines()):
        if '"type"' not in line or '"result"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "result":
            continue
        st = d.get("api_error_status")
        return AgentResult(st if isinstance(st, int) else None,
                           bool(d.get("is_error")), str(d.get("result") or ""))
    return None


def classify(log_text, rc, gate_rc=True):
    """LIMIT, AUTH, or None (the run may be judged).

    A structured result line is authoritative whatever the exit code: a
    finished run never carries `api_error_status`, and its `is_error` is
    false. Without one, the whole transcript is searched — but only for a
    non-zero exit when `gate_rc` holds, because the task domain is limit
    vocabulary and a finished run routinely describes its own backoff code.
    """
    r = parse_result_line(log_text)
    if r is not None:
        if r.api_error_status is not None:
            if r.api_error_status in AUTH_STATUSES or AUTH_RE.search(r.text):
                return AUTH
            return LIMIT
        if r.is_error:
            if AUTH_RE.search(r.text):
                return AUTH
            if LIMIT_RE.search(r.text):
                return LIMIT
        return None
    if gate_rc and rc == 0:
        return None
    if AUTH_RE.search(log_text):
        return AUTH
    if PLAIN_LIMIT_RE.search(log_text):
        return LIMIT
    return None


def quota_wall(text):
    """Is this a wall the lane must cool for (not a transport hiccup)?"""
    return bool(QUOTA_RE.search(text))


def reason(log_text):
    """The line that names the fault, for ledgers and cooldown files: the
    structured result text when there is one, else the last line that
    matches."""
    r = parse_result_line(log_text)
    if r is not None and r.text:
        return r.text.strip()
    lines = [l.strip() for l in log_text.splitlines() if l.strip()]
    for line in reversed(lines):
        if PLAIN_LIMIT_RE.search(line) or AUTH_RE.search(line):
            return line
    return lines[-1] if lines else ""


_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION_RE = re.compile(
    r"(?:resets?|retry|try again)\s+(?:in|after)\s+"
    r"((?:\d+\s*(?:d|days?|h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)(?![a-z])\s*)+)",
    re.I)
_PIECE_RE = re.compile(r"(\d+)\s*([dhms])", re.I)
_CLOCK_RE = re.compile(
    r"resets?\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),\s*)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(?utc\)?", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def reset_hint_s(text, now=None):
    """Seconds until the provider says the wall lifts, or None when the
    message carries no hint. Durations ("Resets in 3 hours", "11m35s",
    "21 days") and UTC clock times ("resets 7:40pm (UTC)", "resets 12am
    (UTC)", "resets Jul 29, 12am (UTC)"); a clock time is the next such
    instant after `now`."""
    m = _DURATION_RE.search(text)
    if m:
        total = sum(int(n) * _UNIT_S[u.lower()]
                    for n, u in _PIECE_RE.findall(m.group(1)))
        return total if total > 0 else None
    m = _CLOCK_RE.search(text)
    if not m:
        return None
    now = now or datetime.now(timezone.utc)
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    minute = int(m.group(4) or 0)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if m.group(1):
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            return None
        try:
            t = t.replace(month=month, day=int(m.group(2)))
        except ValueError:
            return None
        if t <= now:
            t = t.replace(year=t.year + 1)
    elif t <= now:
        t = t + timedelta(days=1)
    return int((t - now).total_seconds())
