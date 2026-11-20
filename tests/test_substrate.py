"""Declarative `[substrate]` subsystem: TOML parsing, DockerContext/K8sContext,
DomainOverlay composition, remote VerifyHost guard, Docker-only kind fallback,
and label-based zombie discovery."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT, at_workspace, patch_plane, runs, use_workspace  # noqa: F401

import fae.experiment
from fae.cell import verify
from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess, Infra, liveness_declared
from fae.cell.infra.dind import DindSidecar, VerifyDind
from fae.cell.substrate import (
    DockerContext,
    DockerSubstrate,
    DomainOverlay,
    ImageSpec,
    K8sContext,
    K8sSubstrate,
    NoneContext,
    NoneSubstrate,
    SubstrateSpec,
    build_infra_class,
    cluster_name_for,
)
from fae.experiment.variants import files as variant_files
from fae.experiment.variants.base import Variant

zombies = runs.zombies
state = runs.host


class _SampleOverlay(DomainOverlay):
    PREFIXES = {"container": "fae-ov-"}
    ENV_FILE = "ov.env"
    RUN_DIR = ".ov"

    def __init__(self):
        self.events = []

    def ok(self, infra):
        self.events.append("ok")
        return True

    def on_cell_setup(self, substrate, infra):
        self.events.append(("cell_setup", substrate is not None))
        return AgentAccess(env={"OV_URL": "http://ov:9000"}, mounts=(("/h/ov", "/ov", "ro"),))

    def on_cell_teardown(self, substrate, infra):
        self.events.append("cell_teardown")

    def on_verify_setup(self, substrate, ctx, env):
        self.events.append(("verify_setup", substrate.kind))
        return {"DOCKER_HOST": substrate.docker_host, "OV": "1"}

    def on_verify_teardown(self, substrate, ctx, env):
        self.events.append("verify_teardown")

    @classmethod
    def identities(cls, cid):
        return [("container", f"fae-ov-{cid}")]

    @classmethod
    def image_context(cls, conf):
        return [("/tmp/sdk", "sdk")]


class TestSubstrateTomlParsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.exp_dir = Path(self._tmp.name) / "exp"
        (self.exp_dir / "variants").mkdir(parents=True)
        (self.exp_dir / "app").mkdir()
        (self.exp_dir / "app" / "Dockerfile").write_text("FROM scratch\n")

    def _read(self, name, toml_text):
        p = self.exp_dir / "variants" / f"{name}.toml"
        p.write_text(textwrap.dedent(toml_text))
        return variant_files.read(p, self.exp_dir)

    def test_docker_substrate_parses_images_ports_and_overlay(self):
        mod = SimpleNamespace(Ov=_SampleOverlay)
        with mock.patch.dict(sys.modules, {"experiment.ovmod": mod}):
            vcls = self._read("dock", """
                label = "Dock"
                [substrate]
                kind = "docker"
                lifecycle = "split"
                access = true
                cpus = 2
                memory = "2g"
                expose_ports = [8080]
                images = ["redis:7-alpine", { dir = "app", as_tag = "app:1" }]
                overlay = "ovmod:Ov"
                lock = "dock"
                lock_slots = 3
                params = { mode = "fast" }
            """)
        self.assertTrue(issubclass(vcls.INFRA, DockerSubstrate))
        self.assertEqual(vcls.SUBSTRATE.kind, "docker")
        self.assertEqual(vcls.SUBSTRATE.lifecycle, "split")
        self.assertTrue(vcls.ACCESS_INFRA)
        self.assertEqual(vcls.SUBSTRATE.expose_ports, (8080,))
        self.assertEqual(vcls.LOCK, "dock")
        self.assertEqual(vcls.LOCK_SLOTS, 3)
        self.assertEqual(vcls.PARAMS, {"mode": "fast"})
        self.assertEqual(len(vcls.SUBSTRATE.images), 2)
        self.assertEqual(vcls.SUBSTRATE.images[0].source, "redis:7-alpine")
        self.assertTrue(vcls.SUBSTRATE.images[1].is_build)
        self.assertEqual(vcls.SUBSTRATE.images[1].as_tag, "app:1")
        self.assertIn("fae-ov-", vcls.INFRA.PREFIXES["container"])
        self.assertEqual(vcls.INFRA.ENV_FILE, "ov.env")
        self.assertEqual(vcls.INFRA.image_context(None), [("/tmp/sdk", "sdk")])
        self.assertTrue(liveness_declared(vcls))

    def test_none_substrate_preserves_default_liveness_rule(self):
        bare = self._read("bare", """
            [substrate]
            kind = "none"
        """)
        self.assertTrue(issubclass(bare.INFRA, NoneSubstrate))
        self.assertFalse(liveness_declared(bare))

        with_run = self._read("own_img", """
            [verify.run]
            image = "python:3.12-slim"
            command = ["python3", "a.py"]
            [substrate]
            kind = "none"
        """)
        self.assertTrue(liveness_declared(with_run))

    def test_refuses_conflicting_or_invalid_declarations(self):
        with self.assertRaises(variant_files.VariantFileError):
            self._read("both", "[infra]\nlock = 'a'\n[substrate]\nkind = 'none'\n")
        with self.assertRaises(variant_files.VariantFileError):
            self._read("badkind", "[substrate]\nkind = 'vm'\n")
        with self.assertRaises(variant_files.VariantFileError):
            self._read("badlife", "[substrate]\nkind = 'docker'\nlifecycle = 'forever'\n")
        with self.assertRaises(variant_files.VariantFileError):
            self._read("noclass", "[substrate]\nkind = 'custom'\n")
        with self.assertRaises(variant_files.VariantFileError):
            self._read("extraclass", "[substrate]\nkind = 'docker'\nclass = 'x:Y'\n")


class TestDockerSubstrateAndContext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def _variant(self, **spec_kw):
        spec = SubstrateSpec(kind="docker", **spec_kw)
        infra_cls = build_infra_class(spec, name="Test")
        return type("V", (Variant,), {"ID": "v1", "INFRA": infra_cls, "SUBSTRATE": spec,
                                      "ACCESS_INFRA": spec.access})

    def test_cell_lifecycle_refused_with_remote_verify_host(self):
        vcls = self._variant(lifecycle="cell")
        conf = SimpleNamespace(get=lambda k, d=None: "ssh://op@vm" if k == "VERIFY_DOCKER_HOST" else d)
        infra = vcls.INFRA(vcls, cid="c1", ws=self.ws, root=self.ws, conf=conf)
        self.assertFalse(infra.ok())
        self.assertEqual(infra.shared_containers(), [DindSidecar.name_for("c1")])

    def test_split_without_access_skips_cell_daemon_and_probes_verify_host(self):
        vcls = self._variant(lifecycle="split", access=False)
        conf = SimpleNamespace(get=lambda k, d=None: "ssh://op@vm" if k == "VERIFY_DOCKER_HOST" else d)
        infra = vcls.INFRA(vcls, cid="c1", ws=self.ws, root=self.ws, conf=conf)
        with mock.patch.object(DockerContext, "ensure") as ensure:
            self.assertEqual(infra.cell_setup(), {})
        ensure.assert_not_called()
        with mock.patch("fae.cell.verify_host.VerifyHost.answers", return_value=True) as ans:
            self.assertTrue(infra.alive())
        ans.assert_called_once()
        self.assertEqual(infra.shared_containers(), [])

    def test_split_with_access_merges_overlay_agent_access(self):
        vcls = self._variant(lifecycle="split", access=True, overlay=_SampleOverlay)
        infra = vcls.INFRA(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        fake_ctx = DockerContext("c1", scope="cell", in_verify=False, api_port=24567)
        with mock.patch.object(DockerContext, "ensure", return_value=fake_ctx):
            acc = infra.cell_setup()
        self.assertIsInstance(acc, AgentAccess)
        self.assertEqual(acc.env["DOCKER_HOST"], "tcp://host.docker.internal:24567")
        self.assertEqual(acc.env["OV_URL"], "http://ov:9000")
        self.assertIn(("/h/ov", "/ov", "ro"), acc.mounts)
        self.assertIn("host.docker.internal:host-gateway", acc.extra_hosts)

    def test_docker_context_reset_and_down_scope(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            if argv[3:] == ["ps", "-aq"]:
                return subprocess.CompletedProcess(argv, 0, stdout="c1 c2\n", stderr="")
            if argv[3:] == ["volume", "ls", "-q"]:
                return subprocess.CompletedProcess(argv, 0, stdout="v1\n", stderr="")
            if argv[3:6] == ["network", "ls", "--format"]:
                return subprocess.CompletedProcess(argv, 0, stdout="bridge\nhost\nnone\nappnet\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        ctx = DockerContext("c1", scope="cell", in_verify=True, log=lambda m: None)
        with mock.patch("subprocess.run", side_effect=fake_run):
            ctx.reset()
        self.assertIn(["docker", "-H", "tcp://fae-dind-c1:2375", "rm", "-f", "-v", "c1", "c2"], calls)
        self.assertIn(["docker", "-H", "tcp://fae-dind-c1:2375", "volume", "rm", "-f", "v1"], calls)
        self.assertIn(["docker", "-H", "tcp://fae-dind-c1:2375", "network", "rm", "appnet"], calls)

        # cell-scope down() is a no-op without force=True; verify-scope removes container
        with mock.patch.object(infra_base, "run") as rm:
            ctx.down()
            rm.assert_not_called()
            ctx.down(force=True)
            rm.assert_called_once_with(["docker", "rm", "-f", "-v", "fae-dind-c1"])

        vctx = DockerContext("c1", scope="verify", in_verify=True, log=lambda m: None)
        with mock.patch.object(infra_base, "run") as rm:
            vctx.down()
            rm.assert_called_once_with(["docker", "rm", "-f", "-v", VerifyDind.name_for("c1")])

    def test_verify_main_binds_substrate_and_calls_down_in_finally(self):
        vcls = self._variant(lifecycle="verify")
        seen = {}
        downs = []

        class _Ver:
            FILES = ()

            def verify(self, ctx):
                seen["substrate"] = ctx.substrate
                ctx.substrate.down = lambda force=False: downs.append("down")
                return verify.Verdict(ok=True)

        defn = SimpleNamespace(
            variants={"v1": vcls},
            variant=lambda vid: vcls if vid == "v1" else None,
            verifier_class=lambda: _Ver,
        )
        ctx = verify.Ctx(root=str(self.ws), experiment_dir=str(self.ws),
                         workspace=str(self.ws), artifacts=str(self.ws),
                         out=str(self.ws), cid="c1", task="T1", variant="v1")
        ctx_path = self.ws / "ctx.json"
        out_path = self.ws / "verdict.json"
        ctx_path.write_text(ctx.to_json())
        with mock.patch.object(fae.experiment._experiment, "load", return_value=defn):
            rc = verify.main(["--ctx", str(ctx_path), "--out", str(out_path)])
        self.assertEqual(rc, 0)
        self.assertIsInstance(seen["substrate"], DockerContext)
        self.assertEqual(seen["substrate"].scope, "verify")
        self.assertEqual(downs, ["down"])


class TestK8sSubstrateAndLabelReaper(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_k8s_identities_and_docker_only_tool_fallback(self):
        spec = SubstrateSpec(kind="k8s", lifecycle="split", access=True, cluster_prefix="fae-k8s-")
        infra_cls = build_infra_class(spec, name="K")
        vcls = type("KV", (Variant,), {"ID": "k1", "INFRA": infra_cls, "SUBSTRATE": spec,
                                       "ACCESS_INFRA": True})
        self.assertEqual(infra_cls.identities("c1"), [
            ("cluster", cluster_name_for("c1", "fae-k8s-")),
            ("cluster", cluster_name_for("c1-verify", "fae-k8s-")),
        ])
        infra = infra_cls(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(infra, "tool",
                                  return_value=subprocess.CompletedProcess([], 0, stdout="ok", stderr="")) as tool, \
                mock.patch.object(infra_base, "run",
                                  return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            kctx = K8sContext("c1", scope="cell", kubeconfig_dir=self.ws, infra=infra)
            self.assertTrue(kctx.via_image)
            kctx.kubectl("get", "ns")
        tool.assert_called_once()
        self.assertEqual(tool.call_args.args[0], ["kubectl", "get", "ns"])

    def test_zombies_discovers_labeled_containers_without_declared_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "cid1").mkdir()
            use_workspace(self, ws)
            patch_plane(self, ws)

            def fake_sh(argv):
                if "label=fae-cell" in argv:
                    return "custom-fixture-xyz\tcid1\nfae-agent-cid1\tcid1\n"
                return ""

            with mock.patch.object(zombies, "_leaked_lock_holders", return_value=[]), \
                    mock.patch.object(state, "loop_parents", return_value=set()), \
                    mock.patch.object(state, "containers", return_value=["fae-agent-cid1"]), \
                    mock.patch.object(state, "sh", side_effect=fake_sh), \
                    mock.patch.object(state, "loop_pids", return_value={}), \
                    mock.patch.object(zombies, "_strays", return_value=[]), \
                    mock.patch.object(zombies, "_iter_age_s", return_value=None), \
                    mock.patch.object(zombies, "_docker_age_s",
                                      return_value=zombies.ZOMBIE_UNKNOWN_GRACE_S + 1):
                zs = zombies.find_zombies()
            names = [ident for kind, ident, _, _ in zs if kind == "container"]
            self.assertEqual(names.count("fae-agent-cid1"), 1)
            self.assertIn("custom-fixture-xyz", names)


if __name__ == "__main__":
    unittest.main()
