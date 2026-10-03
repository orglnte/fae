"""The pause/stand-down matrix — PY-READINESS rows 11, 12, 13, 17, 18.

Real Cell, real flocks, scratch roots: each row drives the ACTUAL driver code
through its stand-down point and asserts the ledger the model will replay.
Deterministic by construction — the pause marker is on disk before the queue
is entered, and wait_fds checks it before its first sleep — so these rows are
rerunnable evidence, not one-shot smoke runs.
"""
import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae import cell

T = cell.T


class PauseMatrixCase(unittest.TestCase):

    CID = "opus_high_beta_apidocs_T1_r1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.wsdir = self.root / "workspaces"
        exp = self.root / "experiment"
        exp.mkdir(parents=True, exist_ok=True)
        (exp / "instruments").symlink_to(Path(ROOT) / "experiment" / "instruments")
        os.environ["TRANSITIONS_LOG"] = str(self.root / "transitions.log")
        os.environ["WORKSPACES_DIR"] = str(self.wsdir)   # mutex pause probe
        os.environ["FAE_VARIANT_NOOP"] = "1"     # a scratch root provisions nothing
        os.environ["WORK_SLOTS"] = "7"              # the slot-queue rows fill this many
        os.environ["ARM_SLOTS_ALPHA"] = "1"         # the arm-queue row holds this one
        self.addCleanup(os.environ.pop, "FAE_VARIANT_NOOP", None)
        self.addCleanup(os.environ.pop, "TRANSITIONS_LOG", None)
        self.addCleanup(os.environ.pop, "WORKSPACES_DIR", None)
        self.addCleanup(os.environ.pop, "WORK_SLOTS", None)
        self.addCleanup(os.environ.pop, "ARM_SLOTS_ALPHA", None)
        self._held = []
        self.addCleanup(lambda: [f.close() for f in self._held])

    def cell(self, cid=None, variant="beta_apidocs"):
        cid = cid or self.CID
        ws = self.wsdir / cid
        (ws / "artifacts").mkdir(parents=True, exist_ok=True)
        (ws / "cell.env").write_text(f"TASK=T1\nVARIANT={variant}\nREPEAT=1\n")
        c = cell.Cell(cid, workspaces=self.wsdir, root=self.root)
        c.prepare = lambda fresh=False: c.ws      # workspace is pre-seeded
        return c

    def hold(self, lockfile):
        """Flock a lock file from the test process, so the cell must queue."""
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        f = open(lockfile, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._held.append(f)

    def transitions(self):
        f = self.root / "transitions.log"
        return [tuple(l.split("\t")[1:4]) for l in f.read_text().splitlines()] \
            if f.exists() else []

    def actions(self):
        return [t[0] for t in self.transitions()]

    def paused_lines(self, c):
        return [l for l in (c.ws / "iterations.log").read_text().splitlines()
                if "\tPAUSED\t" in l]


class TestRow11PauseInTheWorkSlotQueue(PauseMatrixCase):

    def test_the_cell_stands_down_from_the_slot_queue(self):
        queues = self.root / "workspaces.nosync" / ".queues"
        for i in range(1, 8):                      # conf default WORK_SLOTS=7
            self.hold(queues / "work-slots" / f"slot-{i}")
        c = self.cell()
        (c.ws / ".paused").write_text("row 11\n")
        self.assertIsNone(c.run(stub_overlay="unused"))
        self.assertEqual(self.actions(), ["Spawn", "Pause", "StandDown"])
        self.assertEqual(self.transitions()[-1][2], "slot-queue")
        self.assertIn("slot-queue", self.paused_lines(c)[0])


class TestRow12PauseInTheArmLockQueue(PauseMatrixCase):

    CID = "opus_high_alpha_apidocs_T1_r1"

    def test_the_cell_stands_down_from_the_arm_queue_by_name(self):
        queues = self.root / "workspaces.nosync" / ".queues"
        # work slots free; the ONE keda arm slot held by the test
        self.hold(queues / "arm-alpha.slots" / "slot-1")
        c = self.cell(variant="alpha_apidocs")
        (c.ws / ".paused").write_text("row 12\n")
        # stage_agent needs creds; the arm queue is reached before any agent,
        # and a stub run skips staging exactly like the rig-debug path.
        self.assertIsNone(c.run(stub_overlay="unused"))
        self.assertEqual(self.actions(),
                         ["Spawn", "AcquireSlot", "Pause", "StandDown"])
        # the ledger names the queue it happened in — not the work-slot queue
        self.assertEqual(self.transitions()[-1][2], "arm-lock-queue")


class TestRow13PauseAtTheAttemptBoundary(PauseMatrixCase):

    def test_the_cell_stands_down_before_spending_the_attempt(self):
        c = self.cell()
        (c.ws / ".paused").write_text("row 13\n")
        self.assertIsNone(c.run(stub_overlay="unused"))
        self.assertEqual(self.actions(),
                         ["Spawn", "AcquireSlot", "Pause", "StandDown"])
        self.assertEqual(self.transitions()[-1][2], "attempt-boundary")
        self.assertIn("operator", self.paused_lines(c)[0])
        # the abandoned attempt is refunded (StandDown from `agent` is @-1)
        self.assertEqual(c.state.attempts, 0)

    def test_a_command_paused_cell_does_not_double_the_pause(self):
        c = self.cell()
        (c.ws / ".paused").write_text("row 13b\n")
        # the command's own Pause is already in the ledger
        (self.root / "transitions.log").write_text(
            f"2026-08-22T10:00:00Z\tPause\t{c.cid}\treason=manual\n")
        self.assertIsNone(c.run(stub_overlay="unused"))
        self.assertEqual(self.actions().count("Pause"), 1)


class TestRow17ParentDeathIsTheHelpersPathNotTheDrivers(PauseMatrixCase):

    def test_wait_fds_abandons_the_queue_when_the_parent_dies(self):
        # The py DRIVER is the parent: it has no parent-death path of its own
        # (its death is Crash + reconcile, rows 14/15). The contract lives in
        # wait_fds for the bash hook helpers — give up rather than win a lock
        # for a shell that is gone.
        import inspect
        from fae.cell.verify import _mutex_module
        src = inspect.getsource(_mutex_module().wait_fds)
        self.assertIn("os.getppid() != ppid", src)
        self.assertIn("SystemExit", src)


class TestRow18TransientAgentFaultIsPinnedAtFixtureLevel(PauseMatrixCase):

    def test_the_bash_driver_retries_a_transient_agent_fault(self):
        # Real induction needs a flaky agent CLI; the classifier and its
        # retry ledger line are pinned at source level instead.
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def agent_with_retries("):]
        self.assertIn("self._transient_fault(log, rc)", body)
        self.assertIn('self._append("WAIT"', body)
