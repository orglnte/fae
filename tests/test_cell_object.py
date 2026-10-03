"""The Cell object: its state machine, its seal, and what it refuses.

The point of embedding the transitions is that an illegal one raises HERE, at
the instant it is attempted, instead of surfacing hours later as a replay
violation nobody can reconstruct. So most of these tests are about what the
object REFUSES — a permissive FSM is indistinguishable from no FSM.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae import cell            # the package, not a file loaded by path
from fae.cell import experiment as _experiment  # noqa: E402
SHAPES = _experiment.current().gate.arrangements

T, Loop, Phase = cell.T, cell.Loop, cell.Phase


class CellTestCase(unittest.TestCase):

    CID = "sonnet_high_beta_apidocs_T1_r1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.wsdir = self.root / "workspaces"
        (self.wsdir / self.CID / "artifacts").mkdir(parents=True)
        # ckpt.py is invoked by path under the cell's own tree, so the tree
        # needs one — the same isolation-by-location rule the bash side has.
        exp = self.root / "experiment"
        exp.mkdir(parents=True, exist_ok=True)
        (exp / "instruments").symlink_to(Path(ROOT) / "experiment" / "instruments")
        os.environ["TRANSITIONS_LOG"] = str(self.root / "transitions.log")
        self.addCleanup(os.environ.pop, "TRANSITIONS_LOG", None)

    def cell(self, ledger_lines="", env=None, sealed=None):
        ws = self.wsdir / self.CID
        (ws / "iterations.log").write_text(ledger_lines)
        (ws / "cell.env").write_text(env or
                                     "TASK=T1\nVARIANT=beta_apidocs\n"
                                     "REPEAT=1\n"
                                     "ATTEMPT_BUDGET=10\n")
        if sealed:
            (ws / ".sealed").write_text(sealed)
        return cell.Cell(self.CID, workspaces=self.wsdir, root=self.root)

    def transitions(self):
        f = self.root / "transitions.log"
        return [l.split("\t")[1] for l in f.read_text().splitlines()] \
            if f.exists() else []


class TestTheTwoVocabulariesCannotDisagree(CellTestCase):

    def test_every_phase_maps_to_a_loop_state(self):
        # A phase with no loop lets the operator console and the model
        # describe the same cell differently.
        self.assertEqual(set(Phase), set(cell.PHASE_TO_LOOP))

    def test_a_cell_queued_for_its_arm_is_still_TLA_agent(self):
        # It already holds the work slot; calling it `idle` would let a second
        # cell believe the slot is free.
        self.assertIs(cell.PHASE_TO_LOOP[Phase.ARM_LOCK], Loop.AGENT)

    def test_a_cell_queued_for_a_work_slot_holds_nothing(self):
        self.assertIs(cell.PHASE_TO_LOOP[Phase.SLOT_WAIT], Loop.IDLE)

    def test_only_the_verify_phase_is_TLA_verify(self):
        # verify-lock is QUEUED for the verify, not verifying: mapping it to
        # `verify` would put two cells in the verify state at once.
        verify_phases = [p for p, l in cell.PHASE_TO_LOOP.items()
                         if l is Loop.VERIFY]
        self.assertEqual(verify_phases, [Phase.VERIFY])


class TestTheTransitionTableRefuses(CellTestCase):

    def test_a_second_spawn_on_a_live_loop_is_illegal(self):
        # THE 2026-08-18 failure: a cell readmitted while the model still had
        # its previous loop live. 12 of these produced 71 replay violations.
        c = self.cell()
        c.apply(T.SPAWN)
        with self.assertRaises(cell.IllegalTransition):
            c.apply(T.SPAWN)

    def test_a_slot_cannot_be_acquired_twice(self):
        c = self.cell()
        c.apply(T.SPAWN)
        c.apply(T.ACQUIRE_SLOT)
        with self.assertRaises(cell.IllegalTransition):
            c.apply(T.ACQUIRE_SLOT)

    def test_verifying_without_an_attempt_is_illegal(self):
        c = self.cell()
        c.apply(T.SPAWN)
        with self.assertRaises(cell.IllegalTransition):
            c.apply(T.ACQUIRE_VERIFY)      # still `idle`, no slot

    def test_the_rig_is_only_taken_under_a_verify(self):
        c = self.cell()
        c.apply(T.SPAWN)
        c.apply(T.ACQUIRE_SLOT)
        with self.assertRaises(cell.IllegalTransition):
            c.apply(T.ACQUIRE_RIG)

    def test_resume_is_refused_for_a_cell_that_was_not_paused(self):
        # A Resume that did not happen desyncs the replay and cascades — a log
        # of transitions that did not occur is worse than no log.
        c = self.cell()
        with self.assertRaises(cell.IllegalTransition):
            c.apply(T.RESUME)

    def test_a_legal_sequence_is_allowed(self):
        # The inverse: a table that refuses everything would pass every test
        # above and be useless.
        c = self.cell()
        for t in (T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, T.ACQUIRE_RIG,
                  T.RELEASE_RIG, T.VERIFY_GREEN):
            c.apply(t)
        self.assertIs(c.state.loop, Loop.NONE)
        self.assertEqual(c.state.outcome, "green")


class TestApplyingAndRecordingAreOneAct(CellTestCase):

    def test_a_legal_transition_is_recorded(self):
        c = self.cell()
        c.apply(T.SPAWN)
        self.assertEqual(self.transitions(), ["Spawn"])

    def test_an_illegal_transition_records_NOTHING(self):
        # If the refusal still logged, the log would describe a fleet that
        # never existed — which is precisely the failure mode being closed.
        c = self.cell()
        c.apply(T.SPAWN)
        with self.assertRaises(cell.IllegalTransition):
            c.apply(T.SPAWN)
        self.assertEqual(self.transitions(), ["Spawn"])


class TestAttemptArithmeticMatchesTheSpec(CellTestCase):

    def test_an_attempt_begins_when_the_slot_is_taken(self):
        c = self.cell()
        c.apply(T.SPAWN); c.apply(T.ACQUIRE_SLOT)
        self.assertEqual(c.state.attempts, 1)

    def test_an_attempt_abandoned_before_judgement_is_not_spent(self):
        # The respawn redoes it. Counting it would charge the cell for a crash.
        c = self.cell()
        c.apply(T.SPAWN); c.apply(T.ACQUIRE_SLOT)
        c.apply(T.CRASH)
        self.assertEqual(c.state.attempts, 0)

    def test_a_stand_down_before_the_slot_costs_nothing(self):
        c = self.cell()
        c.apply(T.SPAWN)
        c.apply(T.PAUSE)
        c.apply(T.STAND_DOWN)
        self.assertEqual(c.state.attempts, 0)


class TestSealedCellsAreReadOnly(CellTestCase):

    SEAL = "sealed=2026-08-18T00:00:00Z\tverdict=green\tattempts=4\tby=x\n"

    def test_state_is_still_readable(self):
        # The whole point: Cell must work on the 470 cells bash produced.
        # `attempts` is how many attempts were SPENT — one ITER line each —
        # so the fixture is a real three-attempt cell, not one ITER labelled 3.
        c = self.cell("2026-08-01T00:00:00Z\tSTART\tc\tattempt=1\n"
                      "2026-08-01T00:01:00Z\tITER\tfail\tattempt=1\n"
                      "2026-08-01T00:02:00Z\tSTART\tc\tattempt=2\n"
                      "2026-08-01T00:03:00Z\tITER\tfail\tattempt=2\n"
                      "2026-08-01T00:04:00Z\tSTART\tc\tattempt=3\n"
                      "2026-08-01T00:10:00Z\tITER\tgreen\tattempt=3\n"
                      "2026-08-01T00:10:01Z\tEND\tc\tgreen=true\n",
                      sealed=self.SEAL)
        self.assertTrue(c.sealed)
        self.assertEqual(c.verdict, "green")
        self.assertEqual(c.attempts, 3)
        self.assertIn("verdict=green", c.seal_record())

    def test_running_is_refused(self):
        c = self.cell(sealed=self.SEAL)
        with self.assertRaises(cell.Sealed):
            c.run()

    def test_checkpointing_is_refused(self):
        c = self.cell(sealed=self.SEAL)
        with self.assertRaises(cell.Sealed):
            c.checkpoint("pre attempt 1")

    def test_judging_is_refused(self):
        c = self.cell(sealed=self.SEAL)
        with self.assertRaises(cell.Sealed):
            c.judge(1, [])

    def test_recording_an_attempt_is_refused(self):
        c = self.cell(sealed=self.SEAL)
        with self.assertRaises(cell.Sealed):
            c.record("green", "attempt=1")

    def test_a_verify_that_would_overwrite_the_result_is_refused(self):
        c = self.cell(sealed=self.SEAL)
        with self.assertRaises(cell.Sealed):
            c.verify(shape="G1")

    def test_validation_is_ALLOWED(self):
        # Derived, and explicitly re-runnable: forbidding it would make an old
        # cell un-rescorable under improved rules, which is most of what these
        # cells are kept for.
        c = self.cell(sealed=self.SEAL)
        (c.ws / "validation.json").write_text(json.dumps({"verdict": "VALID"}))
        self.assertTrue(c.is_valid())

    def test_an_unsealed_cell_refuses_nothing(self):
        # The inverse: the guard must be the seal, not the method.
        c = self.cell()
        self.assertFalse(c.sealed)
        c.checkpoint("pre attempt 1")          # does not raise
        self.assertEqual(c.judge(1, []), "fail")

    def test_sealing_is_idempotent_and_keeps_the_first_verdict(self):
        c = self.cell()
        self.assertTrue(c.seal("green", 2))
        self.assertFalse(c.seal("budget", 10))
        self.assertIn("verdict=green", c.seal_record())


class TestJudgement(CellTestCase):

    def result(self, green):
        return cell.VerifyResult(green=green)

    def test_one_failing_arrangement_fails_the_attempt(self):
        c = self.cell()
        self.assertEqual(c.judge(1, [self.result(True), self.result(False)]),
                         "fail")

    def test_a_short_gate_is_a_failure_not_a_pass(self):
        # The gate stops at the first failing arrangement, so "fewer results
        # than required" is exactly the shape of a cell that failed one.
        c = self.cell()
        self.assertEqual(len(c.gate_shapes), 6)
        self.assertEqual(c.judge(1, [self.result(True)] * 5), "fail")

    def test_all_six_arrangements_green_is_green(self):
        c = self.cell()
        self.assertEqual(c.judge(1, [self.result(True)] * 6), "green")

    def test_a_smoke_gate_is_the_seed_arrangement(self):
        c = self.cell()
        c.conf.values["SHAPE_GATE"] = "one"
        self.assertEqual(c.gate_shapes, (None,))
        self.assertEqual(c.judge(1, [self.result(True)]), "green")


class TestTheBudgetIsNotACellProperty(CellTestCase):

    def test_it_is_the_constant_whatever_cell_env_says(self):
        c = self.cell(env="TASK=T1\nTREATMENT=beta\nATTEMPT_BUDGET=3\n")
        self.assertEqual(c.budget, 10)
        self.assertEqual(cell.ATTEMPT_BUDGET, 10)


class TestImplAttribution(CellTestCase):

    def test_absent_means_bash(self):
        # The 470 cells sealed on 2026-08-18 predate the field and predate any
        # other implementation, so nothing was rewritten to add it.
        c = self.cell()
        self.assertEqual(c.impl, "bash")

    def test_it_is_read_from_cell_env_when_present(self):
        c = self.cell(env="TASK=T1\nTREATMENT=beta\nIMPL=py\n")
        self.assertEqual(c.impl, "py")


if __name__ == "__main__":
    unittest.main()


class TestTheGateIsSixArrangementsByDefault(CellTestCase):
    """A green minted on one arrangement is an UNGATED green: it proves
    mount-and-release and says nothing about re-acquisition. The setting lives
    in the loaded config, so reading only the environment silently shrinks the gate."""

    def test_the_default_gate_is_all_six(self):
        c = self.cell()
        self.assertEqual(len(c.gate_shapes), 6)
        self.assertEqual(set(c.gate_shapes), set(SHAPES))

    def test_it_comes_from_config_not_the_environment(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        block = src[src.index("    def gate_shapes"):]
        block = block[:block.index("\n    def ", 10)]
        code = block[block.index('"""', block.index('"""') + 3) + 3:]
        self.assertIn("self.conf.values.get(", code)
        self.assertNotIn("os.environ", code)

    def test_a_short_gate_cannot_pass(self):
        c = self.cell()
        ok = [cell.VerifyResult(green=True) for _ in range(5)]
        self.assertEqual(c.judge(1, ok), "fail")
        self.assertEqual(c.judge(1, ok + [cell.VerifyResult(green=True)]), "green")

    def test_config_can_still_turn_it_off_deliberately(self):
        c = self.cell()
        c.conf.values["SHAPE_GATE"] = "one"
        self.assertEqual(c.gate_shapes, (None,))

    def test_smoke_env_reaches_the_gate(self):
        """`cli.py experiment smoke` (no --full-gate) sets SHAPE_GATE=one in the child's
        environment expecting the single-arrangement pipeline check — this is
        the actual mechanism that must carry it into conf.values, not a
        direct .values mutation like the tests above."""
        from fae.cell import config as cfgmod
        cfgmod._cache.clear()
        v = cfgmod.load(str(self.root), env=dict(os.environ, SHAPE_GATE="one", SMOKE="1"))
        self.assertEqual(v.values.get("SHAPE_GATE"), "one")
        cfgmod._cache.clear()
        v = cfgmod.load(str(self.root), env=dict(os.environ, SHAPE_GATE=""))
        self.assertEqual(v.values.get("SHAPE_GATE"), "")

    def test_a_cut_gate_outside_a_smoke_cell_is_refused(self):
        from fae.cell import config as cfgmod
        cfgmod._cache.clear()
        env = {k: v for k, v in os.environ.items() if k != "SMOKE"}
        with self.assertRaisesRegex(SystemExit, "only a smoke cell"):
            cfgmod.load(str(self.root), env=dict(env, SHAPE_GATE="one"))
        cfgmod._cache.clear()


class TestTheGateReportsItsProgress(CellTestCase):
    """A gate in flight is ~15 minutes of infra. Without a SHAPE event per
    arrangement the fleet's GATE column reads 0/6 for the whole of it, and a
    failed arrangement is never named in the next attempt's feedback."""

    def gate_with(self, greens):
        c = self.cell()
        seq = iter(greens)
        c.verify = lambda shape=None, out_dir=None: cell.VerifyResult(
            green=next(seq), shape=shape)
        return c, c.gate(attempt=3)

    def shape_lines(self, c):
        return [l for l in (c.ws / "iterations.log").read_text().splitlines()
                if "\tSHAPE\t" in l]

    def test_each_passing_arrangement_is_recorded(self):
        c, results = self.gate_with([True] * 6)
        self.assertEqual(len(results), 6)
        lines = self.shape_lines(c)
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(l.rstrip().endswith("pass") for l in lines))
        self.assertTrue(all("attempt=3" in l for l in lines))

    def test_the_parser_counts_them_as_gate_progress(self):
        # live_shape_pass is keyed to the IN-FLIGHT attempt — len(iters) + 1 —
        # so the gate must report under the number of the attempt running it.
        c = self.cell()
        seq = iter([True] * 6)
        c.verify = lambda shape=None, out_dir=None: cell.VerifyResult(
            green=next(seq), shape=shape)
        c.gate(attempt=1)
        from fae import ledger
        self.assertEqual(ledger.parse(c.ws)["live_shape_pass"], 6)

    def test_a_failure_names_the_arrangement_and_stops(self):
        # attempt=3 seeds G4, so the rotated order is G4 G1 G2 G3 G5 G6
        # and the third arrangement is G2.
        c, results = self.gate_with([True, True, False])
        self.assertEqual(len(results), 3)
        last = self.shape_lines(c)[-1]
        self.assertIn("FAIL", last)
        self.assertIn("G2", last)
        self.assertIn("passed: G4 G1", last)

    def test_a_failing_gate_leaves_the_charged_feedback_alone(self):
        # A run of the gate may still be voided after it returns; the file
        # is the charge's to write. Here "G6|" is the verdict already
        # charged for the attempt before.
        c = self.cell()
        (c.ws / "shapegate.last").write_text("G6|\n")
        seq = iter([True, False])
        c.verify = lambda shape=None, out_dir=None: cell.VerifyResult(
            green=next(seq), shape=shape)
        c.gate(attempt=6)
        self.assertEqual((c.ws / "shapegate.last").read_text(), "G6|\n")

    def test_a_passing_gate_leaves_it_alone_too(self):
        c = self.cell()
        (c.ws / "shapegate.last").write_text("G1|old\n")
        seq = iter([True] * 6)
        c.verify = lambda shape=None, out_dir=None: cell.VerifyResult(
            green=next(seq), shape=shape)
        c.gate(attempt=1)
        self.assertEqual((c.ws / "shapegate.last").read_text(), "G1|old\n")


