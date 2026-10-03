"""THE cell — one experimental unit, and everything true about it.

Built beside the bash driver (harness/run_cell.sh) and then in its place:
the two took the same flocks on the same files, so a fleet ran both at once
while equivalence was proven, and every cell records which one ran it
(`IMPL`; absent means the bash era).

WHAT THIS FILE OWNS, and what it does not. It owns the cell's IDENTITY, its
LIFECYCLE and its JUDGEMENT. Everything else is a collaborator:

    fsm.py           the state machine (pure; no files, no processes, no clock)
    fae/queues.py    the slots it holds while it runs
    checkpoints.py   per-attempt provenance (git tree hashes)
    surface.py       the authorable surface: manifest, seal, heal, check
    verify.py        the boundary to the experiment's verifier: Ctx out,
                     Verdict back, out of process

`apply()` is where the model and reality are kept in one piece: it checks the
transition is enabled, changes the state, AND records it — one call, so the
transition log is DERIVED from the state change rather than written beside it.
In bash those are separate statements and can disagree, which is how `Resume`
came to be logged for cancelled cells that were never resumed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import traceback
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as _config
from . import faults
from . import rig as _rig
from . import variants as _variants
from .checkpoints import Checkpoints
from .surface import Surface, authorable
from .fsm import (ENABLED, PHASE_TO_LOOP, IllegalTransition, Loop, Phase,
                  Sealed, State, T, step)
from .verify import (RUN_OUT, Ctx, Verdict, _mutex_module as _load_mutex, run_verifier,
                     take_events)

_mutex = _load_mutex()

from fae import paths as _paths  # noqa: E402
from fae import plane as _plane  # noqa: E402
from fae.queues import Queues  # noqa: E402

HARNESS = _paths.ENGINE
ROOT = _paths.ROOT

# The budget is a constant, not a knob — attempts-to-green is the dependent
# variable, and a per-cell budget makes two cells incomparable. Mirrored in
# fae/driver/common.py and in fae/cell/config.py; prepare.py records it into cell.env.
# Nothing reads it from the environment.
class Halt(RuntimeError):
    """The cell stopped without a verdict, and a human has to look.

    The EXIT CODE is the contract: `conduct` treats 42 as SYSTEMIC and stops
    the fleet (a broken CLI or missing credential would otherwise burn every
    queued cell), while a per-cid code like LOCK_EXIT or INFRA_EXIT stops
    only this cell.
    A halt that returns a verdict-shaped None instead reads to the supervisor
    as a clean end.
    """

    def __init__(self, msg, code=42):
        super().__init__(msg)
        self.code = code


ATTEMPT_BUDGET = 10


def _now():
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


from fae import ledger  # noqa: E402


class VerifyResult:
    """One arrangement's verdict as seen by the cell: the Verdict the
    verifier answered, in the loop's own vocabulary."""

    def __init__(self, green, shape=None, stage_failed="", metrics=None,
                 out_dir=None, seconds=0.0, contract=None, charge=True, why=""):
        self.green = green
        self.shape = shape
        self.stage_failed = stage_failed
        self.metrics = metrics or {}
        self.out_dir = out_dir
        self.seconds = seconds
        self.contract = list(contract or [])     # the verdict's stand_down
        self.charge = charge                     # False: a rig fault, refunded
        self.why = why

    @classmethod
    def from_verdict(cls, v, shape=None, out_dir=None):
        return cls(green=v.ok, shape=shape, stage_failed=v.stage, metrics=v.metrics,
                   out_dir=out_dir, seconds=v.seconds, contract=v.stand_down,
                   charge=v.charge, why=v.why)

    @property
    def e2e(self):
        return (self.metrics.get("e2e_pass"), self.metrics.get("e2e_total"))

    def __repr__(self):
        return (f"VerifyResult(green={self.green} shape={self.shape} "
                f"stage={self.stage_failed or '-'}{'' if self.charge else ' void'})")


def hold_awake(pid):
    """Idle sleep halts the monotonic clock every gate window is measured on;
    the assertion lives exactly as long as the driver."""
    if (os.environ.get(_variants.NOOP_ENV) == "1"
            or sys.platform != "darwin" or not shutil.which("caffeinate")):
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(pid)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None



AGENT_AGREE_S = 60.0          # engine and client agree within this, or
AGENT_AGREE_FRAC = 0.1        # within this fraction of the client's figure


def agent_time_fields(attempt, runs, client_s):
    """Fields of the AGENT ledger event: the attempt's total agent run time
    (waits between runs excluded), the number of runs, the last run's time,
    the client's own account of that last run, and whether the two agree."""
    last = runs[-1]
    if client_s is None:
        check = "-"
    else:
        tol = max(AGENT_AGREE_S, AGENT_AGREE_FRAC * client_s)
        check = "agree" if abs(last - client_s) <= tol else "disagree"
    return [f"attempt={attempt}", f"s={round(sum(runs))}", f"runs={len(runs)}",
            f"last_s={round(last)}",
            "client_s=" + ("-" if client_s is None else str(round(client_s))),
            f"check={check}"]

