"""The void / guard matrix — PY-READINESS rows 5-9, 19, 20.

A VOID is a rig fault, never a build verdict: the attempt is refunded, the
cell halts 45 for operator review, and NOTHING is recorded as an ITER. These
tests drive the REAL run loop (real checkpoints, real flocks, scratch roots)
into each void stage through a stubbed gate — the classification, the ledger
and the exit contract are the driver's own code, judged exactly as the model
replays them.
"""
import hashlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from _ctx import ROOT

import fae.experiment
from fae import cell
SHAPES = fae.experiment.exp().definition.gate.arrangements

T = cell.T


class VoidMatrixCase(unittest.TestCase):

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
        self._exp_dir = os.environ.get("EXPERIMENT_DIR")
        os.environ["EXPERIMENT_DIR"] = str(exp)      # the config's tree is this scratch root's
        self.addCleanup(lambda: os.environ.__setitem__("EXPERIMENT_DIR", self._exp_dir))
        os.environ["WORKSPACES_DIR"] = str(self.wsdir)
        os.environ["FAE_VARIANT_NOOP"] = "1"     # a scratch root provisions nothing
        self.addCleanup(os.environ.pop, "FAE_VARIANT_NOOP", None)
        self.addCleanup(os.environ.pop, "TRANSITIONS_LOG", None)
        self.addCleanup(os.environ.pop, "WORKSPACES_DIR", None)
        self.overlay = self.root / "overlay"
        (self.overlay / "app").mkdir(parents=True)
        (self.overlay / "app/authored.txt").write_text("the attempt's edit\n")

    def cell(self):
        ws = self.wsdir / self.CID
        (ws / "artifacts" / "app").mkdir(parents=True, exist_ok=True)
        # every prepared cell carries one; an absent manifest is only noise
        (ws / ".skeleton_manifest").touch()
        (ws / "cell.env").write_text("TASK=T1\nVARIANT=beta_apidocs\n"
                                     "REPEAT=1\n")
        c = cell.Cell(self.CID, workspaces=self.wsdir, root=self.root)
        c.prepare = lambda fresh=False: c.ws
        c.infra_ok = lambda: True        # no docker probe in a unit run
        return c

    def transitions(self):
        f = self.root / "transitions.log"
        return [tuple(l.split("\t")[1:4]) for l in f.read_text().splitlines()] \
            if f.exists() else []

    def ledger_events(self, c):
        return [l.split("\t")[1] for l in
                (c.ws / "iterations.log").read_text().splitlines()]


