"""The console: tables, the live views (status/watch/monitor), and the
backlog summary they all fold in.

render() is the one table-builder every view (status, watch, monitor) prints
through — a single source keeps the flat and grouped layouts consistent.
queued_summary/_pending_kind read the backlog (fae/queues.py) and resolve
each pending spec's state (fae/driver/state.py) into the QUEUED section's tags.
"""
from __future__ import annotations

import collections
import os
import time
import ujson as json
from datetime import datetime, timezone

from fae.driver import common
from fae.driver import state
from fae.driver import zombies
from fae.driver.common import faults


# --- tables -------------------------------------------------------------------

def fmt_table(rows, hdr):
    rows = [hdr] + rows
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(hdr))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
                     for r in rows)


def _tail_hist(h, n):
    """Last n chars of a stage history, cut at a token boundary with an
    ellipsis instead of mid-word."""
    if len(h) <= n:
        return h
    cut = h[-(n - 1):]
    return "…" + (cut.split(",", 1)[-1] if "," in cut else cut)


def _pending_kind(cid, live_loops):
    """What a pending spec is waiting on — the state its CELL is in:

      fresh        no workspace: nobody has run this rep
      prepared     seeded, never launched
      interrupted  ran and stopped (crash, hang-kill, operator stop)
      paused       an operator (or a wall stand-down) holds it
      flagged      out of repair budget: needs `cell resume`
      running      its cell is live — admission skips it
      done         already terminal: admission retires the spec
    """
    ws = common.WS / cid
    if not ws.is_dir():
        return "fresh"
    if (ws / "reconcile.flagged").exists():
        return "flagged"
    if state.pause_lock(cid):
        return "paused"
    if cid in live_loops:
        return "running"
    st = state.cell_state(ws, {}, set())
    if st and st["state"] == "DONE":
        return "done"
    if st and state.never_started(st):
        return "prepared"
    return "interrupted"


def queued_summary():
    """Pending specs per agent, newest queue state. Returns display lines.

    The headline counts every pending spec and breaks it down by what
    admission will find when it reaches it — see _pending_kind. Only `fresh`
    and `prepared` are work nobody has started; the rest are queued for a
    reason the operator can act on.
    """
    qs = common.queues()
    rows, total = [], 0
    kinds = collections.Counter()
    live_loops = set(state.loop_parents())
    # A parked lane is the operator's pause (`experiment pause M`): its specs are
    # untouched, so they are still backlog — shown here tagged rather than
    # vanishing from the fleet picture.
    for d in qs.lane_dirs(include_parked=True):
        is_parked = d.name.endswith(".parked")
        agent = qs.lane_agent(d)
        paths = qs.specs_in(d)
        if not paths:
            continue
        for p in paths:
            kinds[_pending_kind(qs.spec_cid(p), live_loops)] += 1
        total += len(paths)
        try:
            nxt = qs.read_spec(paths[0])
        except (OSError, ValueError):
            continue
        tags = (["[BUDGET-HOLD]" if agent in qs.weekly_load()["hold"] else "[PAUSED]"]
                if is_parked else [])
        _cu = qs.cooldown_until(agent)
        if _cu > time.time():
            tags.append(f"[LIMIT until "
                        f"{datetime.fromtimestamp(_cu, timezone.utc):%m-%d %H:%M}Z]")
        # `next:` names the head of the lane, and admission may refuse it: a
        # paused cell is never started, an interrupted one resumes mid-budget.
        # The same breakdown the headline gives, per lane, so a lane that is
        # not plain backlog says what it actually holds. `fresh` is omitted —
        # that is the ordinary case and every lane would carry it.
        _kinds = collections.Counter(_pending_kind(qs.spec_cid(q), live_loops)
                                     for q in paths)
        for _k in ("prepared", "interrupted", "paused", "flagged",
                   "running", "done"):
            if _kinds[_k]:
                tags.append(f"[{_kinds[_k]} {_k}]")
        # The tags are the reason a lane is not moving, so they line up in
        # their own column instead of trailing a variable-length spec name.
        nxt_txt = f"{nxt['variant']} r{nxt['rep']}"
        rows.append(f"  {agent:8} {len(paths):3} pending  next: "
                    f"{nxt_txt:<28}{'  '.join(tags)}")
    if not rows:
        return []
    order = ["fresh", "prepared", "interrupted", "paused", "flagged",
             "running", "done"]
    of_which = ", ".join(f"{kinds[k]} {k}" for k in order if kinds[k])
    head = f"\n— QUEUED ({total}) — of which: {of_which} "
    return [head + "—" * max(4, 78 - len(head))] + rows


