"""Every variant's agents run in an image of their own: the base (every
model's client) plus the variant's tools layer, so one variant's agent never
finds another's tools or SDK. No docker: tags, paths and argv only."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401

from fae.experiment import config as _config
from fae.cell.cell import Cell
from fae.cell import agent_image as _image
from fae.cell import image as _cimage
from fae.experiment.variants.base import Variant


class _Def:
    name = "exp"
    path = Path("/exp")


def _variant(vid, layer_dir=None):
    return type(f"V_{vid}", (Variant,), {"ID": vid, "AGENT_IMAGE_DIR": layer_dir})


class TestTheTags(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_each_tools_directory_gets_its_own_layer_tag(self):
        a = _image.agent_tag(_Def, self.root, _variant("alpha_x", "/exp/variants/alpha/agent"))
        b = _image.agent_tag(_Def, self.root, _variant("beta_x", "/exp/variants/beta/agent"))
        self.assertEqual(a, "fae-exp-variants-alpha-agent:latest")
        self.assertEqual(b, "fae-exp-variants-beta-agent:latest")

    def test_variants_that_name_one_directory_share_one_tag(self):
        a = _image.agent_tag(_Def, self.root, _variant("alpha_x", "/exp/variants/alpha/agent"))
        b = _image.agent_tag(_Def, self.root, _variant("alpha_y", "/exp/variants/alpha/agent"))
        self.assertEqual(a, b)

    def test_a_variant_without_a_layer_runs_in_the_base(self):
        self.assertEqual(_image.agent_tag(_Def, self.root, _variant("alpha")), _image.BASE_AGENT)

    def test_the_experiments_own_base_is_used_when_its_root_has_one(self):
        self.assertEqual(_image.base_tag(_Def, self.root), _image.BASE_AGENT)
        (self.root / _image.BASE_DOCKERFILE).write_text("FROM scratch\n")
        self.assertEqual(_image.base_tag(_Def, self.root), "fae-exp-agent-base:latest")
        self.assertEqual(_image.base_dockerfile(self.root), self.root / _image.BASE_DOCKERFILE)
        self.assertEqual(_image.agent_tag(_Def, self.root, _variant("alpha")),
                         "fae-exp-agent-base:latest")

    def test_the_engines_base_is_the_fallback(self):
        self.assertEqual(_image.base_dockerfile(self.root).name, "Dockerfile")
        self.assertEqual(_image.base_dockerfile(self.root).parent.name, "agent-container")


class TestTheArgv(unittest.TestCase):
    def _argv(self, conf, image):
        with tempfile.TemporaryDirectory() as d:
            prompt = Path(d) / "PROMPT.md"
            prompt.write_text("do it")
            return Cell.agent_argv(conf, "m_high_alpha_sealed_apidocs_T1_r1", d,
                                            d, prompt, image=image)

    def test_the_cells_own_image_is_the_one_run(self):
        argv = self._argv({"AGENT_CLI": "claude", "AGENT_MODEL": "m", "AGENT_IMAGE": ""},
                          "fae-exp-agent-alpha:latest")
        self.assertIn("fae-exp-agent-alpha:latest", argv)

    def test_AGENT_IMAGE_forces_one_image_on_every_variant(self):
        argv = self._argv({"AGENT_CLI": "claude", "AGENT_MODEL": "m", "AGENT_IMAGE": "forced:1"},
                          "fae-exp-agent-alpha:latest")
        self.assertIn("forced:1", argv)
        self.assertNotIn("fae-exp-agent-alpha:latest", argv)


class TestTheConfig(unittest.TestCase):
    def test_experiment_init_writes_no_one_image_for_all_variants(self):
        src = (Path(_config.__file__)).read_text()
        body = src[src.index("def render_default_toml("):src.index("def render_default_toml(") + 4000]
        self.assertNotIn("agent_image =", body)

    def test_the_config_carries_no_image_unless_the_environment_forces_one(self):
        src = (Path(_config.__file__)).read_text()
        self.assertIn('v["AGENT_IMAGE"] = env.get("AGENT_IMAGE") or ""', src)


class TestTheLayerBuild(unittest.TestCase):
    def test_a_layer_is_built_from_the_base_when_its_label_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            layer = Path(d) / "layer"
            layer.mkdir()
            (layer / "Dockerfile").write_text("ARG BASE\nFROM $BASE\n")
            cls = _variant("alpha", str(layer))
            with mock.patch.object(_image, "image_id", return_value="sha256:base"), \
                 mock.patch.object(_image, "label", return_value="old"), \
                 mock.patch.object(_cimage, "build") as build:
                tag = _image.for_agent(_Def, {}, cls, Path(d), log=lambda *_: None)
            self.assertEqual(tag, "fae-exp-layer:latest")
            self.assertEqual(build.call_args.kwargs["base"], _image.BASE_AGENT)

    def test_no_layer_no_build(self):
        with mock.patch.object(_cimage, "build") as build:
            tag = _image.for_agent(_Def, {}, _variant("alpha"), Path("/nonexistent"),
                                   log=lambda *_: None)
        self.assertEqual(tag, _image.BASE_AGENT)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
