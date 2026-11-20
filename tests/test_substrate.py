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
        return {**substrate.env(), "OV": "1"}

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
        with self.assertRaises(variant_files.VariantFileError):
            self._read("badimg1", "[substrate]\nkind = 'docker'\nimages = [123]\n")
        with self.assertRaises(variant_files.VariantFileError):
            self._read("badimg2", "[substrate]\nkind = 'docker'\nimages = [{ as_tag = 'a:1' }]\n")
        with self.assertRaises(variant_files.VariantFileError):
            self._read("badimg3", "[substrate]\nkind = 'docker'\nimages = [{ dir = 'app', bogus = 1 }]\n")

        # k8s default memory is 4g, docker default memory is 1g, authoring.access_infra fallback works
        k8s_v = self._read("k8s_def", """
            [authoring]
            access_infra = true
            [substrate]
            kind = "k8s"
            cluster_prefix = "my-k-"
        """)
        self.assertEqual(k8s_v.SUBSTRATE.memory, "4g")
        self.assertEqual(k8s_v.SUBSTRATE.cluster_prefix, "my-k-")
        self.assertTrue(k8s_v.ACCESS_INFRA)
        self.assertEqual(k8s_v.INFRA.PREFIXES, {"cluster": ("my-k-",)})

        # kind = "custom" with valid Infra subclass
        class _CustomInfra(Infra):
            def alive(self):
                return True

        mod = SimpleNamespace(Custom=_CustomInfra, NotOverlay=object)
        with mock.patch.dict(sys.modules, {"experiment.custmod": mod}):
            cv = self._read("cust", """
                [substrate]
                kind = "custom"
                class = "custmod:Custom"
                lock = "gpu"
                lock_slots = 2
            """)
            self.assertIs(cv.INFRA, _CustomInfra)
            self.assertEqual((cv.LOCK, cv.LOCK_SLOTS), ("gpu", 2))
            with self.assertRaises(variant_files.VariantFileError):
                self._read("badov", "[substrate]\nkind = 'docker'\noverlay = 'custmod:NotOverlay'\n")


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
        # verify and split lifecycles ARE allowed with remote VerifyHost
        for life in ("verify", "split"):
            v_ok = self._variant(lifecycle=life)
            i_ok = v_ok.INFRA(v_ok, cid="c1", ws=self.ws, root=self.ws, conf=conf)
            self.assertTrue(i_ok._check_remote_lifecycle())

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

    def test_substrate_infra_delegates_not_ready_to_overlay(self):
        calls = []

        class _NotReadyOverlay(DomainOverlay):
            def not_ready(self, substrate, ctx, env):
                calls.append((substrate, ctx.cid, env.get("K")))

        vcls = self._variant(lifecycle="verify", overlay=_NotReadyOverlay)
        infra = vcls.INFRA(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        ctx = verify.Ctx(root=str(self.ws), experiment_dir=str(self.ws),
                         workspace=str(self.ws), artifacts=str(self.ws),
                         out=str(self.ws), cid="c1", task="T1", variant="v1")
        ctx.substrate = DockerContext("c1", scope="verify", in_verify=True)
        infra.not_ready(ctx, {"K": "V"})
        self.assertEqual(calls, [(ctx.substrate, "c1", "V")])


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


class TestSubstrateBaseAndNone(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_build_infra_class_errors_and_metadata_forwarding(self):
        with self.assertRaises(ValueError):
            build_infra_class(SubstrateSpec(kind="bogus"))

        class _Ov(DomainOverlay):
            PREFIXES = {"container": ("ov-a-", "fae-dind-"), "cluster": "ov-c-"}
            ENV_FILE = "custom.env"
            VERIFY_ADOPTS = ("A",)
            RUN_DIR = ".ovrun"

            def ok(self, infra):
                return True

            def shared_containers(self, infra):
                return ["ov-shared"]

            @classmethod
            def identities(cls, cid):
                return [("container", f"ov-{cid}")]

            @classmethod
            def stray(cls, live, workspaces):
                return [("container", "ov-stray", "c0")]

            @classmethod
            def agent_image_context(cls, conf):
                return [("/src", "dst")]

        spec = SubstrateSpec(kind="docker", lifecycle="cell", overlay=_Ov)
        cls = build_infra_class(spec, name="X")
        self.assertEqual(cls.ENV_FILE, "custom.env")
        self.assertEqual(cls.VERIFY_ADOPTS, ("A",))
        self.assertEqual(cls.RUN_DIR, ".ovrun")
        self.assertEqual(cls.PREFIXES["container"].count("fae-dind-"), 1)
        self.assertIn("fae-vdind-", cls.PREFIXES["container"])
        self.assertIn("ov-a-", cls.PREFIXES["container"])
        self.assertEqual(cls.PREFIXES["cluster"], ("ov-c-",))
        self.assertEqual(cls.stray(set(), self.ws), [("container", "ov-stray", "c0")])
        self.assertEqual(cls.agent_image_context(None), [("/src", "dst")])
        self.assertEqual(cls.identities("c1"),
                         [("container", "fae-dind-c1"), ("container", "ov-c1")])
        vcls = type("V", (Variant,), {"ID": "v", "INFRA": cls, "SUBSTRATE": spec})
        inst = cls(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        self.assertEqual(inst.shared_containers(), ["fae-dind-c1", "ov-shared"])

    def test_none_context_and_none_substrate_full_lifecycle(self):
        nc = NoneContext()
        self.assertEqual(nc.env(), {})
        self.assertIs(nc.up(), nc)
        self.assertIs(nc.ensure(), nc)
        nc.reset()
        nc.down(force=True)
        with nc as entered:
            self.assertIs(entered, nc)

        events = []

        class _Ov(DomainOverlay):
            def ok(self, infra):
                events.append("ok")
                return infra.cid != "bad-ov"

            def on_cell_setup(self, sub, infra):
                events.append(("cell_setup", sub.kind))
                return AgentAccess(env={"N": "1"})

            def on_cell_teardown(self, sub, infra):
                events.append("cell_teardown")

            def on_verify_setup(self, sub, ctx, env):
                events.append(("verify_setup", sub.kind))
                return {"V": "2"}

            def on_verify_teardown(self, sub, ctx, env):
                events.append("verify_teardown")

        spec = SubstrateSpec(kind="none", images=(ImageSpec(source="alpine:3"),), overlay=_Ov)
        cls = build_infra_class(spec, name="N")
        vcls = type("NV", (Variant,), {
            "ID": "n1", "INFRA": cls, "SUBSTRATE": spec,
            "RUN": {"image": "python:3.12-slim", "command": ["python3", "a.py"]},
        })
        inst = cls(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        self.assertIsInstance(inst.make_context(), NoneContext)

        # _check_spec_images failure paths: docker unreachable, missing image
        with mock.patch.object(infra_base, "ok", return_value=False):
            self.assertFalse(inst.ok())
        with mock.patch.object(infra_base, "ok", side_effect=lambda argv: argv[:2] == ["docker", "info"]):
            self.assertFalse(inst.ok())

        # runs_own_image docker unreachable & ensure_run_image RuntimeError
        with mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertFalse(inst.ok())
        with mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch("fae.cell.infra.secrunner.ensure_run_image", side_effect=RuntimeError("no img")):
            self.assertFalse(inst.ok())
        # Happy path + overlay failure
        with mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch("fae.cell.infra.secrunner.ensure_run_image"):
            self.assertTrue(inst.ok())
            bad_inst = cls(vcls, cid="bad-ov", ws=self.ws, root=self.ws, conf=None)
            self.assertFalse(bad_inst.ok())

        # alive() delegates to _daemon_alive when runs_own_image() is True
        with mock.patch.object(infra_base, "daemon_answers", return_value=True):
            self.assertTrue(inst.alive())
        with mock.patch.object(infra_base, "daemon_answers", return_value=False):
            self.assertFalse(inst.alive())

        # cell/verify hooks + verify_down
        acc = inst.cell_setup()
        self.assertEqual(acc, AgentAccess(env={"N": "1"}))
        inst.cell_teardown()
        ctx = verify.Ctx(root=str(self.ws), experiment_dir=str(self.ws),
                         workspace=str(self.ws), artifacts=str(self.ws),
                         out=str(self.ws), cid="c1", task="T1", variant="n1")
        ctx.substrate = NoneContext()
        self.assertEqual(inst.verify_setup(ctx, {}), {"V": "2"})
        inst.verify_teardown(ctx, {})
        down_called = []
        ctx.substrate = SimpleNamespace(down=lambda: down_called.append("down"))
        inst.verify_down(ctx)
        self.assertEqual(down_called, ["down"])
        self.assertIn(("cell_setup", "none"), events)
        self.assertIn("cell_teardown", events)
        self.assertIn(("verify_setup", "none"), events)
        self.assertIn("verify_teardown", events)

    def test_check_spec_images_build_success_and_error(self):
        img_dir = self.ws / "img"
        img_dir.mkdir()
        spec = SubstrateSpec(kind="none", images=(ImageSpec(dir=img_dir, as_tag="app:1"),))
        cls = build_infra_class(spec, name="B")
        vcls = type("BV", (Variant,), {"ID": "b1", "INFRA": cls, "SUBSTRATE": spec})
        inst = cls(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        with mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch.object(ImageSpec, "host_tag", return_value="app:sha") as ht:
            self.assertTrue(inst._check_spec_images())
        ht.assert_called_once()
        with mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch.object(ImageSpec, "host_tag", side_effect=RuntimeError("build err")):
            self.assertFalse(inst._check_spec_images())


class TestDockerContextDetailed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_allocate_ports_and_read_published_port(self):
        from fae.cell.substrate import docker as dmod
        slot = infra_base.cksum("cid-1") % dmod.PORT_SPAN
        want_api_seed = dmod.CELL_API_BASE + slot
        want_exp_seed = dmod.CELL_EXPOSE_BASE + (slot * 2) % dmod.PORT_SPAN
        with mock.patch.object(dmod, "free_port_from", side_effect=[24100, 25100, 25105]) as fp:
            api, exposed = dmod._allocate_ports("cid-1", (8080, 9090))
        self.assertEqual(api, 24100)
        self.assertEqual(exposed, {8080: 25100, 9090: 25105})
        self.assertEqual([c.args[0] for c in fp.call_args_list], [want_api_seed, want_exp_seed, 25101])

        with mock.patch.object(dmod, "free_port_from", return_value=None):
            with self.assertRaises(infra_base.HookFailure):
                dmod._allocate_ports("cid-1", ())
        with mock.patch.object(dmod, "free_port_from", side_effect=[24100, None]):
            with self.assertRaises(infra_base.HookFailure):
                dmod._allocate_ports("cid-1", (8080,))

        with mock.patch.object(infra_base, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout="127.0.0.1:24555\n", stderr="")):
            self.assertEqual(dmod._read_published_port("c", 2375), 24555)
        with mock.patch.object(infra_base, "run",
                               return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="")):
            self.assertIsNone(dmod._read_published_port("c", 2375))
        with mock.patch.object(infra_base, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout="bad:port\n", stderr="")):
            self.assertIsNone(dmod._read_published_port("c", 2375))

    def test_docker_context_queries_and_snapshot(self):
        from fae.cell.substrate import docker as dmod
        cell_ctx = DockerContext("c1", scope="cell", in_verify=False, log=lambda m: None)
        self.assertEqual(cell_ctx.host, "fae-dind-c1")
        with mock.patch.object(dmod, "_read_published_port", return_value=24321):
            self.assertEqual(cell_ctx.docker_host, "tcp://127.0.0.1:24321")
        self.assertEqual(cell_ctx.env(), {"DOCKER_HOST": "tcp://127.0.0.1:24321"})

        fallback_ctx = DockerContext("c1", scope="cell", in_verify=False, log=lambda m: None)
        with mock.patch.object(dmod, "_read_published_port", return_value=None):
            self.assertTrue(fallback_ctx.docker_host.startswith("tcp://127.0.0.1:24"))

        with mock.patch.object(infra_base, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout="other\nfae-dind-c1\n", stderr="")):
            self.assertTrue(cell_ctx.exists())
        with mock.patch.object(infra_base, "ok", return_value=True):
            self.assertTrue(cell_ctx.alive())
        with mock.patch.object(infra_base, "daemon_answers", return_value=True):
            self.assertTrue(cell_ctx.answers())

        def fake_docker(*argv, **kw):
            if argv[:2] == ("ps", "-q"):
                return subprocess.CompletedProcess(argv, 0, stdout="id1\n", stderr="")
            if argv[0] == "inspect":
                return subprocess.CompletedProcess(argv, 0, stdout='[{"Name":"/web"}]', stderr="")
            if argv[:3] == ("ps", "-a", "--format"):
                if "Status" in argv[3]:
                    return subprocess.CompletedProcess(argv, 0, stdout="web\tUp 5s\ndb\tExited (1)\n", stderr="")
                return subprocess.CompletedProcess(argv, 0, stdout="web\ndb\n", stderr="")
            if argv[0] == "logs":
                return subprocess.CompletedProcess(argv, 0, stdout="boom\n", stderr="err\n")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(cell_ctx, "docker", side_effect=fake_docker):
            self.assertEqual(cell_ctx.containers(), [{"Name": "/web"}])
            self.assertEqual(cell_ctx.container_names(all=True), ["web", "db"])
            snap = cell_ctx.snapshot()
        self.assertIn("web\tUp 5s", snap)
        self.assertIn("--- db log:", snap)
        self.assertIn("boom", snap)

        # Malformed inspect JSON returns []
        with mock.patch.object(cell_ctx, "docker", side_effect=[
            subprocess.CompletedProcess([], 0, stdout="id1\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="not-json", stderr=""),
        ]):
            self.assertEqual(cell_ctx.containers(), [])

    def test_docker_context_load_images_stream_and_retag(self):
        img_dir = self.ws / "app"
        img_dir.mkdir()
        specs = (ImageSpec(source="redis:7"), ImageSpec(dir=img_dir, as_tag="app:1"))
        ctx = DockerContext("c1", scope="verify", in_verify=True, images=specs, log=lambda m: None)

        fake_save = mock.Mock(stdout=mock.Mock(), returncode=0)
        docker_calls = []

        def fake_docker(*argv, **kw):
            docker_calls.append(argv)
            if argv[0] == "images":
                return subprocess.CompletedProcess(argv, 0, stdout="redis:7\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(ctx, "_ensure_host_image", side_effect=["redis:7", "app:sha"]), \
                mock.patch.object(ctx, "docker", side_effect=fake_docker), \
                mock.patch("subprocess.Popen", return_value=fake_save) as popen, \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            ctx.load_images()
        self.assertEqual(popen.call_args.args[0], ["docker", "save", "app:sha"])
        self.assertIn(("tag", "app:sha", "app:1"), docker_calls)

        # Pull failure raises HookFailure in _ensure_host_image
        with mock.patch.object(infra_base, "ok", return_value=False):
            with self.assertRaises(infra_base.HookFailure):
                ctx._ensure_host_image(ImageSpec(source="missing:1"))

        # Load failure raises HookFailure
        fake_save.returncode = 1
        with mock.patch.object(ctx, "_ensure_host_image", return_value="missing:1"), \
                mock.patch.object(ctx, "docker", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")), \
                mock.patch("subprocess.Popen", return_value=fake_save), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(infra_base.HookFailure):
                ctx.load_images(["missing:1"])

    def test_docker_context_start_verify_and_start_cell_bind_retry(self):
        from fae.cell.substrate import docker as dmod
        vctx = DockerContext("c1", scope="verify", network="fae-net-c1", in_verify=True, log=lambda m: None)
        started_argvs = []

        def fake_ok(argv):
            started_argvs.append(argv)
            return True

        with mock.patch.dict(os.environ, {"CPUSET_MEASURED": "0-1"}), \
                mock.patch.object(infra_base, "run"), \
                mock.patch.object(infra_base, "ok", side_effect=fake_ok), \
                mock.patch.object(vctx, "alive", return_value=True), \
                mock.patch.object(vctx, "load_images") as load_imgs:
            self.assertIs(vctx.ensure(), vctx)
        load_imgs.assert_called_once()
        run_argv = [a for a in started_argvs if a[:2] == ["docker", "run"]][0]
        self.assertIn("--cpuset-cpus", run_argv)
        self.assertIn("fae-cell=c1", run_argv)
        self.assertIn("fae-role=substrate", run_argv)

        # Verify timeout raises HookFailure and removes container
        with mock.patch.object(infra_base, "run") as rm_run, \
                mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch.object(vctx, "alive", return_value=False), \
                mock.patch("time.sleep"):
            with self.assertRaises(infra_base.HookFailure):
                vctx._start_verify()
        self.assertIn(mock.call(["docker", "rm", "-f", "-v", vctx.container]), rm_run.call_args_list)

        # Cell scope reuse when already alive
        cctx = DockerContext("c1", scope="cell", network="fae-net-c1", expose_ports=(8080,),
                             in_verify=False, log=lambda m: None)
        with mock.patch.object(cctx, "exists", return_value=True), \
                mock.patch.object(dmod, "_read_published_port", side_effect=[24111, 25111]), \
                mock.patch.object(cctx, "alive", return_value=True), \
                mock.patch.object(cctx, "load_images") as cload:
            cctx.ensure()
        self.assertEqual(cctx.api_port, 24111)
        self.assertEqual(cctx.exposed_ports, {8080: 25111})
        cload.assert_called_once()

        # Cell scope bind retry: fails attempt 1, succeeds attempt 2
        run_results = [
            subprocess.CompletedProcess([], 1, stdout="", stderr="port already allocated"),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),  # rm after fail
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),  # attempt 2 ok
        ]
        with mock.patch.dict(os.environ, {"CPUSET_AGENT": "2-3"}), \
                mock.patch.object(cctx, "exists", return_value=False), \
                mock.patch.object(dmod, "_allocate_ports", return_value=(24222, {8080: 25222})), \
                mock.patch.object(infra_base, "run", side_effect=run_results), \
                mock.patch.object(cctx, "alive", return_value=True), \
                mock.patch.object(cctx, "load_images"):
            cctx._start_cell()
        self.assertEqual(cctx.api_port, 24222)

        # Cell scope in_verify=True raises when daemon does not answer
        vcell = DockerContext("c1", scope="cell", in_verify=True, log=lambda m: None)
        with mock.patch.object(vcell, "answers", return_value=False):
            with self.assertRaises(infra_base.HookFailure):
                vcell.ensure()

    def test_docker_substrate_ok_alive_identities_and_hooks(self):
        spec_split = SubstrateSpec(kind="docker", lifecycle="split", access=True, overlay=_SampleOverlay)
        cls = build_infra_class(spec_split, name="DS")
        vcls = type("DV", (Variant,), {"ID": "d1", "INFRA": cls, "SUBSTRATE": spec_split, "ACCESS_INFRA": True})
        self.assertEqual(cls.identities("c1"), [
            ("container", DindSidecar.name_for("c1")),
            ("container", VerifyDind.name_for("c1")),
            ("container", "fae-ov-c1"),
        ])
        infra = cls(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        with mock.patch.object(infra_base, "ok", return_value=False):
            self.assertFalse(infra.ok())
        with mock.patch.object(infra_base, "ok", side_effect=[True, False]):
            self.assertFalse(infra.ok())
        with mock.patch.object(infra_base, "ok", return_value=True):
            self.assertTrue(infra.ok())

        with mock.patch.object(DockerContext, "answers", return_value=True):
            self.assertTrue(infra.alive())

        # cell_teardown calls overlay and down(force=True)
        with mock.patch.object(DockerContext, "down") as down:
            infra.cell_teardown()
        down.assert_called_once_with(force=True)
        self.assertIn("cell_teardown", infra.overlay.events)

        # verify_setup / verify_teardown with split lifecycle
        ctx = verify.Ctx(root=str(self.ws), experiment_dir=str(self.ws),
                         workspace=str(self.ws), artifacts=str(self.ws),
                         out=str(self.ws), cid="c1", task="T1", variant="d1")
        ctx.substrate = infra.make_context(ctx)
        with mock.patch.object(ctx.substrate, "up") as up:
            res = infra.verify_setup(ctx, {})
        up.assert_called_once()
        self.assertEqual(res["OV"], "1")
        infra.verify_teardown(ctx, {})
        self.assertIn("verify_teardown", infra.overlay.events)

        # cell lifecycle verify_setup calls ensure()+reset(), verify_teardown calls reset()
        spec_cell = SubstrateSpec(kind="docker", lifecycle="cell")
        ccls = build_infra_class(spec_cell, name="DC")
        cvcls = type("DCV", (Variant,), {"ID": "dc", "INFRA": ccls, "SUBSTRATE": spec_cell})
        cinfra = ccls(cvcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        ctx.substrate = cinfra.make_context(ctx)
        with mock.patch.object(ctx.substrate, "ensure") as ens, \
                mock.patch.object(ctx.substrate, "reset") as rst, \
                mock.patch.object(ctx.substrate, "answers", return_value=True):
            env = cinfra.verify_setup(ctx, {})
            cinfra.verify_teardown(ctx, {})
        ens.assert_called_once()
        self.assertEqual(rst.call_count, 2)
        self.assertEqual(env, {"DOCKER_HOST": "tcp://fae-dind-c1:2375"})


class TestK8sSubstrateDetailed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_k8s_context_ensure_apply_load_down(self):
        kctx = K8sContext("c1", scope="verify", kubeconfig_dir=self.ws,
                          cluster_prefix="ec-", images=(ImageSpec(source="redis:7"),),
                          cpus=2.0, memory="3g", via_image=False, log=lambda m: None)
        self.assertEqual(kctx.env(), {"KUBECONFIG": str(self.ws / ".kubecfg")})
        with mock.patch.object(kctx.cluster, "create_or_resume") as cr, \
                mock.patch.object(kctx.cluster, "join") as join, \
                mock.patch.object(kctx.cluster, "kubeconfig", return_value="apiVersion: v1\n"), \
                mock.patch.object(kctx.cluster, "cap") as cap, \
                mock.patch.object(kctx.cluster, "load_images") as load_imgs, \
                mock.patch.dict(os.environ, {"CPUSET_MEASURED": "0-1"}):
            kctx.ensure(container="fae-verify-c1")
        cr.assert_called_once()
        join.assert_called_once_with("fae-verify-c1")
        self.assertTrue(kctx.kubeconfig.is_file())
        cap.assert_called_once_with(2.0, "3g", cpuset="0-1")
        load_imgs.assert_called_once_with(["redis:7"])

        with mock.patch.object(kctx, "kubectl", return_value=subprocess.CompletedProcess([], 0)) as kctl:
            kctx.apply("/m.yaml", namespace="ns1", server_side=True)
        kctl.assert_called_once_with("apply", "--server-side", "-f", "/m.yaml", "-n", "ns1")

        with mock.patch.object(kctx.cluster, "running", return_value=True):
            self.assertEqual(kctx.containers(), [kctx.node])
        with mock.patch.object(kctx.cluster, "delete") as delete:
            kctx.down()
        delete.assert_called_once()
        self.assertFalse(kctx.kubeconfig.exists())

    def test_k8s_substrate_ok_sweep_cell_and_verify_hooks(self):
        spec = SubstrateSpec(kind="k8s", lifecycle="split", access=True,
                             cluster_prefix="ec-", overlay=_SampleOverlay)
        cls = build_infra_class(spec, name="KS")
        vcls = type("KV", (Variant,), {"ID": "k1", "INFRA": cls, "SUBSTRATE": spec, "ACCESS_INFRA": True})
        infra = cls(vcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        with mock.patch.object(infra_base, "ok", return_value=False):
            self.assertFalse(infra.ok())
        with mock.patch.object(infra_base, "ok", return_value=True):
            self.assertTrue(infra.ok())
        with mock.patch.object(infra_base, "daemon_answers", return_value=True):
            self.assertTrue(infra.alive())

        # cell_setup returns merged AgentAccess (K8s kubeconfig + network + overlay)
        fake_sub = mock.Mock(network="kind", kubeconfig=self.ws / "kubeconfig")
        with mock.patch.object(K8sContext, "ensure", return_value=fake_sub):
            acc = infra.cell_setup()
        self.assertIsInstance(acc, AgentAccess)
        self.assertEqual(acc.network, "kind")
        self.assertEqual(acc.env["KUBECONFIG"], "/home/node/.kube/config")
        self.assertEqual(acc.env["OV_URL"], "http://ov:9000")

        with mock.patch.object(K8sContext, "down") as down:
            infra.cell_teardown()
        down.assert_called_once_with(force=True)

        # verify_setup joins verify_container and runs overlay; verify_teardown calls down()
        ctx = verify.Ctx(root=str(self.ws), experiment_dir=str(self.ws),
                         workspace=str(self.ws), artifacts=str(self.ws),
                         out=str(self.ws), cid="c1", task="T1", variant="k1")
        ctx.verify_container = "fae-verify-c1"
        ctx.substrate = infra.make_context(ctx)
        with mock.patch.object(ctx.substrate, "up") as up, \
                mock.patch.object(ctx.substrate, "down") as vdown:
            res = infra.verify_setup(ctx, {})
            infra.verify_teardown(ctx, {})
        up.assert_called_once_with(container="fae-verify-c1")
        vdown.assert_called_once()
        self.assertEqual(res["OV"], "1")

        # cell lifecycle: _verify_ctx uses scope="cell" and verify_teardown keeps cluster up
        cspec = SubstrateSpec(kind="k8s", lifecycle="cell", cluster_prefix="ec-")
        ccls = build_infra_class(cspec, name="KC")
        cvcls = type("KCV", (Variant,), {"ID": "kc", "INFRA": ccls, "SUBSTRATE": cspec})
        cinfra = ccls(cvcls, cid="c1", ws=self.ws, root=self.ws, conf=None)
        csub = cinfra.make_context(ctx)
        self.assertEqual(csub.scope, "cell")
        self.assertEqual(csub.kubeconfig_dir, self.ws)
        ctx.substrate = csub
        with mock.patch.object(csub, "down") as cdown:
            cinfra.verify_teardown(ctx, {})
        cdown.assert_not_called()

        # sweep() with host kind vs Docker-only fallback
        with mock.patch("shutil.which", return_value="/usr/bin/kind"), \
                mock.patch("fae.cell.substrate.k8s.Cluster.sweep") as sw:
            cls.sweep()
        sw.assert_called_once_with("ec-")

        fake_exp = SimpleNamespace(definition=SimpleNamespace(variants={"k1": vcls}))
        fake_inst = mock.Mock(tool=mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")))
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("fae.experiment.exp", return_value=fake_exp), \
                mock.patch("fae.experiment.variants.infra_for", return_value=fake_inst), \
                mock.patch("fae.cell.substrate.k8s.Cluster.sweep") as sw:
            cls.sweep()
        sw.assert_called_once()
        self.assertEqual(sw.call_args.args[0], "ec-")
        self.assertIn("run", sw.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
