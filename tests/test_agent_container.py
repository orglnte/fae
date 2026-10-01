"""An agent container docker could not start is the rig's fault: the cell
halts, nothing is charged, and the attempt never reaches the verifier."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401

from fae.cell.cell import Cell

NO_IMAGE = ("Unable to find image 'fae-exp-agent-alpha:latest' locally\n"
            "docker: Error response from daemon: pull access denied for "
            "fae-exp-agent-alpha, repository does not exist\n")


class TestTheAgentThatNeverStarted(unittest.TestCase):
    def _cell(self, rc, text):
        c = Cell.__new__(Cell)
        c.ws = Path(tempfile.mkdtemp())
        c.conf = {"AGENT_CLI": "claude", "LIMIT_RETRY_S": "0", "AGENT_FAULT_RETRIES": "1"}
        c.ledger = []
        c._append = lambda *f: c.ledger.append(f)
        c.hb = lambda *a: None
        c.tree = lambda: "pre"

        def agent(attempt, **kw):
            (c.ws / f"agent.attempt-{attempt}.log").write_text(text)
            return rc
        c._agent = agent
        return c

    def test_docker_run_failing_halts_without_a_verdict(self):
        c = self._cell(125, NO_IMAGE)
        self.assertEqual(c.agent_with_retries(1, pre="pre"), Cell.AGENT_CONTAINER)
        self.assertEqual(c.ledger, [("HALT", "attempt=1", "agent-container")])

    def test_an_agent_exiting_125_on_its_own_is_judged(self):
        c = self._cell(125, '{"type":"result","duration_ms":1000}\nwrote app.py\n')
        with mock.patch.object(Cell, "_transient_fault", return_value=None):
            self.assertIs(c.agent_with_retries(1, pre="other"), True)
        self.assertNotIn(("HALT", "attempt=1", "agent-container"), c.ledger)

    def test_the_driver_halts_the_cell_on_it(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        self.assertIn('if outcome == self.AGENT_CONTAINER:\n'
                      '                    self.apply(T.CRASH, "agent-container")', src)


if __name__ == "__main__":
    unittest.main()
