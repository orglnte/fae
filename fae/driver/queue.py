"""The backlog: one FILE per spec, its STATE is the directory it sits in.

    queue/<agent>/<seq>.<cid>.json    pending; lane order is the filename sort
    queue/<agent>.parked/             operator-paused lane (one dir rename)
    running/<agent>/<cid>.json        claimed by this lane's cell
    done/<agent>/<cid>.json           terminal
    backups/<why>-<stamp>.<name>      taken out of play by an operator verb

Every transition is a single rename(2), so a spec is always in exactly one
state: a crash at any instant can neither lose nor duplicate it, and no
journal, lock or in-flight record is needed to make that true.
"""
from __future__ import annotations

import re
import time
import ujson as json
from datetime import datetime, timezone
from pathlib import Path

from fae.driver import common

SEQ_START = 100000              # appends count up, front-inserts count down


def lane_dir(agent, parked=False):
    return common.QUEUES / "queue" / (f"{agent}.parked" if parked else agent)


def lane_dirs(include_parked=False):
    """Every lane directory; a lane exists once something is enqueued to it."""
    q = common.QUEUES / "queue"
    if not q.is_dir():
        return []
    return sorted((d for d in q.iterdir() if d.is_dir()
                   and (include_parked or not d.name.endswith(".parked"))),
                  key=lambda d: d.name)


def _parked_queues():
    """Operator-paused lanes (`experiment pause M`). conduct never admits from
    them; they still count as backlog, so a fleet with ONLY parked work is
    not 'done' — it is waiting for a experiment resume."""
    q = common.QUEUES / "queue"
    if not q.is_dir():
        return []
    return sorted(d for d in q.iterdir()
                  if d.is_dir() and d.name.endswith(".parked"))


def lane_agent(d):
    n = d.name
    return n[:-len(".parked")] if n.endswith(".parked") else n


def _seq_key(p):
    """Numeric, not lexical: '99999.x.json' must sort before '100000.x.json'
    even though '1' < '9' as characters. Every writer here zero-pads to 6
    digits so the two agree in practice, but a hand-edited or externally
    renamed spec (a front/back reorder run outside this module) can drop
    that padding — admission order must not silently invert when it does.
    A name with no leading number sorts last, never hidden mid-lane."""
    m = re.match(r"^(?:tmp-)?(\d+)\.", p.name)
    return (int(m.group(1)), p.name) if m else (float("inf"), p.name)


def _dir_specs(d):
    return sorted(d.glob("*.json"), key=_seq_key) if d.is_dir() else []


def lane_specs(agent):
    """One lane's pending specs in admission order. A parked lane offers
    none — parking is what stops admission."""
    return _dir_specs(lane_dir(agent))


_SEQ_PREFIX = re.compile(r"^(?:\d+|tmp-\d+)\.")


def spec_cid(p):
    """The cid a spec file names, in either shape: <seq>.<cid>.json while it
    waits in a lane, <cid>.json once claimed."""
    return _SEQ_PREFIX.sub("", p.name)[:-len(".json")]


def read_spec(p):
    return json.loads(p.read_text())


def _seqs(d):
    out = []
    for p in _dir_specs(d):
        try:
            out.append(int(p.name.split(".", 1)[0]))
        except ValueError:
            continue
    return out


def _renumber(d):
    """Restart a lane's sequence at SEQ_START, order preserved. Two passes so
    a new name can never collide with an old one."""
    specs = _dir_specs(d)
    for i, p in enumerate(specs):
        p.rename(d / f"tmp-{i:06d}.{spec_cid(p)}.json")
    for i, p in enumerate(_dir_specs(d)):
        p.rename(d / f"{SEQ_START + i:06d}.{spec_cid(p)}.json")


def _next_seq(d, front):
    seqs = _seqs(d)
    if not seqs:
        return SEQ_START
    seq = min(seqs) - 1 if front else max(seqs) + 1
    if seq < 0:
        _renumber(d)
        seqs = _seqs(d)
        seq = min(seqs) - 1 if front else max(seqs) + 1
    return seq


def _writable_lane(agent):
    """Where new specs land: the parked directory when the lane is parked, so
    enqueueing cannot resurrect admission from a lane the operator paused."""
    parked = lane_dir(agent, parked=True)
    return parked if parked.is_dir() else lane_dir(agent)


