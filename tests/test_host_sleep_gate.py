"""A host sleep costs the arrangement it interrupted, not the attempt.

Cell.verify voids an arrangement the host slept through (stage host-sleep,
refunded), a green included. The gate runs that arrangement again, keeping
the ones already passed and the agent's tree; before it does, and before
any arrangement after a sleep, it waits for a wake that outlasts a dark
wake and runs the cell's setup again, which brings back what the sleep
killed.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

import fae.experiment
from fae.cell import cell
from fae.cell.host_sleep import Reading
from fae.cell.infra.base import HookFailure
from fae.cell.verify import Verdict


class Clocks:
    def __init__(self):
        self.now = Reading(1000.0, 10.0)

    def read(self):
        return self.now

    def sleep(self, s, awake=1.0):
        self.now = Reading(self.now.wall + s + awake, self.now.mono + awake)


class CellCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        (d / "ws" / "c" / "artifacts").mkdir(parents=True)
        self.clocks = Clocks()
        p = mock.patch.object(cell.host_sleep, "reading", self.clocks.read)
        p.start()
        self.addCleanup(p.stop)
        self.c = cell.Cell("c", workspaces=d / "ws", root=ROOT)

    def ledger(self):
        f = self.c.ws / "iterations.log"
        return f.read_text() if f.exists() else ""


class TestTheVerifyVoidsASleep(CellCase):

    def verify_with(self, verdict, sleep_s):
        def runner(ctx, variant, timeout_s, log_dir=None):
            self.clocks.sleep(sleep_s)
            return verdict
        self.c._infra = mock.Mock(alive=lambda: True)
        vcls = mock.Mock(REQUIRED_OUTPUTS=(), NOT_RUN_STAGES=frozenset(), MEASURED_STAGES=None,
                         FILES=(), FEEDBACK_LOGS=())
        with mock.patch.object(cell, "run_verifier", runner), \
                mock.patch.object(cell.Cell, "expected_fp", new_callable=mock.PropertyMock,
                                  return_value=""), \
                mock.patch.object(fae.experiment._experiment.Experiment, "definition",
                                  new_callable=mock.PropertyMock,
                                  return_value=mock.Mock(exclusive=None,
                                                         verifier_class=lambda: vcls)):
            return self.c.verify(shape="A")

    def test_a_green_the_host_slept_through_is_void(self):
        r = self.verify_with(Verdict(ok=True), sleep_s=3600)
        self.assertEqual((r.green, r.stage_failed, r.charge), (False, "host-sleep", False))
        self.assertIn("slept 3600s", r.why)

    def test_a_charged_fail_the_host_slept_through_is_void(self):
        r = self.verify_with(Verdict(ok=False, stage="scaling", charge=True), sleep_s=600)
        self.assertEqual((r.stage_failed, r.charge), ("host-sleep", False))

    def test_jitter_is_no_sleep(self):
        r = self.verify_with(Verdict(ok=False, stage="scaling", charge=True), sleep_s=5)
        self.assertEqual((r.stage_failed, r.charge), ("scaling", True))

    def test_a_stand_down_stays_the_operators(self):
        r = self.verify_with(Verdict(ok=False, stage="contract", charge=False,
                                     stand_down=("cache gone",)), sleep_s=600)
        self.assertEqual((r.stage_failed, r.contract), ("contract", ["cache gone"]))


class GateCase(CellCase):
    """The gate over three arrangements, each verify answered from a script;
    the wake wait and the setup are counted, not run."""

    SHAPES = ("A", "B", "C")

    def setUp(self):
        super().setUp()
        self.setups, self.waits, self.events = [], [], []
        p = mock.patch.object(cell.host_sleep, "wait_awake",
                              lambda: self.waits.append(1) or self.events.append("wait") or 130.0)
        p.start()
        self.addCleanup(p.stop)
        self.c._set_up_at = self.clocks.read()
        self.setup_returns(0)

    def setup_returns(self, rc):
        def setup():
            started = self.clocks.read()
            self.setups.append(1)
            self.events.append("setup")
            if rc == 0:
                self.c._set_up_at = started
            return rc, {}
        self.c.setup = setup

    def gate(self, answers):
        calls = []

        def verify(shape=None, out_dir=None):
            calls.append(shape)
            self.events.append(shape)
            stage = answers[shape].pop(0)
            if stage == "host-sleep":
                self.clocks.sleep(900)
            return cell.VerifyResult(green=stage == "green", shape=shape,
                                     stage_failed="" if stage == "green" else stage,
                                     charge=stage not in ("host-sleep", "infra"))
        self.c.verify = verify
        self.c._surface = mock.Mock(check=lambda: [])
        with mock.patch.object(cell.Cell, "gate_shapes", new_callable=mock.PropertyMock,
                               return_value=list(self.SHAPES)), \
                mock.patch.object(cell.Cell, "gate_def", new_callable=mock.PropertyMock,
                                  return_value=mock.Mock(rotate=False)):
            return self.c.gate(attempt=2), calls


class TestTheGateRerunsOnlyTheSleptArrangement(GateCase):

    def test_the_slept_arrangement_runs_again_and_the_passed_ones_stand(self):
        results, calls = self.gate({"A": ["green"], "B": ["host-sleep", "green"],
                                    "C": ["green"]})
        self.assertEqual(calls, ["A", "B", "B", "C"])
        self.assertEqual([(r.shape, r.green) for r in results],
                         [("A", True), ("B", True), ("C", True)])
        self.assertIn("B void host-sleep, re-run 1/3", self.ledger())

    def test_a_host_that_keeps_sleeping_leaves_the_arrangement_void(self):
        results, calls = self.gate({"A": ["green"], "B": ["host-sleep"] * 4, "C": ["green"]})
        self.assertEqual(calls, ["A"] + ["B"] * 4)
        self.assertEqual((results[-1].shape, results[-1].stage_failed, results[-1].charge),
                         ("B", "host-sleep", False))
        self.assertIn("B void host-sleep, re-run 3/3", self.ledger())

    def test_another_void_is_not_rerun(self):
        results, calls = self.gate({"A": ["green"], "B": ["infra"], "C": ["green"]})
        self.assertEqual(calls, ["A", "B"])
        self.assertEqual(results[-1].stage_failed, "infra")


class TestAfterASleepTheInfraIsSetUpAgain(GateCase):

    def test_no_sleep_no_wait_no_setup(self):
        self.setup_returns(0)
        self.gate({"A": ["green"], "B": ["green"], "C": ["green"]})
        self.assertEqual((self.waits, self.setups), ([], []))
        self.assertNotIn("WAKE", self.ledger())

    def test_a_sleep_before_the_gate_waits_and_sets_up_once(self):
        self.setup_returns(0)
        self.clocks.sleep(3990)
        results, calls = self.gate({"A": ["green"], "B": ["green"], "C": ["green"]})
        self.assertEqual((len(self.waits), len(self.setups)), (1, 1))
        self.assertEqual(calls, ["A", "B", "C"])
        self.assertIn("WAKE\tc\tattempt=2\tslept=3990s waited=130s; setting the infra up again",
                      self.ledger())

    def test_the_rerun_of_a_slept_arrangement_waits_and_sets_up_first(self):
        self.setup_returns(0)
        results, calls = self.gate({"A": ["green"], "B": ["host-sleep", "green"],
                                    "C": ["green"]})
        self.assertEqual(calls, ["A", "B", "B", "C"])
        self.assertEqual((len(self.waits), len(self.setups)), (1, 1))
        led = self.ledger()
        self.assertLess(led.index("B void host-sleep, re-run 1/3"), led.index("WAKE"))

    def test_a_sleep_during_the_setup_sets_up_again(self):
        slept_once = []

        def setup():
            started = self.clocks.read()
            self.events.append("setup")
            if not slept_once:
                slept_once.append(1)
                self.clocks.sleep(600)
            self.c._set_up_at = started
            return 0, {}
        self.c.setup = setup
        self.clocks.sleep(600)
        results, calls = self.gate({"A": ["green"], "B": ["green"], "C": ["green"]})
        self.assertEqual(self.events, ["wait", "setup", "wait", "setup", "A", "B", "C"])

    def test_a_failed_setup_is_an_infra_void_without_a_verify(self):
        self.setup_returns(1)
        self.clocks.sleep(600)
        results, calls = self.gate({"A": ["green"], "B": ["green"], "C": ["green"]})
        self.assertEqual(calls, [])
        self.assertEqual((results[-1].stage_failed, results[-1].charge), ("infra", False))
        self.assertIn("SETUP-FAILED rc=1 after a host sleep", self.ledger())


class TestSetupRecordsWhatTheAgentGets(CellCase):

    def test_a_setup_keeps_the_agent_env_and_when_it_started(self):
        def cell_setup():
            self.clocks.sleep(600)          # asleep inside the setup: seen by the next check
            return {"KUBE_MOUNT": "-e K=1"}
        self.c._infra = mock.Mock(cell_setup=cell_setup)
        self.clocks.sleep(100)
        started = self.clocks.read()
        self.assertEqual(self.c.setup(), (0, {"KUBE_MOUNT": "-e K=1"}))
        self.assertEqual(self.c.agent_env, {"KUBE_MOUNT": "-e K=1"})
        self.assertEqual(self.c._set_up_at, started)

    def test_a_failed_setup_keeps_the_last_good_one(self):
        self.c.agent_env = {"KUBE_MOUNT": "-e K=1"}
        before = self.c._set_up_at
        self.c._infra = mock.Mock(cell_setup=mock.Mock(side_effect=HookFailure("x")))
        self.clocks.sleep(100)
        self.assertEqual(self.c.setup(), (1, {}))
        self.assertEqual((self.c.agent_env, self.c._set_up_at), ({"KUBE_MOUNT": "-e K=1"}, before))


class TestTheAgentGetsTheLatestSetup(unittest.TestCase):
    CID = "opus_high_beta_apidocs_T1_r1"

    def setUp(self):
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.wsdir = self.root / "workspaces"
        for k, v in (("TRANSITIONS_LOG", str(self.root / "transitions.log")),
                     ("WORKSPACES_DIR", str(self.wsdir)), ("FAE_VARIANT_NOOP", "1")):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        self.overlay = self.root / "overlay"
        self.overlay.mkdir()
        (self.overlay / "authored.txt").write_text("edit\n")

    def test_the_agent_runs_with_what_the_setup_returned(self):
        ws = self.wsdir / self.CID
        (ws / "artifacts").mkdir(parents=True)
        (ws / ".skeleton_manifest").touch()
        (ws / "cell.env").write_text("TASK=T1\nVARIANT=beta_apidocs\nREPEAT=1\n")
        c = cell.Cell(self.CID, workspaces=self.wsdir, root=self.root)
        c.prepare = lambda fresh=False: c.ws
        c.infra_ok = lambda: True

        def setup():
            c.agent_env = {"KUBE_MOUNT": "-e PORT=7001"}
            return 0, c.agent_env
        c.setup = setup
        seen = []

        def agent(attempt, extra_env=None, **kw):
            seen.append(extra_env)
            return "agent-fault"
        c.agent_with_retries = agent
        with self.assertRaises(cell.Halt):
            c.run(ignore_slots=True, stub_overlay=self.overlay)
        self.assertEqual(seen, [{"KUBE_MOUNT": "-e PORT=7001"}])


if __name__ == "__main__":
    unittest.main()