class TestVoidStagesRefundTheAttempt(VoidMatrixCase):
    """Rows 5-9: a verdict the verifier does not charge (a rig fault) —
    whatever its stage — is HALT, a void-tagged VerifyFail, the slot
    released, exit 45, and NO ITER line."""

    CHARGED = "G6|G3\n"      # the feedback the last CHARGED attempt left

    def void(self, stage):
        c = self.cell()
        (c.ws / "shapegate.last").write_text(self.CHARGED)
        gate = lambda: [cell.VerifyResult(green=False, shape="G2",
                                          stage_failed=stage, charge=False)]
        with self.assertRaises(cell.Halt) as cm:
            c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate)
        self.assertEqual(cm.exception.code, 45)
        acts = [t[0] for t in self.transitions()]
        self.assertEqual(acts[-3:], ["AcquireVerify", "VerifyFail", "ReleaseSlot"])
        vf = [t for t in self.transitions() if t[0] == "VerifyFail"][-1]
        self.assertIn(f"void={stage}", vf[2])
        events = self.ledger_events(c)
        self.assertIn("HALT", events)
        self.assertNotIn("ITER", events, f"{stage}: a void burned the attempt")
        self.assertEqual((c.ws / "shapegate.last").read_text(), self.CHARGED,
                         f"{stage}: a void rewrote the charged feedback")
        return c

    def test_a_contract_void_stands_down_and_leaves_the_feedback_alone(self):
        # r12 08:14Z: the bring-up world was not the documented one — the
        # attempt is void and the cell pauses, and the prompt that resumes
        # it must still describe the attempt that was charged.
        c = self.cell()
        (c.ws / "shapegate.last").write_text(self.CHARGED)
        gate = lambda: [cell.VerifyResult(
            green=False, shape="G1", stage_failed="contract", charge=False,
            contract=["the lease ledger is not empty at bring-up"])]
        self.assertIsNone(c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate))
        events = self.ledger_events(c)
        self.assertNotIn("ITER", events)
        self.assertIn("PAUSED", events)
        self.assertEqual((c.ws / "shapegate.last").read_text(), self.CHARGED)

    def test_harness_fp(self):
        self.void("harness-fp")

    def test_a_failed_heal_voids_before_any_verify(self):
        # The gate asks the surface first; a fixed file still differing after
        # heal is a harness anomaly, refunded, and no arrangement runs.
        c = self.cell()
        (c.ws / "shapegate.last").write_text(self.CHARGED)
        with mock.patch.object(cell.Surface, "check", return_value=["Dockerfile (modified)"]), \
                mock.patch.object(cell.Cell, "verify", side_effect=AssertionError("verify ran")):
            with self.assertRaises(cell.Halt) as cm:
                c.run(ignore_slots=True, stub_overlay=self.overlay)
        self.assertEqual(cm.exception.code, 45)
        vf = [t for t in self.transitions() if t[0] == "VerifyFail"][-1]
        self.assertIn("void=harness-heal", vf[2])
        events = self.ledger_events(c)
        self.assertNotIn("ITER", events)
        self.assertIn("ALERT", events)
        self.assertIn("HEAL-FAILED", (c.ws / "iterations.log").read_text())

    def test_harness_heal(self):
        self.void("harness-heal")

    def test_libs_tamper(self):
        self.void("libs-tamper")

    def test_exclusive_lock(self):
        self.void("exclusive-lock")

    def test_store(self):
        self.void("store")

    def test_nostart(self):
        # row 4's driver half — the stage the cohort actually hit live
        self.void("nostart")

    def test_a_plain_failure_is_not_a_void(self):
        # The inverse: a scaling failure IS a verdict — ITER recorded, no HALT.
        c = self.cell()
        gate = lambda: [cell.VerifyResult(green=False, shape="G2",
                                          stage_failed="scaling")]
        self.assertEqual(c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate),
                         "failed")
        events = self.ledger_events(c)
        self.assertIn("ITER", events)
        self.assertNotIn("HALT", events)
        # ...and the charge is what leaves the feedback for the next prompt.
        self.assertEqual((c.ws / "shapegate.last").read_text(), "G2|\n")


class TestRow19TheNoEditGuard(VoidMatrixCase):

    def test_an_identical_rebuild_skips_the_verify_and_burns_the_attempt(self):
        c = self.cell()
        # judge the CURRENT tree once, so the stub's rebuild is byte-identical
        (c.ws / "artifacts" / "app/authored.txt").write_text("the attempt's edit\n")
        c.ckpt.init()
        c.ckpt.judged = c.ckpt.commit("seeded as judged")
        never = lambda: self.fail("verify ran on an unedited build")
        self.assertEqual(c.run(ignore_slots=True, stub_overlay=self.overlay, verify=never),
                         "failed")
        events = self.ledger_events(c)
        self.assertIn("NOEDIT", events)
        self.assertTrue((c.ws / "noedit.last").exists())
        iters = [l for l in (c.ws / "iterations.log").read_text().splitlines()
                 if "\tITER\t" in l]
        self.assertIn("stage=no-edit", iters[0])