def lane_has(agent, cid):
    """Is this cid already pending or claimed? One glob replaces parsing every
    queued line."""
    for d in (lane_dir(agent), lane_dir(agent, parked=True), rundir(agent)):
        if d.is_dir() and any(d.glob(f"*{cid}.json")):
            return True
    return False


def enqueue(agent, spec, front=False):
    """Add a spec to a lane. Returns its path, or None when the lane already
    holds that cid — or when the cell is sealed and must never run again."""
    cid = common.cell_id(agent, spec["variant"], spec.get("rep", 1), spec.get("task", "T1"))
    if lane_has(agent, cid):
        return None
    # Queueing a sealed cell would put a spec in a lane that conduct can only
    # ever refuse — a permanently stuck queue entry. Said out loud rather than
    # dropped silently: a top-up that skips cells should say which and why.
    if common.is_sealed(cid):
        print(f"skipping {cid}: SEALED — {common.seal_reason(cid)}")
        return None
    d = _writable_lane(agent)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_next_seq(d, front):06d}.{cid}.json"
    p.write_text(json.dumps(spec) + "\n")
    return p


def rundir(agent):
    return common.QUEUES / "running" / agent


def running_specs(agent=None):
    """Claimed specs — the fleet's live cells, one per lane at cap 1."""
    if agent is not None:
        return _dir_specs(rundir(agent))
    base = common.QUEUES / "running"
    if not base.is_dir():
        return []
    return sorted((p for d in base.iterdir() if d.is_dir()
                   for p in _dir_specs(d)), key=lambda p: p.name)


def claim(agent, p):
    """QUEUED -> RUNNING. Refuses to overwrite an existing claim: rename would
    drop it silently, and two claims on one cid means two cells."""
    d = rundir(agent)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{spec_cid(p)}.json"
    if dest.exists():
        raise FileExistsError(f"{spec_cid(p)} is already claimed")
    p.rename(dest)
    return dest


def release(agent, p, front=True):
    """RUNNING -> QUEUED, keeping the spec's place at the head by default."""
    d = _writable_lane(agent)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{_next_seq(d, front):06d}.{spec_cid(p)}.json"
    p.rename(dest)
    return dest


def finish(agent, p):
    """RUNNING -> DONE."""
    d = common.QUEUES / "done" / agent
    d.mkdir(parents=True, exist_ok=True)
    dest = d / p.name
    p.rename(dest)
    return dest


def shelve(p, why):
    """Any state -> backups/. Specs are never deleted, only taken out of
    play, so a mistaken stop is restorable by moving the file back."""
    d = common.QUEUES / "backups"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = d / f"{why}-{stamp}.{p.name}"
    p.rename(dest)
    return dest


def park_lane(agent):
    """'parked' | 'already' | 'empty'."""
    live, parked = lane_dir(agent), lane_dir(agent, parked=True)
    if parked.is_dir():
        return "already"
    if not live.is_dir():
        return "empty"
    live.rename(parked)
    return "parked"


def unpark_lane(agent):
    """'resumed' | 'not-paused' | 'conflict' (both dirs exist — someone
    hand-moved things; merging silently would reorder the backlog)."""
    live, parked = lane_dir(agent), lane_dir(agent, parked=True)
    if not parked.is_dir():
        return "not-paused"
    if live.is_dir():
        return "conflict"
    parked.rename(live)
    return "resumed"


def pending_specs():
    """[(agent, spec path, parked)] of every pending spec, lanes in name
    order, each lane in its admission order."""
    out = []
    for d in lane_dirs(include_parked=True):
        parked = d.name.endswith(".parked")
        out += [(lane_agent(d), p, parked) for p in _dir_specs(d)]
    return out


def done_specs(agent=None):
    """Terminal specs."""
    base = common.QUEUES / "done"
    dirs = [base / agent] if agent else (sorted(d for d in base.iterdir() if d.is_dir())
                                         if base.is_dir() else [])
    return [p for d in dirs for p in _dir_specs(d)]


def cancel(p, agent, stamp):
    """QUEUED -> out of play, before admission: moved into the lock plane's
    .to_be_deleted/<stamp>/queue/<agent>/, never deleted; moving it back
    restores it."""
    d = common.QUEUES / ".to_be_deleted" / stamp / "queue" / agent
    d.mkdir(parents=True, exist_ok=True)
    dest = d / p.name
    p.rename(dest)
    return dest