class TestFeedbackIsLeftAtTheCharge(CellTestCase):
    """haiku_high_alpha_apidocs_T1_r12, 2026-08-29: attempt 5 was
    charged on G6; two voided runs of attempt 6 (contract, nostart) then
    rewrote shapegate.last as G1, and the attempt 6 that counted was told it
    had failed on G1. Feedback names the CHARGED verdict, so it is written at
    the charge — by the loop, never by the gate."""

    def results(self, *spec):
        return [cell.VerifyResult(green=g, shape=s) for s, g in spec]

    def test_a_charged_failure_names_the_arrangement_and_the_passed_ones(self):
        c = self.cell()
        c.feedback(self.results(("G4", True), ("G1", False)), green=False)
        self.assertEqual((c.ws / "shapegate.last").read_text().strip(),
                         "G1|G4")

    def test_a_charged_green_removes_the_stale_feedback(self):
        c = self.cell()
        (c.ws / "shapegate.last").write_text("G1|old\n")
        c.feedback(self.results(*[(s, True) for s in SHAPES]), green=True)
        self.assertFalse((c.ws / "shapegate.last").exists())

    def test_a_single_arrangement_gate_is_named_seed(self):
        c = self.cell()
        c.feedback([cell.VerifyResult(green=False)], green=False)
        self.assertEqual((c.ws / "shapegate.last").read_text().strip(),
                         "seed|")

    def test_the_iter_note_names_the_arrangement_from_the_verdict(self):
        # The ledger field and the file come from the same results; neither
        # reads the other, so a stale file cannot leak into the ledger.
        c = self.cell()
        note = c.iter_note(5, self.results(("G3", True), ("G6", False)),
                           "2db679f06076abcd", stage="scaling")
        self.assertIn("shape-gate=G6", note)
        self.assertFalse((c.ws / "shapegate.last").exists())


