"""The queues: what waits to run, what runs, what ran, the slots a running
cell holds, and the agents' books that decide when a lane may admit its next
cell. One directory, <root>/workspaces.nosync/.queues (fae/plane.py); this
module is the only code that reads or writes it.

    queue/<agent>/<seq>.<cid>.json    pending; lane order is the filename sort
    queue/<agent>.parked/             operator-paused lane (one dir rename)
    running/<agent>/<cid>.json        claimed by this lane's cell
    done/<agent>/<cid>.json           terminal
    backups/<why>-<stamp>.<name>      taken out of play by an operator verb
    .to_be_deleted/<stamp>/queue/     cancelled before admission
    work-slots/slot-<n>               the cap on running cells (flock)
    arm-<lock>.slots/slot-<n>         a variant lock's cap (flock)
    weekly.json, cooldown.<agent>     when a lane may admit
    agent-io.json                     the agents' output, sampled by supervision

A spec moves by one rename(2), so it is always in exactly one state: a crash
at any instant can neither lose nor duplicate it. Every change also holds
<root>/workspaces.nosync/.locks/queues-lock for the change alone, so two
processes never interleave a multi-rename change (a renumber, a park, the
weekly hold). It is never held while a slot is taken or held: slots are
taken without waiting, by whoever admits the cell, and handed to the cell
process, which holds them by fd for its whole life.
"""
from __future__ import annotations

import os
import re
import time
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fae import mutex

SEQ_START = 100000              # appends count up, front-inserts count down
MAX_SLOTS, MAX_LOCK_SLOTS = 16, 8

# The claude lanes share one seven-day cap; conduct holds BUDGET_LANES once
# the fleet has spent BUDGET_HOLD_AT of the week and releases them within
# BUDGET_RELEASE_H of the reset.
BUDGET_LANES = [m for m in os.environ.get("BUDGET_LANES", "fable,opus").split(",") if m]
BUDGET_HOLD_AT = float(os.environ.get("BUDGET_HOLD_AT", 0.75))
BUDGET_RELEASE_H = float(os.environ.get("BUDGET_RELEASE_H", 24))
_WEEKLY_EVENT_RE = re.compile(r'"type":"rate_limit_event","rate_limit_info":(\{[^}]*\})')
_SEQ_PREFIX = re.compile(r"^(?:\d+|tmp-\d+)\.")


@contextmanager
def _nothing():
    yield


def _changes(method):
    """The method changes .queues/: it runs under the queues-lock."""
    def held(self, *a, **k):
        with self._changing():
            return method(self, *a, **k)
    held.__name__, held.__doc__ = method.__name__, method.__doc__
    return held


def hhmm():
    return f"{datetime.now(timezone.utc):%H:%M:%S}"


class Book:
    """One JSON file of the queues. A missing or unreadable file reads as
    `default`; a save replaces the file whole."""

    def __init__(self, path, default=dict, changing=None):
        self.path = Path(path)
        self._default = default
        self._changing = changing

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return self._default()

    def save(self, doc):
        with (self._changing() if self._changing else _nothing()):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(doc, sort_keys=True))
            os.replace(tmp, self.path)
        return doc


class Slots:
    """The slot files a cell has open and the ones it holds. The flock lives
    on the open file, so closing is the release, and the kernel does it if
    the process dies."""

    def __init__(self, files):
        self._files = files          # [(pool, path, file object)]
        self.held = []               # paths

    def _pool(self, pool):
        return [(p, f) for k, p, f in self._files if k == pool]

    def keep_held(self):
        """Close the candidates that were not won."""
        keep = []
        for k, p, f in self._files:
            if p in self.held:
                keep.append((k, p, f))
            else:
                try:
                    f.close()
                except OSError:
                    pass
        self._files = keep

    def fds(self):
        """The held files' fds, to hand to the process that runs the cell."""
        return [f.fileno() for _, p, f in self._files if p in self.held]

    def handover(self):
        """`fd:path,...` for the cell process's CELL_SLOT_FDS."""
        return ",".join(f"{f.fileno()}:{p}" for _, p, f in self._files if p in self.held)

    def close(self):
        """THE release. Idempotent."""
        for _, _, f in self._files:
            try:
                f.close()
            except OSError:
                pass
        self._files = []
        self.held = []


