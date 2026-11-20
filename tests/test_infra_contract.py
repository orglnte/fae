"""Infra contract: Infra runs without a Cell object via variants.infra_for,
AgentAccess carries typed Docker flags for agent containers, and
Infra.REQUIRED_IMAGES drives the default ok() check."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401

import fae.experiment
from fae.cell import verify
from fae.cell.cell import Cell
from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess, Infra, NoopInfra, ok, run
from fae.experiment import check_exp
from fae.experiment import variants
from fae.experiment.variants.base import Variant


class _RecordingInfra(Infra):
    REQUIRED_IMAGES = ("img-a:1", "img-b:2")

    def alive(self):
        return True

    def cell_setup(self):
        return AgentAccess(
            network="fae-cell-c1",
            mounts=(("/host/cfg", "/etc/cfg", "ro"),),
            env={"API_URL": "http://svc:8080"},
            extra_hosts=("host.docker.internal:host-gateway",),
        )


class _SampleVariant(Variant):
    ID = "sample"
    INFRA = _RecordingInfra


class TestAgentAccess(unittest.TestCase):
    def test_docker_args_order_and_default_ro_mount(self):
        acc = AgentAccess(
            network="net-1",
            extra_hosts=("a:1.2.3.4",),
            mounts=(("/h/ro", "/c/ro"), ("/h/rw", "/c/rw", "rw")),
            env={"Z_VAR": "2", "A_VAR": "1"},
        )
        self.assertEqual(acc.docker_args(), [
            "--network", "net-1",
            "--add-host", "a:1.2.3.4",
            "-v", "/h/ro:/c/ro:ro",
            "-v", "/h/rw:/c/rw:rw",
            "-e", "A_VAR=1",
            "-e", "Z_VAR=2",
        ])

    def test_to_legacy_env_formats_shell_strings_and_omits_empty(self):
        self.assertEqual(AgentAccess().to_legacy_env(), {})
        acc = AgentAccess(
            network="net-1",
            mounts=(("/host/my cfg", "/etc/cfg"),),
            env={"FOO": "bar baz"},
        )
        legacy = acc.to_legacy_env()
        self.assertEqual(legacy["DOCKER_NET"], "--network net-1")
        self.assertIn("-v '/host/my cfg:/etc/cfg:ro'", legacy["KUBE_MOUNT"])
        self.assertIn("-e 'FOO=bar baz'", legacy["KUBE_MOUNT"])


class TestInfraWithoutCell(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_constructs_directly_with_keywords_and_compat_cell(self):
        conf = {"KEY": "val"}
        direct = _RecordingInfra(_SampleVariant, cid="c1", ws=self.ws, root=self.ws, conf=conf)
        self.assertIsNone(direct.cell)
        self.assertEqual(direct.cid, "c1")
        self.assertEqual(direct.ws, self.ws)
        self.assertEqual(direct.conf, conf)

        cell = SimpleNamespace(cid="c2", ws=self.ws, root=self.ws, conf=conf)
        legacy = _RecordingInfra(_SampleVariant, cell)
        self.assertIs(legacy.cell, cell)
        self.assertEqual(legacy.cid, "c2")

    def test_public_run_and_ok_aliases(self):
        self.assertIs(infra_base._run, run)
        self.assertIs(infra_base._ok, ok)
        self.assertTrue(ok(["true"]))

    def test_required_images_checked_by_default_ok(self):
        plain = Infra(_SampleVariant, cid="c1", ws=self.ws, root=self.ws)
        self.assertTrue(plain.ok())

        inst = _RecordingInfra(_SampleVariant, cid="c1", ws=self.ws, root=self.ws)
        seen = []

        def fake_ok(argv, **kw):
            seen.append(list(argv))
            return True

        with mock.patch.object(infra_base, "ok", side_effect=fake_ok):
            self.assertTrue(inst.ok())
        self.assertEqual(seen, [
            ["docker", "info"],
            ["docker", "image", "inspect", "img-a:1"],
            ["docker", "image", "inspect", "img-b:2"],
        ])

        def missing_second(argv, **kw):
            return argv != ["docker", "image", "inspect", "img-b:2"]

        with mock.patch.object(infra_base, "ok", side_effect=missing_second):
            self.assertFalse(inst.ok())

        with mock.patch.object(infra_base, "ok", return_value=False):
            self.assertFalse(inst.ok())


class TestInfraForAndCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def test_infra_for_builds_without_cell_and_honors_noop(self):
        defn = SimpleNamespace(variants={"sample": _SampleVariant})
        inst = variants.infra_for("sample", "c1", self.ws, self.root, conf={}, definition=defn)
        self.assertIsInstance(inst, _RecordingInfra)
        self.assertIsNone(inst.cell)

        unknown = variants.infra_for("missing", "c1", self.ws, self.root, conf={}, definition=defn)
        self.assertIsInstance(unknown, NoopInfra)

        with mock.patch.dict(os.environ, {variants.NOOP_ENV: "1"}):
            noop = variants.infra_for("sample", "c1", self.ws, self.root, conf={}, definition=defn)
        self.assertIsInstance(noop, NoopInfra)

    def test_variants_main_setup_formats_agent_access(self):
        inst = _RecordingInfra(_SampleVariant, cid="c1", ws=self.ws, root=self.root, conf={})
        buf = io.StringIO()
        with mock.patch.object(variants, "infra_for", return_value=inst), \
                mock.patch("fae.paths.root", return_value=self.root), \
                redirect_stdout(buf):
            rc = variants.main(["sample", "setup", "c1", str(self.ws)])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("DOCKER_NET='--network fae-cell-c1'", out)
        self.assertIn("KUBE_MOUNT=", out)
        self.assertIn("API_URL=http://svc:8080", out)


class TestAgentArgvAndTeardown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("hello")

    def test_agent_argv_uses_typed_access_and_preserves_legacy_strings(self):
        conf = {"AGENT_CLI": "claude", "AGENT_IMAGE": "img:1"}
        acc = AgentAccess(network="cell-net", mounts=(("/h/x", "/c/x"),), env={"K": "V"})
        argv = Cell.agent_argv(conf, "c1", self.root, self.root, self.prompt,
                               docker_net="--network ignored", access=acc)
        self.assertIn("--network", argv)
        self.assertIn("cell-net", argv)
        self.assertNotIn("ignored", argv)
        self.assertIn("/h/x:/c/x:ro", argv)
        self.assertIn("K=V", argv)

        legacy = Cell.agent_argv(conf, "c1", self.root, self.root, self.prompt,
                                 docker_net="--network leg-net", kube_mount="-e LEG=1")
        self.assertIn("leg-net", legacy)
        self.assertIn("LEG=1", legacy)

    def test_verify_main_teardown_runs_without_cell_object(self):
        seen = []

        class _I(Infra):
            def alive(self):
                return True

            def verify_teardown(self, ctx, env):
                seen.append(("infra", self.cell, ctx.cid))

        class _V(Variant):
            ID = "only"
            INFRA = _I

        class _Ver:
            RUN_LOG = "run.log"

            def teardown(self, ctx):
                seen.append(("verifier", ctx.cid))

        defn = SimpleNamespace(
            variants={"only": _V},
            variant=lambda vid: _V if vid == "only" else None,
            verifier_class=lambda: _Ver,
        )
        out = self.root / "out"
        out.mkdir()
        ctx_path = out / "ctx.json"
        ctx = verify.Ctx(root=str(self.root), experiment_dir=str(self.root / "exp"),
                         workspace=str(out), artifacts=str(out), out=str(out),
                         cid="c-td", task="T1", variant="only", arrangement="A")
        ctx_path.write_text(ctx.to_json())
        with mock.patch.object(fae.experiment._experiment, "load", return_value=defn), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name"):
            rc = verify.main(["--ctx", str(ctx_path), "--teardown"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [("infra", None, "c-td"), ("verifier", "c-td")])

    def test_probe_variants_builds_infra_without_cell(self):
        seen_cells = []

        class _ProbeInfra(Infra):
            def alive(self):
                return True

            def ok(self):
                seen_cells.append(self.cell)
                return True

            def image(self):
                return "img:ok"

        class _V(Variant):
            ID = "p1"
            INFRA = _ProbeInfra

        fake_exp = SimpleNamespace(
            root=self.root,
            definition=SimpleNamespace(active=("p1",), variants={"p1": _V}),
        )
        buf = io.StringIO()
        with mock.patch.object(fae.experiment, "exp", return_value=fake_exp), \
                mock.patch.object(check_exp, "_authorable_error", return_value=""), \
                redirect_stdout(buf):
            bad = check_exp.probe_variants(["p1"])
        self.assertEqual(bad, 0)
        self.assertEqual(seen_cells, [None])


if __name__ == "__main__":
    unittest.main()
