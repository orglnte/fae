"""The AGENT ledger event: how long the agent ran in an attempt (waits between
runs excluded), and, where the client reports its own duration, whether the
engine's measure and the client's agree."""
import json
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae.cell import cell as cellmod
from fae.cell import config as _config


class TestTheFields(unittest.TestCase):
    def test_total_runs_and_last_run(self):
        f = cellmod.agent_time_fields(3, [100.2, 50.6], None)
        self.assertEqual(f, ["attempt=3", "s=151", "runs=2", "last_s=51", "client_s=-", "check=-"])

    def test_agree_within_a_minute_or_a_tenth(self):
        self.assertIn("check=agree", cellmod.agent_time_fields(1, [620.0], 600.0))
        self.assertIn("check=agree", cellmod.agent_time_fields(1, [3300.0], 3000.0))
        self.assertIn("check=disagree", cellmod.agent_time_fields(1, [700.0], 600.0))
        self.assertIn("check=disagree", cellmod.agent_time_fields(1, [3400.0], 3000.0))


class TestTheClientsOwnAccount(unittest.TestCase):
    def _log(self, lines):
        d = tempfile.mkdtemp()
        p = Path(d) / "agent.attempt-1.log"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_claude_result_event_is_read(self):
        log = self._log(['{"type":"assistant"}',
                         json.dumps({"type": "result", "duration_ms": 616317})])
        self.assertAlmostEqual(_config.client_reported_seconds("claude", log), 616.317)

    def test_other_clients_and_missing_events_report_nothing(self):
        log = self._log(['{"type":"assistant"}'])
        self.assertIsNone(_config.client_reported_seconds("claude", log))
        self.assertIsNone(_config.client_reported_seconds("agy", log))
        self.assertIsNone(_config.client_reported_seconds("opencode", log))
        self.assertIsNone(_config.client_reported_seconds("claude", Path("/nonexistent/log")))


class TestWhereItIsWritten(unittest.TestCase):
    def test_the_event_is_written_when_the_attempt_goes_to_judging(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def agent_with_retries("):src.index("    def _charged_note(self):")]
        tail = body[body.rindex("continue"):]
        self.assertIn('self._append("AGENT", *agent_time_fields(attempt, runs, client_s))', tail)
        self.assertIn("runs.append(time.monotonic() - t0)", body)


if __name__ == "__main__":
    unittest.main()