def requeued(s):
    """A crashed cell whose spec waits in its lane: admission restarts it,
    nobody needs to act."""
    return s["state"] == "CRASHED" and state._queued(s["cid"])


def display_state(s):
    """STATE·why as experiment status prints it; a requeued crash reads as queued."""
    if requeued(s):
        return "QUEUED·interrupted"
    return s["state"] + (f"·{s['why']}" if s["why"] else "")


def render(flat=False, running_only=False):
    states, loops, boxes = state.all_states(running_only)
    n_loops = len(state.loop_parents())   # real loops — tees outlive theirs
    out = []
    if flat:
        rows = [(s["cid"],
                 s["agent_model"][:24],
                 s["state"] + (f"·{s['why']}" if s["why"] else "")
                 + (" ⚠" if s.get("taint") else "")
                 + (f" @{s['green_at']}" if s["green_at"] else
                    f" @{s['att']}" if s["state"] == "DONE" else
                    f" {s['att']}/{s['budget']}"),
                 s["live"], s["shape"],
                 s["hist"][:48], s["detail"]) for s in states]
        out.append(fmt_table(rows, ("CELL", "AGENT VERSION", "STATE", "LIVE", "GATE", "LAST REP", "LAST ERR / BLOCK")))
    else:
        # Two tables (operator request 2026-07-25): everything WORKING in one
        # table up top; everything else in one table ordered label > agent.
        # Derived from the definition, not a copy of its mapping.
        variants = common.definition().variants
        label_of = {vid: cls.LABEL for vid, cls in variants.items()}

        def vshort(agent, version):
            # version only — the agent name is its own column/id already
            v = version.replace("claude-", "")
            for pfx in (agent + "-", agent + " ", agent.capitalize() + " "):
                if v.startswith(pfx):
                    v = v[len(pfx):]
            return v[:18]
        running_raw, other, attention = [], [], []
        for s in states:
            # (a `live = loop_parents()` sat here, inside the per-cell loop, and
            # was never read — the name is rebound below and the loop count
            # comes from n_loops. It cost one full `ps -axww -E` sweep PER CELL
            # per refresh: 60+ per `status`, each dumping every process's
            # environment. loop_parents' own docstring says that text carries
            # agent credentials and must not be printed, and run_cell_pids
            # deliberately avoids -E for exactly that reason.)
            hb = state.heartbeat(common.WS / s["cid"])
            phase = hb.get("phase") if hb else ""
            if s["state"] == "RUNNING" or (s["state"] == "WAITING" and phase not in ("", None)):
                running_raw.append((s["cid"], vshort(s["agent"], s["agent_model"]),
                                    phase or s["why"],
                                    state._dur(hb.get("phase_age") if hb else None),
                                    f"{s['att']}/{s['budget']}", s["shape"],
                                    _tail_hist(s["hist"], 40), s["detail"]))
            else:
                label = label_of.get(s["variant"], s["variant"])
                st_txt = (display_state(s) + (" ⚠" if s.get("taint") else ""))
                att_txt = (f"@{s['green_at']}" if s["green_at"]
                           else str(s["att"]) if s["state"] == "DONE"
                           else f"{s['att']}/{s['budget']}")
                other.append((label, s["agent"], vshort(s["agent"], s["agent_model"]),
                              f"{s['variant']} {s['task']} r{s['rep']}",
                              st_txt, att_txt, s["live"], s["shape"],
                              _tail_hist(s["hist"], 34), s["detail"][:44]))
            if s["state"] == "CRASHED" and not requeued(s):
                attention.append((s["cid"], f"CRASHED/{s['why']}: {s['detail']}"))
            elif s["state"] == "WAITING" and s["why"] == "limit":
                attention.append((s["cid"], f"waiting on a limit: {s['detail']}"))
            # a pause is the operator's answer to the alerts before it
            if s.get("alerts_open") and s["state"] not in ("DONE", "PAUSED"):
                attention.append((s["cid"], f"{s['alerts_open']}× ALERT — {s['alert_last']}"))
            _green = s["state"] == "DONE" and s["why"] == "green"
            if s.get("noedit") and not _green:
                # The attempt was charged, and whether it deserved to be is not
                # decidable here: an agent error and a deliberate no-op look
                # identical from outside. Greens are exempt — the cell solved
                # it, so no attempt was lost to the ambiguity.
                attention.append((s["cid"],
                                  f"{s['noedit']}× NOEDIT — investigate "
                                  f"(agent fault or no-op): {s['noedit_last']}"))
        running_raw.sort()
        cid_to_id = {r[0]: str(i+1) for i, r in enumerate(running_raw)}
        running = []
        for i, r in enumerate(running_raw):
            phase, hist, detail = r[2], r[6], r[7]
            # LAST REP carries the block reason while the cell waits on a
            # holder, its stage history otherwise; BLOCK is the phase's kind.
            if detail.startswith("held by "):
                holder_cid = detail.split("held by ", 1)[1].strip()
                # There is no stale-lock case to render: a held lock has a live
                # holder by construction, because the kernel releases it when
                # the holder dies. A holder that just finished leaves the
                # running list while its waiters are still mid-poll, which is a
                # normal handover, not a fault.
                last = (f"blocked by {cid_to_id[holder_cid]}" if holder_cid in cid_to_id
                        else "waiting (holder finishing)")
            else:
                last = hist
            running.append((str(i+1), r[0], r[1], r[2], r[3], r[4], r[5],
                            last[:40], "YES" if phase in state.WAIT_PHASES else "NO"))
        greens = [r for r in other if r[4].startswith("DONE·green")]
        other = [r for r in other if not r[4].startswith("DONE·green")]
        other.sort(key=lambda r: (r[0], r[1], r[2]))
        greens.sort(key=lambda r: (r[0], r[1], r[2]))
        # bottom-up visibility order: what scrolls away first matters least
        hdr9 = ("LABEL", "AGENT", "VER", "VARIANT·TASK·REP", "STATE",
                "ATTEMPT", "LIVE", "GATE", "LAST REP", "DETAIL")
        if not running_only:
            out.append(f"— OTHER ({len(other)}) — by label · agent " + "—" * 34)
            out.append(fmt_table(other, hdr9) if other else "  (none)")
            out.append(f"\n— GREEN ({len(greens)}) " + "—" * 48)
            out.append(fmt_table(greens, hdr9) if greens else "  (none)")
        out.append(f"\n— RUNNING ({len(running)}) " + "—" * 46)
        out.append(fmt_table(running, ("ID", "CELL", "VER", "PHASE", "TIME", "ATTEMPT",
                                       "GATE", "LAST REP", "BLOCK"))
                   if running else "  (none)")
        triage = [f"  ! {cid_to_id.get(cid, cid)}: {why}" for cid, why in attention]
        triage += [f"  z {kind:<10} {ident}  owner={owner}  {note}"
                   for kind, ident, owner, note in zombies.find_zombies()]
        if triage:
            out.append("\n— TRIAGE (attention + zombies) " + "—" * 34)
            out.extend(triage)
        # QUEUED: work that exists only as a spec in .queues/queue/<agent>/.
        # Every other section renders WORKSPACES, so pending cells were
        # invisible to the console entirely — a 100+ cell backlog could sit
        # there with nothing in `status` acknowledging it.
        qsec = queued_summary()
        if qsec:
            out.extend(qsec)
        live = sum(1 for n in boxes if n.startswith(common.AGENT_CONTAINER_PREFIX))
        from fae.driver.conduct import Conduct
        conduct_s = Conduct().run_line()
        out.append(f"\n{live} containers, {n_loops} loops, {conduct_s}, {datetime.now(timezone.utc):%H:%M:%S}Z")
        _mp = common.mem_pressure()
        if _mp["label"]:
            out.append(f"mem: {_mp['used_gb']:.1f}/{_mp['total_gb']:.1f}GB used "
                       f"({_mp['avail_pct']}% avail), pressure={_mp['label']}  ·  "
                       f"swap {_mp['swap_used_mb']:.0f}/{_mp['swap_total_mb']:.0f}MB")
        out.append(common.queues().weekly_line())
    return "\n".join(out)