class TestTheFeedbackPromptTellsTheAgentWhatFailed(CellTestCase):
    """An attempt retried without feedback is spent blind — the agent cannot
    see which arrangement failed, at which stage, or what the verify said."""

    def prepared(self, ledger_lines="", **files):
        c = self.cell(ledger_lines=ledger_lines)
        (c.ws / "PROMPT.md").write_text("Read TODO.md and do the task.\n")
        (c.ws / "metrics.json").write_text(
            '{"stage_failed": "scaling", "e2e_pass": 7, "e2e_total": 7}')
        (c.ws / "verify.log").write_text("FAIL[scaling]: saw 0 mounts\n")
        (c.ws / "deploy.log").write_text("[deploy] ready\n")
        for name, body in files.items():
            (c.ws / name.replace("_", ".")).write_text(body)
        return c

    def test_a_refunded_run_after_the_charge_does_not_become_the_feedback(self):
        c = self.prepared(service_deploy_log="the charged run\n")
        charged = c.ws / "arrangements" / "01-a1-G2-charged"
        charged.mkdir(parents=True)
        for name, body in (("verify.log", "FAIL[e2e]: 3/7\n"), ("deploy.log", "[deploy] charged\n"),
                           ("service.deploy.log", "the charged run\n"),
                           ("metrics.json", '{"stage_failed": "e2e", "e2e_pass": 3, "e2e_total": 7}')):
            (charged / name).write_text(body)
        (c.ws / "arrangements" / "02-a2-G1-refunded").mkdir()
        (c.ws / "verify.log").write_text("verifier timed out\n")
        (c.ws / "service.deploy.log").write_text("the refunded run\n")
        t = c._prompt(2).read_text()
        self.assertIn("FAIL[e2e]: 3/7", t)
        self.assertIn("[deploy] charged", t)
        self.assertNotIn("verifier timed out", t)
        self.assertEqual((c.ws / "feedback" / "service.deploy.log").read_text(), "the charged run\n")

    def test_a_charge_archived_before_end_states_falls_back_to_the_workspace(self):
        c = self.prepared()
        (c.ws / "arrangements" / "01-G1").mkdir(parents=True)
        self.assertIn("saw 0 mounts", c._prompt(2).read_text())

    def test_an_attempt_stopped_by_the_limit_is_told_so(self):
        c = self.prepared(timeout_last="1\n")
        self.assertIn("did not finish: it was stopped", c._prompt(2).read_text())
        (c.ws / "timeout.last").unlink()
        self.assertNotIn("did not finish", c._prompt(3).read_text())

    def test_attempt_one_gets_the_bare_task(self):
        c = self.prepared()
        self.assertEqual(c._prompt(1).name, "PROMPT.md")

    def test_a_retry_carries_the_stage_and_the_e2e_count(self):
        c = self.prepared()
        t = c._prompt(2).read_text()
        self.assertIn("attempt 1 failed verification", t)
        self.assertIn("stage: scaling", t)
        self.assertIn("e2e 7/7", t)

    def test_it_names_the_failing_arrangement(self):
        c = self.prepared(shapegate_last="G4|G1 G2\n")
        t = c._prompt(2).read_text()
        self.assertIn("FAILED on arrangement: G4", t)
        self.assertIn("passed: G1 G2", t)
        self.assertIn("ALL arrangements must pass", t)

    def test_it_carries_the_verify_and_deploy_tails(self):
        c = self.prepared()
        t = c._prompt(2).read_text()
        self.assertIn("FAIL[scaling]: saw 0 mounts", t)
        self.assertIn("[deploy] ready", t)

    def test_a_no_edit_attempt_is_called_out(self):
        c = self.prepared(noedit_last="1\n")
        self.assertIn("modified NO files at all", c._prompt(2).read_text())

    def test_reverted_out_of_surface_edits_are_called_out(self):
        c = self.prepared(heal_last="TODO.md docs/project.md\n")
        t = c._prompt(2).read_text()
        self.assertIn("were reverted before verification", t)
        self.assertIn("TODO.md", t)

    def test_files_moved_out_of_the_surface_are_called_out(self):
        c = self.prepared(evict_last="conftest.py tests/test_a.py\n")
        t = c._prompt(2).read_text()
        self.assertIn("moved out before\nverification and had no effect", t)
        self.assertIn("conftest.py tests/test_a.py", t)

    def test_the_task_itself_is_still_first(self):
        c = self.prepared()
        t = c._prompt(2).read_text()
        self.assertTrue(t.startswith("Read TODO.md"))

    def test_a_retry_stages_the_full_logs_for_the_agent(self):
        c = self.prepared(service_deploy_log="ERROR unknown service_id 'x'\n")
        (c.ws / "cluster-diag").mkdir()
        (c.ws / "cluster-diag" / "keda-operator.log").write_text("ERROR scaler\n")
        t = c._prompt(2).read_text()
        self.assertIn("/feedback/", t)
        fb = c.ws / "feedback"
        self.assertIn("unknown service_id", (fb / "service.deploy.log").read_text())
        self.assertIn("ERROR scaler", (fb / "cluster-diag" / "keda-operator.log").read_text())
        self.assertFalse((c.artifacts / "feedback").exists())

    def test_the_staged_logs_are_replaced_each_attempt(self):
        c = self.prepared(service_deploy_log="first\n")
        c._prompt(2)
        (c.ws / "service.deploy.log").write_text("second\n")
        (c.ws / "cluster-diag").mkdir(exist_ok=True)
        c._prompt(3)
        self.assertEqual((c.ws / "feedback" / "service.deploy.log").read_text(), "second\n")

    def test_a_missing_metrics_file_does_not_break_the_prompt(self):
        c = self.prepared()
        (c.ws / "metrics.json").unlink()
        self.assertIn("BUILD FEEDBACK", c._prompt(2).read_text())

    def test_the_header_is_the_charged_verdict_not_the_last_verify(self):
        # r12: a voided run of attempt 6 left metrics.json at nostart 0/0;
        # the charged attempt 5 was scaling 7/7 on G6, and that is what the
        # header must say.
        c = self.prepared(
            shapegate_last="G6|\n",
            ledger_lines="2026-08-29T06:40:34Z\tSTART\tc\tattempt=5\n"
                         "2026-08-29T06:45:23Z\tITER\tfail\tattempt=5 "
                         "stage=scaling e2e=7/7 verify_s=171 "
                         "tree=2db679f06076 shape-gate=G6\n")
        (c.ws / "metrics.json").write_text(
            '{"stage_failed": "nostart", "e2e_pass": 0, "e2e_total": 0}')
        t = c._prompt(6).read_text()
        self.assertIn("attempt 5 failed verification", t)
        self.assertIn("stage: scaling", t)
        self.assertIn("e2e 7/7", t)
        self.assertIn("FAILED on arrangement: G6", t)
        self.assertNotIn("nostart", t)


