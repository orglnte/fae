"""Driver contracts pinned on the python driver's own behavior.

Each class here is the py oracle for a claim that was only ever asserted
against the bash driver's source — seal placement, the no-edit oracle, the
tamper/fingerprint ordering, the agent retry rules, a loud setup failure,
the ticker's arming point. Real Cell, scratch roots, stubbed agent/verify:
the run loop is the code under test.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae import cell
from fae.cell import experiment as _experiment  # noqa: E402
SHAPES = _experiment.current().gate.arrangements

T = cell.T
CELL_SRC = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
VERIFY_SRC = (Path(ROOT) / "fae" / "cell" / "verify.py").read_text()


class DriverCase(unittest.TestCase):

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
        os.environ["WORKSPACES_DIR"] = str(self.wsdir)
        os.environ["FAE_VARIANT_NOOP"] = "1"     # a scratch root provisions nothing
        self.addCleanup(os.environ.pop, "FAE_VARIANT_NOOP", None)
        self.addCleanup(os.environ.pop, "TRANSITIONS_LOG", None)
        self.addCleanup(os.environ.pop, "WORKSPACES_DIR", None)
        self.overlay = self.root / "overlay"
        self.overlay.mkdir()
        (self.overlay / "authored.txt").write_text("edit\n")

    def cell(self):
        ws = self.wsdir / self.CID
        (ws / "artifacts").mkdir(parents=True, exist_ok=True)
        (ws / "artifacts" / ".skeleton_manifest").touch()
        (ws / "cell.env").write_text("TASK=T1\nTREATMENT=beta\n"
                                     "CONDITION=apidocs\nREPEAT=1\n")
        c = cell.Cell(self.CID, workspaces=self.wsdir, root=self.root)
        c.prepare = lambda fresh=False: c.ws
        c.substrate_ok = lambda: True
        c.stage_agent = lambda: None
        return c

    @staticmethod
    def green():
        return [cell.VerifyResult(green=True, shape=s) for s in SHAPES]

    @staticmethod
    def red():
        return [cell.VerifyResult(green=False, shape="G2", stage_failed="scaling")]

    def events(self, c):
        return [l.split("\t")[1] for l in
                (c.ws / "iterations.log").read_text().splitlines()]

    def run_body(self):
        body = CELL_SRC[CELL_SRC.index("    def run(self"):]
        return body[:body.index("\n    def ", 10)]


class TestSealPlacement(DriverCase):
    """A cell becomes finished evidence exactly at a VERDICT: green or the
    budget spent. Every other way out leaves it resumable and unsealed."""

    def test_green_seals_after_the_end_line(self):
        c = self.cell()
        self.assertEqual(c.run(stub_overlay=self.overlay, verify=self.green), "green")
        ev = self.events(c)
        self.assertIn("END", ev)
        self.assertNotIn("SEAL", ev)                  # the marker is a file
        self.assertTrue((c.ws / ".sealed").exists())
        self.assertIn("verdict=green", (c.ws / ".sealed").read_text())

    def test_a_spent_budget_seals_too(self):
        c = self.cell()
        self.assertEqual(c.run(stub_overlay=self.overlay, verify=self.red), "failed")
        self.assertIn("END", self.events(c))
        self.assertIn("verdict=budget", (c.ws / ".sealed").read_text())

    def test_a_void_does_not_seal(self):
        c = self.cell()
        gate = lambda: [cell.VerifyResult(green=False, shape="G2",
                                          stage_failed="exclusive-lock", charge=False)]
        with self.assertRaises(cell.Halt):
            c.run(stub_overlay=self.overlay, verify=gate)
        self.assertNotIn("END", self.events(c))
        self.assertFalse((c.ws / ".sealed").exists())

    def test_a_pause_does_not_seal(self):
        c = self.cell()
        (c.ws / ".paused").write_text("op\n")
        self.assertIsNone(c.run(stub_overlay=self.overlay, verify=self.green))
        self.assertNotIn("END", self.events(c))
        self.assertFalse((c.ws / ".sealed").exists())

    def test_a_sealed_cell_refuses_to_run_again(self):
        c = self.cell()
        c.run(stub_overlay=self.overlay, verify=self.green)
        c2 = cell.Cell(self.CID, workspaces=self.wsdir, root=self.root)
        with self.assertRaises(cell.Sealed):
            c2.run(stub_overlay=self.overlay, verify=self.green)


class TestTheNoEditOracleIsTheJudgedTree(DriverCase):

    def test_noedit_reads_the_judged_tree_and_nothing_else(self):
        src = (Path(ROOT) / "fae" / "cell" / "checkpoints.py").read_text()
        body = src[src.index("    def noedit"):]
        nxt = body.find("\n    def ", 10)
        body = body if nxt < 0 else body[:nxt]
        self.assertIn("self.judged", body)
        for forbidden in ("mtime", "st_mtime", "newer", "attempt_stamp"):
            self.assertNotIn(forbidden, body)
        prop = src[src.index("    def judged(self)"):]
        self.assertIn('".judged_tree"', prop[:400])


class TestTamperIsCheckedAfterTheFingerprint(DriverCase):

    def test_a_pin_mismatch_wins_over_the_verifiers_verdict(self):
        # Both are voids; the fingerprint says "operator edit", the verifier's
        # own manifest says "something wrote into the cell" — the engine
        # re-checks the pin AFTER the verifier answers and overrides whatever
        # it said, or every operator edit reads as a tamper.
        body = CELL_SRC[CELL_SRC.index("    def verify(self"):]
        body = body[:body.index("    def _persist_verdict")]
        self.assertLess(body.index("run_verifier("), body.index('stage="harness-fp"'))


class TestAgentRetries(DriverCase):
    """Row 18. A usage wall is not a failed build: the SAME attempt retries.
    A CLI that produces nothing is retried too, but capped."""

    def _agent_script(self, c, outcomes):
        calls = []

        def fake(attempt, stub_overlay=None, agent_cmd=None, extra_env=None):
            text, rc = outcomes[min(len(calls), len(outcomes) - 1)]
            calls.append(rc)
            (c.ws / f"agent.attempt-{attempt}.log").write_text(text)
            (c.ws / "artifacts" / f"edit{len(calls)}.txt").write_text("x\n")
            return rc

        c._agent = fake
        return calls

    def test_a_limit_retries_the_same_attempt_without_charging_it(self):
        c = self.cell()
        c.conf.values["LIMIT_RETRY_S"] = "0"
        calls = self._agent_script(c, [("rate limit reached\n", 1),
                                       ("built the thing\n", 0)])
        self.assertEqual(c.run(verify=self.green), "green")
        self.assertEqual(calls, [1, 0])
        ev = self.events(c)
        self.assertIn("WAIT", ev)
        self.assertEqual(ev.count("ITER"), 1)
        self.assertTrue(list(c.ws.glob("agent.attempt-1.wait-*.log")))

    def test_a_dead_cli_is_capped_and_halts_systemic(self):
        c = self.cell()
        c.conf.values["LIMIT_RETRY_S"] = "0"
        c.conf.values["AGENT_FAULT_RETRIES"] = "1"
        calls = self._agent_script(c, [("", 1)])
        with self.assertRaises(cell.Halt) as cm:
            c.run(verify=self.green)
        self.assertEqual(cm.exception.code, 42)
        self.assertEqual(len(calls), 2)                  # 1 + cap
        ev = self.events(c)
        self.assertIn("HALT", ev)
        self.assertNotIn("ITER", ev)
        self.assertEqual([t.split("\t")[1] for t in
                          (self.root / "transitions.log").read_text().splitlines()][-1],
                         "Crash")


class TestASetupFailureIsLoud(DriverCase):

    def test_a_failed_hook_alerts_and_halts_the_cell(self):
        c = self.cell()
        c.setup = lambda arena: (7, {})
        with self.assertRaises(cell.Halt) as cm:
            c.run(verify=self.green)
        self.assertEqual(cm.exception.code, 45)
        alert = [l for l in (c.ws / "iterations.log").read_text().splitlines()
                 if "\tALERT\t" in l][0]
        self.assertIn("SETUP-FAILED rc=7", alert)
        self.assertNotIn("ITER", self.events(c))

    def test_a_stub_still_provisions_the_substrate(self):
        c = self.cell()
        c.setup = lambda arena: (7, {})
        with self.assertRaises(cell.Halt) as cm:
            c.run(stub_overlay=self.overlay, verify=self.green)
        self.assertEqual(cm.exception.code, 45)

class TestTheTickerIsArmedBeforeTheAgent(DriverCase):

    def test_started_once_before_any_attempt(self):
        body = self.run_body()
        self.assertEqual(body.count("self.start_ticker("), 1)
        self.assertLess(body.index("self.start_ticker("),
                        body.index("self.agent_with_retries("))


class TestEveryExitClearsTheHolderNotes(DriverCase):
    """The fd is the lock; the holder notes are the fleet's display of who
    holds what. A cell that halts in setup must not leave notes naming it,
    or TRIAGE reads a zombie holder for resources that are long gone."""

    def _notes_naming(self, c):
        orch = self.root / "workspaces.nosync" / ".orch"
        return [h for h in orch.glob("*/slot-*.holder")
                if h.read_text().split()[0] == c.cid]

    def test_a_setup_failure_leaves_no_note(self):
        c = self.cell()
        c.setup = lambda arena: (7, {})
        with self.assertRaises(cell.Halt):
            c.run(verify=self.green)
        self.assertEqual(self._notes_naming(c), [])

    def test_a_void_leaves_no_note(self):
        c = self.cell()
        gate = lambda: [cell.VerifyResult(green=False, shape="G2",
                                          stage_failed="store", charge=False)]
        with self.assertRaises(cell.Halt):
            c.run(stub_overlay=self.overlay, verify=gate)
        self.assertEqual(self._notes_naming(c), [])


class TestWallsAreNotBuilds(DriverCase):
    """Row 18, live wordings. A wall the CLI reports as a successful run
    (exit 0, structured error result) retries the same attempt; an untouched
    tree plus wall prose is a wall whatever the exit code; an auth wall halts
    for a human; a pause during the wait stands the cell down."""

    def _agent_script(self, c, outcomes):
        """outcomes: (transcript, rc, edit?) per call; the last one repeats."""
        calls = []

        def fake(attempt, stub_overlay=None, agent_cmd=None, extra_env=None):
            text, rc, edit = outcomes[min(len(calls), len(outcomes) - 1)]
            calls.append(rc)
            (c.ws / f"agent.attempt-{attempt}.log").write_text(text)
            if edit:
                (c.ws / "artifacts" / f"edit{len(calls)}.txt").write_text("x\n")
            if callable(edit):
                edit()
            return rc

        c._agent = fake
        return calls

    def transitions(self):
        f = self.root / "transitions.log"
        return [l.split("\t")[1] for l in f.read_text().splitlines()]

    def test_the_real_429_result_line_waits_whatever_the_exit_code(self):
        from fae.testagent import LIMIT429_LINES
        wall = "\n".join(LIMIT429_LINES) + "\n"
        for rc in (0, 1):
            with self.subTest(rc=rc):
                c = self.cell()
                c.conf.values["LIMIT_RETRY_S"] = "0"
                calls = self._agent_script(c, [(wall, rc, False),
                                               ("built the thing\n", 0, True)])
                self.assertEqual(c.run(verify=self.green), "green")
                self.assertEqual(calls, [rc, 0])
                ev = self.events(c)
                self.assertEqual(ev.count("WAIT"), 1)
                self.assertEqual(ev.count("ITER"), 1)
                self.assertNotIn("NOEDIT", ev)
                self.assertTrue(list(c.ws.glob("agent.attempt-1.wait-*.log")))
                shutil.rmtree(c.ws)

    def test_an_untouched_tree_with_wall_prose_waits_even_on_exit_0(self):
        from fae.testagent import LIMIT_LINE
        c = self.cell()
        c.conf.values["LIMIT_RETRY_S"] = "0"
        calls = self._agent_script(c, [(LIMIT_LINE + "\n", 0, False),
                                       ("built the thing\n", 0, True)])
        self.assertEqual(c.run(verify=self.green), "green")
        self.assertEqual(calls, [0, 0])
        ev = self.events(c)
        self.assertEqual(ev.count("WAIT"), 1)
        self.assertNotIn("NOEDIT", ev)

    def test_an_edited_tree_with_wall_prose_on_exit_0_is_judged(self):
        """Row 19 stays: prose in a finished build's transcript is prose."""
        from fae.testagent import LIMIT_LINE
        c = self.cell()
        self._agent_script(c, [(f"added backoff\n{LIMIT_LINE}\n", 0, True)])
        self.assertEqual(c.run(verify=self.green), "green")
        self.assertNotIn("WAIT", self.events(c))

    def test_an_auth_wall_halts_systemic_without_an_attempt(self):
        from fae.testagent import AUTH_LINE
        c = self.cell()
        self._agent_script(c, [(AUTH_LINE + "\n", 0, False)])
        with self.assertRaises(cell.Halt) as cm:
            c.run(verify=self.green)
        self.assertEqual(cm.exception.code, 42)
        ev = self.events(c)
        self.assertIn("HALT", ev)
        self.assertNotIn("ITER", ev)
        self.assertNotIn("WAIT", ev)
        self.assertIn("auth-wall", (c.ws / "iterations.log").read_text())
        self.assertEqual(self.transitions()[-1], "Crash")

    def test_a_pause_during_the_wait_stands_down_not_crashes(self):
        from fae.testagent import LIMIT429_LINES
        wall = "\n".join(LIMIT429_LINES) + "\n"
        c = self.cell()
        c.conf.values["LIMIT_RETRY_S"] = "0"
        pause = lambda: (c.ws / ".paused").write_text("manual by=operator\n")
        self._agent_script(c, [(wall, 0, pause)])
        self.assertIsNone(c.run(verify=self.green))
        ev = self.events(c)
        self.assertIn("PAUSED", ev)
        self.assertNotIn("HALT", ev)
        self.assertNotIn("ITER", ev)
        self.assertIn("limit-wait", (c.ws / "iterations.log").read_text())
        tr = self.transitions()
        self.assertEqual(tr[-1], "StandDown")
        self.assertIn("Pause", tr)
        self.assertNotIn("Crash", tr)