class Queues:
    """The queues under `base`. `cell_id(agent, variant, rep, task)` names a
    spec's cell and `refuse(cid)` returns why a cell may not be queued (None
    when it may); both are needed only to enqueue. `workspaces` is where the
    agents' attempt logs are read for the weekly cap."""

    SEQ_START = SEQ_START

    def __init__(self, base, *, locks=None, cell_id=None, refuse=None, workspaces=None):
        self.base = Path(base)
        self.lock = Path(locks or self.base.parent / ".locks") / "queues-lock"
        self._cell_id = cell_id
        self._refuse = refuse
        self.workspaces = Path(workspaces) if workspaces else None
        self._held = 0

    @contextmanager
    def _changing(self):
        """The queues-lock, re-entered freely by the method that holds it."""
        if self._held:
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
            return
        with mutex.fs_lock(self.lock):
            self._held = 1
            try:
                yield
            finally:
                self._held = 0

    # --- specs and lanes ------------------------------------------------------

    def lane_dir(self, agent, parked=False):
        return self.base / "queue" / (f"{agent}.parked" if parked else agent)

    def rundir(self, agent):
        return self.base / "running" / agent

    def lane_dirs(self, include_parked=False):
        """Every lane directory; a lane exists once something is enqueued to it."""
        q = self.base / "queue"
        if not q.is_dir():
            return []
        return sorted((d for d in q.iterdir() if d.is_dir()
                       and (include_parked or not d.name.endswith(".parked"))),
                      key=lambda d: d.name)

    def parked_lanes(self):
        """Operator-paused lanes. conduct never admits from them; they still
        count as backlog, so a fleet with ONLY parked work is not done."""
        q = self.base / "queue"
        if not q.is_dir():
            return []
        return sorted(d for d in q.iterdir()
                      if d.is_dir() and d.name.endswith(".parked"))

    @staticmethod
    def lane_agent(d):
        n = d.name
        return n[:-len(".parked")] if n.endswith(".parked") else n

    @staticmethod
    def _seq_key(p):
        """Numeric, not lexical: '99999.x.json' sorts before '100000.x.json'
        even when a hand-renamed spec dropped the zero padding. A name with
        no leading number sorts last, never hidden mid-lane."""
        m = re.match(r"^(?:tmp-)?(\d+)\.", p.name)
        return (int(m.group(1)), p.name) if m else (float("inf"), p.name)

    @classmethod
    def specs_in(cls, d):
        """The specs in one directory, in admission order."""
        return sorted(d.glob("*.json"), key=cls._seq_key) if d.is_dir() else []

    def lane_specs(self, agent):
        """One lane's pending specs in admission order. A parked lane offers
        none — parking is what stops admission."""
        return self.specs_in(self.lane_dir(agent))

    @staticmethod
    def spec_cid(p):
        """The cid a spec file names, in either shape: <seq>.<cid>.json while
        it waits in a lane, <cid>.json once claimed."""
        return _SEQ_PREFIX.sub("", p.name)[:-len(".json")]

    @staticmethod
    def read_spec(p):
        return json.loads(p.read_text())

    def _seqs(self, d):
        out = []
        for p in self.specs_in(d):
            try:
                out.append(int(p.name.split(".", 1)[0]))
            except ValueError:
                continue
        return out

    def _renumber(self, d):
        """Restart a lane's sequence at SEQ_START, order preserved. Two passes
        so a new name can never collide with an old one."""
        for i, p in enumerate(self.specs_in(d)):
            p.rename(d / f"tmp-{i:06d}.{self.spec_cid(p)}.json")
        for i, p in enumerate(self.specs_in(d)):
            p.rename(d / f"{SEQ_START + i:06d}.{self.spec_cid(p)}.json")

    def _next_seq(self, d, front):
        seqs = self._seqs(d)
        if not seqs:
            return SEQ_START
        seq = min(seqs) - 1 if front else max(seqs) + 1
        if seq < 0:
            self._renumber(d)
            seqs = self._seqs(d)
            seq = min(seqs) - 1 if front else max(seqs) + 1
        return seq

    def _writable_lane(self, agent):
        """Where new specs land: the parked directory when the lane is parked,
        so enqueueing cannot resurrect admission from a paused lane."""
        parked = self.lane_dir(agent, parked=True)
        return parked if parked.is_dir() else self.lane_dir(agent)

    def lane_has(self, agent, cid):
        """Is this cid already pending or claimed?"""
        for d in (self.lane_dir(agent), self.lane_dir(agent, parked=True), self.rundir(agent)):
            if d.is_dir() and any(d.glob(f"*{cid}.json")):
                return True
        return False

    @_changes
    def enqueue(self, agent, spec, front=False):
        """Add a spec to a lane. Returns its path, or None when the lane
        already holds that cid or the cell may not be queued (refuse)."""
        cid = self._cell_id(agent, spec["variant"], spec.get("rep", 1), spec.get("task", "T1"))
        if self.lane_has(agent, cid):
            return None
        why = self._refuse(cid) if self._refuse else None
        if why:
            print(f"skipping {cid}: {why}")
            return None
        d = self._writable_lane(agent)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self._next_seq(d, front):06d}.{cid}.json"
        p.write_text(json.dumps(spec) + "\n")
        return p

    def is_parked(self, agent):
        return self.lane_dir(agent, parked=True).is_dir()

    def queued_specs(self, agent):
        """The agent's pending specs, its parked lane included."""
        return self.specs_in(self.lane_dir(agent)) + self.specs_in(self.lane_dir(agent, parked=True))

    def specs_of(self, agent, cid):
        """Every spec naming cid in the agent's lane, parked lane and claims."""
        return [p for d in (self.lane_dir(agent), self.lane_dir(agent, parked=True), self.rundir(agent))
                if d.is_dir() for p in sorted(d.glob(f"*{cid}.json"))]

    def is_claimed(self, agent, cid):
        return (self.rundir(agent) / f"{cid}.json").exists()

    @_changes
    def adopt(self, agent, cid, spec):
        """A claim for a live cell started outside the scheduler."""
        d = self.rundir(agent)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{cid}.json"
        p.write_text(json.dumps(spec) + "\n")
        return p

    def running_specs(self, agent=None):
        """Claimed specs — the fleet's live cells."""
        if agent is not None:
            return self.specs_in(self.rundir(agent))
        base = self.base / "running"
        if not base.is_dir():
            return []
        return sorted((p for d in base.iterdir() if d.is_dir()
                       for p in self.specs_in(d)), key=lambda p: p.name)

    @_changes
    def claim(self, agent, p):
        """QUEUED -> RUNNING. Refuses to overwrite an existing claim: rename
        would drop it silently, and two claims on one cid means two cells."""
        d = self.rundir(agent)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{self.spec_cid(p)}.json"
        if dest.exists():
            raise FileExistsError(f"{self.spec_cid(p)} is already claimed")
        p.rename(dest)
        return dest

    @_changes
    def release(self, agent, p, front=True):
        """RUNNING -> QUEUED, keeping the spec's place at the head by default."""
        d = self._writable_lane(agent)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{self._next_seq(d, front):06d}.{self.spec_cid(p)}.json"
        p.rename(dest)
        return dest

    @_changes
    def finish(self, agent, p):
        """RUNNING -> DONE."""
        d = self.base / "done" / agent
        d.mkdir(parents=True, exist_ok=True)
        dest = d / p.name
        p.rename(dest)
        return dest

    @_changes
    def shelve(self, p, why):
        """Any state -> backups/. Specs are never deleted, only taken out of
        play, so a mistaken stop is restorable by moving the file back."""
        d = self.base / "backups"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = d / f"{why}-{stamp}.{p.name}"
        p.rename(dest)
        return dest

    @_changes
    def park_lane(self, agent):
        """'parked' | 'already' | 'empty'."""
        live, parked = self.lane_dir(agent), self.lane_dir(agent, parked=True)
        if parked.is_dir():
            return "already"
        if not live.is_dir():
            return "empty"
        live.rename(parked)
        return "parked"

    @_changes
    def unpark_lane(self, agent):
        """'resumed' | 'not-paused' | 'conflict' (both dirs exist — someone
        hand-moved things; merging silently would reorder the backlog)."""
        live, parked = self.lane_dir(agent), self.lane_dir(agent, parked=True)
        if not parked.is_dir():
            return "not-paused"
        if live.is_dir():
            return "conflict"
        parked.rename(live)
        return "resumed"

    def pending_specs(self):
        """[(agent, spec path, parked)] of every pending spec, lanes in name
        order, each lane in its admission order."""
        out = []
        for d in self.lane_dirs(include_parked=True):
            parked = d.name.endswith(".parked")
            out += [(self.lane_agent(d), p, parked) for p in self.specs_in(d)]
        return out

    def parked_count(self):
        return sum(len(self.specs_in(d)) for d in self.parked_lanes())

    def done_specs(self, agent=None):
        """Terminal specs."""
        base = self.base / "done"
        dirs = [base / agent] if agent else (sorted(d for d in base.iterdir() if d.is_dir())
                                             if base.is_dir() else [])
        return [p for d in dirs for p in self.specs_in(d)]

    @_changes
    def cancel(self, p, agent, stamp):
        """QUEUED -> out of play, before admission: moved into
        .to_be_deleted/<stamp>/queue/<agent>/, never deleted; moving it back
        restores it."""
        d = self.base / ".to_be_deleted" / stamp / "queue" / agent
        d.mkdir(parents=True, exist_ok=True)
        dest = d / p.name
        p.rename(dest)
        return dest

    # --- slots ----------------------------------------------------------------

    def _pool_dir(self, lock=None):
        return self.base / (f"arm-{lock}.slots" if lock else "work-slots")

    def slot_files(self, lock=None):
        """The pool's slot files that exist, in slot order: the work pool, or
        a variant lock's pool."""
        d = self._pool_dir(lock)
        if not d.is_dir():
            return []
        return sorted((p for p in d.glob("slot-*") if re.fullmatch(r"slot-\d+", p.name)),
                      key=lambda p: int(p.name[len("slot-"):]))

    def slot_pools(self):
        """Every variant lock that has a pool on disk."""
        return sorted(d.name[len("arm-"):-len(".slots")]
                      for d in self.base.glob("arm-*.slots") if d.is_dir())

    def slot_note(self, slot):
        """(cid, pid, taken-at epoch) of the slot's last holder, or None."""
        try:
            f = (Path(str(slot) + ".holder")).read_text().split()
            return f[0], int(f[1]), int(f[2]) if len(f) > 2 else None
        except (OSError, IndexError, ValueError):
            return None

    @staticmethod
    def slot_held(slot):
        """The kernel's answer: is the slot's flock held right now?"""
        return mutex.probe_held(slot)

    @_changes
    def clear_slot_note(self, slot):
        mutex.clear_holder(slot)

    def occupied(self, n):
        """How many of the first n work slots are held."""
        d = self._pool_dir()
        return sum(1 for i in range(1, n + 1) if self.slot_held(d / f"slot-{i}"))

    def _open(self, pool, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            raise RuntimeError(
                f"{path} is a lock DIRECTORY, but a lock is a FILE here. "
                f"mkdir locks and flock locks do not exclude each other at "
                f"all, so a tree holding both has no mutual exclusion.")
        return (pool, path, mutex.open_lock(path))

    @_changes
    def open_slots(self, work_slots, lock=None, lock_slots=1):
        """Every candidate slot file of the work pool and, for a locked
        variant, of its lock's pool, open and not yet held."""
        if work_slots > MAX_SLOTS:
            raise RuntimeError(f"WORK_SLOTS={work_slots} exceeds the slot block ({MAX_SLOTS})")
        if lock and lock_slots > MAX_LOCK_SLOTS:
            raise RuntimeError(f"lock '{lock}' wants {lock_slots} slots, over the "
                               f"slot block ({MAX_LOCK_SLOTS})")
        files = []
        try:
            files += [self._open("work", self._pool_dir() / f"slot-{i}")
                      for i in range(1, work_slots + 1)]
            if lock:
                files += [self._open("lock", self._pool_dir(lock) / f"slot-{i}")
                          for i in range(1, lock_slots + 1)]
        except BaseException:
            Slots(files).close()
            raise
        return Slots(files)

    def try_slots(self, cid, work_slots, lock=None, lock_slots=1):
        """The slots a cell needs, taken now or not at all: a free work slot and,
        for a variant with a lock, a free slot of the lock's pool. Never waits.

        Returns (slots, None) with the slots held, or (None, pool) naming the
        pool that had none free ("work" / "lock"); nothing is kept then."""
        slots = self.open_slots(work_slots, lock, lock_slots)
        try:
            for pool in ("work", "lock") if lock else ("work",):
                cand = slots._pool(pool)
                got = mutex.try_fds([f.fileno() for _, f in cand])
                if got is None:
                    slots.close()
                    return None, pool
                slots.held.append(next(p for p, f in cand if f.fileno() == got))
            slots.keep_held()
            with self._changing():
                for path in slots.held:
                    mutex.note_holder(path, cid, os.getpid())
            return slots, None
        except BaseException:
            slots.close()
            raise

    def adopt_slots(self, cid, handover):
        """The slots handed to this process (`handover` is CELL_SLOT_FDS,
        `fd:path,...`). Each fd must hold its flock — a try on the fd this
        process inherited succeeds only for the holder — and the holder notes
        are rewritten with this process's pid. Returns the Slots, or None when
        any handed slot is not held."""
        files = []
        for item in filter(None, handover.split(",")):
            fd_s, _, path = item.partition(":")
            try:
                f = os.fdopen(int(fd_s), "a+")
            except (OSError, ValueError):
                Slots(files).close()
                return None
            files.append(("handed", Path(path), f))
        slots = Slots(files)
        if not files or not all(mutex.try_fd(f.fileno()) for _, _, f in files):
            slots.close()
            return None
        slots.held = [p for _, p, _ in files]
        with self._changing():
            for path in slots.held:
                mutex.note_holder(path, cid, os.getpid())
        return slots

    @_changes
    def clear_holder_notes(self, cid, lock=None):
        """Drop every slot holder note naming this cell. The fd is the lock —
        the notes are the fleet's display of who holds what — so they must go
        on every exit, or a halted cell reads as a zombie holder."""
        for d in [self._pool_dir()] + ([self._pool_dir(lock)] if lock else []):
            for holder in d.glob("slot-*.holder"):
                try:
                    if holder.read_text().split()[0] == cid:
                        holder.unlink(missing_ok=True)
                except (OSError, IndexError):
                    pass

    # --- the agents' books ----------------------------------------------------

    def agent_io_book(self):
        return Book(self.base / "agent-io.json", changing=self._changing)

    def cooldown_file(self, agent):
        return self.base / f"cooldown.{agent}"

    @_changes
    def set_cooldown(self, agent, until, detail):
        """The lane may not admit before `until` (epoch seconds)."""
        self.base.mkdir(parents=True, exist_ok=True)
        self.cooldown_file(agent).write_text(f"{int(until)} {detail}\n")
        return int(until)

    def cooldown_until(self, agent):
        """Epoch seconds until which the lane is limit-cooling, 0 = not cooling."""
        try:
            return int(self.cooldown_file(agent).read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return 0

    @_changes
    def clear_cooldown(self, agent):
        self.cooldown_file(agent).unlink(missing_ok=True)

    # --- the weekly cap -------------------------------------------------------

    def _weekly_book(self):
        return self.base / "weekly.json"

    def weekly_load(self):
        try:
            st = json.loads(self._weekly_book().read_text())
        except (OSError, ValueError):
            st = {}
        if not isinstance(st, dict):
            st = {}
        st.setdefault("utilization", None)
        st.setdefault("resets_at", None)
        st.setdefault("seen_at", 0.0)
        st.setdefault("hold", [])
        return st

    @_changes
    def weekly_save(self, st):
        self.base.mkdir(parents=True, exist_ok=True)
        self._weekly_book().write_text(json.dumps(st))

    @_changes
    def weekly_cap_observe(self, st=None, now=None):
        """Fold every seven_day rate_limit_event the agents logged since the
        last scan into the book; the newest event (log mtime, then line
        order) is the reading. `allowed_warning` carries `utilization`,
        `rejected` is the cap itself, plain `allowed` says nothing."""
        st = self.weekly_load() if st is None else st
        now = time.time() if now is None else now
        newest = None
        if self.workspaces is not None and self.workspaces.is_dir():
            for log in self.workspaces.glob("*/agent.attempt-*.log"):
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
        self.weekly_save(st)
        return st

    @staticmethod
    def weekly_reading(st, now=None):
        """(utilization, resets_at) as of now: a reading from before the reset
        says nothing about this week."""
        now = time.time() if now is None else now
        resets = st.get("resets_at")
        if resets and resets <= now:
            return None, resets
        return st.get("utilization"), resets

    @_changes
    def weekly_budget_apply(self, st, now=None, out=print):
        """Hold the budget lanes past BUDGET_HOLD_AT; release them within
        BUDGET_RELEASE_H of the reset or once it has passed. Lanes the
        operator parked are not the budget's to touch: only lanes in `hold`
        are released."""
        now = time.time() if now is None else now
        util, resets = self.weekly_reading(st, now)
        hold = list(st.get("hold", []))
        near_reset = bool(resets) and resets - now <= BUDGET_RELEASE_H * 3600
        changed = False
        for m in list(hold):
            if near_reset:
                r = self.unpark_lane(m)
                hold.remove(m)
                changed = True
                left = max(0.0, (resets - now) / 3600)
                out(f"  [{hhmm()}] weekly cap: released {m} ({r}; the reset is "
                    f"{left:.0f}h away — spend the remainder)")
        if util is not None and util >= BUDGET_HOLD_AT and not near_reset:
            parked = []
            for m in BUDGET_LANES:
                if m in hold or self.is_parked(m):
                    continue                      # held already, or the operator's park
                if self.park_lane(m) == "empty":
                    self.lane_dir(m, parked=True).mkdir(parents=True, exist_ok=True)
                hold.append(m)
                parked.append(m)
                changed = True
            if parked:
                when = (f"{datetime.fromtimestamp(resets, timezone.utc):%a %H:%M}Z"
                        if resets else "unknown")
                out(f"  [{hhmm()}] ALERT weekly cap {util:.0%} — budget hold: "
                    f"parked {', '.join(parked)} (resets {when}); the other lanes "
                    f"keep the remaining {1 - util:.0%}")
        if changed:
            st["hold"] = hold
            self.weekly_save(st)
        return st

    @_changes
    def weekly_hold_clear(self, agent, out=print):
        """An operator resume of a held lane ends the hold: it is not re-parked
        until the next reading crosses the threshold again."""
        st = self.weekly_load()
        if agent in st["hold"]:
            st["hold"].remove(agent)
            self.weekly_save(st)
            out(f"  queue[{agent}]: budget hold cleared by the operator")

    def weekly_line(self, now=None):
        """One status line: what the fleet knows about this week's cap."""
        st = self.weekly_load()
        now = time.time() if now is None else now
        util, resets = self.weekly_reading(st, now)
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
