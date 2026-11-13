"""A verify on another Docker daemon (fae/cell/verify_host.py): the local
daemon changes nothing; a remote one gets the verify's files at the same
paths before, gives its output back after, runs the container as its own
user with no core pinning, and a daemon that does not answer is a refunded
rig fault."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT

sys.path.insert(0, str(ROOT))
from fae.cell import verify, verify_host  # noqa: E402
from fae.cell.verify import Ctx, run_verifier, verify_argv  # noqa: E402
from fae.cell.verify_host import VerifyHost  # noqa: E402

REMOTE = "ssh://op@vm.example"


def _ctx(ws):
    return Ctx(root=str(ROOT), experiment_dir=str(ROOT / "tests" / "fixture_experiment"),
               workspace=str(ws), artifacts=str(ws / "artifacts"), out=str(ws / ".verify-out"),
               cid="cell-x", task="T1", variant="only", arrangement="A")


class TestTheLocalDaemonIsToday(unittest.TestCase):

    def test_no_copy_and_no_docker_host(self):
        h = VerifyHost("")
        self.assertFalse(h.remote)
        self.assertNotIn("DOCKER_HOST", h.env({"DOCKER_HOST": "x", "A": "1"}))
        with mock.patch.object(verify_host.subprocess, "run") as run:
            h.push(["/tmp"])
            h.pull("/tmp")
        run.assert_not_called()
        self.assertIsNone(h.identity())

    def test_the_argv_keeps_the_configured_pinning(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            (ws / "artifacts").mkdir(parents=True)
            conf = SimpleNamespace(exported={}, get=lambda k, default=None: "0-2" if k == "CPUSET_MEASURED" else None)
            argv = verify_argv(_ctx(ws), "img:1", conf, host=VerifyHost(""))
        self.assertIn("--cpuset-cpus=0-2", argv)


class TestARemoteDaemon(unittest.TestCase):

    def test_it_is_reached_as_docker_names_it_and_copied_to_over_ssh(self):
        h = VerifyHost(REMOTE)
        self.assertTrue(h.remote)
        self.assertEqual(h.ssh_target, "op@vm.example")
        self.assertEqual(h.env({})["DOCKER_HOST"], REMOTE)
        self.assertIsNone(VerifyHost("tcp://vm:2375").ssh_target)

    def test_a_directory_is_mirrored_at_its_own_path(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            with mock.patch.object(verify_host.subprocess, "run",
                                   side_effect=lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0)):
                VerifyHost(REMOTE).push([d])
        argv = calls[0]
        self.assertEqual(argv[0], "rsync")
        self.assertIn("--delete", argv)
        self.assertEqual(argv[-2:], [d + "/", f"op@vm.example:{d}/"])

    def test_the_output_comes_back_without_overwriting_the_local_client_log(self):
        calls = []
        with mock.patch.object(verify_host.subprocess, "run",
                               side_effect=lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0)):
            VerifyHost(REMOTE).pull("/w/.verify-out")
        argv = calls[0]
        self.assertEqual(argv[-2:], ["op@vm.example:/w/.verify-out/", "/w/.verify-out/"])
        self.assertIn("/verifier.log", argv)
        self.assertEqual(argv[argv.index("/verifier.log") - 1], "--exclude")

    def test_after_a_verify_its_cell_network_leaves_the_host(self):
        h = VerifyHost(REMOTE)
        ctx = SimpleNamespace(out="/w/.verify-out", cid="cell-x")
        with mock.patch.object(h, "pull"), mock.patch.object(h, "docker") as d:
            verify._pull(h, ctx, Path(tempfile.gettempdir()))
        d.assert_called_once_with("network", "rm", "fae-net-cell-x")

    def test_a_failed_copy_raises(self):
        with mock.patch.object(verify_host.subprocess, "run",
                               return_value=SimpleNamespace(returncode=12, stderr="broken pipe", stdout="")):
            with self.assertRaises(RuntimeError):
                VerifyHost(REMOTE).pull("/w/out")

    def test_the_container_runs_as_the_remote_user_unpinned(self):
        h = VerifyHost(REMOTE)
        h._identity = ("1001", "1002", "111")
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            (ws / "artifacts").mkdir(parents=True)
            conf = SimpleNamespace(exported={}, get=lambda k, default=None: "0-2" if k == "CPUSET_MEASURED" else None)
            argv = verify_argv(_ctx(ws), "img:1", conf, host=h)
            td = verify.teardown_argv(_ctx(ws), "img:1", conf, host=h)
        for a in (argv, td):
            self.assertFalse(any(x.startswith("--cpuset-cpus") for x in a), a)
            self.assertEqual(a[a.index("--user") + 1], "1001:1002")
            self.assertEqual(a[a.index("--group-add") + 1], "111")
            self.assertIn("CPUSET_MEASURED=", a)

    def test_up_runs_only_when_the_daemon_does_not_answer(self):
        h = VerifyHost(REMOTE, up_cmd="start-vm")
        with mock.patch.object(h, "answers", return_value=True), \
                mock.patch.object(verify_host, "run_command") as run:
            self.assertTrue(h.ensure_up(log=lambda m: None))
        run.assert_not_called()
        answers = iter([False, True])
        with mock.patch.object(h, "answers", side_effect=lambda: next(answers)), \
                mock.patch.object(verify_host, "run_command") as run:
            self.assertTrue(h.ensure_up(log=lambda m: None))
        run.assert_called_once()

    def test_an_unreachable_host_is_a_refunded_fault(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            (ws / "artifacts").mkdir(parents=True)
            infra = SimpleNamespace(conf=SimpleNamespace(
                get=lambda k, default=None: REMOTE if k == "VERIFY_DOCKER_HOST" else None), variant=None)
            with mock.patch.object(VerifyHost, "answers", return_value=False):
                v = run_verifier(_ctx(ws), infra, timeout_s=5)
        self.assertEqual((v.ok, v.stage, v.charge), (False, "verifier-host", False))


class TestTheAuthoringHostGetsEveryCoreWhenVerifiesRunElsewhere(unittest.TestCase):

    def test_split_or_all(self):
        from fae.cell import placement
        with mock.patch.object(placement, "docker_ncpu", return_value=6):
            self.assertEqual(placement.current(), placement.Placement("0-2", "3-5"))
            self.assertEqual(placement.current(verify_remote=True), placement.Placement("", "0-5"))


if __name__ == "__main__":
    unittest.main()
