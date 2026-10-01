"""fae/cell/substrate/sandbox: the throwaway container a verifier runs a
judged program in. The argv is pinned (no network, a memory and pid
ceiling, one mount); a run against no daemon is the program's failure,
not a rig error; a fresh copy never aliases the judged tree."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401

from fae.cell.substrate import sandbox


class TestTheArgv(unittest.TestCase):
    def test_no_network_a_ceiling_and_the_workdir_at_workspace(self):
        argv = sandbox.argv("img:1", "fae-x-cid", "/w", ("python3", "p.py"),
                            mounts=(("/h/tool", "/opt/tool"),))
        self.assertEqual(argv[:6], ["docker", "run", "--rm", "-i", "--name", "fae-x-cid"])
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--memory", argv)
        self.assertIn("--pids-limit", argv)
        self.assertIn("/w:/workspace", argv)
        self.assertIn("/h/tool:/opt/tool:ro", argv)
        self.assertEqual(argv[-3:], ["img:1", "python3", "p.py"])
        self.assertEqual(argv[argv.index("-w") + 1], "/workspace")

    def test_the_operators_uid_no_capabilities_no_escalation_one_core(self):
        argv = sandbox.argv("img:1", "fae-x-cid", "/w", ("true",))
        self.assertEqual(argv[argv.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertEqual(argv[argv.index("--security-opt") + 1], "no-new-privileges")
        self.assertIn("--cpus=1", argv)
        self.assertFalse(any("docker.sock" in a for a in argv))


class TestTheFreshCopy(unittest.TestCase):
    def test_it_is_rebuilt_every_time_and_is_not_the_judged_tree(self):
        with tempfile.TemporaryDirectory() as d:
            art, out = Path(d) / "artifacts", Path(d) / "out"
            art.mkdir()
            (art / "calc.py").write_text("print(1)\n")
            w1 = sandbox.fresh_copy(art, out)
            (w1 / "built").write_text("x")
            w2 = sandbox.fresh_copy(art, out)
            self.assertEqual(w1, w2)
            self.assertTrue((w2 / "calc.py").is_file())
            self.assertFalse((w2 / "built").exists())
            self.assertFalse((art / "built").exists())


@unittest.skipUnless(shutil.which("docker"), "needs a docker client on PATH")
class TestARunWithNoDaemon(unittest.TestCase):
    def test_the_client_error_is_the_programs_rc_not_a_rig_error(self):
        env = dict(os.environ, DOCKER_HOST="tcp://127.0.0.1:1")
        with tempfile.TemporaryDirectory() as d:
            out, err, rc, error = sandbox.run("img:1", "fae-sandbox-test", d, ("true",),
                                              timeout_s=30, env=env)
        self.assertIsNone(error)
        self.assertNotEqual(rc, 0)
        self.assertTrue(err)