class TestRow20HealRevertsOutOfSurfaceEdits(VoidMatrixCase):

    def test_a_fixed_file_edit_is_reverted_and_recorded(self):
        c = self.cell()
        art = c.ws / "artifacts"
        skel = self.root / "experiment" / "task" / "skeleton"
        skel.mkdir(parents=True)
        (skel / "config.py").write_text("FIXED = True\n")
        sha = hashlib.sha256(b"FIXED = True\n").hexdigest()
        (c.ws / ".skeleton_manifest").write_text(f"config.py\t1\t{sha}\n")
        (art / "config.py").write_text("FIXED = False  # agent overreach\n")
        gate = lambda: [cell.VerifyResult(green=True, shape=s)
                        for s in SHAPES]
        with mock.patch.object(c.variant_cls, "TEMPLATE", (skel,)):
            self.assertEqual(c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate), "green")
        self.assertEqual((art / "config.py").read_text(), "FIXED = True\n")
        events = self.ledger_events(c)
        self.assertIn("HEAL", events)
        heal = [l for l in (c.ws / "iterations.log").read_text().splitlines()
                if "\tHEAL\t" in l][0]
        self.assertIn("config.py", heal)

    def test_a_stray_is_moved_out_recorded_and_the_next_prompt_says_so(self):
        c = self.cell()
        (self.overlay / "conftest.py").write_text("import sys\n")
        gate = lambda: [cell.VerifyResult(green=True, shape=s) for s in SHAPES]
        self.assertEqual(c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate), "green")
        self.assertFalse((c.artifacts / "conftest.py").exists())
        self.assertEqual((c.ws / ".out-of-surface" / "attempt-1" / "conftest.py").read_text(),
                         "import sys\n")
        heal = [l for l in (c.ws / "iterations.log").read_text().splitlines()
                if "\tHEAL\t" in l]
        self.assertEqual(len(heal), 1)
        self.assertIn("moved out of surface: conftest.py", heal[0])
        self.assertEqual((c.ws / "evict.last").read_text(), "conftest.py\n")

    def test_a_heal_note_does_not_outlive_the_attempt_it_describes(self):
        c = self.cell()
        (c.ws / "heal.last").write_text("Dockerfile\n")
        (c.ws / "evict.last").write_text("conftest.py\n")
        gate = lambda: [cell.VerifyResult(green=True, shape=s) for s in SHAPES]
        c.run(ignore_slots=True, stub_overlay=self.overlay, verify=gate)
        self.assertFalse((c.ws / "heal.last").exists())
        self.assertFalse((c.ws / "evict.last").exists())



