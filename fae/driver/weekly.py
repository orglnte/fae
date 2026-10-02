"""The weekly Claude usage cap, and the per-lane limit cooldown.

Weekly cap: the claude lanes share one seven-day cap, and the CLI reports it
in every attempt log as a rate_limit_event: `allowed_warning` carries
`utilization` once usage passes the warning band, `rejected` is the cap
itself, plain `allowed` says nothing about the week. Conduct reads those
events, holds the budget lanes once the fleet has spent BUDGET_HOLD_AT of the
week, and releases them when the remainder is about to expire anyway.

Cooldown: a lane that hits a provider quota/rate wall parks itself for a
parsed-or-default interval rather than retrying a multi-hour cap in-cell.
"""
from __future__ import annotations

import os
import re
import time
import ujson as json
from datetime import datetime, timezone

from fae.driver import common
from fae.driver import queue
from fae.driver import state
from fae.driver.common import faults

# --- weekly cap ---------------------------------------------------------------
BUDGET_LANES = [m for m in os.environ.get("BUDGET_LANES", "fable,opus").split(",") if m]
BUDGET_HOLD_AT = float(os.environ.get("BUDGET_HOLD_AT", 0.75))
BUDGET_RELEASE_H = float(os.environ.get("BUDGET_RELEASE_H", 24))
_WEEKLY_EVENT_RE = re.compile(r'"type":"rate_limit_event","rate_limit_info":(\{[^}]*\})')


def _weekly_book():
    return common.ORCH / "weekly.json"


def weekly_load():
    try:
        st = json.loads(_weekly_book().read_text())
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("utilization", None)
    st.setdefault("resets_at", None)
    st.setdefault("seen_at", 0.0)
    st.setdefault("hold", [])
    return st


def weekly_save(st):
    common.ORCH.mkdir(parents=True, exist_ok=True)
    _weekly_book().write_text(json.dumps(st))


def weekly_cap_observe(st=None, now=None):
    """Fold every seven_day rate_limit_event logged since the last scan into
    the book; the newest event (log mtime, then line order) is the reading."""
    st = weekly_load() if st is None else st
    now = time.time() if now is None else now
    newest = None
    if common.WS.is_dir():
        for log in common.WS.glob("*/agent.attempt-*.log"):
            try:
                mt = log.stat().st_mtime
            except OSError:
                continue
            if mt <= st["seen_at"]:
                continue
            try:
                text = log.read_text(errors="replace")
            except OSError:
                continue
            for i, m in enumerate(_WEEKLY_EVENT_RE.finditer(text)):
                try:
                    info = json.loads(m.group(1))
                except ValueError:
                    continue
                if info.get("rateLimitType") != "seven_day":
                    continue
                if newest is None or (mt, i) > newest[0]:
                    newest = ((mt, i), info, log.parent.name)
    if newest is not None:
        (mt, _), info, cid = newest
        status = info.get("status")
        util = (1.0 if status == "rejected"
                else info.get("utilization") if status == "allowed_warning"
                else None)
        st.update(utilization=util, resets_at=info.get("resetsAt"),
                  source_cid=cid, event_at=mt)
    st["seen_at"] = now
    weekly_save(st)
    return st


def weekly_reading(st, now=None):
    """(utilization, resets_at) as of now: a reading from before the reset
    says nothing about this week."""
    now = time.time() if now is None else now
    resets = st.get("resets_at")
    if resets and resets <= now:
        return None, resets
    return st.get("utilization"), resets


def weekly_budget_apply(st, now=None, out=print):
    """Hold the budget lanes past BUDGET_HOLD_AT; release them within
    BUDGET_RELEASE_H of the reset or once it has passed. Lanes the operator
    parked are not conduct's to touch: only lanes in `hold` are released."""
    now = time.time() if now is None else now
    util, resets = weekly_reading(st, now)
    hold = list(st.get("hold", []))
    near_reset = bool(resets) and resets - now <= BUDGET_RELEASE_H * 3600
    changed = False
    for m in list(hold):
        if near_reset:
            r = queue.unpark_lane(m)
            hold.remove(m)
            changed = True
            left = max(0.0, (resets - now) / 3600)
            out(f"  [{common._hhmm()}] weekly cap: released {m} ({r}; the reset is "
                f"{left:.0f}h away — spend the remainder)")
    if util is not None and util >= BUDGET_HOLD_AT and not near_reset:
        parked = []
        for m in BUDGET_LANES:
            if m in hold or queue.lane_dir(m, parked=True).is_dir():
                continue                      # held already, or the operator's park
            if queue.park_lane(m) == "empty":
                queue.lane_dir(m, parked=True).mkdir(parents=True, exist_ok=True)
            hold.append(m)
            parked.append(m)
            changed = True
        if parked:
            when = (f"{datetime.fromtimestamp(resets, timezone.utc):%a %H:%M}Z"
                    if resets else "unknown")
            out(f"  [{common._hhmm()}] ALERT weekly cap {util:.0%} — budget hold: "
                f"parked {', '.join(parked)} (resets {when}); the other lanes "
                f"keep the remaining {1 - util:.0%}")
    if changed:
        st["hold"] = hold
        weekly_save(st)
    return st


