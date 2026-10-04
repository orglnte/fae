"""fae/cell/verify.py — the boundary: run_verifier hands a Ctx to the
experiment's verifier inside a container of the variant's image and reads
one Verdict back; every way the child can fail is a refunded rig fault, and
the container — everything the verifier started — is removed with it."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

sys.path.insert(0, str(ROOT))
from fae import experiment as _experiment  # noqa: E402
from fae.cell import image as _image  # noqa: E402
from fae.cell import verify  # noqa: E402
from fae.cell.verify import Ctx, Verdict, run_verifier, verify_argv  # noqa: E402
from fae.cell.infra.base import NoopInfra  # noqa: E402
from fae.cell.variants.base import Variant  # noqa: E402


def _definition(root, verifier_body):
    """A minimal experiment root whose verifier is `verifier_body`."""
    d = Path(root) / "experiment"
    d.mkdir()
    (d / "__init__.py").write_text("NAME = 'mini'\ndef variant_classes():\n    return ()\n"
                                   "def verifier_class():\n    from .verifier import V\n    return V\n")
    (d / "verifier.py").write_text(textwrap.dedent(verifier_body))
    (d / "verifier.Dockerfile").write_text("FROM python:3.12.3-slim\n")
    return d


class _Shim:
    def __init__(self, cid, ws, root):
        self.cid, self.ws, self.root, self.conf, self.variant = cid, ws, root, None, "only"


class RunnerCase(unittest.TestCase):
    """The docker client is mocked: a fake Popen that plays the child's
    part by writing (or not writing) the verdict file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out = self.root / "ws" / "cell-x"
        (self.out / "artifacts").mkdir(parents=True)
        self.calls = []

    def ctx(self, exp):
        return Ctx(root=str(ROOT), experiment_dir=str(exp), workspace=str(self.out),
                   artifacts=str(self.out / "artifacts"), out=str(self.out), cid="cell-x",
                   task="T1", variant="only", arrangement="A")

    def variant(self):
        return NoopInfra(Variant, _Shim("cell-x", self.out, ROOT))

    def _popen(self, child):
        """A Popen whose wait() runs `child(argv)` -> rc, or raises."""
        calls = self.calls

        class FakePopen:
            def __init__(self, argv, **kw):
                calls.append(("popen", argv, kw))
                self.argv, self.pid = argv, 4242

            def wait(self, timeout=None):
                return child(self.argv)

            def kill(self):
                calls.append(("kill",))

        return FakePopen

    def run_with(self, exp, child, timeout_s=60):
        def fake_run(argv, **kw):
            self.calls.append(("run", list(argv)))
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(_image, "for_variant", return_value="fae-mini-verifier:abc"), \
                mock.patch.object(_image.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(verify.subprocess, "Popen", self._popen(child)):
            return run_verifier(self.ctx(exp), self.variant(), timeout_s=timeout_s)

    def _child_writes(self, verdict):
        def child(argv):
            out = Path(argv[argv.index("--out") + 1])
            out.write_text(verdict.to_json())
            return 0 if verdict.ok else 1
        return child


class TestTheRoundTrip(RunnerCase):
    def test_a_verdict_comes_back_whole(self):
        exp = _definition(self.root, "")
        want = Verdict(ok=True, metrics={"cases": 3, "passed": 3}, arrangement="A",
                       files=("a.log",), seconds=0.5)
        v = self.run_with(exp, self._child_writes(want))
        self.assertEqual(v, want)

    def test_a_refund_and_a_stand_down_survive_the_wire(self):
        exp = _definition(self.root, "")
        want = Verdict(ok=False, stage="contract", charge=False,
                       stand_down=("the world is not the documented one",))
        v = self.run_with(exp, self._child_writes(want))
        self.assertFalse(v.charge)
        self.assertEqual(v.stand_down, ("the world is not the documented one",))

    def test_the_ctx_is_written_where_the_child_reads_it(self):
        exp = _definition(self.root, "")
        seen = {}

        def child(argv):
            seen["ctx"] = Ctx.from_json(Path(argv[argv.index("--ctx") + 1]).read_text())
            return 1
        self.run_with(exp, child)
        self.assertEqual((seen["ctx"].cid, seen["ctx"].variant, seen["ctx"].task),
                         ("cell-x", "only", "T1"))


class TestTheChildIsAContainer(RunnerCase):
    def test_the_argv_is_a_docker_run_of_the_variants_image(self):
        exp = _definition(self.root, "")
        self.run_with(exp, self._child_writes(Verdict(ok=True)))
        argv = next(c[1] for c in self.calls if c[0] == "popen")
        self.assertEqual(argv[:4], ["docker", "run", "--rm", "--name"])
        self.assertEqual(argv[4], "fae-verify-cell-x")
        self.assertIn("--user", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "fae-net-cell-x")
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", argv)
        img = argv.index("fae-mini-verifier:abc")
        self.assertEqual(argv[img + 1:img + 4], ["python3", "-m", "fae.cell.verify_child"])
        self.assertEqual(argv[-4], "--ctx")
        self.assertEqual(argv[-2], "--out")

    def test_every_root_is_mounted_at_its_own_path(self):
        exp = _definition(self.root, "")
        argv = verify_argv(self.ctx(exp), "img:1")
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertIn(f"{ROOT}:{ROOT}:ro", mounts)
        self.assertIn(f"{self.out}:{self.out}", mounts)
        self.assertEqual(len(mounts), len(set(mounts)))

    def test_only_the_verifys_own_directory_is_writable_and_nothing_contains_it(self):
        exp = _definition(self.root, "")
        ws = self.out
        (ws / "artifacts").mkdir(exist_ok=True)
        (ws / "iterations.log").write_text("")
        (ws / ".verify-out").mkdir(exist_ok=True)
        out = ws / ".verify-out"
        ctx = Ctx(root=str(ROOT), experiment_dir=str(exp), workspace=str(ws),
                  artifacts=str(ws / "artifacts"), out=str(out), cid="cell-x",
                  task="T1", variant="only", arrangement="A")
        for argv in (verify_argv(ctx, "img:1"), verify.teardown_argv(ctx, "img:1")):
            mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"
                      and "docker.sock" not in argv[i + 1]]
            paths = [m.split(":")[0] for m in mounts]
            writable = [m for m in mounts if not m.endswith(":ro")]
            self.assertEqual(writable, [f"{out}:{out}"])
            # a writable bind inside a read-only one vanishes on Docker Desktop
            self.assertFalse([p for p in paths if p != str(out) and str(out).startswith(p + "/")])
            self.assertNotIn(str(ws), paths)
            self.assertIn(f"{ws}/artifacts:{ws}/artifacts:ro", mounts)
            self.assertIn(f"{ws}/iterations.log:{ws}/iterations.log:ro", mounts)

    def test_a_reverify_leaves_out_the_workspace_entry_that_holds_its_directory(self):
        exp = _definition(self.root, "")
        ws = self.out
        out = ws / "reverify" / "t1" / ".verify-out"
        out.mkdir(parents=True, exist_ok=True)
        ctx = Ctx(root=str(ROOT), experiment_dir=str(exp), workspace=str(ws),
                  artifacts=str(ws / "artifacts"), out=str(out), cid="cell-x",
                  task="T1", variant="only", arrangement="A")
        paths = [m[0] for m in verify.verify_mounts(ctx, root_reads=())]
        self.assertNotIn(str(ws / "reverify"), paths)
        self.assertIn(str(out), paths)

    def test_an_experiment_outside_the_root_is_mounted_too(self):
        # unmounted, the child cannot load the definition and every verify halts
        exp = _definition(self.root, "")
        self.assertFalse(str(exp).startswith(str(ROOT)))
        for argv in (verify_argv(self.ctx(exp), "img:1"),
                     verify.teardown_argv(self.ctx(exp), "img:1")):
            mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
            self.assertTrue(any(str(exp).startswith(m.split(":")[0]) for m in mounts), mounts)

    def test_the_environment_is_the_drivers_minus_the_hosts_own_and_secrets(self):
        exp = _definition(self.root, "")
        environ = {"PATH": "/host/bin", "HOME": "/Users/x", "AGENT": "opus",
                   "CLAUDE_CODE_OAUTH_TOKEN": "sk-1", "OPENCODE_API_KEY": "k",
                   "NAMESPACE": "ns-x"}
        argv = verify_argv(self.ctx(exp), "img:1", environ=environ)
        env = dict(a.split("=", 1) for i, a in enumerate(argv) if i and argv[i - 1] == "-e")
        self.assertEqual(env["AGENT"], "opus")
        self.assertEqual(env["NAMESPACE"], "ns-x")
        self.assertEqual(env["REPO_ROOT"], str(ROOT))
        self.assertEqual(env["FAE_VERIFY_CONTAINER"], "fae-verify-cell-x")
        self.assertEqual(env["FAE_CELL_NET"], "fae-net-cell-x")
        self.assertTrue(env["HOME"].startswith(str(self.out)))
        self.assertEqual(env["USER"], "fae")      # the uid has no passwd entry in the image
        self.assertNotIn("PATH", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("OPENCODE_API_KEY", env)

    def test_the_image_is_logged_before_the_child_starts(self):
        exp = _definition(self.root, "")
        self.run_with(exp, self._child_writes(Verdict(ok=True)))
        log = (self.out / "verifier.log").read_text()
        self.assertIn("image=fae-mini-verifier:abc", log)
        self.assertIn("container=fae-verify-cell-x", log)


class TestEveryFailureIsARefundedRigFault(RunnerCase):
    def test_a_crash_is_stage_verifier_uncharged(self):
        exp = _definition(self.root, "")
        v = self.run_with(exp, lambda argv: 1)
        self.assertEqual((v.ok, v.stage, v.charge), (False, "verifier", False))
        self.assertIn("without a verdict", v.why)

    def test_an_unreadable_verdict_is_stage_verifier(self):
        exp = _definition(self.root, "")

        def child(argv):
            Path(argv[argv.index("--out") + 1]).write_text("{not json")
            return 0
        v = self.run_with(exp, child)
        self.assertEqual((v.stage, v.charge), ("verifier", False))

    def test_a_hang_removes_the_container(self):
        exp = _definition(self.root, "")

        def child(argv):
            raise subprocess.TimeoutExpired(argv, 2)
        v = self.run_with(exp, child, timeout_s=2)
        self.assertEqual((v.stage, v.charge), ("verifier-timeout", False))
        rms = [c[1] for c in self.calls if c[0] == "run" and c[1][:3] == ["docker", "rm", "-f"]]
        self.assertTrue(any("fae-verify-cell-x" in r for r in rms),
                        "the container was not removed on timeout")
        self.assertIn(("kill",), self.calls)

    def test_the_container_is_removed_on_the_normal_path_too(self):
        exp = _definition(self.root, "")
        self.run_with(exp, self._child_writes(Verdict(ok=True)))
        rms = [c[1] for c in self.calls if c[0] == "run" and c[1][:3] == ["docker", "rm", "-f"]]
        self.assertTrue(any("fae-verify-cell-x" in r for r in rms))

    def test_a_verify_that_did_not_end_on_its_own_is_torn_down(self):
        """A timeout, a signal (SIGTERM reaches the cell as KeyboardInterrupt)
        or any exception: what the verify started beside its container is
        removed by the full teardown; a verify that ended runs none."""
        exp = _definition(self.root, "")

        def hang(argv):
            raise subprocess.TimeoutExpired(argv, 2)

        raised = []

        def signalled(argv):
            # the signal interrupts the first wait; the cleanup's wait then returns
            if not raised:
                raised.append(1)
                raise KeyboardInterrupt("signal 15")
            return -15
        with mock.patch.object(verify, "run_teardown") as td:
            self.run_with(exp, hang, timeout_s=2)
        self.assertEqual(td.call_count, 1)
        with mock.patch.object(verify, "run_teardown") as td:
            with self.assertRaises(KeyboardInterrupt):
                self.run_with(exp, signalled)
        self.assertEqual(td.call_count, 1)
        with mock.patch.object(verify, "run_teardown") as td:
            self.run_with(exp, self._child_writes(Verdict(ok=True)))
            self.run_with(exp, lambda argv: 1)
        self.assertEqual(td.call_count, 0)

    def test_no_image_is_a_refunded_fault_not_a_verdict(self):
        exp = _definition(self.root, "")
        with mock.patch.object(_image, "for_variant", side_effect=RuntimeError("docker build failed")):
            v = run_verifier(self.ctx(exp), self.variant(), timeout_s=5)
        self.assertEqual((v.ok, v.stage, v.charge), (False, "verifier-image", False))


class TestTheTeardownChild(unittest.TestCase):
    """`verify_child --teardown`: the runner stopped, the infra's teardown,
    then the verifier's own, even when the infra's raises."""

    def test_the_verifier_tears_down_what_it_provisioned_after_the_infra(self):
        d = _experiment.definition()
        vid = sorted(d.variants)[0]
        order = []
        with tempfile.TemporaryDirectory() as t:
            ctx = Ctx(root=str(ROOT), experiment_dir=str(d.path), workspace=t,
                      artifacts=t, out=t, cid="cell-x", task="T1", variant=vid)
            (Path(t) / "ctx.json").write_text(ctx.to_json())
            vcls = d.variant(vid)

            def infra_down(self, ctx, env):
                order.append("infra")
                raise RuntimeError("cluster delete failed")
            with mock.patch("fae.cell.infra.secrunner.stop_by_name",
                            side_effect=lambda *a, **k: order.append("runner")), \
                    mock.patch.object(vcls.INFRA, "verify_teardown", infra_down), \
                    mock.patch.object(d.verifier_class(), "teardown",
                                      lambda self, ctx: order.append("verifier")):
                with self.assertRaises(RuntimeError):
                    verify.main(["--ctx", str(Path(t) / "ctx.json"), "--teardown"])
        self.assertEqual(order, ["runner", "infra", "verifier"])


class TestTheChildIsItsOwnSession(unittest.TestCase):
    def test_the_runner_starts_a_new_session_and_hands_over_no_fd(self):
        src = (Path(ROOT) / "fae" / "cell" / "verify.py").read_text()
        body = src[src.index("def run_verifier("):src.index("def _end(")]
        self.assertIn("start_new_session=True", body)
        self.assertNotIn("pass_fds", body)
        self.assertNotIn("close_fds=False", body)

    def test_the_child_argv_reads_as_a_driver_to_the_reaper(self):
        from fae.driver.conduct import zombies
        self.assertIn("fae.cell", " ".join(verify.CHILD_ARGV))
        with mock.patch.object(zombies.common, "sh", return_value=" ".join(verify.CHILD_ARGV)):
            self.assertTrue(zombies._is_driver_pid(1))

    def test_the_runner_never_names_the_hosts_interpreter(self):
        # sys.executable is the HOST's python; the child runs the image's.
        src = (Path(ROOT) / "fae" / "cell" / "verify.py").read_text()
        body = src[src.index("CHILD_ARGV"):src.index("def main(")]
        self.assertNotIn("sys.executable", body)


def _docker_reachable():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class TestTheDeclaredImage(unittest.TestCase):
    def test_the_fixture_verifier_declares_a_buildable_image(self):
        """A verifier whose IMAGE_DIR is missing voids every verify; the tag
        must resolve without a daemon."""
        cls = _experiment.definition().verifier_class()
        self.assertTrue(cls.IMAGE_DIR and (Path(cls.IMAGE_DIR) / "Dockerfile").is_file())
        self.assertRegex(_image.tag("fixture-verifier", cls.IMAGE_DIR, cls.image_context(None)),
                         r"^fae-fixture-verifier:[0-9a-f]{12}$")


@unittest.skipUnless(_docker_reachable(), "needs the docker daemon")
class TestTheRealRoundTrip(unittest.TestCase):
    """The fixture experiment's verifier, in its own image, over the wire:
    the one test that proves the container path end to end."""

    def test_the_fixture_verifier_answers_from_inside_its_image(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "ws" / "real-x"
        (out / "artifacts").mkdir(parents=True)
        (out / "artifacts" / "answer.txt").write_text("42\n")
        fixture = Path(_experiment.definition().path)      # the suite's own experiment
        ctx = Ctx(root=str(ROOT), experiment_dir=str(fixture), workspace=str(out),
                  artifacts=str(out / "artifacts"), out=str(out), cid="real-x",
                  task="T1", variant="alpha", arrangement="G1")
        _image.network_up("real-x")
        self.addCleanup(_image.network_down, "real-x")
        v = run_verifier(ctx, NoopInfra(Variant, _Shim("real-x", out, ROOT)), timeout_s=600)
        self.assertTrue(v.ok, (v, (out / "verifier.log").read_text()))
        self.assertEqual(v.metrics, {"answer": "42"})
        self.assertIn("image=fae-fixture-verifier:", (out / "verifier.log").read_text())


class TestTheWire(unittest.TestCase):
    def test_json_round_trip(self):
        v = Verdict(ok=True, stage="x", why="y", charge=False, stand_down=("a",),
                    metrics={"k": None}, arrangement="G1", seconds=1.5, files=("f",))
        self.assertEqual(Verdict.from_json(v.to_json()), v)
        c = Ctx(root="/r", experiment_dir="/r/experiment", workspace="/w", artifacts="/w/a",
                out="/w", cid="c", task="T1", variant="s", arrangement=None, expected_fp="abc")
        self.assertEqual(Ctx.from_json(c.to_json()), c)


if __name__ == "__main__":
    unittest.main()


class TestTheTeardownRunsInAFreshContainer(RunnerCase):
    """After a kill the verify container is gone; what the arrangement
    provisioned outside it is torn down by the variant's own
    verify_teardown, in a new container of the same image."""

    def test_the_argv_is_the_teardown_flag_in_the_variants_image(self):
        exp = _definition(self.root, "")
        argv = verify.teardown_argv(self.ctx(exp), "fae-mini-verifier:abc")
        self.assertEqual(argv[:5], ["docker", "run", "--rm", "--name", "fae-verify-cell-x-teardown"])
        self.assertEqual(argv[argv.index("--network") + 1], "fae-net-cell-x")
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", argv)
        self.assertEqual(argv[argv.index("--add-host") + 1], "host.docker.internal:host-gateway")
        img = argv.index("fae-mini-verifier:abc")
        self.assertEqual(argv[img + 1:img + 5], ["python3", "-m", "fae.cell.verify_child", "--teardown"])
        self.assertEqual(argv[-2], "--ctx")
        self.assertTrue(argv[-1].endswith("/teardown.ctx.json"))

    def test_the_verify_argv_carries_the_host_alias_too(self):
        exp = _definition(self.root, "")
        argv = verify_argv(self.ctx(exp), "fae-mini-verifier:abc")
        self.assertEqual(argv[argv.index("--add-host") + 1], "host.docker.internal:host-gateway")

    def test_the_verifiers_cpu_cap_is_the_containers(self):
        exp = _definition(self.root, "")
        self.assertFalse(any(a.startswith("--cpus") for a in verify_argv(self.ctx(exp), "img:1")),
                         "no cap declared, none applied")
        argv = verify_argv(self.ctx(exp), "img:1", cpus=2)
        self.assertIn("--cpus=2", argv)
        self.assertLess(argv.index("--cpus=2"), argv.index("img:1"))

    def test_the_verify_is_pinned_only_when_the_config_asks(self):
        from types import SimpleNamespace
        exp = _definition(self.root, "")
        off = SimpleNamespace(exported={}, get=lambda k, d=None: d)
        on = SimpleNamespace(exported={}, get=lambda k, d=None: "0-3" if k == "CPUSET_MEASURED" else d)
        self.assertFalse(any(a.startswith("--cpuset-cpus") for a in
                             verify_argv(self.ctx(exp), "img:1", off)))
        self.assertIn("--cpuset-cpus=0-3", verify_argv(self.ctx(exp), "img:1", on))

    def test_run_verifier_applies_the_declared_cap(self):
        # one process, one experiment: the class the runner reads is the
        # loaded definition's verifier, so its cap is patched there
        exp = _definition(self.root, "")
        with mock.patch.object(_experiment.definition().verifier_class(), "CPUS", 2):
            self.run_with(exp, self._child_writes(Verdict(ok=True)))
        argv = next(c[1] for c in self.calls if c[0] == "popen")
        self.assertIn("--cpus=2", argv)

    def test_run_teardown_writes_the_ctx_runs_the_child_and_removes_the_container(self):
        exp = _definition(self.root, "")
        seen = []

        class FakePopen:
            def __init__(self, argv, **kw):
                seen.append(("popen", list(argv)))
                self.pid = 1

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        with mock.patch.object(_image, "for_variant", return_value="fae-mini-verifier:abc"), \
                mock.patch.object(_image, "remove_container", side_effect=lambda n: seen.append(("rm", n))), \
                mock.patch.object(verify.subprocess, "Popen", FakePopen):
            rc = verify.run_teardown(self.ctx(exp), self.variant())
        self.assertEqual(rc, 0)
        self.assertEqual([k for k, _ in seen], ["rm", "popen", "rm"])
        ctx_file = self.out / ".verifier" / "teardown.ctx.json"
        self.assertTrue(ctx_file.is_file())
        self.assertIn("--teardown", seen[1][1])
        self.assertIn("teardown start image=fae-mini-verifier:abc", (self.out / "verifier.log").read_text())

    def test_the_teardown_child_stops_the_runner_before_the_infras_teardown(self):
        d = _experiment.definition()
        cls = d.variant("alpha_apidocs")
        ctx = Ctx(root=str(ROOT), experiment_dir=str(d.path), workspace=str(self.out),
                  artifacts=str(self.out / "artifacts"), out=str(self.out), cid="cell-x",
                  task="T1", variant="alpha_apidocs", arrangement="A")
        (self.out / "t.json").write_text(ctx.to_json())
        order = []
        from fae.cell.infra import secrunner
        with mock.patch.object(secrunner, "stop_by_name",
                               side_effect=lambda cid, w, log=None: order.append(("stop", cid, log.name))), \
                mock.patch.object(cls.INFRA, "verify_teardown",
                                  lambda self, c, env: order.append(("teardown", type(self).__name__))):
            self.assertEqual(verify.main(["--teardown", "--ctx", str(self.out / "t.json")]), 0)
        self.assertEqual(order, [("stop", "cell-x", d.verifier_class().RUN_LOG),
                                 ("teardown", cls.INFRA.__name__)])

    def test_no_image_is_no_container_and_no_crash(self):
        exp = _definition(self.root, "")
        with mock.patch.object(_image, "for_variant", side_effect=RuntimeError("no daemon")):
            self.assertIsNone(verify.run_teardown(self.ctx(exp), self.variant()))
