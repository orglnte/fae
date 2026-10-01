"""fae/cell/substrate/secrunner: the judged program's own container. What it
mounts, what it may do, and that a program inside it cannot reach the host's
records or its Docker daemon."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401

from fae.cell.substrate import secrunner


def make(tmp, **kw):
    t = Path(tmp)
    return secrunner.SecRunner(image="img:1", cid="c1", workdir=t / "run", scratch=t / "scratch",
                         argv=("python3", "-m", "uvicorn", "app.main:app"),
                         networks=("fae-net-c1", "kind"), env={"DB_DSN": "postgresql://s"},
                         log=t / "service.deploy.log", **kw)


class TestTheArgv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.argv = make(self.tmp, cpus=1).run_argv()

    def mounts(self):
        return [self.argv[i + 1] for i, a in enumerate(self.argv) if a == "-v"]

    def test_only_the_copy_and_the_scratch_are_mounted(self):
        self.assertEqual(self.mounts(), [f"{self.tmp}/run:/workspace", f"{self.tmp}/scratch:/scratch"])

    def test_no_docker_socket(self):
        self.assertFalse(any("docker.sock" in a for a in self.argv))

    def test_no_capabilities_no_escalation_the_operators_uid_and_ceilings(self):
        a = self.argv
        self.assertEqual(a[a.index("--cap-drop") + 1], "ALL")
        self.assertEqual(a[a.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(a[a.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertIn("--memory", a)
        self.assertIn("--pids-limit", a)
        self.assertIn("--cpus=1", a)
        self.assertEqual(a[a.index("--ulimit") + 1], "nofile=65536:65536")
        self.assertNotIn("--privileged", a)

    def test_its_name_its_cell_label_and_the_first_network(self):
        a = self.argv
        self.assertEqual(a[a.index("--name") + 1], "fae-secrun-c1")
        self.assertIn("fae-cell=c1", a)
        self.assertEqual(a[a.index("--network") + 1], "fae-net-c1")

    def test_env_and_program_come_last(self):
        a = self.argv
        self.assertIn("HOME=/scratch", a)
        self.assertIn("USER=fae", a)
        self.assertIn("DB_DSN=postgresql://s", a)
        self.assertEqual(a[-5:], ["img:1", "python3", "-m", "uvicorn", "app.main:app"])

    def test_pinned_only_when_asked(self):
        self.assertFalse(any(x.startswith("--cpuset-cpus") for x in self.argv))
        self.assertIn("--cpuset-cpus=0-3", make(self.tmp, cpuset="0-3").run_argv())

    def test_no_network_means_none(self):
        r = make(self.tmp)
        r.networks = ()
        a = r.run_argv()
        self.assertEqual(a[a.index("--network") + 1], "none")


class TestStartAndStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_start_connects_the_other_networks_and_stop_keeps_the_logs(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["docker", "logs"]:
                return mock.Mock(returncode=0, stdout="started\nGET /health 200\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        r = make(self.tmp)
        with mock.patch.object(secrunner.subprocess, "run", run):
            self.assertIsNone(r.start())
            r.stop()
        self.assertIn(["docker", "network", "connect", "kind", "fae-secrun-c1"], calls)
        self.assertEqual(calls[-1], ["docker", "rm", "-f", "fae-secrun-c1"])
        self.assertEqual((Path(self.tmp) / "service.deploy.log").read_text(),
                         "started\nGET /health 200\n")
        self.assertTrue((Path(self.tmp) / "scratch").is_dir())

    def test_a_failed_start_says_why(self):
        def run(argv, **kw):
            if argv[:3] == ["docker", "run", "-d"]:
                return mock.Mock(returncode=125, stdout="", stderr="no such image\n")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(secrunner.subprocess, "run", run):
            self.assertIn("no such image", make(self.tmp).start())


def _docker_answers():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


HOSTILE = r'''
import os, socket, sys
results = []
try:
    s = socket.socket(socket.AF_UNIX); s.connect("/var/run/docker.sock"); results.append("socket=REACHED")
except OSError:
    results.append("socket=unreachable")
target = os.environ["HOST_LEDGER"]
try:
    with open(target, "a") as f:
        f.write("forged\n")
    results.append("ledger-path=written-inside-the-container-only")
except OSError:
    results.append("ledger-path=unwritable")
print(" ".join(results), flush=True)
'''


@unittest.skipUnless(_docker_answers(), "needs a Docker daemon")
class TestAHostileProgramReachesNothingOfTheHost(unittest.TestCase):
    """Negative control: the judged program tries the Docker socket and the
    host's ledger path; neither reaches the host."""

    def test_the_socket_is_absent_and_the_host_ledger_is_unchanged(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as d:
            d = Path(d)
            ledger = d / "ws" / "iterations.log"
            ledger.parent.mkdir()
            ledger.write_text("the ledger\n")
            (d / "run").mkdir()
            (d / "run" / "hostile.py").write_text(HOSTILE)
            r = secrunner.SecRunner(image="python:3.12-slim", cid=f"selftest-{os.getpid()}",
                              workdir=d / "run", scratch=d / "scratch",
                              argv=("python3", "hostile.py"), env={"HOST_LEDGER": str(ledger)},
                              log=d / "out.log")
            self.assertIsNone(r.start())
            deadline = time.time() + 60
            while r.alive() and time.time() < deadline:
                time.sleep(0.5)
            r.stop()
            out = (d / "out.log").read_text()
            self.assertIn("socket=unreachable", out)
            self.assertEqual(ledger.read_text(), "the ledger\n")


if __name__ == "__main__":
    unittest.main()
