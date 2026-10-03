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
from fae.queues import Book


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
        self.assertIn('self._append("AGENT", *agent_time_fields(attempt, runs, client_s), client)', tail)
        self.assertIn("runs.append(time.monotonic() - t0)", body)



class _Images:
    def client_versions(self, image):
        return {}


class TestTheClientVersion(unittest.TestCase):
    """Each AGENT line names the CLI that ran and its version in the image it
    ran in, so an attempt stays traceable while the clients follow upstream."""

    def _images(self):
        from types import SimpleNamespace
        from fae.cell.agent_image import AgentImage
        return AgentImage(Path(tempfile.mkdtemp()), SimpleNamespace(name="x"), conf={})

    def test_versions_are_probed_once_per_image_id(self):
        from unittest import mock
        from fae.cell import agent_image as ai
        probes = []

        def installed(self, iid):
            probes.append(iid)
            return {"claude": "2.1.286"}
        im = self._images()
        with mock.patch.object(ai, "image_id", side_effect=["sha:a", "sha:a", "sha:b"]), \
                mock.patch.object(ai.AgentImage, "installed", installed):
            self.assertEqual(im.client_versions("fae-agent"), {"claude": "2.1.286"})
            self.assertEqual(im.client_versions("fae-agent"), {"claude": "2.1.286"})
            im.client_versions("fae-agent")
        self.assertEqual(probes, ["sha:a", "sha:b"])

    def test_an_image_that_cannot_be_inspected_has_no_versions(self):
        from unittest import mock
        from fae.cell import agent_image as ai
        with mock.patch.object(ai, "image_id", return_value=""):
            self.assertEqual(self._images().client_versions("gone"), {})

    def _cell(self, cli):
        c = cellmod.Cell.__new__(cellmod.Cell)
        c.root = Path(tempfile.mkdtemp())
        c.conf = {"AGENT_CLI": cli, "AGENT_IMAGE": "fae-agent:latest"}
        c._agent_images = lambda: _Images()
        return c

    def test_the_field_names_the_cli_and_its_version(self):
        from unittest import mock
        with mock.patch.object(_Images, "client_versions",
                               return_value={"claude": "2.1.286", "agy": "1.2.14"}):
            self.assertEqual(self._cell("claude")._client_field(), "client=claude:2.1.286")
            self.assertEqual(self._cell("testagent")._client_field(), "client=testagent:-")

    def test_a_failed_lookup_writes_a_dash_and_never_raises(self):
        from unittest import mock
        with mock.patch.object(_Images, "client_versions", side_effect=RuntimeError("docker")):
            self.assertEqual(self._cell("claude")._client_field(), "client=claude:-")

    def test_the_scorer_still_reads_a_line_that_carries_it(self):
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("sc", Path(ROOT) / "fae" / "scoring" / "score_cell.py")
        sc = _ilu.module_from_spec(spec)
        spec.loader.exec_module(sc)
        log = Path(tempfile.mkdtemp()) / "iterations.log"
        log.write_text("t\tAGENT\tc\tattempt=1\ts=379\truns=1\tlast_s=379\tclient_s=376"
                       "\tcheck=agree\tclient=claude:2.1.286\n"
                       "t\tITER\tgreen\tattempt=1 verify_s=4\n")
        self.assertEqual(sc.parse_agent_time(log.read_text())["agent_s_total"], 379)


if __name__ == "__main__":
    unittest.main()