def status(args):
    print(render(args.flat, getattr(args, "running_only", False)))


def watch(args):
    """Live console — READ-ONLY. Zombies are listed so an operator can see
    them; nothing here acts on the fleet.

    Supervision belongs to `experiment run`: it holds the schedule and knows what
    it started. A console open in a terminal is not a controller, and one that
    acts is a second controller racing the first. `cli.py experiment repair`
    is the operator's deliberate path."""
    try:
        while True:
            zs = zombies.find_zombies()
            os.system("clear")
            print(render(args.flat, getattr(args, "running_only", False)))
            if zs:
                print("\nZOMBIES (listed only — `experiment run` reaps, "
                      "or `cli.py experiment repair`):")
                for kind, ident, owner, note in zs:
                    print(f"  {kind:<10} {ident}  owner={owner}  {note}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def parse_reset(reason):
    """epoch when a limit lifts, from 'Resets in 1h47m' / 'resets 3:10am (UTC)'."""
    now = datetime.now(timezone.utc)
    s = faults.reset_hint_s(reason, now=now)
    return now.timestamp() + s if s else None


def monitor(args):
    """Passive supervisor: surfaces limit and auth walls. Exiting (Ctrl-C, y)
    leaves all runs alive.

    It no longer SIGSTOPs anything. Freezing a loop is not a pause — a frozen
    loop still holds the per-arm lock, so auto-pausing one agent on a usage
    limit would deadlock that arm for every other agent. And it was redundant:
    run_cell already retries the SAME attempt every LIMIT_RETRY_S on a
    limit/5xx fault, burning no budget, so a usage wall self-heals when the
    window rolls. What still needs a human is an AUTH wall, which never lifts
    on its own — that is what this reports."""
    common.CONDUCT.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            states, _, _ = state.all_states()
            walls = []
            for s in states:
                if not (s["state"] == "WAITING" and s["why"] == "limit"):
                    continue
                reason = s["detail"].lower()
                if not any(h in reason for h in common.LIMIT_HINTS):
                    continue
                kind = "AUTH" if any(h in reason for h in common.AUTH_HINTS) else "limit"
                when = parse_reset(s["detail"])
                walls.append((kind, s["cid"], s["detail"], when))
            os.system("clear")
            print("MONITOR (passive — reports walls; Ctrl-C to detach, runs continue)")
            for l in zombies.janitor_lines():
                print(l)
            for kind, cid, detail, when in walls:
                if kind == "AUTH":
                    print(f"  !! {cid}: AUTH wall — this does NOT self-heal: "
                          f"run /login + restage creds ({detail[:40]})")
                else:
                    eta = (f", resets {datetime.fromtimestamp(when, timezone.utc):%H:%M}Z"
                           if when else "")
                    print(f"  {cid}: usage wall, retrying same attempt{eta}")
            print(render(False, getattr(args, "running_only", False)))
            time.sleep(args.interval)
        except KeyboardInterrupt:
            try:
                ans = input("\nexit monitor? runs keep going [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = "y"
            if ans == "y":
                print("monitor detached — runs continue (experiment stop all to stop them)")
                return


def queue_list(args):
    """The work list per agent lane: pending (in admission order, parked
    lanes marked), running, and with --done the terminal specs."""
    qs = common.queues()
    agents = set(args.agents or [])
    pending = [(a, p, parked) for a, p, parked in qs.pending_specs()
               if not agents or a in agents]
    running = [p for p in qs.running_specs() if not agents or p.parent.name in agents]
    lanes = sorted({a for a, _, _ in pending} | {p.parent.name for p in running})
    if not lanes:
        print("the queue is empty")
    for lane in lanes:
        mine = [(p, parked) for a, p, parked in pending if a == lane]
        live = [p for p in running if p.parent.name == lane]
        parked = any(pk for _, pk in mine)
        print(f"{lane}{' (paused)' if parked else ''}: {len(mine)} pending, {len(live)} running")
        for p in live:
            print(f"  running  {qs.spec_cid(p)}")
        for p, _ in mine:
            print(f"  pending  {qs.spec_cid(p)}")
    if args.done:
        done = [p for p in qs.done_specs() if not agents or p.parent.name in agents]
        print(f"done: {len(done)}")
        for p in done:
            print(f"  done     {p.parent.name:10s} {qs.spec_cid(p)}")