class TestTheSeededArrangementRotatesPerAttempt(CellTestCase):
    """The bash driver seeds arrangement CURATED[attempt % 6] via
    VERIFY_SHAPE_SEED. A gate that always starts at G1 lets an agent hardcode
    the first timeline it saw, and diverges which arrangement produces the
    failure feedback between the two implementations."""

    def first_shape(self, attempt):
        c = self.cell()
        seen = []

        def v(shape=None, out_dir=None):
            seen.append(shape)
            return cell.VerifyResult(green=False, shape=shape)

        c.verify = v
        c.gate(attempt=attempt)
        return seen[0]

    def test_attempt_one_starts_at_bsb_like_the_bash_driver(self):
        self.assertEqual(self.first_shape(1), "G2")

    def test_the_rotation_cycles_all_six(self):
        firsts = {self.first_shape(a) for a in range(1, 7)}
        self.assertEqual(firsts, set(SHAPES))

    def test_an_explicit_seed_shape_still_wins(self):
        c = self.cell()
        seen = []

        def v(shape=None, out_dir=None):
            seen.append(shape)
            return cell.VerifyResult(green=False, shape=shape)

        c.verify = v
        c.gate(attempt=1, seed_shape="G5")
        self.assertEqual(seen[0], "G5")

    def test_a_single_arrangement_gate_does_not_rotate(self):
        c = self.cell()
        seen = []

        def v(shape=None, out_dir=None):
            seen.append(shape)
            return cell.VerifyResult(green=True, shape=shape)

        c.verify = v
        c.conf.values["SHAPE_GATE"] = "one"
        c.gate(attempt=4)
        self.assertEqual(seen, [None])


