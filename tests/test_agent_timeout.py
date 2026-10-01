"""An agent past the wall-clock limit is killed, its container removed, and
the attempt judged as it stands: a runaway agent is the agent's failure."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401

from fae.cell.cell import Cell


def bare_cell(conf):
    c = Cell.__new__(Cell)
    c.ws = Path(tempfile.mkdtemp())
    c.cid = "m_high_arm_apidocs_T1_r1"
    c.conf = conf
    c.ledger = []
    c._append = lambda *f: c.ledger.append(f)
    c.hb = lambda *a: None
    c.tree = lambda: "pre"
    return c


class TestTheLimit(unittest.TestCase):
    def test_a_run_past_the_limit_is_killed_and_its_container_removed(self):
        c = bare_cell({"AGENT_TIMEOUT_S": "1"})
        real = subprocess.run
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[:3] == ["docker", "rm", "-f"]:
                return mock.Mock(returncode=0)
            return real(argv, **kw)
        log = c.ws / "agent.log"
        with log.open("w") as f, mock.patch("fae.cell.cell.subprocess.run", run):
            rc = c._run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], f, None)
        self.assertEqual(rc, Cell.AGENT_TIMED_OUT)
        self.assertIn(["docker", "rm", "-f", "fae-agent-m_high_arm_apidocs_T1_r1"], calls)
        self.assertIn("killed after 1s", log.read_text())

    def test_a_run_inside_the_limit_returns_its_own_code(self):
        c = bare_cell({"AGENT_TIMEOUT_S": "30"})
        with (c.ws / "agent.log").open("w") as f:
            self.assertEqual(c._run_bounded([sys.executable, "-c", "raise SystemExit(3)"], f, None), 3)

    def test_the_default_is_three_hours(self):
        self.assertEqual(bare_cell({})._agent_timeout_s(), 3 * 3600)


class TestTheAttemptIsJudged(unittest.TestCase):
    def test_a_timed_out_attempt_goes_to_the_verifier_with_an_alert(self):
        c = bare_cell({"AGENT_CLI": "claude", "AGENT_TIMEOUT_S": "60"})

        def agent(attempt, **kw):
            (c.ws / f"agent.attempt-{attempt}.log").write_text("row 1\nrow 2\n")
            return Cell.AGENT_TIMED_OUT
        c._agent = agent
        self.assertIs(c.agent_with_retries(2, pre="pre"), True)
        self.assertEqual(c.ledger[0], ("ALERT", "attempt=2",
                                       "AGENT-TIMEOUT killed after 60s; judged as it stands"))
        self.assertEqual(c.ledger[1][0], "AGENT")
        self.assertTrue(c.ledger[1][-1].startswith("client=claude:"))


class TestTheNextPromptSaysSo(unittest.TestCase):
    def test_a_timed_out_attempt_leaves_a_note_a_finished_one_clears(self):
        c = bare_cell({"AGENT_CLI": "claude", "AGENT_TIMEOUT_S": "60"})
        rcs = [Cell.AGENT_TIMED_OUT, 0]

        def agent(attempt, **kw):
            (c.ws / f"agent.attempt-{attempt}.log").write_text('{"type":"result"}\nwrote app.py\n')
            return rcs.pop(0)
        c._agent = agent
        c.agent_with_retries(1, pre="pre")
        self.assertTrue((c.ws / "timeout.last").is_file())
        with mock.patch.object(Cell, "_transient_fault", return_value=None):
            c.agent_with_retries(2, pre="other")
        self.assertFalse((c.ws / "timeout.last").exists())


if __name__ == "__main__":
    unittest.main()
