"""fae/cell/agent_image.py: the agents' images, their clients vs upstream, and
the one readiness policy. No docker, no network — every probe is mocked."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fae.cell import agent_image as ai
from fae.cell import image as cimage
from fae.cell.agent_image import AgentImage


def images(root=None):
    root = Path(root or tempfile.mkdtemp())
    return AgentImage(root, SimpleNamespace(name="mini", path=Path("/mini"), active=(),
                                            variant=lambda v: None), conf={})


class ParseAndCompare(unittest.TestCase):
    def test_parse_versions_takes_the_number_per_tool(self):
        text = "claude=2.1.261 (Claude Code)\nopencode=1.18.29\nagy=1.1.27\n"
        self.assertEqual(ai.parse_versions(text),
                         {"claude": "2.1.261", "opencode": "1.18.29", "agy": "1.1.27"})

    def test_parse_versions_skips_a_tool_that_printed_no_number(self):
        text = "claude=2.1.261 (Claude Code)\nopencode=sh: opencode: not found\nagy=1.1.27\n"
        self.assertNotIn("opencode", ai.parse_versions(text))

    def test_stale_is_numeric_not_lexical(self):
        have = {"claude": "2.1.9", "opencode": "1.18.29", "agy": "1.1.27"}
        want = {"claude": "2.1.10", "opencode": "1.18.29", "agy": "1.1.27"}
        self.assertEqual(ai.stale(have, want), [("claude", "2.1.9", "2.1.10")])

    def test_stale_ignores_tools_unknown_on_either_side(self):
        self.assertEqual(ai.stale({"claude": "2.1.261"}, {"agy": "9.9.9"}), [])

    def test_installed_newer_than_upstream_is_not_stale(self):
        self.assertEqual(ai.stale({"claude": "2.2.0"}, {"claude": "2.1.261"}), [])


class UpstreamCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.im = images(self.tmp.name)

    def test_the_books_live_in_the_images_folder(self):
        self.assertEqual(self.im.upstream_book.path.parent.name, ".images")
        self.assertEqual(self.im.clients_book.path.parent.name, ".images")

    def test_second_lookup_within_ttl_does_not_hit_upstream(self):
        with mock.patch.object(AgentImage, "upstream", return_value={"claude": "2.1.261"}) as up:
            self.assertEqual(self.im.upstream_cached("arm64", now=1000)["claude"], "2.1.261")
            self.assertEqual(self.im.upstream_cached("arm64", now=1000 + 60)["claude"], "2.1.261")
            self.assertEqual(up.call_count, 1)

    def test_lookup_after_ttl_refreshes(self):
        with mock.patch.object(AgentImage, "upstream",
                               side_effect=[{"claude": "2.1.261"}, {"claude": "2.1.270"}]) as up:
            self.im.upstream_cached("arm64", now=1000)
            self.assertEqual(self.im.upstream_cached("arm64", now=1000 + ai.CHECK_TTL_S + 1)
                             ["claude"], "2.1.270")
            self.assertEqual(up.call_count, 2)

    def test_arch_change_invalidates_the_cache(self):
        with mock.patch.object(AgentImage, "upstream", return_value={}) as up:
            self.im.upstream_cached("arm64", now=1000)
            self.im.upstream_cached("amd64", now=1000)
            self.assertEqual(up.call_count, 2)


class EnsureCurrent(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.im = images()
        mock.patch.object(AgentImage, "arch", return_value="arm64").start()
        mock.patch.object(AgentImage, "base", return_value="fae-agent:latest").start()
        self.addCleanup(mock.patch.stopall)

    def test_current_image_is_left_alone(self):
        with mock.patch.object(AgentImage, "installed", return_value={"claude": "2.1.261"}), \
             mock.patch.object(AgentImage, "upstream_cached", return_value={"claude": "2.1.261"}), \
             mock.patch.object(AgentImage, "rebuild") as rb:
            self.assertTrue(self.im.ensure_current(log=self.log.append))
            rb.assert_not_called()
        self.assertEqual(self.log, [])

    def test_unreachable_upstream_never_blocks(self):
        with mock.patch.object(AgentImage, "installed", return_value={"claude": "2.1.197"}), \
             mock.patch.object(AgentImage, "upstream_cached", return_value={}), \
             mock.patch.object(AgentImage, "rebuild") as rb:
            self.assertTrue(self.im.ensure_current(log=self.log.append))
            rb.assert_not_called()

    def test_stale_image_is_rebuilt_with_the_wanted_versions(self):
        with mock.patch.object(AgentImage, "installed",
                               side_effect=[{"claude": "2.1.197"}, {"claude": "2.1.261"},
                                            {"claude": "2.1.261"}]), \
             mock.patch.object(AgentImage, "upstream_cached", return_value={"claude": "2.1.261"}), \
             mock.patch.object(AgentImage, "rebuild", return_value=0) as rb:
            self.assertTrue(self.im.ensure_current(log=self.log.append))
            rb.assert_called_once_with({"claude": "2.1.261"}, "fae-agent:latest")
        self.assertIn("claude 2.1.197 < 2.1.261", self.log[0])
        self.assertTrue(self.log[-1].startswith("agent image rebuilt"))

    def test_failed_rebuild_reports_false(self):
        with mock.patch.object(AgentImage, "installed", return_value={"claude": "2.1.197"}), \
             mock.patch.object(AgentImage, "upstream_cached", return_value={"claude": "2.1.261"}), \
             mock.patch.object(AgentImage, "rebuild", return_value=1):
            self.assertFalse(self.im.ensure_current(log=self.log.append))
        self.assertIn("FAILED", self.log[-1])

    def test_rebuild_that_did_not_advance_reports_false(self):
        with mock.patch.object(AgentImage, "installed", return_value={"claude": "2.1.197"}), \
             mock.patch.object(AgentImage, "upstream_cached", return_value={"claude": "2.1.261"}), \
             mock.patch.object(AgentImage, "rebuild", return_value=0):
            self.assertFalse(self.im.ensure_current(log=self.log.append))
        self.assertIn("still behind", self.log[-1])


class RebuildArgs(unittest.TestCase):
    def test_rebuild_builds_the_base_dockerfile_with_versions_as_build_args(self):
        with mock.patch.object(ai.subprocess, "run") as run:
            run.return_value.returncode = 0
            images().rebuild({"claude": "2.1.261", "agy": "1.1.27", "opencode": "1.18.29"}, "x:y")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["docker", "build"])
        for arg in ("CLAUDE_CODE_VERSION=2.1.261", "AGY_VERSION=1.1.27", "OPENCODE_VERSION=1.18.29"):
            self.assertIn(arg, argv)
        self.assertEqual(argv[argv.index("-t") + 1], "x:y")
        self.assertTrue(argv[argv.index("-f") + 1].endswith("agent-container/Dockerfile"))


class DockerfileContract(unittest.TestCase):
    def test_dockerfile_declares_every_build_arg_rebuild_passes(self):
        text = (Path(__file__).resolve().parents[1]
                / "fae" / "agent-container" / "Dockerfile").read_text()
        for arg in ai.BUILD_ARG.values():
            self.assertIn(f"ARG {arg}=", text)

    def test_build_sh_forwards_every_build_arg(self):
        text = (Path(__file__).resolve().parents[1]
                / "fae" / "agent-container" / "build.sh").read_text()
        for arg in ai.BUILD_ARG.values():
            self.assertIn(arg, text)


class TheLayer(unittest.TestCase):
    """Each variant's layer over the base: built when its variant declares
    one, rebuilt when what it is built from moved."""

    def _variant(self, layer):
        from fae.experiment.variants.base import Variant
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        if layer:
            (d / "Dockerfile").write_text("ARG BASE\nFROM $BASE\n")
        return type("V", (Variant,), {"ID": "alpha_sealed",
                                      "AGENT_IMAGE_DIR": d if layer else None})

    def _def(self):
        return SimpleNamespace(name="mini", path=Path("/mini"))

    def test_a_current_layer_is_left_alone_and_a_new_base_rebuilds_it(self):
        v, root = self._variant(True), Path(tempfile.mkdtemp())
        want1 = cimage.content_hash(v.AGENT_IMAGE_DIR, [], base="sha256:base1")
        with mock.patch.object(ai, "image_id", return_value="sha256:base1"), \
                mock.patch.object(ai, "label", return_value=want1), \
                mock.patch.object(cimage, "build") as build:
            ai.for_agent(self._def(), None, v, root)
        build.assert_not_called()
        with mock.patch.object(ai, "image_id", return_value="sha256:base2"), \
                mock.patch.object(ai, "label", return_value=want1), \
                mock.patch.object(cimage, "build") as build:
            ai.for_agent(self._def(), None, v, root)
        build.assert_called_once()
        self.assertEqual(build.call_args.kwargs["labels"],
                         (("fae-content", cimage.content_hash(v.AGENT_IMAGE_DIR, [], base="sha256:base2")),))

    def test_a_missing_base_is_named_not_built_here(self):
        with mock.patch.object(ai, "image_id", return_value=""):
            with self.assertRaises(RuntimeError) as e:
                ai.for_agent(self._def(), None, self._variant(True), Path(tempfile.mkdtemp()))
        self.assertIn("build the base first", str(e.exception))

    def test_ready_builds_the_missing_base_then_checks_clients_then_every_layer(self):
        order = []
        layers = {"fae-x-agent-alpha:latest": object(), "fae-x-agent-beta:latest": object()}
        im = images()
        with mock.patch.object(cimage, "present", return_value=False), \
                mock.patch.object(AgentImage, "base", return_value="fae-agent:latest"), \
                mock.patch.object(AgentImage, "rebuild", side_effect=lambda w, i: order.append("base") or 0), \
                mock.patch.object(AgentImage, "ensure_current", side_effect=lambda i, log: order.append("clients") or True), \
                mock.patch.object(AgentImage, "layers", return_value=layers), \
                mock.patch.object(ai, "for_agent", side_effect=lambda d, c, cls, r, log: order.append("layer")):
            self.assertTrue(im.ready(log=lambda m: None))
        self.assertEqual(order, ["base", "clients", "layer", "layer"])

    def test_ready_for_one_variant_builds_only_its_layer_under_the_image_lock(self):
        im, v = images(), self._variant(True)
        built, held = [], []
        real_lock = ai.mutex.fs_lock

        def lock(path):
            held.append(Path(path).name)
            return real_lock(path)

        with mock.patch.object(cimage, "present", return_value=True), \
                mock.patch.object(AgentImage, "ensure_current", return_value=True), \
                mock.patch.object(AgentImage, "tag", return_value="fae-x-agent-alpha:latest"), \
                mock.patch.object(ai.mutex, "fs_lock", side_effect=lock), \
                mock.patch.object(ai, "for_agent", side_effect=lambda d, c, cls, r, log: built.append(cls)):
            self.assertTrue(im.ready(v, log=lambda m: None))
        self.assertEqual(built, [v])
        self.assertEqual(held, ["image-lock"])


if __name__ == "__main__":
    unittest.main()