class TestDriverDecisionsAreDeterministic(CellTestCase):
    """PY-READINESS support: identical state must produce identical driver
    decisions — the whole-gate order, the verdict, and the recorded note are
    pure functions of their inputs, so a replayed cell is exactly the cell."""

    def gate_order(self, attempt):
        c = self.cell()
        seen = []

        def v(shape=None, out_dir=None):
            seen.append(shape)
            return cell.VerifyResult(green=True, shape=shape)

        c.verify = v
        c.gate(attempt=attempt)
        return seen

    def test_the_whole_gate_order_is_a_function_of_the_attempt(self):
        for attempt in range(1, 13):
            once, twice = self.gate_order(attempt), self.gate_order(attempt)
            self.assertEqual(once, twice)
            self.assertEqual(once[0], SHAPES[attempt % len(SHAPES)])
            self.assertEqual(sorted(once), sorted(SHAPES))

    def test_the_verdict_is_a_function_of_the_results(self):
        c = self.cell()
        green6 = [cell.VerifyResult(green=True, shape=s) for s in SHAPES]
        self.assertEqual(c.judge(1, green6), "green")
        self.assertEqual(c.judge(1, green6), "green")
        self.assertEqual(c.judge(1, green6[:5]), "fail")   # short list = fail
        red = green6[:5] + [cell.VerifyResult(green=False, shape="G6")]
        self.assertEqual(c.judge(1, red), "fail")
        self.assertEqual(c.judge(c.budget + 1, green6), "budget")

    def test_the_note_is_byte_identical_for_identical_state(self):
        c = self.cell()
        r = cell.VerifyResult(green=False, shape="G3", stage_failed="scaling",
                              metrics={"e2e_pass": 7, "e2e_total": 7},
                              seconds=138.9)
        (c.ws / "shapegate.last").write_text("G3|G2\n")
        post = "a" * 40
        note = c.iter_note(2, [r], post, stage="scaling")
        self.assertEqual(note, c.iter_note(2, [r], post, stage="scaling"))
        self.assertEqual(note, "attempt=2 stage=scaling e2e=7/7 "
                               "verify_s=138 tree=aaaaaaaaaaaa shape-gate=G3")