class TestTheDriverKeepsTheHostAwake(DriverCase):
    """Idle sleep halts the monotonic clock the gate is measured on; the
    driver holds a caffeinate assertion for exactly its own lifetime."""

    def test_darwin_with_caffeinate_holds_and_releases_the_assertion(self):
        from unittest import mock
        import fae.cell.cell as cellmod
        spawned, ended = [], []

        class Held:
            def terminate(self):
                ended.append(True)

        def popen(argv, **kw):
            spawned.append(argv)
            return Held()

        with mock.patch.dict(os.environ, {cellmod._treatments.NOOP_ENV: "0"}), \
             mock.patch.object(cellmod.sys, "platform", "darwin"), \
             mock.patch.object(cellmod.shutil, "which", lambda n: f"/usr/bin/{n}"), \
             mock.patch.object(cellmod.subprocess, "Popen", popen):
            h = cellmod.hold_awake(4242)
            self.assertIsNotNone(h)
            h.terminate()
        self.assertEqual(spawned, [["caffeinate", "-i", "-w", "4242"]])
        self.assertEqual(ended, [True])

    def test_no_caffeinate_binary_means_no_assertion(self):
        import fae.cell.cell as cellmod
        from unittest import mock
        with mock.patch.dict(os.environ, {cellmod._treatments.NOOP_ENV: "0"}), \
             mock.patch.object(cellmod.sys, "platform", "darwin"), \
             mock.patch.object(cellmod.shutil, "which", lambda n: None):
            self.assertIsNone(cellmod.hold_awake(4242))

    def test_a_test_root_skips_the_assertion(self):
        import fae.cell.cell as cellmod
        self.assertEqual(os.environ.get(cellmod._treatments.NOOP_ENV), "1")
        self.assertIsNone(cellmod.hold_awake(4242))

    def test_the_run_holds_and_releases_it_for_exactly_its_own_lifetime(self):
        from unittest import mock
        import fae.cell.cell as cellmod
        ended = []

        class Held:
            def terminate(self):
                ended.append(True)

        calls = []

        def fake_hold_awake(pid):
            calls.append(pid)
            return Held()

        c = self.cell()
        with mock.patch.object(cellmod, "hold_awake", fake_hold_awake):
            self.assertEqual(c.run(stub_overlay=self.overlay, verify=self.green), "green")
        self.assertEqual(calls, [os.getpid()])
        self.assertEqual(ended, [True])
