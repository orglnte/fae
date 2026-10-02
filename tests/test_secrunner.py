"""fae/cell/substrate/secrunner: the judged program's own container. What it
mounts, what it may do, its one lifecycle (start, wait or use, stop) for a
program that ends and one that serves alike, the variant built on it, and
that a program inside it cannot reach the host's records or its Docker
daemon."""
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
        self.argv = make(self.tmp, cpus=1).create_argv()

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
        self.assertEqual(a[:5], ["docker", "create", "--pull", "never", "--name"])
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
        self.assertIn("--cpuset-cpus=0-3", make(self.tmp, cpuset="0-3").create_argv())

    def test_no_network_means_none(self):
        r = make(self.tmp)
        r.networks = ()
        a = r.create_argv()
        self.assertEqual(a[a.index("--network") + 1], "none")

    def test_without_scratch_home_is_the_workspace_and_extras_are_read_only(self):
        r = make(self.tmp, mounts=(("/h/tool", "/opt/tool"),))
        r.scratch = None
        a = r.create_argv()
        mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
        self.assertEqual(mounts, [f"{self.tmp}/run:/workspace", "/h/tool:/opt/tool:ro"])
        self.assertIn("HOME=/workspace", a)

    def test_stdin_keeps_it_open_for_an_attached_start(self):
        self.assertNotIn("-i", self.argv)
        a = make(self.tmp).create_argv(stdin=True)
        self.assertEqual(a[a.index("-i") + 1:a.index("-i") + 3], ["-a", "stdin"])


class TestTheFreshCopy(unittest.TestCase):
    def test_it_is_rebuilt_every_time_and_is_not_the_judged_tree(self):
        with tempfile.TemporaryDirectory() as d:
            art, out = Path(d) / "artifacts", Path(d) / "out"
            art.mkdir()
            (art / "calc.py").write_text("print(1)\n")
            w1 = secrunner.fresh_copy(art, out)
            (w1 / "built").write_text("x")
            w2 = secrunner.fresh_copy(art, out)
            self.assertEqual(w1, w2)
            self.assertTrue((w2 / "calc.py").is_file())
            self.assertFalse((w2 / "built").exists())
            self.assertFalse((art / "built").exists())


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

    def test_run_returns_the_exit_code_and_the_output_and_removes_it(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["docker", "wait"]:
                return mock.Mock(returncode=0, stdout="3\n", stderr="")
            if argv[:2] == ["docker", "logs"]:
                return mock.Mock(returncode=0, stdout="plan: 2 to add\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        r = make(self.tmp, cwd="/workspace/infra")
        r.log = None
        with mock.patch.object(secrunner.subprocess, "run", run):
            self.assertEqual(r.run(60), (3, "plan: 2 to add\n"))
        self.assertEqual(calls[-1], ["docker", "rm", "-f", "fae-secrun-c1"])
        self.assertEqual(r.create_argv()[r.create_argv().index("-w") + 1], "/workspace/infra")

    def test_a_failed_start_says_why(self):
        def run(argv, **kw):
            if argv[:2] == ["docker", "create"]:
                return mock.Mock(returncode=125, stdout="", stderr="no such image\n")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(secrunner.subprocess, "run", run):
            self.assertIn("no such image", make(self.tmp).start())



class TestTheVariant(unittest.TestCase):
    """SecRunnerVariant: an experiment declares the runtime and the commands;
    the engine runs them in the runner with no network and the tight caps."""

    class Prog(secrunner.SecRunnerVariant):
        ARM = TECH = "prog"
        IMAGE = "img:1"
        RUN = ("python3", "p.py")

    def test_the_runner_has_no_network_and_the_tight_caps(self):
        r = self.Prog.runner("c1", "/w", self.Prog.RUN)
        a = r.create_argv()
        self.assertEqual(a[a.index("--network") + 1], "none")
        self.assertEqual(a[a.index("--memory") + 1], secrunner.RUN_MEMORY)
        self.assertEqual(a[a.index("--pids-limit") + 1], str(secrunner.RUN_PIDS))
        self.assertIn("--cpus=1", a)
        self.assertNotIn("--ulimit", a)
        self.assertEqual(a[-3:], ["img:1", "python3", "p.py"])

    def test_run_answers_stdout_stderr_code_and_no_error(self):
        with mock.patch.object(secrunner.SecRunner, "run", return_value=(0, "42\n", "")) as m:
            self.assertEqual(self.Prog.run("c1", "/w", self.Prog.RUN, "6*7\n", 20),
                             ("42\n", "", 0, None))
        self.assertEqual(m.call_args.kwargs, {"stdin": "6*7\n", "split": True})

    def test_a_run_that_did_not_end_is_an_error_not_an_exit_code(self):
        with mock.patch.object(secrunner.SecRunner, "run",
                               return_value=(None, "", "timed out after 20s")):
            self.assertEqual(self.Prog.run("c1", "/w", self.Prog.RUN, "", 20),
                             ("", "", None, "timed out after 20s"))

    def test_it_declares_a_liveness_probe(self):
        from fae.cell.variants.base import liveness_declared
        self.assertTrue(liveness_declared(self.Prog))

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


@unittest.skipUnless(_docker_answers(), "needs a Docker daemon")
class TestOneLifecycleOnARealDaemon(unittest.TestCase):
    """A program that ends and one that serves go through the same start,
    wait or use, and stop; nothing is left behind."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        (self.d / "run").mkdir()

    def runner(self, argv, **kw):
        return secrunner.SecRunner(image="python:3.12-slim", cid=f"selftest-{os.getpid()}",
                                   workdir=self.d / "run", argv=argv, **kw)

    def tearDown(self):
        subprocess.run(["docker", "rm", "-f", f"fae-secrun-selftest-{os.getpid()}"],
                       capture_output=True)

    def gone(self):
        p = subprocess.run(["docker", "ps", "-aq", "--filter",
                            f"name=fae-secrun-selftest-{os.getpid()}"],
                           capture_output=True, text=True)
        return p.stdout.strip() == ""

    def test_stdin_in_stdout_and_stderr_apart_and_the_exit_code(self):
        r = self.runner(("python3", "-c", "import sys; l = sys.stdin.read(); print(eval(l)); "
                                          "print('to stderr', file=sys.stderr); sys.exit(3)"))
        self.assertEqual(r.run(30, stdin="6 * 7\n", split=True), (3, "42\n", "to stderr\n"))
        self.assertTrue(self.gone())

    def test_a_program_past_its_timeout_is_removed(self):
        r = self.runner(("python3", "-c", "import time; time.sleep(60)"))
        self.assertEqual(r.run(2, stdin="", split=True), (None, "", "timed out after 2s"))
        self.assertTrue(self.gone())

    def test_a_service_runs_until_stopped_and_its_output_is_kept(self):
        r = self.runner(("python3", "-c", "import time; print('up', flush=True); time.sleep(60)"),
                        log=self.d / "service.log")
        self.assertIsNone(r.start())
        deadline = time.time() + 20
        while "up" not in (r.output() or "") and time.time() < deadline:
            time.sleep(0.3)
        self.assertTrue(r.alive())
        r.stop()
        self.assertEqual((self.d / "service.log").read_text(), "up\n")
        self.assertTrue(self.gone())

    def test_a_missing_image_is_not_pulled(self):
        r = self.runner(("true",))
        r.image = "fae-no-such-image:0"
        rc, why = r.run(20)
        self.assertIsNone(rc)
        self.assertIn("No such image", why)
