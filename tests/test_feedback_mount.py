"""The judged run's logs reach the agent through one read-only mount."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)
from fae.cell import config as C


def _conf(cli):
    return C.Config({"AGENT_CLI": cli, "AGENT_MODEL": "m", "AGENT_IMAGE": "img",
                     "AGENT_HOME": tempfile.mkdtemp()}, {})


def _argv(cli, feedback):
    d = Path(tempfile.mkdtemp())
    prompt = d / "PROMPT.md"
    prompt.write_text("task\n")
    return C.build_agent_argv(_conf(cli), "cid1", "/ws/art", d / "home", prompt,
                              feedback=feedback)


class TestFeedbackMount(unittest.TestCase):
    def mounts(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]

    def test_claude_mounts_the_feedback_dir_read_only_and_adds_it(self):
        argv = _argv("claude", "/ws/feedback")
        self.assertIn("/ws/feedback:/feedback:ro", self.mounts(argv))
        self.assertIn("/feedback", argv[argv.index("--add-dir") + 1:])

    def test_agy_mounts_it_too(self):
        argv = _argv("agy", "/ws/feedback")
        self.assertIn("/ws/feedback:/feedback:ro", self.mounts(argv))
        self.assertEqual(argv.count("--add-dir"), 2)

    def test_no_feedback_dir_means_no_mount(self):
        for cli in ("claude", "agy"):
            argv = _argv(cli, "")
            self.assertFalse(any(":/feedback" in m for m in self.mounts(argv)), cli)
            self.assertNotIn("/feedback", argv)


if __name__ == "__main__":
    unittest.main()


class TestFeedbackLogs(unittest.TestCase):
    """The logs staged for the next attempt are the verifier's FEEDBACK_LOGS,
    not a list the engine keeps."""

    def stage(self, names):
        from types import SimpleNamespace
        from unittest import mock
        from fae.cell import experiment as _experiment
        from fae.cell.cell import Cell
        ws = Path(tempfile.mkdtemp())
        for n in ("verify.log", "deploy.log", "tool.log", "other.log"):
            (ws / n).write_text(n)
        verifier = type("V", (), {"FEEDBACK_LOGS": names})
        with mock.patch.object(_experiment.current(), "verifier_class", return_value=verifier):
            staged = Cell._stage_feedback(SimpleNamespace(ws=ws))
        return staged, sorted(p.name for p in (ws / "feedback").iterdir())

    def test_the_verifier_names_the_logs(self):
        staged, files = self.stage(("verify.log", "tool.log", "absent.log"))
        self.assertEqual(staged, ["verify.log", "tool.log"])
        self.assertEqual(files, ["tool.log", "verify.log"])

    def test_the_default_is_verify_and_deploy(self):
        from fae.cell.verify import Verifier
        self.assertEqual(Verifier.FEEDBACK_LOGS, ("verify.log", "deploy.log"))
