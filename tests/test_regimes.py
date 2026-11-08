"""The regime log (fae/conduct/regimes.py): a run start records its caps,
Docker's CPUs with their split and the commits it runs; a drain records its
start and its end."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs, ROOT  # noqa: F401
from fae.cell import placement
from fae.conduct import regimes
from test_conduct import ConductCase


class TestRecord(unittest.TestCase):

    def test_a_run_start_names_its_caps_cpus_and_commits(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(placement, "docker_ncpu", return_value=6), \
                mock.patch.object(regimes, "_head", side_effect=["e1", "x1"]):
            regimes.run_started(d, cap=5, per_agent=2, overrides={"a": 3},
                                excluded=["opencode"], engine_dir="/e", experiment_root="/x")
            [line] = regimes.read(d)
        self.assertEqual({k: v for k, v in line.items() if k != "ts"},
                         {"event": "run-start", "cap": 5, "per_agent": 2, "overrides": {"a": 3},
                          "excluded": ["opencode"], "docker_cpus": 6, "cpus_measured": "0-2",
                          "cpus_agent": "3-5", "engine": "e1", "experiment": "x1"})

    def test_lines_append(self):
        with tempfile.TemporaryDirectory() as d:
            regimes.record(d, "drain", cells=4)
            regimes.record(d, "drained")
            self.assertEqual([l["event"] for l in regimes.read(d)], ["drain", "drained"])


class TestTheRunRecordsItsStart(ConductCase):

    def test_conduct_writes_a_run_start_line(self):
        self.run_conduct(n=3)
        lines = regimes.read(runs.experiment.exp().workspace.conduct)
        self.assertEqual([(l["event"], l["cap"]) for l in lines], [("run-start", 3)])


if __name__ == "__main__":
    unittest.main()