class Cell:
    """Constructing a Cell READS; it never writes. That is what lets it be used
    on the 470 sealed cells the bash implementation produced — their state is
    fully readable, and the only thing that may be added is a re-verify."""

    def __init__(self, cid, workspaces=None, root=None):
        self.cid = cid
        self.root = Path(root or _paths.root())
        self.workspaces = Path(workspaces or os.environ.get("WORKSPACES_DIR")
                               or self.root / "workspaces.nosync")
        self.ws = self.workspaces / cid
        self.artifacts = self.ws / "artifacts"
        self.state = State()
        self.conf = _config.load(self.root)
        self._fp = None
        self._phase, self._attempt = Phase.SETUP, 0
        self.ckpt = Checkpoints(self.ws)
        self._surface = None
        self._env = self._read_env()
        self._replay_ledger()

    # --- identity ---------------------------------------------------------

    def _read_env(self):
        out = {}
        f = self.ws / "cell.env"
        if f.exists():
            for line in f.read_text(errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        return out

    @property
    def task(self):
        return self._env.get("TASK", "T1")

    @property
    def variant(self):
        return self._env.get("VARIANT", "")

    @property
    def reference(self):
        """A smoke cell: the variant's known answer was seeded."""
        return self._env.get("REFERENCE") == "1"

    @property
    def rep(self):
        return int(self._env.get("REPEAT", "1"))

    @property
    def impl(self):
        """Which implementation ran this cell. ABSENT MEANS BASH: the 470 cells
        sealed on 2026-08-18 predate the field, and predate any other
        implementation, so nothing was rewritten to add it."""
        return self._env.get("IMPL", "bash")

    @property
    def budget(self):
        return ATTEMPT_BUDGET

    # --- recorded result --------------------------------------------------

    def _replay_ledger(self):
        """The LEDGER decides doneness, not metrics.json: metrics holds only
        the last single verify's numbers, so a loop killed part-way through a
        six-arrangement gate leaves a green metrics.json behind while the
        ledger correctly says the attempt never finished."""
        L = ledger.parse(self.ws) if (self.ws / "iterations.log").exists() else None
        self._ledger = L
        if L:
            self.state.attempts = L["att"]
            self.state.outcome = L["verdict"]

    @property
    def attempts(self):
        return self.state.attempts

    @property
    def verdict(self):
        """green | failed | revoked | None (still open)."""
        return self.state.outcome

    @property
    def terminal(self):
        return self.verdict in ("green", "failed", "revoked")

    # --- sealing ----------------------------------------------------------
    # A cell that reached a verdict is finished evidence. The marker makes the
    # RESULT immutable — iterations.log, metrics.json, trace.csv, verify.log,
    # artifacts/ — while derived files (validation.json, score*.json) stay
    # regenerable, since they are functions of the result and the scoring rules
    # are meant to improve. New evidence goes under reverify/<ts>/.
    #
    # The file format is a contract with fae/driver/common.py, which reads the
    # same marker (is_sealed / seal_reason) before a cell is spawned, resumed
    # or queued.
    SEAL_MARKER = ".sealed"
    # The driver every cell records in cell.env IMPL and the ledger: bash
    # (harness/run_cell.sh) -> py (the python driver this engine replaced) ->
    # fae (this engine), so each population is attributable to its driver.
    IMPL = "fae"
    SEAL_EXIT = 46

    @property
    def sealed(self):
        return (self.ws / self.SEAL_MARKER).exists()

    def seal_record(self):
        """The seal's own line — verdict, attempts, who sealed it."""
        try:
            return (self.ws / self.SEAL_MARKER).read_text().splitlines()[0]
        except (OSError, IndexError):
            return ""

    @property
    def sealed_verdict(self):
        for field in self.seal_record().split("\t"):
            if field.startswith("verdict="):
                return field.split("=", 1)[1]
        return None

    def _refuse_if_sealed(self, what):
        if self.sealed:
            raise Sealed(f"{self.cid} is sealed ({self.seal_record()}); "
                         f"{what} would change a recorded result. To redo the "
                         f"cell, delete it (two-stage) and requeue.")

    def seal(self, verdict, attempts=None, by="cell.py"):
        """Idempotent, and the FIRST verdict is the true one — a re-seal that
        overwrote it would let a later pass relabel how a cell ended. There is
        no unseal: redoing a cell is delete-and-requeue.

        Returns True if this call is what sealed it.
        """
        p = self.ws / self.SEAL_MARKER
        if p.exists():
            return False
        p.write_text(f"sealed={_now()}\tverdict={verdict}\t"
                     f"attempts={self.attempts if attempts is None else attempts}\t"
                     f"by={by}\n")
        p.chmod(0o444)
        return True

    # --- transitions ------------------------------------------------------

    def apply(self, t, extra=""):
        """Check, change, and RECORD — in that order and in one call."""
        t = T(t)
        try:
            step(self.state, t)
        except IllegalTransition as e:
            raise IllegalTransition(f"{self.cid}: {e}") from None
        self._emit(t.value, extra)
        return self.state

    def _emit(self, action, extra=""):
        log = self._transitions_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"{_now()}\t{action}\t{self.cid}\t{extra}\n")

    def _transitions_log(self):
        return Path(os.environ.get(
            "TRANSITIONS_LOG",
            _plane.transitions_log(self.root)))

    def note_pause(self):
        """Mark this cell paused before a StandDown — without doubling the
        operator's event. The COMMAND owns Pause (fae/driver/ops.py's
        request_pause emits it with the .paused file); the driver emits one
        only when the ledger lacks it (a raw file write), so the replay sees
        exactly one Pause however the pause arrived. The local intent flips
        either way, which is
        what makes the StandDown legal to apply."""
        intent = "run"
        try:
            for line in self._transitions_log().read_text().splitlines():
                p = line.split("\t")
                if len(p) < 3 or p[2] != self.cid:
                    continue
                if p[1] == "EPOCH":
                    m = re.search(r"intent=(\w+)", p[3] if len(p) > 3 else "")
                    intent = m.group(1) if m else "run"
                elif p[1] in ("Pause", "Resume", "Kill"):
                    intent = {"Pause": "paused", "Resume": "run",
                              "Kill": "killed"}[p[1]]
        except OSError:
            pass
        if intent == "run":
            self.apply(T.PAUSE)
        else:
            self.state.intent = "paused"

    # --- heartbeat --------------------------------------------------------

    def hb(self, phase, attempt=0):
        """Declare the phase. Write-only telemetry: it must never fail an
        attempt. `since` is carried forward while phase and attempt are
        unchanged — the file's mtime cannot answer "how long in this phase",
        because the ticker rewrites it to prove liveness."""
        phase = Phase(phase)
        self._phase, self._attempt = phase, attempt
        f = self.ws / ".loop"
        since = int(time.time())
        try:
            m = re.search(r"phase=(\S+).*?attempt=(\S+).*?since=(\d+)",
                          f.read_text())
            if m and m.group(1) == phase.value and m.group(2) == str(attempt):
                since = int(m.group(3))
        except (OSError, ValueError, AttributeError):
            pass
        try:
            f.write_text(f"pid={os.getpid()} cid={self.cid} "
                         f"phase={phase.value} attempt={attempt} "
                         f"ts={int(time.time())} since={since}\n")
        except OSError:
            pass
        return PHASE_TO_LOOP[phase]

    @property
    def surface(self):
        """The authorable surface of this cell's artifacts (fae/cell/surface.py)."""
        if self._surface is None:
            self._surface = Surface(self.artifacts, self.variant)
        return self._surface

    # --- checkpoints (delegated, guarded) ---------------------------------

    def tree(self):
        return self.ckpt.tree()

    def checkpoint(self, label):
        self._refuse_if_sealed("checkpointing")
        return self.ckpt.commit(label)

    @property
    def judged_tree(self):
        return self.ckpt.judged

    def noedit(self):
        return self.ckpt.noedit()

    def charged_tree(self):
        """The tree of the last CHARGED verdict (the ITER line's tree=), or
        before any verdict the tree attempt 1 started from. Read from the
        ledger: it is the one record a void never writes. Abbreviated as the
        ledger abbreviates it; None when no attempt has started."""
        try:
            lines = (self.ws / "iterations.log").read_text(
                errors="replace").splitlines()
        except OSError:
            return None
        base = None
        for line in lines:
            p = line.split("\t")
            if len(p) < 4:
                continue
            if p[1] == "ITER":
                m = re.search(r"\btree=([0-9a-f]+)", p[-1])
                if m:
                    base = m.group(1)
            elif p[1] == "CKPT" and base is None:
                m = re.search(r"\bpre tree=([0-9a-f]+)", p[-1])
                if m:
                    base = m.group(1)
        return base

    def restore_charged(self, attempt):
        """An attempt starts from the tree its predecessor was CHARGED on.
        A run that ends without a verdict (void, stand-down, stop, kill,
        pause after the agent ran) leaves the agent's edits behind; they are
        checkpointed as provenance, then the charged tree is restored. Done
        at the START, not at each void: a SIGKILL runs no code, and this one
        place covers every way a run can end. Returns the tree restored to,
        or None when the tree already was the charged one."""
        base = self.charged_tree()
        if not base:
            return None
        cur = self.tree()
        if cur.startswith(base):
            return None
        self.checkpoint(f"abandoned before attempt {attempt} (never charged)")
        try:
            now = self.ckpt.restore(base)
            if not now.startswith(base):
                raise RuntimeError(f"tree is {now[:12]} after restoring {base}")
        except RuntimeError as e:
            # Without the charged tree the retry is a different experiment;
            # this host's checkpoint chain is broken, not the fleet.
            self._append("HALT", f"attempt={attempt}", f"restore-failed: {e}")
            self.apply(T.CRASH, "restore-failed")
            raise Halt(f"HALT[infra]: {self.cid} could not restore the "
                       f"charged tree {base}: {e}", self.INFRA_EXIT) from e
        # A checkout rewrites files; the seal is a property of the tree.
        try:
            self.surface.seal()
        except (OSError, ValueError):
            pass
        self._append("RESTORE", f"attempt={attempt}",
                     f"tree={base[:12]} (was {cur[:12]})")
        return base

    # --- judgement --------------------------------------------------------

    @property
    def gate_def(self):
        """The experiment's gate: the arrangements every attempt must pass."""
        from . import experiment as _experiment
        return _experiment.current().gate

    @property
    def gate_shapes(self):
        """The arrangements this attempt must pass.

        Every arrangement the experiment's gate declares: GREEN means the
        build passes all of them. A smoke cell (a pipeline check, never
        scored) may set SHAPE_GATE to anything but "all" for the single seed
        arrangement; the config refuses it anywhere else.
        """
        if str(self.conf.values.get("SHAPE_GATE") or "all") == "all":
            return tuple(self.gate_def.arrangements)
        return (None,)

    def judge(self, attempt, verify_results):
        """Green iff EVERY required arrangement passed. A short result list is
        a FAILURE, not a pass: the gate stops at the first failing arrangement,
        so "fewer than required" is the shape of a cell that failed one."""
        self._refuse_if_sealed("judging")
        results = list(verify_results)
        if not results or not all(r.green for r in results):
            return "fail"
        if len(results) < len(self.gate_shapes):
            return "fail"
        return "green" if attempt <= self.budget else "budget"

    def record(self, result, note=""):
        """Append the ITER line through the one writer that exists, so the
        ledger keeps having a single appender."""
        self._refuse_if_sealed("recording an attempt")
        ledger.record_iter(self.ws, result, note)

    def iter_note(self, attempt, results, post, stage=None):
        """The ITER note, field for field what the bash driver recorded.

        Everything that reads e2e=, verify_s= and shape-gate= off ITER lines
        must find the same fields whichever implementation ran the cell — a
        note that names only the stage leaves holes in every analysis that
        compares the two. `e2e=` is the verifier's own vocabulary: written
        when its metrics carry e2e_pass/e2e_total, absent otherwise.
        """
        m = (results[-1].metrics or {}) if results else {}
        e2e = (f" e2e={m.get('e2e_pass', '?')}/{m.get('e2e_total', '?')}"
               if "e2e_pass" in m or "e2e_total" in m else "")
        verify_s = int(sum(r.seconds or 0 for r in results))
        if stage is None:
            shapes = " shapes=all" if len(self.gate_shapes) > 1 else ""
            return (f"attempt={attempt}{e2e}{shapes} "
                    f"verify_s={verify_s} tree={post[:12]}")
        note = (f"attempt={attempt} stage={stage}{e2e} "
                f"verify_s={verify_s} tree={post[:12]}")
        failing = [r for r in results if not r.green]
        if failing:
            note += f" shape-gate={failing[0].shape or 'seed'}"
        return note

    def is_valid(self):
        """Is the RECORDED result trustworthy? Distinct from `verdict`, which
        says what the result was. Allowed on a sealed cell: validation is
        derived, and the rules are meant to improve and be re-applied."""
        try:
            return json.loads((self.ws / "validation.json").read_text()
                              ).get("verdict") != "TAINTED"
        except (OSError, ValueError):
            return None          # never validated — not the same as valid

    # --- verify -----------------------------------------------------------

    def _fingerprint(self):
        try:
            return _rig.fp(self.root, self.conf.child_env({"REPO_ROOT": self.root}))
        except (RuntimeError, OSError):
            return None

    @property
    def expected_fp(self):
        """Pinned once per cell: every arrangement is checked against the
        surface as it stood when the cell started."""
        if self._fp is None:
            self._fp = self._fingerprint()
        return self._fp

    def exclusive_acquire(self, name, poll=5.0):
        """The lock the verifier declares exclusive (`EXCLUSIVE` in the
        definition: a singleton infra concurrent holders would destroy
        for each other; None when every verify owns its own). Held on THIS
        process's fd around the verifier subprocess, which inherits no fd.
        Returns the open file (closing it is the release), or None past the
        wait deadline or on an operator pause."""
        locks = _plane.locks(self.root)
        d = Path(self.conf.get("RIG_LOCK_DIR") or locks / "rig-lock") if name == "rig" \
            else locks / f"{name}-lock"
        d.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + int(self.conf.get("RIG_LOCK_WAIT_S") or 3600)
        fh = _mutex.open_lock(d)
        while not _mutex.try_fd(fh):
            if (self.ws / ".paused").exists() or time.time() >= deadline:
                fh.close()
                return None
            time.sleep(poll)
        _mutex.note_holder(d, self.cid, os.getpid())
        return fh

    def verify(self, shape=None, out_dir=None):
        """Run ONE arrangement through the experiment's verifier, out of
        process (fae/cell/verify.py), under the exclusive lock it
        declares; pin the fingerprint around it; persist what it answered.

        `out_dir` is what makes a re-verify non-destructive.
        """
        if self.sealed and out_dir is None:
            raise Sealed(f"{self.cid} is sealed; a verify that writes into the "
                         f"workspace would overwrite the recorded result. "
                         f"Pass out_dir=... to add evidence instead.")
        from . import experiment as _experiment
        out = Path(out_dir or self.ws)
        run_out = out / RUN_OUT
        run_out.mkdir(parents=True, exist_ok=True)
        ctx = Ctx(root=str(self.root), experiment_dir=str(self.conf.get("EXPERIMENT_DIR")),
                  workspace=str(self.ws), artifacts=str(self.artifacts), out=str(run_out),
                  cid=self.cid, task=self.task, variant=self.variant,
                  arrangement=shape, expected_fp=self.expected_fp)
        definition = _experiment.current()
        self._archive_interrupted(out)
        self._mark_inflight(out, shape)
        # an infra already dead voids fast, before a deploy and a load
        # are spent on a corpse
        if not self.infra.alive():
            v = Verdict(ok=False, stage="infra", charge=False,
                        why="the arm's infra was dead before the arrangement",
                        arrangement=shape)
            self._persist_verdict(out, v)
            return VerifyResult.from_verdict(v, shape, out_dir)
        name = definition.exclusive
        fh = None
        if name:
            fh = self.exclusive_acquire(name)
            if fh is None:
                v = Verdict(ok=False, stage="exclusive-lock", charge=False,
                            why=f"gave up waiting for the {name} lock", arrangement=shape)
                self._persist_verdict(out, v)
                return VerifyResult.from_verdict(v, shape, out_dir)
        before = self._record_snapshot()
        started = time.time()
        try:
            v = run_verifier(ctx, self.infra,
                             timeout_s=int(self.conf.get("VERIFIER_TIMEOUT_S") or 7200),
                             log_dir=out)
        finally:
            if fh is not None:
                fh.close()
        changed = self._record_changes(before)
        if changed:
            # the verify reached the cell's record: nothing it reports can be trusted
            self._append("ALERT", f"attempt={self._attempt}",
                         "INTEGRITY the verify changed the cell's record: "
                         + " ".join(changed)[:300])
            v = Verdict(ok=False, stage="integrity", charge=False,
                        why=f"the verify changed the cell's record ({', '.join(changed[:5])})",
                        stand_down=("integrity",), arrangement=v.arrangement,
                        seconds=v.seconds)
        self._take_verify_output(run_out, out, record_events=out_dir is None and not changed)
        missing = self._missing_outputs(run_out, v, started)
        if missing and not changed:
            # a rig defect, the same on every retry: the operator's, not a refund
            self._append("ALERT", f"attempt={self._attempt}",
                         "RIG-OUTPUT the verify left no " + " ".join(missing)[:300])
            v = Verdict(ok=False, stage="rig-output", charge=False,
                        why=f"the verify left no {', '.join(missing)}",
                        stand_down=("rig-output",), metrics=v.metrics,
                        arrangement=v.arrangement, seconds=v.seconds, files=v.files)
        measured = definition.verifier_class().MEASURED_STAGES
        if (not v.ok and v.charge and (measured is None or v.stage in measured)
                and not self.infra.alive()):
            # died under the measurement: what it measured is not the build's
            v = Verdict(ok=False, stage="infra", charge=False,
                        why=f"the arm's infra died during the arrangement (was: {v.stage})",
                        metrics=v.metrics, arrangement=v.arrangement,
                        seconds=v.seconds, files=v.files)
        if self.expected_fp and self._fingerprint() != self.expected_fp:
            # the verify surface moved under the cell: nothing this
            # arrangement measured can be attributed
            v = Verdict(ok=False, stage="harness-fp", charge=False,
                        why="verify-surface fingerprint changed mid-run",
                        metrics=v.metrics, arrangement=v.arrangement,
                        seconds=v.seconds, files=v.files)
        self._persist_verdict(out, v)
        return VerifyResult.from_verdict(v, shape, out_dir)

    INFLIGHT = ".verify-inflight.json"
    # The cell's record, which no verify may change: hashed whole, or (for
    # the archive, the checkpoint refs and the judged tree) by size and mtime.
    # The ledger is checked apart: the supervisor may append an ALERT to a
    # live cell's ledger while it verifies, and nothing else may.
    RECORD_FILES = ("cell.env", ".skeleton_manifest", ".sealed")
    RECORD_TREES = ("arrangements", ".attempts.git/refs", "artifacts")
    SUPERVISOR_EVENTS = frozenset({"ALERT"})

    def _record_snapshot(self):
        import hashlib
        led = self.ws / "iterations.log"
        snap = {"iterations.log": led.read_bytes() if led.is_file() else None}
        for name in self.RECORD_FILES:
            f = self.ws / name
            if f.is_file():
                snap[name] = hashlib.sha256(f.read_bytes()).hexdigest()
        for name in (".attempts.git/HEAD", ".attempts.git/packed-refs"):
            f = self.ws / name
            if f.is_file():
                snap[name] = f.read_bytes()
        for tree in self.RECORD_TREES:
            root = self.ws / tree
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    st = p.stat()
                    snap[str(p.relative_to(self.ws))] = (st.st_size, st.st_mtime_ns)
        return snap

    def _record_changes(self, before):
        """Relpaths of the cell's record a verify changed, added or removed.
        The ledger counts as changed when its earlier content moved or a
        line other than a supervisor's was appended."""
        after = self._record_snapshot()
        old, new = before.pop("iterations.log"), after.pop("iterations.log")
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if old != new:
            grown = new is not None and old is not None and new.startswith(old)
            added = new[len(old):].decode(errors="replace").splitlines() if grown else []
            if not grown or any(len(l.split("\t")) < 2 or l.split("\t")[1] not in
                                self.SUPERVISOR_EVENTS for l in added):
                changed.insert(0, "iterations.log")
        return changed
    # Written by the host alone; a verifier that declares one of these names
    # as an output does not get it copied over the host's record.
    HOST_OWNED = frozenset({"iterations.log", "metrics.json", "verifier.log", "cell.env",
                            INFLIGHT, "arrangements", "artifacts", "feedback",
                            ".skeleton_manifest", "score.json", "validation.json",
                            ".sealed", RUN_OUT})

    def _missing_outputs(self, run_out, v, started):
        """The experiment's REQUIRED_OUTPUTS a verify that ran did not write:
        absent from its own directory, or left there by an earlier verify."""
        from . import experiment as _experiment
        cls = _experiment.current().verifier_class()
        if v.stage in cls.NOT_RUN_STAGES:
            return []
        missing = []
        for n in cls.REQUIRED_OUTPUTS:
            f = run_out / n
            try:
                fresh = f.stat().st_mtime >= started - 1
            except OSError:
                fresh = False
            if not fresh:
                missing.append(n)
        return missing

    def _take_verify_output(self, run_out, out, record_events=True):
        """After the verify's container exits: append the ledger events it
        recorded (only the allowed kinds, under this cell's id), then copy the
        verifier's declared outputs from its own directory up into `out`,
        where every reader expects them. A symlink is never followed."""
        from . import experiment as _experiment
        for stamp, event, fields in take_events(run_out):
            if record_events:
                ledger.append(self.ws, event, self.cid, *fields, stamp=stamp)
        cls = _experiment.current().verifier_class()
        for name in dict.fromkeys((*cls.FILES, *cls.FEEDBACK_LOGS, *cls.REQUIRED_OUTPUTS)):
            if name in self.HOST_OWNED or "/" in name:
                continue
            src, dst = run_out / name, out / name
            if src.is_symlink() or not src.exists():
                continue
            try:
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                elif dst.exists() or dst.is_symlink():
                    dst.unlink()
                if src.is_dir():
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)
            except OSError:
                continue

    def _mark_inflight(self, out, shape):
        """Written before a verify runs, removed when its verdict is archived:
        one still present at the next verify is a run that never returned."""
        log = out / "verifier.log"
        (out / self.INFLIGHT).write_text(json.dumps({
            "attempt": self._attempt, "arrangement": shape, "started": _now(),
            "started_epoch": time.time(),
            "verifier_log_offset": log.stat().st_size if log.is_file() else 0}) + "\n")

    def _archive_interrupted(self, out):
        marker = out / self.INFLIGHT
        try:
            m = json.loads(marker.read_text())
        except (OSError, ValueError):
            return
        self._archive(out, m, "interrupted", {
            "ok": False, "charge": False, "stage": "interrupted",
            "why": "the verify never returned: the cell was stopped or its process died"})

    def _persist_verdict(self, out, v):
        """metrics.json (the identity, then the verifier's numbers) and every
        run's evidence under arrangements/NN-a<attempt>-<label>-<end state>/,
        before the next arrangement reuses the names."""
        doc = {"cell_id": self.cid, "task": self.task or None,
               "variant": self.variant or None, **v.metrics}
        (out / "metrics.json").write_text(json.dumps(doc, indent=2) + "\n")
        try:
            m = json.loads((out / self.INFLIGHT).read_text())
        except (OSError, ValueError):
            m = {"attempt": self._attempt, "arrangement": v.arrangement,
                 "started": None, "verifier_log_offset": None}
        end = "green" if v.ok else ("charged" if v.charge else "refunded")
        self._archive(out, {**m, "arrangement": v.arrangement}, end,
                      {"ok": v.ok, "charge": v.charge, "stage": v.stage, "why": v.why,
                       "seconds": v.seconds, "stand_down": list(v.stand_down)},
                      files=v.files)

    def _archive(self, out, m, end, verdict, files=()):
        from . import experiment as _experiment
        cls = _experiment.current().verifier_class()
        names = list(dict.fromkeys((*(files or cls.FILES), *cls.FEEDBACK_LOGS,
                                    *cls.REQUIRED_OUTPUTS, "metrics.json")))
        base = out / "arrangements"
        base.mkdir(exist_ok=True)
        n = len([d for d in base.iterdir() if d.is_dir()]) + 1
        d = base / f"{n:02d}-a{m.get('attempt') or 0}-{m.get('arrangement') or 'seed'}-{end}"
        d.mkdir(exist_ok=True)
        # a log older than the run is a previous run's, not this one's
        since = m.get("started_epoch")
        copied = []
        for f in names:
            src = out / f
            try:
                if since is not None and src.exists() and src.stat().st_mtime < since - 1:
                    continue
                if src.is_dir():
                    shutil.copytree(src, d / f)
                elif src.is_file():
                    shutil.copy2(src, d / f)
                else:
                    continue
            except OSError:
                continue
            copied.append(f)
        offset = m.get("verifier_log_offset")
        log = out / "verifier.log"
        if offset is not None and log.is_file():
            with log.open("rb") as fh:
                fh.seek(offset)
                (d / "verifier.log").write_bytes(fh.read())
            copied.append("verifier.log")
        (d / "verdict.json").write_text(json.dumps({
            "attempt": m.get("attempt"), "arrangement": m.get("arrangement"),
            "end_state": end, "refunded": end in ("refunded", "interrupted"),
            "started": m.get("started"), "ended": _now(), **verdict,
            "files": copied}, indent=2) + "\n")
        (out / self.INFLIGHT).unlink(missing_ok=True)

    def gate(self, attempt=1, seed_shape=None):
        """The full shape gate. Stops at the first failure — the attempt is
        already lost, and the remaining arrangements would cost another quarter
        of an hour of infra to say the same thing.

        Each arrangement's verdict is a SHAPE event: it is what the fleet's
        GATE column counts, so without it a gate in flight reads as 0/6 for its
        whole duration. The gate leaves NO feedback file: a run of it may still
        be voided, and feedback belongs to the charged verdict (`feedback`).
        """
        results, passed = [], []
        bad = self.surface.check()
        if bad:
            # heal ran before this gate: a fixed file that still differs is a
            # harness anomaly, refunded, never a build verdict
            self._append("ALERT", f"attempt={attempt}",
                         "HEAL-FAILED fixed files differ after heal: " + " ".join(bad)[:400])
            return [VerifyResult(green=False, stage_failed="harness-heal", charge=False)]
        order = list(self.gate_shapes)
        if seed_shape is None and len(order) > 1 and self.gate_def.rotate:
            # The seeded arrangement rotates with the attempt number, so an
            # agent iterating on trace feedback never sees the same first
            # timeline twice in a row and cannot hardcode it.
            seed_shape = order[attempt % len(order)]
        if seed_shape in order:
            order.remove(seed_shape)
            order.insert(0, seed_shape)
        for s in order:
            r = self.verify(shape=s)
            results.append(r)
            label = s or "seed"
            if r.green:
                passed.append(label)
                self._append("SHAPE", f"attempt={attempt}", f"{label} pass")
                continue
            self._append("SHAPE", f"attempt={attempt}",
                         f"{label} FAIL (passed: {' '.join(passed) or 'none'})")
            break
        return results

    def feedback(self, results, green):
        """Leave shapegate.last for the next attempt's prompt — from the
        verdict being CHARGED, at the charge. While the gate wrote it, it
        named whatever ran last: a run voided afterwards (contract, nostart)
        had already replaced the charged attempt's arrangement with its own,
        and the next prompt blamed the wrong one. A void never reaches here,
        so it leaves the charged feedback exactly as it was."""
        sg = self.ws / "shapegate.last"
        if green:
            sg.unlink(missing_ok=True)
            return
        results = list(results)
        passed = [r.shape or "seed" for r in results if r.green]
        failed = next((r.shape or "seed" for r in results if not r.green),
                      "seed")
        sg.write_text(f"{failed}|{' '.join(passed)}\n")

    def reverify(self, stamp=None):
        """Re-run the gate WITHOUT touching the recorded result.

        Everything lands under `reverify/<ts>/`; iterations.log is not appended
        to, and metrics.json, trace.csv and verify.log keep the numbers the
        cell was judged on. This is the tool for iterating on the verify
        itself — change it, re-verify a sample, diff, repeat — which is
        impossible while the only way to re-run is to overwrite.
        """
        out = self.ws / "reverify" / (stamp or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        out.mkdir(parents=True, exist_ok=True)
        # The arm's infra is provisioned the same way a run provisions it:
        # the verify probes a cache that only the setup hook brings up.
        results = []
        # a re-verify always runs the full gate
        prev = {"SHAPE_GATE": self.conf.values.get("SHAPE_GATE")}
        self.conf.values["SHAPE_GATE"] = "all"
        try:
            rc, _ = self.setup()
            if rc != 0:
                raise RuntimeError(f"{self.variant} cell_setup failed (rc={rc}) "
                                   f"— cannot re-verify without its infra")
            # one verify on the machine at a time, the same lock a run's gate holds
            vlock = self.verify_lock_acquire()
            if vlock is None:
                raise RuntimeError(f"{self.cid} is paused (.paused): not re-verified")
            try:
                for s in self.gate_shapes:
                    results.append(self.verify(shape=s, out_dir=out))
                    if not results[-1].green:
                        break
            finally:
                vlock.close()
        finally:
            for k, v in prev.items():
                if v is None:
                    self.conf.values.pop(k, None)
                else:
                    self.conf.values[k] = v
            self.teardown()
        (out / "reverify.json").write_text(json.dumps({
            "cid": self.cid, "at": _now(),
            "shapes": [r.shape for r in results],
            "green": all(r.green for r in results)
                     and len(results) == len(self.gate_shapes),
            "stages": [r.stage_failed for r in results]}, indent=2) + "\n")
        return results

    # --- the attempt loop -------------------------------------------------

    def _append(self, event, *fields):
        return ledger.append(self.ws, event, self.cid, *fields)

    def prepare(self, fresh=False):
        """Seed the workspace through the one module that knows how, recording
        IMPL (this engine's) so the result is attributable without later
        archaeology.
        `cli.py experiment prepare` calls the same function over the matrix."""
        from . import prepare as _prepare
        _prepare.prepare(self.cid, self.task, self.variant,
                         self.rep, workspaces=self.workspaces, root=self.root,
                         fresh=fresh, reference=self.reference, impl=self.IMPL,
                         agent_model=self.conf.values.get("AGENT_MODEL")
                         or os.environ.get("AGENT", "?"), cfg=self.conf)
        self._env = self._read_env()
        return self.ws

    @property
    def variant_cls(self):
        """This cell's variant class (the experiment's declaration), or None
        for a variant the experiment does not declare."""
        return _variants.registry().get(self.variant)

    def agent_image(self):
        """The image this cell's agents run in: its arm's layer over the base."""
        from . import experiment as _experiment
        from . import image as _image
        return _image.agent_tag(_experiment.current(), self.root, self.variant_cls)

    @property
    def arm(self):
        """The exclusive lock this cell holds for its lifetime (the variant's
        LOCK), or None: bounded by the work slot alone."""
        s = self.variant_cls
        return s.LOCK if s else None

    @property
    def queues(self):
        """The queues (fae/queues.py): this cell's slots live there."""
        return Queues(_plane.queues(self.root), locks=_plane.locks(self.root))

    def verify_lock_acquire(self, poll=5.0):
        """The fleet-wide verify lock: gates are serialized WHOLE, so one
        cell's six arrangements never interleave with another's and verify_s
        never counts another cell's rig time. Blocks until owned; returns the
        open file (closing it is the release), or None on an operator pause —
        checked both while queued and right after winning, so the lock is
        never held by a cell that intends to exit.
        """
        d = Path(self.conf.get("VERIFY_LOCK_DIR")
                 or _plane.locks(self.root) / "verify-lock")
        d.parent.mkdir(parents=True, exist_ok=True)
        fh = _mutex.open_lock(d)
        while not _mutex.try_fd(fh):
            if (self.ws / ".paused").exists():
                fh.close()
                return None
            time.sleep(poll)
        _mutex.note_holder(d, self.cid, os.getpid())
        if (self.ws / ".paused").exists():
            fh.close()
            return None
        return fh

    def loop_lock(self):
        """Exclusive claim on this workspace. Returns the open file, or None if
        another loop already owns the cell."""
        d = _plane.locks(self.root) / "loop-locks"
        d.mkdir(parents=True, exist_ok=True)
        fh = _mutex.open_lock(d / self.cid)
        if not _mutex.try_fd(fh):
            fh.close()
            return None
        _mutex.note_holder(d / self.cid, self.cid, os.getpid())
        return fh

    def start_ticker(self, interval=None):
        """Rewrite .loop on a timer so liveness stays visible while a phase
        blocks. Returns a stop callable.

        The heartbeat is what identifies this loop to the fleet — it carries
        the cell's OWN pid, where a ps scan can only find whichever ancestor
        shell matches. So the ticker RE-ASSERTS it rather than echoing: a
        missing file is rewritten from the last phase this object declared.
        """
        stop = threading.Event()
        # HB_TICK is config (or its default), not exported env; read it
        # through the config so an operator's override is seen.
        interval = int(interval or os.environ.get("HB_TICK")
                       or self.conf.get("HB_TICK", 30))

        def beat():
            while not stop.wait(interval):
                self.hb(self._phase, self._attempt)

        threading.Thread(target=beat, daemon=True, name="hb").start()
        return stop.set

    SLOTS_HELD_ENV = "CELL_SLOTS_HELD"

    def acquire_slots(self):
        """Take the work slot, and the lock's slot for a locked variant
        (Queues.acquire_slots). The cell process holds them on its own fds for
        its whole life, so no lock can outlive the process that took it.

        Returns (slots, None), or (slots, queue) when it stood down — the
        ledger must name the queue it happened in. The caller closes `slots`.
        """
        s = self.variant_cls
        return self.queues.acquire_slots(
            self.cid, int(self.conf.get("WORK_SLOTS", 7)), lock=self.arm,
            lock_slots=int(self.conf.get(f"ARM_SLOTS_{(self.arm or '').upper()}",
                                         s.LOCK_SLOTS if s else 1)),
            on_wait_work=lambda: self.hb(Phase.SLOT_WAIT, 0),
            on_work_slot=lambda: self.apply(T.ACQUIRE_SLOT),
            on_wait_lock=lambda: self.hb(Phase.ARM_LOCK, 0))

    @property
    def infra(self):
        """The variant's infra for this cell: its infra class, with the
        variant (fae/cell/infra/base.py)."""
        if getattr(self, "_infra", None) is None:
            self._infra = _variants.for_cell(self)
        return self._infra

    def setup(self):
        """The cell's network, then the arm's provisioning on it. Returns
        (rc, env): 0 and the agent-container env on success; 1 and the
        failure already logged on a rig fault."""
        try:
            self.infra.network_up()
        except RuntimeError as e:
            self.infra.log(f"HALT[infra]: {e}")
            return 1, {}
        try:
            return 0, self.infra.cell_setup()
        except _variants.HookFailure:
            return 1, {}

    def teardown(self):
        """The arm's, then the cell network it lived on."""
        try:
            self.infra.cell_teardown()
        except Exception as e:           # best-effort by contract
            self._append("ALERT", "teardown", f"{type(e).__name__}: {e}")
        try:
            self.infra.network_down()
        except Exception as e:
            self._append("ALERT", "teardown", f"network: {type(e).__name__}: {e}")

    def release_slot(self, reason):
        """Emit the model's one ReleaseSlot and clear this cell's holder
        notes in the same act."""
        self.apply(T.RELEASE_SLOT, reason)
        self.clear_holder_notes()

    def contract_stand_down(self, attempt, broken):
        """The verification world did not match the docs: ALERT, pause the
        cell as the operator would (the .paused file is what keeps conduct
        from respawning it), and stand down. The StandDown releases the slot
        the way the model does."""
        note = " | ".join(broken)
        self._append("ALERT", f"attempt={attempt}", f"CONTRACT: {note[:400]}")
        (self.ws / ".paused").write_text(
            f"contract by=driver at={_now()}\n" + "".join(f"{b}\n" for b in broken))
        self.note_pause()
        self._append("PAUSED", f"attempt={attempt}", "contract")
        if self.state.loop in (Loop.IDLE, Loop.AGENT):
            self.apply(T.STAND_DOWN, "contract")
        self.clear_holder_notes()

    def clear_holder_notes(self):
        """Drop every slot holder note naming this cell (Queues.clear_holder_notes)."""
        self.queues.clear_holder_notes(self.cid, self.arm)

    def infra_ok(self):
        """An ENVIRONMENT fault must never burn an attempt, and must never be
        fed back to the agent as if its code had failed. The variant knows
        what its infra needs (a docker daemon, an image, nothing); the
        image its cells are verified in is built here, before an attempt,
        never under the verify lock."""
        if not _variants.liveness_declared(self.infra.variant):
            self.infra.log(f"HALT[infra]: {type(self.infra).__name__} "
                           "declares no alive() probe; every "
                           "arrangement would be void")
            return False
        try:
            authorable(self.variant)
        except RuntimeError as e:
            self.infra.log(f"HALT[definition]: {e}")
            return False
        if not self.infra.ok():
            return False
        try:
            self.infra.image()
        except RuntimeError as e:
            self.infra.log(f"HALT[infra]: verify image: {str(e).splitlines()[0]}")
            return False
        return True

    AGENT_HOMES = {"agy": (".agent-gemini", "stage_agent_gemini"),
                   "opencode": (".agent-opencode", "stage_agent_opencode"),
                   "testagent": (".agent-testagent", "stage_agent_testagent"),
                   "claude": (".agent-claude", "stage_agent_claude")}

    def stage_agent(self):
        """Give the cell its own agent home, holding credentials and nothing
        else. One per cell: a shared home would let each agent read every prior
        agent's memory and transcripts.

        Returns the staged path, or None when the agent needs no CLI.
        """
        if self.conf.get("AGENT", os.environ.get("AGENT", "")) == "human":
            return None
        cli = self.conf.get("AGENT_CLI", "claude")
        rel, _ = self.AGENT_HOMES.get(cli, self.AGENT_HOMES["claude"])
        dest = self.ws / rel
        _config.stage_agent(self.conf, cli, dest, self.root)
        return dest

    @staticmethod
    def _tail(path, n=40):
        try:
            return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    LIMIT_RETRY_S = 600
    AGENT_FAULT_RETRIES = 6

    def _transient_fault(self, log, rc, gate_rc=True):
        """faults.LIMIT (retry the same attempt), faults.AUTH (halt for a
        human), or None. See fae/cell/faults.py for the evidence order."""
        try:
            text = log.read_text(errors="replace")
        except OSError:
            return None
        return faults.classify(text, rc, gate_rc=gate_rc)

    @staticmethod
    def _docker_refused(log):
        """docker's own error in the attempt log: the agent never ran."""
        try:
            return any(l.startswith("docker: ")
                       for l in log.read_text(errors="replace").splitlines())
        except OSError:
            return False

    @staticmethod
    def _produced_output(log):
        """Anything beyond permission warnings and blank lines."""
        try:
            return any(l.strip() and not l.startswith("Permission deny rule")
                       for l in log.read_text(errors="replace").splitlines())
        except OSError:
            return False

    def _pause_sleep(self, seconds):
        """Sleep, waking early if the operator asks this cell to stop. True
        means we are paused."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if (self.ws / ".paused").exists():
                return True
            time.sleep(min(5, max(0.0, deadline - time.time())))
        return (self.ws / ".paused").exists()

    # What agent_with_retries returns besides True (judge the attempt).
    PAUSED = "paused"            # operator stop during a wait: stand down
    AGENT_FAULT = "agent-fault"  # no output past the retry cap: systemic halt
    AGENT_CONTAINER = "agent-container"  # docker could not start the agent: rig fault
    # `docker run`'s own failure (no such image, a bad flag, the daemon refused)
    # as opposed to the agent's exit code
    DOCKER_RUN_FAILED = 125
    AGENT_TIMEOUT_S = 3 * 3600
    AGENT_TIMED_OUT = -9      # _agent's return when the wall-clock limit killed it
    AUTH_WALL = "auth-wall"      # credentials/config: systemic halt

    def agent_with_retries(self, attempt, stub_overlay=None, agent_cmd=None,
                           extra_env=None, pre=None):
        """Run the agent until it produces a build, a fault budget runs out, or
        the operator pauses. Returns True when the attempt may be judged, else
        one of PAUSED / AGENT_FAULT / AUTH_WALL.

        A transient fault retries the SAME attempt indefinitely — a usage wall
        is not a failed build, and charging it as one corrupts
        attempts-to-green. A CLI that produces nothing is retried too, but
        capped: a broken binary must not loop for the life of the fleet. An
        auth or config wall is never retried: no wait lifts it.

        `pre` is the tree the attempt started from. A run that changed nothing
        and whose transcript names a wall is a wall whatever its exit code —
        the rc gate exists to keep a finished build's own prose from voiding
        it, and an untouched tree is not a finished build.
        """
        if stub_overlay:
            # A stub writes no transcript, so the fault checks below would read
            # it as a dead CLI and wait out a limit that does not exist.
            self._agent(attempt, stub_overlay=stub_overlay,
                        agent_cmd=agent_cmd, extra_env=extra_env)
            return True
        log = self.ws / f"agent.attempt-{attempt}.log"
        client = self._client_field()
        retry_s = int(self.conf.get("LIMIT_RETRY_S", self.LIMIT_RETRY_S))
        cap = int(self.conf.get("AGENT_FAULT_RETRIES", self.AGENT_FAULT_RETRIES))
        silent = 0
        runs = []
        while True:
            t0 = time.monotonic()
            rc = self._agent(attempt, stub_overlay=stub_overlay,
                             agent_cmd=agent_cmd, extra_env=extra_env)
            runs.append(time.monotonic() - t0)
            if rc == self.AGENT_TIMED_OUT:
                # a runaway agent is the agent's failure: what it wrote is judged
                self._append("ALERT", f"attempt={attempt}",
                             f"AGENT-TIMEOUT killed after {self._agent_timeout_s()}s; "
                             f"judged as it stands")
                self._append("AGENT", *agent_time_fields(attempt, runs, None), client)
                (self.ws / "timeout.last").write_text("1\n")
                return True
            if rc == self.DOCKER_RUN_FAILED and self._docker_refused(log):
                self._append("HALT", f"attempt={attempt}", "agent-container")
                return self.AGENT_CONTAINER
            kind = self._transient_fault(log, rc)
            if kind is None and pre is not None and self.tree() == pre:
                kind = self._transient_fault(log, rc, gate_rc=False)
            if kind == faults.AUTH:
                self._append("HALT", f"attempt={attempt}", "auth-wall")
                return self.AUTH_WALL
            if kind == faults.LIMIT:
                stamp = f"{datetime.now(timezone.utc):%H%M%S}"
                shutil.copy2(log, self.ws / f"agent.attempt-{attempt}.wait-{stamp}.log")
                self._append("WAIT", f"attempt={attempt}",
                             f"limit — retrying in {retry_s}s")
                self.hb(Phase.LIMIT, attempt)
                if self._pause_sleep(retry_s):
                    return self.PAUSED
                self.hb(Phase.AGENT, attempt)
                continue
            if not self._produced_output(log):
                silent += 1
                if silent > cap:
                    self._append("HALT", f"attempt={attempt}", "agent-fault")
                    return self.AGENT_FAULT
                self._append("WAIT", f"attempt={attempt}",
                             f"agent produced no output (rc={rc}) — "
                             f"retry {silent}/{cap} in {retry_s}s")
                self.hb(Phase.LIMIT, attempt)
                if self._pause_sleep(retry_s):
                    return self.PAUSED
                self.hb(Phase.AGENT, attempt)
                continue
            client_s = _config.client_reported_seconds(
                self.conf.get("AGENT_CLI", "claude"), log)
            self._append("AGENT", *agent_time_fields(attempt, runs, client_s), client)
            (self.ws / "timeout.last").unlink(missing_ok=True)
            return True

    def _client_field(self):
        """`client=<cli>:<version>`: the CLI this attempt runs and its version in
        the image it runs in, `-` when that cannot be read."""
        cli = self.conf.get("AGENT_CLI", "claude")
        try:
            from . import image as _image
            v = _image.client_versions(self.conf.get("AGENT_IMAGE") or self.agent_image(),
                                       self.queues.agent_clients_book()).get(cli)
        except Exception:       # an unreadable version never costs an attempt
            v = None
        return f"client={cli}:{v or '-'}"

    def _charged_note(self):
        """The last ITER note as a dict: the verdict the ledger charged, which
        is the one the feedback prompt is about. Empty when nothing was."""
        notes = ledger.parse(self.ws)["iter_notes"]
        if not notes:
            return {}
        return dict(kv.split("=", 1) for kv in notes[-1].split() if "=" in kv)

    def _prompt(self, attempt):
        """The task, plus why the previous attempt failed.

        An attempt retried without this is spent blind: the agent cannot see
        which arrangement failed, at which stage, or what the verify said.
        """
        base = self.ws / "PROMPT.md"
        if attempt == 1:
            return base
        p = self.ws / f"PROMPT.attempt-{attempt}.md"
        src = self._charged_run()
        try:
            m = json.loads((src / "metrics.json").read_text())
        except (OSError, ValueError):
            m = {}
        # metrics.json is whatever the last verify wrote, and that verify may
        # have been voided since; the charged verdict is the ledger's ITER.
        charged = self._charged_note()
        stage = charged.get("stage", m.get("stage_failed", ""))
        e2e = charged.get("e2e",
                          f"{m.get('e2e_pass', '')}/{m.get('e2e_total', '')}")
        parts = [base.read_text(errors="replace"),
                 f"\n\n---\n# BUILD FEEDBACK — attempt {attempt - 1} "
                 f"failed verification\n",
                 f"Your previous build did not pass verification "
                 f"(stage: {stage}; e2e {e2e}).\n",
                 "Fix your implementation in place and finish again. Keep the "
                 "contract\nstated in TODO.md (endpoints unchanged on port 8080, "
                 "load signals at\n/metrics, truthful cache_mounted in "
                 "/health).\n\n"]

        sg = self.ws / "shapegate.last"
        note = self.gate_def.feedback_note
        if note and sg.is_file() and sg.stat().st_size:
            failed, _, passed = sg.read_text().strip().partition("|")
            parts.append(note.format(passed=passed, failed=failed) + "\n\n")

        if (self.ws / "noedit.last").is_file():
            parts.append(
                "NOTE: your previous attempt modified NO files at all — the "
                "build was\nidentical, so verification was skipped and the "
                "attempt was still spent.\nYou must edit your authorable "
                "surface to make progress.\n\n")

        if (self.ws / "timeout.last").is_file():
            parts.append(
                "NOTE: your previous attempt did not finish: it was stopped "
                "while still\nrunning, and the build was judged as it stood. "
                "Any command you start\nmust terminate on its own.\n\n")

        heal = self.ws / "heal.last"
        if heal.is_file() and heal.stat().st_size:
            parts.append(
                f"NOTE: your changes to FIXED files were reverted before "
                f"verification and had\nno effect: {heal.read_text().strip()}\n"
                "Those files are not yours to edit — solve the task within "
                "your authorable\nsurface only.\n\n")

        evict = self.ws / "evict.last"
        if evict.is_file() and evict.stat().st_size:
            parts.append(
                f"NOTE: files you created outside your authorable surface were "
                f"moved out before\nverification and had no effect: "
                f"{evict.read_text().strip()}\n"
                "Write only within your authorable surface.\n\n")

        parts.append("## verify.log (tail)\n" + self._tail(src / "verify.log"))
        parts.append("\n## deploy.log (tail)\n" + self._tail(src / "deploy.log"))
        staged = self._stage_feedback(src)
        if staged:
            parts.append("\n## full logs\nThe complete logs of that run are "
                         "mounted read-only at /feedback/: "
                         + ", ".join(staged) + "\n")
        p.write_text("".join(parts))
        return p

    def _charged_run(self):
        """The archived run whose charge the next attempt answers: the last
        `-charged` one. A refunded or interrupted verify may
        have overwritten the workspace's logs since; the workspace itself is
        the answer only for a charge archived before end states were named."""
        base = self.ws / "arrangements"
        best = None
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and re.match(r"^\d+-a\d+-.+-charged$", d.name):
                    best = d
        return best or self.ws

    def _stage_feedback(self, src=None):
        """Copy the judged run's logs into <ws>/feedback for the agent to
        read. Outside artifacts/ on purpose: the tree hash is the no-edit
        oracle and a log in it would make every attempt look edited."""
        src_root = src or self.ws
        dest = self.ws / "feedback"
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir()
        staged = []
        from . import experiment as _experiment
        for name in _experiment.current().verifier_class().FEEDBACK_LOGS:
            src = src_root / name
            try:
                if src.is_dir():
                    shutil.copytree(src, dest / name)
                elif src.is_file():
                    shutil.copy2(src, dest / name)
                else:
                    continue
            except OSError:
                continue
            staged.append(name + ("/" if src.is_dir() else ""))
        return staged

    def _agent(self, attempt, stub_overlay=None, agent_cmd=None,
               extra_env=None):
        """One agent invocation. The stub overlay is the deterministic path — a
        fixed pre-made solution copied in place of a model call — and it is
        what makes a bash-vs-Python diff meaningful at all."""
        log = self.ws / f"agent.attempt-{attempt}.log"
        if stub_overlay:
            with log.open("w") as f:
                return subprocess.run(["cp", "-R", f"{stub_overlay}/.",
                                       str(self.artifacts)],
                                      stdout=f, stderr=f).returncode
        cli = self.conf.get("AGENT_CLI", "claude")
        home = self.ws / self.AGENT_HOMES.get(cli, self.AGENT_HOMES["claude"])[0]
        ee = extra_env or {}
        # An AGENT_CMD in the environment (or passed in) is an override — a
        # custom agent, or a rig-test stub. The normal path builds the
        # container argv from the config; no slot fd in pass_fds, so an agent
        # container outliving this loop can never hold its locks.
        # The scripted testagent reads TESTAGENT_PLAN from the environment; the
        # container's `-e TESTAGENT_PLAN` passthrough inherits it, so it must
        # never be forced empty here.
        base = {"CELL_ID": self.cid, "VARIANT": self.variant,
                "SERVICE_PORT": self.conf.get("SERVICE_PORT", "8080")}
        override = agent_cmd or os.environ.get("AGENT_CMD")
        with log.open("w") as f:
            if override:
                env = self.conf.child_env(
                    {**base, "PROMPT_FILE": self._prompt(attempt),
                     "REPO_ROOT": self.root, "ARTIFACTS": self.artifacts,
                     "art": self.artifacts, "ATTEMPT": attempt,
                     "AGENT_CLAUDE": home, "AGENT_GEMINI": home,
                     "AGENT_OPENCODE": home, "AGENT_TESTAGENT": home,
                     "AGENT_CMD": override}, extra_env)
                script = ('cd "$art"\n: "${DOCKER_NET:=}" "${KUBE_MOUNT:=}"\n'
                          'eval "$AGENT_CMD"\n')
                return self._run_bounded(["bash", "-c", script], f, env)
            env = self.conf.child_env(base, extra_env)
            prompt = self._prompt(attempt)
            fb = self.ws / "feedback"
            argv = _config.build_agent_argv(
                self.conf, self.cid, self.artifacts, home, prompt,
                ee.get("DOCKER_NET", ""), ee.get("KUBE_MOUNT", ""),
                feedback=str(fb) if fb.is_dir() else "", image=self.agent_image())
            return self._run_bounded(argv, f, env)

    def _agent_timeout_s(self):
        return int(self.conf.get("AGENT_TIMEOUT_S") or self.AGENT_TIMEOUT_S)

    def _run_bounded(self, argv, f, env):
        """Run the agent under the wall-clock limit. Killing the docker client
        leaves its container running, so the container goes too."""
        limit = self._agent_timeout_s()
        try:
            return subprocess.run(argv, stdout=f, stderr=f, env=env, timeout=limit).returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", _config.agent_container(self.cid)],
                           capture_output=True)
            f.write(f"\n[fae] agent killed after {limit}s (AGENT_TIMEOUT_S)\n")
            f.flush()
            return self.AGENT_TIMED_OUT

    def _restore_fixed(self):
        """Revert out-of-surface edits before judging (Surface.heal). The
        fixed rig behaves like infrastructure the agent cannot write: an edit
        outside the authorable surface never takes effect, no attempt is
        burned policing the boundary, and the build is judged on what it was
        allowed to change. Returns the restored relpaths joined by newlines."""
        from .variants import files
        cls = self.variant_cls
        sources = {**files.template_files(cls), **cls.INPUTS} if cls else {}
        return "\n".join(self.surface.heal(sources))

    def _evict_strays(self, attempt):
        """Move files that are neither seeded nor authorable out of artifacts/
        (Surface.evict) into ws/.out-of-surface/attempt-N/, so they cannot
        reach the verify. Returns the moved relpaths joined by spaces."""
        dest = self.ws / ".out-of-surface" / f"attempt-{attempt}"
        return " ".join(self.surface.evict(dest))

    PAUSE_EXIT = 44
    LOCK_EXIT = 43          # another loop owns the workspace
    INFRA_EXIT = 45     # the infra failed under the cell; nothing to charge

    def run(self, stub_overlay=None, agent_cmd=None, verify=None):
        """The attempt loop. Returns the cell's verdict, or None if it stood
        down without reaching one.

        Holds the loop lock for the whole run and its slots from before the
        setup hook until after teardown; closing them is the release.
        """
        self._refuse_if_sealed("running")
        gate = verify or self.gate
        # A fixed solution verified once — the rig-debug path, matching
        # run_cell.sh so a deterministic run is the same length on both sides.
        budget = 1 if stub_overlay else self.budget

        if self.terminal:
            self.seal("green" if self.verdict == "green" else "budget")
            return self.verdict

        loop_lock = self.loop_lock()
        if loop_lock is None:
            raise Halt(f"refusing: another loop owns {self.cid}", self.LOCK_EXIT)
        awake = hold_awake(os.getpid())

        # Default SIGTERM skips `finally`, so an operator stop would leave the
        # arm's infra running. Raising instead lets teardown happen.
        def _stop(signum, _frame):
            raise KeyboardInterrupt(f"signal {signum}")

        prev = {sig: signal.signal(sig, _stop)
                for sig in (signal.SIGTERM, signal.SIGINT)}

        self.prepare()
        self.ckpt.init()
        self._append("IMPL", self.IMPL)
        # How many attempts the LEDGER has spent. Read before any transition:
        # AcquireSlot moves the model's own counter, which is a different thing.
        prior = self.attempts
        self.apply(T.SPAWN)

        slots = None
        stop_ticker = self.start_ticker()
        setup_env, green, attempt = {}, False, prior
        try:
            self.hb(Phase.SETUP, 0)
            # Always: a cell occupies the rig whether or not an agent runs, and
            # AcquireSlot is what tells the model an attempt has begun.
            slots, queue = self.acquire_slots()
            if queue is not None:
                self._append("PAUSED", "attempt=0", queue)
                self.note_pause()
                self.apply(T.STAND_DOWN, queue)
                return None
            if not stub_overlay:
                try:
                    self.stage_agent()
                except RuntimeError as e:
                    self._append("HALT", "attempt=0", "no-creds")
                    self.apply(T.CRASH, "no-creds")
                    raise Halt(f"HALT[agent]: no creds to stage for "
                               f"{self.cid}: {e}", 42) from e
            # a stub replaces the agent, never the infra the gate runs on
            rc, setup_env = self.setup()
            if rc != 0:
                self._append("ALERT", f"SETUP-FAILED rc={rc}",
                             f"{self.variant} cell_setup; investigate")
                self.apply(T.CRASH, "setup-failed")
                raise Halt(f"HALT[infra]: {self.variant} cell_setup "
                           f"failed for {self.cid} (rc={rc})", self.INFRA_EXIT)

            for attempt in range(prior + 1, budget + 1):
                if (self.ws / ".paused").exists():
                    self.note_pause()
                    self._append("PAUSED", f"attempt={attempt}", "operator")
                    self.apply(T.STAND_DOWN, "attempt-boundary")
                    return None
                if not self.infra_ok():
                    self._append("HALT", f"attempt={attempt}", "infra")
                    self.apply(T.CRASH, "infra")
                    raise Halt(f"HALT[infra]: docker unreachable at "
                               f"{self.cid} attempt {attempt}", self.INFRA_EXIT)
                self.restore_charged(attempt)
                self._append("START", f"attempt={attempt}")
                pre = self.checkpoint(f"pre attempt {attempt}")
                self._append("CKPT", f"attempt={attempt}", f"pre tree={pre[:12]}")
                self.hb(Phase.AGENT, attempt)

                outcome = self.agent_with_retries(
                    attempt, stub_overlay=stub_overlay,
                    agent_cmd=agent_cmd, extra_env=setup_env, pre=pre)
                if outcome == self.PAUSED:
                    # The wait is a pause safe point: the attempt was never
                    # judged, so it is abandoned, not crashed.
                    self.note_pause()
                    self._append("PAUSED", f"attempt={attempt}", "limit-wait")
                    self.apply(T.STAND_DOWN, "limit-wait")
                    return None
                if outcome == self.AUTH_WALL:
                    self.apply(T.CRASH, "auth-wall")
                    raise Halt(f"HALT[agent]: auth or config wall at "
                               f"{self.cid} attempt {attempt} — no wait "
                               f"lifts it; fix the credential/model and "
                               f"re-run (cell resumes here)", 42)
                if outcome == self.AGENT_CONTAINER:
                    self.apply(T.CRASH, "agent-container")
                    raise Halt(f"HALT[agent]: docker could not start the agent "
                               f"container at {self.cid} attempt {attempt} "
                               f"(agent.attempt-{attempt}.log) — nothing charged; "
                               f"build the agent images (`cli.py experiment check`) "
                               f"and re-run (cell resumes here)", 42)
                if outcome is not True:
                    self.apply(T.CRASH, "agent-fault")
                    raise Halt(f"HALT[agent]: no agent output after "
                               f"{self.AGENT_FAULT_RETRIES} retries at "
                               f"{self.cid} attempt {attempt} — CLI fault, "
                               f"fix and re-run (cell resumes here)", 42)
                healed = self._restore_fixed()
                if healed:
                    self._append("HEAL", f"attempt={attempt}",
                                 f"reverted: {healed}")
                    (self.ws / "heal.last").write_text(healed + "\n")
                else:
                    (self.ws / "heal.last").unlink(missing_ok=True)
                moved = self._evict_strays(attempt)
                if moved:
                    self._append("HEAL", f"attempt={attempt}",
                                 f"moved out of surface: {moved}"[:400])
                    (self.ws / "evict.last").write_text(moved + "\n")
                else:
                    (self.ws / "evict.last").unlink(missing_ok=True)

                if self.noedit():
                    self._append("NOEDIT", f"attempt={attempt}",
                                 "INVESTIGATE — no file modified")
                    (self.ws / "noedit.last").write_text("1\n")
                    self.record("budget" if attempt >= budget else "fail",
                                f"attempt={attempt} stage=no-edit "
                                f"tree={self.tree()[:12]}")
                    self.state.attempts = attempt
                    continue
                (self.ws / "noedit.last").unlink(missing_ok=True)

                self.hb(Phase.VERIFY_LOCK, attempt)
                vlock = self.verify_lock_acquire()
                if vlock is None:
                    # Operator pause while queued (or right as the lock was
                    # won): the lock must never be held by a cell that
                    # intends to exit.
                    self.note_pause()
                    self._append("PAUSED", f"attempt={attempt}",
                                 "verify-lock queue")
                    self.apply(T.STAND_DOWN, "verify-lock-queue")
                    return None
                self.hb(Phase.VERIFY, attempt)
                self.apply(T.ACQUIRE_VERIFY)
                try:
                    results = gate(attempt) if verify is None else gate()
                    all_green = bool(results) and all(r.green for r in results)
                    post = self.checkpoint(f"post attempt {attempt} "
                                           f"green={str(all_green).lower()}")
                    self._append("CKPT", f"attempt={attempt}",
                                 f"post tree={post[:12]}")

                    stage = results[-1].stage_failed if results else "?"
                    broken = [n for res in results for n in res.contract]
                    last = results[-1] if results else None
                    if last is not None and not last.charge and broken:
                        # The world around the bring-up was not the documented
                        # one: void, and stand down for the operator — no
                        # repair loop, since the same world would come back.
                        self.apply(T.VERIFY_FAIL, f"attempt={attempt} void={stage}")
                        self.contract_stand_down(attempt, broken)
                        return None
                    if last is not None and not last.charge:
                        # Rig fault, not a verdict: the attempt is not consumed.
                        self._append("HALT", f"attempt={attempt}",
                                     f"infra: {stage}")
                        self.apply(T.VERIFY_FAIL, f"attempt={attempt} void={stage}")
                        self.release_slot("void")
                        raise Halt(f"HALT[infra]: {self.cid} attempt "
                                   f"{attempt} voided at stage {stage}", self.INFRA_EXIT)

                    # The verdict transition is the model's verify release
                    # (VerifyGreen/VerifyFail require the verify to be held
                    # and clear it), so it must land in the ledger while the
                    # flock is still held — after the release, the next
                    # holder's AcquireVerify can interleave ahead of it.
                    # Only a CHARGED verdict judges a tree: the void branches
                    # above return first, so they never move the reference
                    # the no-edit guard compares the next attempt against.
                    self.ckpt.judged = post
                    verdict_green = self.judge(attempt, results) == "green"
                    self.apply(T.VERIFY_GREEN if verdict_green else T.VERIFY_FAIL,
                               f"attempt={attempt}")
                finally:
                    vlock.close()
                # Only a verdict that is recorded may tell the next attempt
                # what failed; the void branches above never reach this.
                self.feedback(results, verdict_green)
                if verdict_green:
                    self.record("green", self.iter_note(attempt, results, post))
                    green = True
                    break
                self.record("budget" if attempt >= budget else "fail",
                            self.iter_note(attempt, results, post, stage=stage))
                self.state.attempts = attempt
                if broken and self.state.loop in (Loop.IDLE, Loop.AGENT):
                    # A teardown left something the docs say it removes. The
                    # verdict stands — the world was right while measured —
                    # but the next arrangement would start on that debris.
                    self.contract_stand_down(attempt, broken)
                    return None

            self._append("END", f"green={str(green).lower()}")
            if self.state.loop is not Loop.NONE:
                self.release_slot("cell end")
            self.seal("green" if green else "budget", attempt)
            return "green" if green else "failed"
        except BaseException as e:
            # A cell that dies must SAY so: without this the ledger's last line
            # is the attempt that was still running, and a killed cell is
            # indistinguishable from a working one. Halt already wrote its own
            # line.
            if not isinstance(e, Halt):
                self._append("HALT", f"attempt={attempt}",
                             f"died: {type(e).__name__}: {e}")
            try:
                with (self.ws / "cell.err").open("a") as f:
                    f.write(f"\n=== {_now()} {self.cid} attempt={attempt}\n")
                    traceback.print_exc(file=f)
            except OSError:
                pass
            raise
        finally:
            for sig, handler in prev.items():
                signal.signal(sig, handler)
            stop_ticker()
            self.teardown()
            self.clear_holder_notes()
            (self.ws / ".loop").unlink(missing_ok=True)
            if slots is not None:
                slots.close()
            loop_lock.close()
            if awake is not None:
                awake.terminate()

    def __repr__(self):
        return (f"Cell({self.cid} impl={self.impl} attempts={self.attempts}"
                f"{' SEALED' if self.sealed else ''}"
                f"{' ' + self.verdict if self.verdict else ''})")