class TestTheIterNoteCarriesTheBashFields(CellTestCase):
    """Analytics read e2e=, verify_s= and shape-gate= off ITER lines. A py
    note naming only attempt/stage/tree leaves holes in every analysis that
    compares cells across the two implementations."""

    def results(self, green):
        return [cell.VerifyResult(green=green, shape="G1",
                                  metrics={"e2e_pass": 7, "e2e_total": 7},
                                  seconds=145.7)]

    def test_the_green_note_matches_the_bash_format(self):
        c = self.cell()
        note = c.iter_note(2, self.results(True), "8baeb981dce4beef")
        self.assertEqual(note,
                         "attempt=2 e2e=7/7 shapes=all verify_s=145 "
                         "tree=8baeb981dce4")

    def test_the_fail_note_names_stage_and_shape_gate(self):
        # From the results being charged — a shapegate.last left by some
        # other run (here a stale G4) is not consulted.
        c = self.cell()
        (c.ws / "shapegate.last").write_text("G4|G1 G2 G3\n")
        note = c.iter_note(1, self.results(False), "874d6af2fe4a99",
                           stage="scaling")
        self.assertEqual(note,
                         "attempt=1 stage=scaling e2e=7/7 verify_s=145 "
                         "tree=874d6af2fe4a shape-gate=G1")

    def test_no_failing_result_means_no_shape_gate_field(self):
        c = self.cell()
        note = c.iter_note(1, self.results(True), "874d6af2fe4a99",
                           stage="scaling")
        self.assertNotIn("shape-gate", note)

    def test_empty_results_do_not_crash_the_note(self):
        c = self.cell()
        note = c.iter_note(1, [], "abcdef0123456789", stage="?")
        self.assertNotIn("e2e=", note)
        self.assertIn("verify_s=0", note)

    def test_e2e_is_the_verifiers_field_not_the_engines(self):
        """A verifier whose metrics carry no e2e pair writes no e2e= field:
        the engine names no stage of any experiment in the ledger."""
        c = self.cell()
        r = [cell.VerifyResult(green=False, shape="G1", metrics={"cases": 8, "passed": 2},
                               seconds=4.2)]
        self.assertEqual(c.iter_note(1, r, "874d6af2fe4a99", stage="cases"),
                         "attempt=1 stage=cases verify_s=4 tree=874d6af2fe4a shape-gate=G1")
        self.assertEqual(c.iter_note(2, [cell.VerifyResult(green=True, shape="G1",
                                                          metrics={"cases": 8, "passed": 8},
                                                          seconds=4.2)], "8baeb981dce4beef"),
                         "attempt=2 shapes=all verify_s=4 tree=8baeb981dce4")


