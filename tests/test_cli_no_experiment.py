"""cli.py at a root with no experiment definition (the engine's own
checkout, say) says where it runs, in one line, instead of a command
running and ending in a traceback; and a scoreboard that refuses ends
`results score` with its exit code, not a traceback."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401
from fae import cli
from fae.cli import _render


class TestNoExperimentAtTheRoot(unittest.TestCase):

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k != "EXPERIMENT_DIR"}
        return mock.patch.dict(os.environ, {**env, "REPO_ROOT": str(self.d), **extra}, clear=True)

    def test_a_command_is_refused_naming_where_to_run(self):
        with self.env():
            why = cli.no_experiment(["results", "score"])
        self.assertIn(f"no experiment at {self.d.resolve()}", why)
        self.assertIn("experiment's root", why)

    def test_help_needs_no_experiment(self):
        with self.env():
            for argv in ([], ["--help"], ["results", "score", "--help"]):
                with self.subTest(argv=argv):
                    self.assertEqual(cli.no_experiment(argv), "")

    def test_a_root_with_a_definition_runs(self):
        (self.d / "experiment").mkdir()
        (self.d / "experiment" / "__init__.py").write_text("")
        with self.env():
            self.assertEqual(cli.no_experiment(["results", "score"]), "")

    def test_the_entry_point_exits_2_with_one_line(self):
        with self.env():
            r = subprocess.run([sys.executable, str(Path(ROOT) / "cli.py"), "results", "score"],
                               capture_output=True, text=True, cwd=self.d)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class TestARefusedScoreboard(unittest.TestCase):

    def test_ends_with_its_exit_code_and_no_traceback(self):
        refusal = subprocess.CalledProcessError(3, ["aggregate"])
        exp = mock.Mock()
        exp.aggregate.side_effect = refusal
        with mock.patch("fae.experiment.exp", return_value=exp), \
                self.assertRaises(SystemExit) as e:
            _render.results_aggregate(mock.Mock(variant=None, where=[], impl=None,
                                                allow_stale=False))
        self.assertEqual(e.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