def weekly_hold_clear(agent, out=print):
    """An operator resume of a held lane ends the hold: conduct will not
    re-park it until the next reading crosses the threshold again."""
    st = weekly_load()
    if agent in st["hold"]:
        st["hold"].remove(agent)
        weekly_save(st)
        out(f"  queue[{agent}]: budget hold cleared by the operator")


def weekly_line(now=None):
    """One status line: what the fleet knows about this week's cap."""
    st = weekly_load()
    now = time.time() if now is None else now
    util, resets = weekly_reading(st, now)
    if util is None:
        what = "<{:.0%}".format(BUDGET_HOLD_AT) if st.get("event_at") else "unknown"
    else:
        what = f"{util:.0%}"
    # the reading is only as fresh as the last seven_day event any agent
    # logged; say how old it is, and call it stale past a day
    age_s = now - st["event_at"] if st.get("event_at") else None
    if age_s is not None and util is not None:
        age = f"{age_s / 3600:.0f}h" if age_s < 172800 else f"{age_s / 86400:.0f}d"
        what += f" as of {age} ago" + (", STALE" if age_s > 86400 else "")
    when = (f" (resets {datetime.fromtimestamp(resets, timezone.utc):%a %H:%M}Z)"
            if resets and resets > now else "")
    hold = f" · hold: {', '.join(st['hold'])}" if st.get("hold") else ""
    return f"claude weekly: {what}{when}{hold}"


# --- limit cooldown -------------------------------------------------------------
COOLDOWN_DEFAULT_S = int(os.environ.get("LIMIT_COOLDOWN_S", 3 * 3600))


def _cooldown_file(agent):
    return common.ORCH / f"cooldown.{agent}"


def _is_quota_wall(text):
    """True only for provider QUOTA/RATE messages. The harness's `limit`
    phase also covers transient connection faults (refused, closed
    mid-response, timeouts), which its own retry loop heals in minutes —
    cooling a lane hours for those idles a healthy agent.

    `hit your (usage|session) limit` plus a bare `resets \\d` (clock-time
    form, "resets 7:40pm (UTC)") cover the Claude Code CLI's own wording,
    which matches none of the other branches: it says "hit your ... limit",
    not "limit reached/exceeded", and "resets 7:40pm" has no "in"/"at" for
    the relative-duration branch. Without these every claude-backed lane
    retried a real multi-hour session cap in-cell forever instead of
    cooling.
    """
    return faults.quota_wall(text)


def _parse_reset_hint(text):
    """Seconds until a provider limit resets, parsed from its error message;
    None when the message carries no usable hint. Day-scale hints ("Resets
    in 3 days") come from weekly caps.

    A clock-time hint ("resets 7:40pm (UTC)") is the Claude Code CLI's
    session-limit wording — always UTC, always a future point today or
    tomorrow, never a duration. Tried second since it is the more permissive
    pattern and would otherwise shadow a message that also carries a
    relative duration.
    """
    return faults.reset_hint_s(text, now=datetime.now(timezone.utc))


def _set_cooldown(agent, detail):
    until = time.time() + (_parse_reset_hint(detail) or COOLDOWN_DEFAULT_S)
    _cooldown_file(agent).write_text(f"{int(until)} {detail[:state.WAIT_REASON_MAX]}\n")
    return int(until)


def _cooldown_until(agent):
    """Epoch seconds until which the lane is limit-cooling, 0 = not cooling."""
    try:
        return int(_cooldown_file(agent).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def _parked_count():
    return sum(len(queue._dir_specs(d)) for d in queue._parked_queues())