class TestAnUndeclaredLivenessProbeHaltsAtPreflight(CellTestCase):
    """The base alive() answers "dead"; an infra class that never
    overrides it would void every arrangement, refunded, forever. The
    preflight refuses it before an attempt is spent."""

    def test_infra_ok_is_false_and_names_the_infra_class(self):
        from fae.cell.infra.base import Infra
        from fae.cell.variants.base import Variant

        class Probeless(Infra):
            pass

        v = type("BetaApidocs", (Variant,), {"ID": "beta_apidocs", "INFRA": Probeless})
        c = self.cell()
        c._infra = Probeless(v, c)
        self.assertFalse(c.infra_ok())
        self.assertIn("Probeless declares no alive() probe",
                      (c.ws / "hooks.log").read_text())

    def test_a_declared_probe_passes_the_check(self):
        from fae.cell.infra.base import Infra, liveness_declared
        from fae.cell.variants.base import Variant

        class Probed(Infra):
            def alive(self):
                return True

        self.assertTrue(liveness_declared(type("V", (Variant,), {"INFRA": Probed})))
        self.assertFalse(liveness_declared(Variant))


class TestAnUndeclaredSurfaceHaltsAtPreflight(CellTestCase):
    """A variant that declares no AUTHORING_SURFACE has no surface to heal or check
    against; the preflight refuses the cell before an attempt is spent."""

    def test_infra_ok_is_false_and_names_the_variant(self):
        from unittest import mock
        from fae.cell import experiment
        from fae.cell.variants.base import Variant

        from fae.cell.infra.base import NoopInfra

        class Surfaceless(Variant):
            ID = "beta_apidocs"
            INFRA = NoopInfra

        c = self.cell()
        c._infra = NoopInfra(Surfaceless, c)
        with mock.patch.object(experiment.Definition, "variant",
                               lambda self, vid: Surfaceless):
            self.assertFalse(c.infra_ok())
        self.assertIn("HALT[definition]: variant 'beta_apidocs' declares no [authoring] surface",
                      (c.ws / "hooks.log").read_text())


class TestReverifyIsInvisibleToTheModel(CellTestCase):
    """A re-verify is derived evidence on a terminal cell, not an attempt: it
    must not appear in transitions.log (AcquireSlot on a terminal cell has no
    model action — the r1/r99 replay violations) and must not append to the
    cell's ledger."""

    SEAL = "sealed=2026-08-18T00:00:00Z\tverdict=green\tattempts=2\tby=x\n"

    def reverified(self):
        c = self.cell("2026-08-01T00:00:00Z\tSTART\tc\tattempt=1\n"
                      "2026-08-01T00:10:00Z\tITER\tgreen\tattempt=1\n"
                      "2026-08-01T00:10:01Z\tEND\tc\tgreen=true\n",
                      sealed=self.SEAL)
        before = (c.ws / "iterations.log").read_text()
        c.setup = lambda: (0, {})
        c.teardown = lambda: None
        seen = []

        def v(shape=None, out_dir=None):
            seen.append(shape)
            return cell.VerifyResult(green=True, shape=shape)

        c.verify = v
        c.reverify(stamp="t")
        return c, before, seen

    def test_no_transition_is_emitted(self):
        c, _, _ = self.reverified()
        self.assertEqual(self.transitions(), [])

    def test_the_ledger_is_not_appended(self):
        c, before, _ = self.reverified()
        self.assertEqual((c.ws / "iterations.log").read_text(), before)

    def test_the_evidence_lands_under_reverify(self):
        c, _, _ = self.reverified()
        self.assertTrue((c.ws / "reverify" / "t" / "reverify.json").is_file())

    def test_it_runs_the_full_gate_even_if_config_disables_it(self):
        # reverify exists to re-judge under the CURRENT rules, and the rule is
        # six arrangements; a config with a cut gate must not shrink it.
        c, _, seen = self.reverified()
        self.assertEqual(seen, list(SHAPES))

    def test_even_when_config_turns_the_gate_off(self):
        c = self.cell(sealed=self.SEAL)
        c.conf.values["SHAPE_GATE"] = "one"
        c.setup = lambda: (0, {})
        c.teardown = lambda: None
        seen = []

        def v(shape=None, out_dir=None):
            seen.append(shape)
            return cell.VerifyResult(green=True, shape=shape)

        c.verify = v
        c.reverify(stamp="t2")
        self.assertEqual(seen, list(SHAPES))


