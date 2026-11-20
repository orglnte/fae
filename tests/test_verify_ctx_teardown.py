"""Enriched verify Ctx (variant_def, conf, infra, runner, fixtures, container
metadata) and two-phase crash teardown ordering."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401

import fae.experiment
from fae.cell import image as _image
from fae.cell import verify, verify_timings as _timings
from fae.cell.infra.base import Infra, NoopInfra
from fae.cell.verify import Ctx, Fixture, RunnerHelper, Verdict
from fae.cell.verify_host import VerifyHost
from fae.cell.verify_timings import HostTimings
from fae.experiment.variants.base import Variant


class _DummyInfra(Infra):
    def alive(self):
        return True


class _DummyVariant(Variant):
    ID = "only"
    INFRA = _DummyInfra
    RUN = {"command": ["python3", "app.py"], "image": "python:3.12-slim"}


class TestCtxWireAndRuntimeBindings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "art").mkdir()
        (self.root / "art" / "a.txt").write_text("hi")
        (self.root / "out").mkdir()

    def _ctx(self):
        return Ctx(
            root=str(self.root),
            experiment_dir=str(self.root / "exp"),
            workspace=str(self.root),
            artifacts=str(self.root / "art"),
            out=str(self.root / "out"),
            cid="c1",
            task="T1",
            variant="only",
            arrangement="A",
        )

    def test_runtime_fields_excluded_from_wire_json(self):
        ctx = self._ctx()
        ctx.variant_def = _DummyVariant
        ctx.conf = {"K": "V"}
        ctx.infra = _DummyInfra(_DummyVariant, cid="c1", ws=self.root, root=self.root)
        ctx.runner = RunnerHelper(ctx)
        ctx.verify_container = "fae-verify-c1"
        ctx.cell_network = "fae-net-c1"

        payload = json.loads(ctx.to_json())
        for runtime_key in ("variant_def", "conf", "infra", "substrate", "runner",
                            "verify_container", "cell_network", "_fixtures"):
            self.assertNotIn(runtime_key, payload)
        self.assertEqual(Ctx.from_json(ctx.to_json()), ctx)

    def test_public_now_alias(self):
        self.assertIs(verify._now, verify.now)
        self.assertRegex(verify.now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_runner_helper_copies_and_delegates_to_secrunner(self):
        ctx = self._ctx()
        ctx.variant_def = _DummyVariant
        helper = RunnerHelper(ctx)
        self.assertEqual(helper.name, "fae-secrun-c1")
        copied = helper.fresh_copy("work")
        self.assertEqual((copied / "a.txt").read_text(), "hi")
        sr = helper.for_variant(copied, argv=["echo", "ok"])
        self.assertEqual(sr.cid, "c1")
        self.assertEqual(sr.argv, ("echo", "ok"))
        self.assertEqual(sr.image, "python:3.12-slim")

    def test_fixtures_attach_labels_and_cleanup_lifo(self):
        ctx = self._ctx()
        ctx.cell_network = "fae-net-c1"
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(verify.subprocess, "run", side_effect=fake_run):
            fx1 = ctx.start_fixture("fae-store-c1", "postgres:16", env={"POSTGRES_PASSWORD": "pw"})
            self.assertIsInstance(fx1, Fixture)
            ctx.run_fixture("postgres:16", argv=("pgbench", "-V"))
            ctx.stop_fixtures()

        # First call removes stale container, second starts detached fixture
        start_argv = calls[1]
        self.assertEqual(start_argv[:3], ["docker", "run", "-d"])
        self.assertIn("fae-cell=c1", start_argv)
        self.assertIn("fae-role=fixture", start_argv)
        self.assertEqual(start_argv[start_argv.index("--network") + 1], "fae-net-c1")
        self.assertIn("POSTGRES_PASSWORD=pw", start_argv)

        # Third call is one-shot --rm fixture
        oneshot = calls[2]
        self.assertEqual(oneshot[:3], ["docker", "run", "--rm"])
        self.assertIn("fae-cell=c1", oneshot)
        self.assertIn("fae-role=fixture", oneshot)

        # Fourth call removes tracked fixture on stop_fixtures()
        self.assertEqual(calls[3], ["docker", "rm", "-f", "-v", "fae-store-c1"])

    def test_verify_main_binds_ctx_and_stops_fixtures_even_on_error(self):
        seen = {}
        stopped = []

        class _Ver:
            FILES = ("v.log",)

            def verify(self, ctx):
                seen["variant_def"] = ctx.variant_def
                seen["infra"] = ctx.infra
                seen["runner"] = ctx.runner
                seen["verify_container"] = ctx.verify_container
                seen["cell_network"] = ctx.cell_network
                ctx._fixtures.append(SimpleNamespace(stop=lambda: stopped.append("fx")))
                raise RuntimeError("boom")

        defn = SimpleNamespace(
            variants={"only": _DummyVariant},
            variant=lambda vid: _DummyVariant if vid == "only" else None,
            verifier_class=lambda: _Ver,
        )
        ctx = self._ctx()
        ctx_path = self.root / "out" / "ctx.json"
        out_path = self.root / "out" / "verdict.json"
        ctx_path.write_text(ctx.to_json())
        env = {"FAE_VERIFY_CONTAINER": "fae-verify-c1", "FAE_CELL_NET": "fae-net-c1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(fae.experiment._experiment, "load", return_value=defn):
            with self.assertRaises(RuntimeError):
                verify.main(["--ctx", str(ctx_path), "--out", str(out_path)])
        self.assertIs(seen["variant_def"], _DummyVariant)
        self.assertIsInstance(seen["infra"], _DummyInfra)
        self.assertIsInstance(seen["runner"], RunnerHelper)
        self.assertEqual(seen["verify_container"], "fae-verify-c1")
        self.assertEqual(seen["cell_network"], "fae-net-c1")
        self.assertEqual(stopped, ["fx"])


class TestCrashTeardownOrdering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "out").mkdir()

    def _ctx(self):
        return Ctx(
            root=str(ROOT),
            experiment_dir=str(ROOT / "tests" / "fixture_experiment"),
            workspace=str(self.root),
            artifacts=str(self.root),
            out=str(self.root / "out"),
            cid="c-td",
            task="T1",
            variant="only",
            arrangement="A",
        )

    def test_teardown_argv_passes_verify_container_and_cell_net(self):
        argv = verify.teardown_argv(self._ctx(), "img:1")
        env = dict(a.split("=", 1) for i, a in enumerate(argv) if i and argv[i - 1] == "-e")
        self.assertEqual(env["FAE_VERIFY_CONTAINER"], "fae-verify-c-td")
        self.assertEqual(env["FAE_VERIFY_IMAGE"], "img:1")
        self.assertEqual(env["FAE_CELL_NET"], "fae-net-c-td")

    def test_teardown_main_runs_verify_down_and_stops_fixtures(self):
        order = []

        class _DownInfra(Infra):
            def alive(self):
                return True

            def verify_teardown(self, ctx, env):
                order.append("verify_teardown")
                raise RuntimeError("overlay err")

            def verify_down(self, ctx):
                order.append("verify_down")

        class _V(Variant):
            ID = "only"
            INFRA = _DownInfra

        class _Ver:
            RUN_LOG = "run.log"

            def teardown(self, ctx):
                order.append("verifier_teardown")

        defn = SimpleNamespace(
            variants={"only": _V},
            variant=lambda vid: _V if vid == "only" else None,
            verifier_class=lambda: _Ver,
        )
        ctx_path = self.root / "out" / "ctx.json"
        ctx_path.write_text(self._ctx().to_json())
        with mock.patch.object(fae.experiment._experiment, "load", return_value=defn), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name",
                           side_effect=lambda *a, **k: order.append("runner")), \
                mock.patch.object(verify, "stop_fixtures_for",
                                  side_effect=lambda cid: order.append(("fixtures", cid))):
            with self.assertRaises(RuntimeError):
                verify.main(["--ctx", str(ctx_path), "--teardown"])
        self.assertEqual(order, ["runner", ("fixtures", "c-td"), "verify_teardown",
                                 "verify_down", "verifier_teardown"])

    def test_remote_crash_keeps_network_until_teardown_completes(self):
        h = VerifyHost("ssh://op@vm.example")
        h._identity = ("1001", "1002", "111")
        events = []

        class FakePopen:
            def __init__(self, argv, **kw):
                self.pid = 99
                events.append(("popen", "--teardown" in argv))

            def wait(self, timeout=None):
                return 137  # crashed without verdict

            def kill(self):
                pass

        calibrated = HostTimings(host=h.docker_host, date="d", fae_commit="a", experiment_commit="b")
        infra = NoopInfra(Variant, cid="c-td", ws=self.root, root=ROOT)

        def fake_docker(*args, **kw):
            events.append(("docker", args))
            return subprocess.CompletedProcess(["docker", *args], 0, stdout="", stderr="")

        with mock.patch.object(h, "ensure_up", return_value=True), \
                mock.patch.object(h, "wait_idle", return_value=(True, [0.0])), \
                mock.patch.object(h, "sample_load", return_value=lambda: None), \
                mock.patch.object(h, "network_up", side_effect=lambda *a: events.append(("net_up",))), \
                mock.patch.object(h, "push", side_effect=lambda *a: events.append(("push",))), \
                mock.patch.object(h, "pull", side_effect=lambda *a: events.append(("pull",))), \
                mock.patch.object(h, "docker", side_effect=fake_docker), \
                mock.patch.object(_timings, "calibration_of", return_value=(calibrated, None)), \
                mock.patch.object(_image, "for_variant", return_value="img:1"), \
                mock.patch.object(_image, "remove_container"), \
                mock.patch.object(verify.subprocess, "Popen", FakePopen):
            v = verify.run_verifier(self._ctx(), infra, host=h)

        self.assertEqual((v.ok, v.stage, v.charge), (False, "verifier", False))
        net_rms = [i for i, e in enumerate(events) if e == ("docker", ("network", "rm", "fae-net-c-td"))]
        td_idx = events.index(("popen", True))
        self.assertEqual(len(net_rms), 1)
        self.assertGreater(net_rms[0], td_idx, f"network dropped before teardown: {events}")


if __name__ == "__main__":
    unittest.main()
