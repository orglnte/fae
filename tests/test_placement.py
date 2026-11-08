"""Placement (fae/cell/placement.py): Docker's CPU count is read at every
placement and split, the verify taking the larger half; a cell moves the
containers both phases share to the phase it enters."""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

import _ctx
from fae.cell import placement


class TestSplit(unittest.TestCase):

    def test_the_verify_takes_the_larger_half(self):
        for n, measured, agent in ((6, "0-2", "3-5"), (5, "0-2", "3-4"),
                                   (3, "0-1", "2"), (2, "0", "1"), (1, "0", "0")):
            with self.subTest(n=n):
                self.assertEqual(placement.split(n), placement.Placement(measured, agent))


class TestApply(unittest.TestCase):

    def test_the_sets_are_written_where_container_starts_read_them(self):
        env = {}
        with mock.patch.object(placement, "docker_ncpu", return_value=6):
            p = placement.apply(env)
        self.assertEqual(p, placement.Placement("0-2", "3-5"))
        self.assertEqual(env, {"CPUSET_MEASURED": "0-2", "CPUSET_AGENT": "3-5"})

    def test_a_silent_docker_leaves_the_previous_sets(self):
        env = {"CPUSET_MEASURED": "0-1", "CPUSET_AGENT": "2"}
        with mock.patch.object(placement, "docker_ncpu", return_value=None):
            self.assertIsNone(placement.apply(env))
        self.assertEqual(env, {"CPUSET_MEASURED": "0-1", "CPUSET_AGENT": "2"})

    def test_the_count_comes_from_docker_info(self):
        done = subprocess.CompletedProcess([], 0, stdout="6\n", stderr="")
        with mock.patch.object(placement.subprocess, "run", return_value=done) as run:
            self.assertEqual(_ctx.DOCKER_NCPU(), 6)
        self.assertEqual(run.call_args[0][0][:2], ["docker", "info"])
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="no daemon")
        with mock.patch.object(placement.subprocess, "run", return_value=bad):
            self.assertIsNone(_ctx.DOCKER_NCPU())


class TestSharedContainersFollowThePhase(unittest.TestCase):

    def cell(self, shared):
        from fae.cell.cell import Cell
        c = Cell.__new__(Cell)
        c._infra = mock.Mock(shared_containers=mock.Mock(return_value=shared))
        return c

    def updates(self, c, phase):
        calls = []
        with mock.patch.object(placement, "docker_ncpu", return_value=6), \
                mock.patch.dict(os.environ, {}), \
                mock.patch("fae.cell.cell.subprocess.run",
                           side_effect=lambda argv, **k: calls.append(argv)):
            c.place(phase)
        return calls

    def test_verify_moves_them_to_the_measured_cpus(self):
        self.assertEqual(self.updates(self.cell(["fae-dind-x"]), "verify"),
                         [["docker", "update", "--cpuset-cpus", "0-2", "fae-dind-x"]])

    def test_authoring_moves_them_to_the_agents_cpus(self):
        self.assertEqual(self.updates(self.cell(["fae-dind-x", "fae-pd-x"]), "agent"),
                         [["docker", "update", "--cpuset-cpus", "3-5", "fae-dind-x"],
                          ["docker", "update", "--cpuset-cpus", "3-5", "fae-pd-x"]])

    def test_releasing_the_verify_lock_moves_them_back(self):
        from fae.cell.cell import _VerifyHold
        c = self.cell(["fae-dind-x"])
        fh = mock.Mock()
        calls = []
        with mock.patch.object(placement, "docker_ncpu", return_value=6), \
                mock.patch.dict(os.environ, {}), \
                mock.patch("fae.cell.cell.subprocess.run",
                           side_effect=lambda argv, **k: calls.append(argv)):
            _VerifyHold(fh, c).close()
        fh.close.assert_called_once()
        self.assertEqual(calls, [["docker", "update", "--cpuset-cpus", "3-5", "fae-dind-x"]])


if __name__ == "__main__":
    unittest.main()