class TestAVoidedRunIsZeroed(VoidMatrixCase):
    """A void refunds the attempt AND the tree: the retry starts from the
    tree the last CHARGED verdict was judged on, so it is the same experiment
    as the run that was voided, not a continuation of it.

    r12 attempt 6 (2026-08-29) STARTed three times — contract void at 08:09Z,
    nostart void at 08:47Z, charged at 08:59Z — and its `CKPT pre tree=` moved
    2db679 -> 5f6996 -> 65c120: each voided run's edits stayed, and the
    attempt that counted was the third agent to work on them.

    These drive the real loop with a scripted agent (the stub overlay pins the
    budget to one attempt, which cannot show a retry) and a scripted gate.
    """

    def scripted(self, files, gate, pause_after=False):
        """A cell whose agent writes `files` and whose gate is `gate`. With
        `pause_after`, the gate also asks for a pause, so the run stops at the
        next attempt boundary the way an operator's pause does — the charge
        is recorded, and the next run is a fresh spawn."""
        c = self.cell()
        c.stage_agent = lambda: None

        def agent(attempt, stub_overlay=None, agent_cmd=None, extra_env=None):
            for rel, text in files.items():
                (c.artifacts / rel).parent.mkdir(parents=True, exist_ok=True)
                (c.artifacts / rel).write_text(text)
            (c.ws / f"agent.attempt-{attempt}.log").write_text("edited\n")
            return 0

        def gated():
            if pause_after:
                (c.ws / ".paused").write_text("test\n")
            return gate()

        c._agent = agent
        return c, gated

    def ledger(self, c):
        return (c.ws / "iterations.log").read_text().splitlines()

    def pre_trees(self, c):
        return [l.split("pre tree=")[1] for l in self.ledger(c)
                if "\tCKPT\t" in l and "pre tree=" in l]

    def restores(self, c):
        return [l.split("\t")[-1] for l in self.ledger(c) if "\tRESTORE\t" in l]

    def chain(self, c):
        env = dict(os.environ, GIT_DIR=str(c.ws / ".attempts.git"),
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        out = subprocess.run(["git", "log", "--format=%T %s", "attempts"],
                             env=env, capture_output=True, text=True, check=True)
        return out.stdout.splitlines()

    FAIL = staticmethod(lambda: [cell.VerifyResult(green=False, shape="G2",
                                                   stage_failed="scaling")])
    VOID = staticmethod(lambda: [cell.VerifyResult(green=False, shape="G1",
                                                   stage_failed="nostart", charge=False)])

    def charge_attempt_1(self):
        """Attempt 1 charged (scaling fail), run stood down at the boundary."""
        c, gate = self.scripted({"app/authored.txt": "A\n"}, self.FAIL, pause_after=True)
        self.assertIsNone(c.run(ignore_slots=True, verify=gate))
        (c.ws / ".paused").unlink()
        iters = [l for l in self.ledger(c) if "\tITER\t" in l]
        self.assertEqual(len(iters), 1)
        return c.tree()

    def test_the_retry_after_a_void_starts_from_the_charged_tree(self):
        charged = self.charge_attempt_1()
        # attempt 2, voided: its agent rewrote one file and added another
        c, gate = self.scripted({"app/authored.txt": "B\n", "app/voided.txt": "x\n"},
                                self.VOID)
        with self.assertRaises(cell.Halt):
            c.run(ignore_slots=True, verify=gate)
        voided = c.tree()
        self.assertNotEqual(voided, charged)
        self.assertTrue((c.artifacts / "app/voided.txt").exists())
        # attempt 2 again, charged: it starts where attempt 1 was charged
        c, gate = self.scripted({"app/authored.txt": "C\n"}, self.FAIL, pause_after=True)
        self.assertIsNone(c.run(ignore_slots=True, verify=gate))
        self.assertEqual(self.restores(c),
                         [f"tree={charged[:12]} (was {voided[:12]})"])
        self.assertEqual(self.pre_trees(c)[-1], charged[:12],
                         "the retried attempt did not start from the charge")
        self.assertFalse((c.artifacts / "app/voided.txt").exists(),
                         "the voided run's new file survived into the retry")
        iters = [l for l in self.ledger(c) if "\tITER\t" in l]
        self.assertEqual(len(iters), 2)
        self.assertIn("attempt=2", iters[-1])

    def test_the_abandoned_tree_stays_in_the_chain_as_provenance(self):
        charged = self.charge_attempt_1()
        c, gate = self.scripted({"app/authored.txt": "B\n"}, self.VOID)
        with self.assertRaises(cell.Halt):
            c.run(ignore_slots=True, verify=gate)
        voided = c.tree()
        c, gate = self.scripted({"app/authored.txt": "C\n"}, self.FAIL, pause_after=True)
        c.run(ignore_slots=True, verify=gate)
        chain = self.chain(c)
        self.assertIn(f"{voided} abandoned before attempt 2 (never charged)", chain)
        self.assertIn(f"{charged} pre attempt 2", chain)

    def test_a_void_does_not_move_the_judged_tree(self):
        # The no-edit guard compares against the CHARGED tree. Had the void
        # moved it, a retry that rebuilt exactly the voided tree would be
        # burned as "no edit" — for an edit no verdict ever saw.
        charged = self.charge_attempt_1()
        c, gate = self.scripted({"app/authored.txt": "B\n"}, self.VOID)
        with self.assertRaises(cell.Halt):
            c.run(ignore_slots=True, verify=gate)
        self.assertEqual(c.ckpt.judged, charged)
        calls = []
        c, gate = self.scripted({"app/authored.txt": "B\n"},
                                lambda: calls.append(1) or self.FAIL(),
                                pause_after=True)
        c.run(ignore_slots=True, verify=gate)
        self.assertEqual(len(calls), 1, "the rebuilt voided tree was not verified")
        iters = [l for l in self.ledger(c) if "\tITER\t" in l]
        self.assertNotIn("no-edit", iters[-1])

    def test_a_void_on_the_first_attempt_retries_from_the_prepared_tree(self):
        c, gate = self.scripted({"app/authored.txt": "B\n"}, self.VOID)
        with self.assertRaises(cell.Halt):
            c.run(ignore_slots=True, verify=gate)
        seed = self.pre_trees(c)[0]
        self.assertNotEqual(c.tree()[:12], seed)
        c, gate = self.scripted({"app/authored.txt": "C\n"}, self.FAIL, pause_after=True)
        c.run(ignore_slots=True, verify=gate)
        self.assertEqual(self.pre_trees(c), [seed, seed])
        self.assertEqual(len(self.restores(c)), 1)
        self.assertTrue(self.restores(c)[0].startswith(f"tree={seed}"))

    def test_a_stop_mid_verify_is_zeroed_the_same_way(self):
        # An operator stop is a SIGTERM the loop turns into KeyboardInterrupt;
        # a kill runs no code at all. Neither can undo the edits at the void,
        # which is why the restore lives at the START.
        charged = self.charge_attempt_1()

        def stopped():
            raise KeyboardInterrupt("signal 15")
        c, gate = self.scripted({"app/authored.txt": "B\n"}, stopped)
        with self.assertRaises(KeyboardInterrupt):
            c.run(ignore_slots=True, verify=gate)
        self.assertIn("died: KeyboardInterrupt", self.ledger(c)[-1])
        c, gate = self.scripted({"app/authored.txt": "C\n"}, self.FAIL, pause_after=True)
        c.run(ignore_slots=True, verify=gate)
        self.assertEqual(len(self.restores(c)), 1)
        self.assertEqual(self.pre_trees(c)[-1], charged[:12])

    def test_a_pause_after_the_agent_ran_is_zeroed_too(self):
        # r12 attempt 4: PAUSED in the verify-lock queue at 21:30Z, resumed
        # at 06:34Z on pre tree eb9f61 != the charged c3057c. The attempt was
        # refunded (StandDown), so it is redone — from the charge.
        charged = self.charge_attempt_1()
        c = self.cell()
        c.stage_agent = lambda: None

        def agent(attempt, **kw):
            (c.artifacts / "app/authored.txt").write_text("B\n")
            (c.ws / f"agent.attempt-{attempt}.log").write_text("edited\n")
            (c.ws / ".paused").write_text("test\n")     # lands before the vlock
            return 0
        c._agent = agent
        self.assertIsNone(c.run(ignore_slots=True, verify=lambda: self.fail("verified while paused")))
        self.assertIn("verify-lock queue", self.ledger(c)[-1])
        (c.ws / ".paused").unlink()
        c, gate = self.scripted({"app/authored.txt": "C\n"}, self.FAIL, pause_after=True)
        c.run(ignore_slots=True, verify=gate)
        self.assertEqual(len(self.restores(c)), 1)
        self.assertEqual(self.pre_trees(c)[-1], charged[:12])

    def test_a_charged_boundary_restores_nothing(self):
        # The inverse: after a charge the tree IS the charged tree, in the
        # same run and across a resume, and the ledger says nothing.
        c, gate = self.scripted({"app/authored.txt": "A\n"}, self.FAIL)
        calls = []
        c.checkpoint = (lambda orig: lambda label: calls.append(label) or orig(label))(c.checkpoint)
        # a scripted agent that always writes A: attempt 1 charged, attempt 2
        # is a no-edit charge, ... until the budget — every boundary charged
        self.assertEqual(c.run(ignore_slots=True, verify=gate), "failed")
        self.assertEqual(self.restores(c), [])
        self.assertFalse(any("abandoned" in l for l in calls))

    def test_a_first_start_has_nothing_to_restore(self):
        c, gate = self.scripted({"app/authored.txt": "A\n"}, self.FAIL, pause_after=True)
        self.assertIsNone(c.charged_tree())
        c.run(ignore_slots=True, verify=gate)
        self.assertEqual(self.restores(c), [])