class TestGatesAreSerializedWhole(CellTestCase):
    """Two cells whose verifies overlap interleave their arrangements on the
    rig, inflate verify_s with each other's queue time, and the second
    AcquireVerify is a replay violation (AcquireVerify requires
    verifyHeld = {})."""

    def locked_cell(self):
        c = self.cell()
        c.conf.values["VERIFY_LOCK_DIR"] = str(self.root / "verify-lock")
        return c

    def test_the_lock_excludes_a_second_holder(self):
        a, b = self.locked_cell(), self.locked_cell()
        fh = a.verify_lock_acquire()
        self.assertIsNotNone(fh)
        # A second acquire would block; probe the flock directly instead.
        from fae.cell.cell import _mutex
        probe = _mutex.open_lock(self.root / "verify-lock")
        self.assertFalse(_mutex.try_fd(probe))
        probe.close()
        fh.close()
        probe2 = _mutex.open_lock(self.root / "verify-lock")
        self.assertTrue(_mutex.try_fd(probe2))
        probe2.close()

    def test_a_pause_while_queued_stands_down_without_the_lock(self):
        a, b = self.locked_cell(), self.locked_cell()
        fh = a.verify_lock_acquire()
        (b.ws / ".paused").write_text("1\n")
        self.assertIsNone(b.verify_lock_acquire(poll=0.01))
        fh.close()

    def test_a_pause_landing_with_the_win_releases_immediately(self):
        c = self.locked_cell()
        (c.ws / ".paused").write_text("1\n")
        self.assertIsNone(c.verify_lock_acquire())
        from fae.cell.cell import _mutex
        probe = _mutex.open_lock(self.root / "verify-lock")
        self.assertTrue(_mutex.try_fd(probe), "the lock leaked on the pause path")
        probe.close()

    def test_the_run_loop_acquires_before_the_model_transition(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def run(self"):]
        i_lock = body.index("self.verify_lock_acquire()")
        i_tr = body.index("self.apply(T.ACQUIRE_VERIFY)")
        i_gate = body.index("results = gate(")
        self.assertLess(i_lock, i_tr)
        self.assertLess(i_tr, i_gate)
        # The verdict transition is the model's verify release, so it must be
        # applied BEFORE the flock closes; the ledger record follows after.
        i_verdict = body.index("T.VERIFY_GREEN if verdict_green else T.VERIFY_FAIL")
        i_close = body.index("vlock.close()")
        i_record = body.index('self.record("green"')
        self.assertLess(i_gate, i_verdict)
        self.assertLess(i_verdict, i_close)
        self.assertLess(i_close, i_record)
        self.assertIn("finally:", body[i_verdict:i_close])

class TestBothDriversScoreTheSeededArrangement(CellTestCase):
    """A phase-1 (seeded-arrangement) failure used to record nothing in the
    bash driver: no SHAPE event, no shapegate.last — so bash agents got
    strictly less feedback than py agents on the same failure. Both drivers
    must record every arrangement verdict, the seeded one included."""

class TestOneReleasePerSlot(CellTestCase):
    """The teardown hook and the driver can both speak ReleaseSlot; the holder
    note is what makes them take turns. The driver clears the note as it emits,
    and the hook emits only when it still finds one — so the ledger carries
    exactly one release however the cell ends."""

    def _holder(self):
        d = self.root / "workspaces.nosync" / ".queues" / "work-slots"
        d.mkdir(parents=True, exist_ok=True)
        h = d / "slot-1.holder"
        h.write_text(f"{self.CID} 12345\n")
        return h

    def test_the_driver_clears_its_holder_note_as_it_emits(self):
        h = self._holder()
        c = self.cell()
        c.apply(T.SPAWN)
        c.apply(T.ACQUIRE_SLOT)
        c.release_slot("cell end")
        self.assertFalse(h.exists())
        self.assertEqual(self.transitions().count("ReleaseSlot"), 1)

    def test_the_driver_leaves_another_cells_note_alone(self):
        h = self._holder()
        h.write_text("someone_else_high_beta_apidocs_T1_r7 999\n")
        c = self.cell()
        c.apply(T.SPAWN)
        c.apply(T.ACQUIRE_SLOT)
        c.release_slot("cell end")
        self.assertTrue(h.exists())

    def test_both_driver_emit_sites_go_through_release_slot(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("def release_slot"):]
        body = body[:body.index("\n    def ")]
        # apply(T.RELEASE_SLOT) lives ONLY inside release_slot; every other
        # site must call the method so the note is cleared with the emission.
        self.assertEqual(src.count("T.RELEASE_SLOT"), body.count("T.RELEASE_SLOT"))
        self.assertGreaterEqual(src.count("self.release_slot("), 2)


class TestOnePausePerPause(CellTestCase):
    """The COMMAND owns the Pause event (request_pause emits it with the
    file). A driver honoring the file emits one only when the ledger lacks
    it — however the pause arrived, replay sees exactly one Pause, and the
    StandDown that follows is enabled either way."""

    def _log(self):
        return self.root / "transitions.log"

    def _pauses(self):
        return self.transitions().count("Pause")

    def test_the_driver_defers_to_the_commands_pause(self):
        self._log().write_text(
            f"2026-08-22T10:00:00Z\tPause\t{self.CID}\treason=manual\n")
        c = self.cell()
        c.apply(T.SPAWN)
        c.note_pause()
        self.assertEqual(self._pauses(), 1)          # only the command's
        self.assertEqual(c.state.intent, "paused")   # but locally honored
        c.apply(T.STAND_DOWN, "attempt-boundary")    # and StandDown enabled

    def test_a_raw_file_pause_is_self_healed(self):
        c = self.cell()
        c.apply(T.SPAWN)
        c.note_pause()
        self.assertEqual(self._pauses(), 1)          # the driver's own
        c.apply(T.STAND_DOWN, "attempt-boundary")

    def test_an_epoch_seeded_pause_counts_as_the_commands(self):
        self._log().write_text(
            f"2026-08-22T10:00:00Z\tEPOCH\t{self.CID}\t"
            "outcome=none intent=paused loop=none\n")
        c = self.cell()
        c.note_pause()
        self.assertEqual(self._pauses(), 0)

    def test_no_driver_site_applies_a_bare_pause(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("def note_pause"):]
        body = body[:body.index("\n    def ")]
        self.assertEqual(src.count("self.apply(T.PAUSE)"),
                         body.count("self.apply(T.PAUSE)"))
        self.assertGreaterEqual(src.count("self.note_pause()"), 3)


class TestPrepareIsNative(CellTestCase):
    """One prepare implementation, no shim hop: Cell.prepare calls
    fae.cell.prepare directly (and so does `cli.py experiment prepare`)."""

    def test_the_py_driver_calls_the_module_not_the_shim(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def prepare(self"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("_prepare.prepare(", body)
        self.assertNotIn("prepare_cell.sh", body)

