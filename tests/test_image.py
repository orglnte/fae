"""fae/cell/image.py — an image is a Dockerfile directory tagged by its
content: the same content is the same tag everywhere, a changed pin or
copied source is a new one; the verify container and the cell network are
named here, once."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

sys.path.insert(0, str(ROOT))
from fae.cell import image  # noqa: E402


class ImageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.d = self.root / "verifier"
        self.d.mkdir()
        (self.d / "Dockerfile").write_text("FROM python:3.12.3-slim\n")


class TestTheTagIsTheContent(ImageCase):
    def test_same_content_same_tag_across_directories(self):
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "Dockerfile").write_text("FROM python:3.12.3-slim\n")
        self.assertEqual(image.tag("x-verifier", self.d), image.tag("x-verifier", other))
        self.assertRegex(image.tag("x-verifier", self.d), r"^fae-x-verifier:[0-9a-f]{12}$")

    def test_a_changed_pin_is_a_new_tag(self):
        before = image.tag("x", self.d)
        (self.d / "Dockerfile").write_text("FROM python:3.12.4-slim\n")
        self.assertNotEqual(image.tag("x", self.d), before)

    def test_a_file_beside_the_dockerfile_counts(self):
        before = image.tag("x", self.d)
        (self.d / "requirements.txt").write_text("redis==8.0.1\n")
        self.assertNotEqual(image.tag("x", self.d), before)

    def test_the_staged_context_counts_and_pycache_does_not(self):
        src = self.root / "sdk"
        (src / "pkg").mkdir(parents=True)
        (src / "pkg" / "a.py").write_text("x = 1\n")
        before = image.tag("x", self.d, context=[(src, "sdk")])
        (src / "pkg" / "__pycache__").mkdir()
        (src / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
        self.assertEqual(image.tag("x", self.d, context=[(src, "sdk")]), before)
        (src / "pkg" / "a.py").write_text("x = 2\n")
        self.assertNotEqual(image.tag("x", self.d, context=[(src, "sdk")]), before)

    def test_the_base_image_counts(self):
        self.assertNotEqual(image.tag("x", self.d, base="fae-v:1"),
                            image.tag("x", self.d, base="fae-v:2"))

    def test_no_dockerfile_is_refused(self):
        with self.assertRaises(RuntimeError):
            image.tag("x", self.root)


class TestTheBuild(ImageCase):
    def test_ensure_builds_only_when_the_daemon_lacks_the_tag(self):
        calls = []

        def fake(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["docker", "image", "inspect"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        src = self.root / "sdk"
        src.mkdir()
        (src / "pyproject.toml").write_text("[project]\nname='sdk'\n")
        with mock.patch.object(image.subprocess, "run", side_effect=fake):
            tag = image.ensure("x", self.d, context=[(src, "some-sdk")], base="fae-b:1",
                               log=lambda m: None)
        build = next(c for c in calls if c[:2] == ["docker", "build"])
        self.assertEqual(build[build.index("-t") + 1], tag)
        self.assertIn("BASE=fae-b:1", build)
        staged = Path(build[-1])
        self.assertNotEqual(staged, self.d, "the build runs on a staged copy")
        calls.clear()
        with mock.patch.object(image.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            image.ensure("x", self.d, context=[(src, "some-sdk")], base="fae-b:1")
        self.assertFalse(any(c[:2] == ["docker", "build"] for c in calls))

    def test_the_staged_tree_carries_the_context_under_its_name(self):
        seen = {}

        def fake(argv, **kw):
            if argv[:2] == ["docker", "build"]:
                stage = Path(argv[-1])
                seen["files"] = sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file())
            return mock.Mock(returncode=0, stdout="", stderr="")
        src = self.root / "sdk"
        (src / "pkg").mkdir(parents=True)
        (src / "pkg" / "a.py").write_text("x = 1\n")
        with mock.patch.object(image.subprocess, "run", side_effect=fake):
            image.build("fae-x:abc", self.d, context=[(src / "pkg", "some-sdk/pkg")], log=lambda m: None)
        self.assertEqual(seen["files"], ["Dockerfile", "some-sdk/pkg/a.py"])

    def test_a_failed_build_raises_with_the_daemons_words(self):
        with mock.patch.object(image.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="no such base")):
            with self.assertRaises(RuntimeError) as cm:
                image.build("fae-x:abc", self.d, log=lambda m: None)
        self.assertIn("no such base", str(cm.exception))


class TestTheDeclarations(ImageCase):
    def _definition(self, verifier_dir, variant_dir=None):
        class V:
            IMAGE_DIR = verifier_dir

            @classmethod
            def image_context(cls, conf):
                return []

        class Var:
            IMAGE_DIR = variant_dir
            INFRA = V

        d = mock.Mock()
        d.name = "fx"
        d.path = self.root
        d.verifier_class.return_value = V
        return d, Var

    def test_a_verifier_without_an_image_dir_is_refused(self):
        d, _ = self._definition(None)
        with self.assertRaises(RuntimeError):
            image.for_verifier(d, None)

    def test_a_variant_without_its_own_layer_uses_the_verifiers(self):
        d, var = self._definition(self.d)
        with mock.patch.object(image, "ensure", side_effect=lambda name, *a, **k: f"fae-{name}:t") as ens:
            self.assertEqual(image.for_variant(var, d, None), "fae-fx-verifier:t")
        self.assertEqual(ens.call_count, 1)

    def test_a_variant_layer_builds_from_the_verifiers_image(self):
        vdir = self.root / "alpha"
        vdir.mkdir()
        (vdir / "Dockerfile").write_text("ARG BASE\nFROM $BASE\n")
        d, var = self._definition(self.d, vdir)
        seen = []

        def ens(name, dockerfile_dir, context=(), base=None, log=None):
            seen.append((name, base))
            return f"fae-{name}:t"
        with mock.patch.object(image, "ensure", side_effect=ens):
            self.assertEqual(image.for_variant(var, d, None), "fae-fx-alpha:t")
        self.assertEqual(seen, [("fx-verifier", None), ("fx-alpha", "fae-fx-verifier:t")])


class TestTheRunArgv(unittest.TestCase):
    def test_a_writable_path_under_a_read_only_one_is_mounted_over_it(self):
        specs = image.mount_specs(read_only=("/r", "/r/ws", "/e"), writable=("/r/ws/.verify-out",))
        self.assertEqual(specs, [("/e", "ro"), ("/r", "ro"), ("/r/ws/.verify-out", "rw")])

    def test_a_path_both_read_only_and_writable_is_writable(self):
        self.assertEqual(image.mount_specs(read_only=("/w",), writable=("/w",)), [("/w", "rw")])

    def test_run_argv_marks_read_only_mounts(self):
        argv = image.run_argv("img", "n", ["x"], mounts=[("/r", "ro"), ("/r/out", "rw")],
                              socket=False, user=False)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-v"],
                         ["/r:/r:ro", "/r/out:/r/out"])

    def test_mounts_are_minimal_and_at_their_own_paths(self):
        self.assertEqual(image.mounts_for("/a/b", "/a", "/c", "/a/b/d", ""), ["/a", "/c"])

    def test_child_env_drops_the_hosts_own_and_secrets(self):
        env = image.child_env({"PATH": "x", "HOME": "y", "MODEL": "m", "MY_TOKEN": "t",
                               "X_API_KEY": "k", "DOCKER_HOST": "tcp://x",
                               "SSL_CERT_FILE": "/opt/local/etc/cert.pem",
                               "REQUESTS_CA_BUNDLE": "/etc/x", "VIRTUAL_ENV": "/v",
                               "DEFAULT_CA_BUNDLE_PATH": "/etc/y"},
                              HOME="/h", K=None)
        self.assertEqual(env, {"MODEL": "m", "HOME": "/h"})

    def test_run_argv_shape(self):
        argv = image.run_argv("img:1", "fae-verify-c", ["python3", "-m", "x"],
                              mounts=["/r", "/w"], env={"B": "2", "A": "1"}, workdir="/r",
                              network="fae-net-c", labels=(("fae-cell", "c"),))
        self.assertEqual(argv[:5], ["docker", "run", "--rm", "--name", "fae-verify-c"])
        self.assertIn("--user", argv)
        # the socket's group rides along, or the daemon refuses the caller's uid
        self.assertEqual(argv[argv.index("--group-add") + 1], str(image.socket_group()))
        self.assertEqual(argv[argv.index("--label") + 1], "fae-cell=c")
        self.assertEqual(argv[argv.index("--network") + 1], "fae-net-c")
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-v"],
                         ["/r:/r", "/w:/w", "/var/run/docker.sock:/var/run/docker.sock"])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-e"], ["A=1", "B=2"])
        self.assertEqual(argv[-4:], ["img:1", "python3", "-m", "x"])

    def test_the_names(self):
        self.assertEqual(image.verify_container("c1"), "fae-verify-c1")
        self.assertEqual(image.cell_network("c1"), "fae-net-c1")
        self.assertTrue(image.verify_container("c1").startswith(image.VERIFY_PREFIX))
        self.assertTrue(image.cell_network("c1").startswith(image.NET_PREFIX))


if __name__ == "__main__":
    unittest.main()
