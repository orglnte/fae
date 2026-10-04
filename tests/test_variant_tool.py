"""Infra.tool — a tool the host does not carry, run by the driver in a
throwaway container of the variant's image."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT

from fae import paths as _paths
from fae.cell import image as _image
from fae.cell.infra import base
from fae.cell.variants.base import Variant


class Imaged(base.Infra):

    def image(self):
        return "fae-x-imaged:abc"


class TestTheToolContainer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "cell-x"
        self.ws.mkdir()
        cell = SimpleNamespace(cid="cell-x", variant="imaged", root=Path(ROOT),
                               ws=self.ws, conf=SimpleNamespace(get=lambda k, d=None: d,
                                                                exported={"REPO_ROOT": str(ROOT)}))
        self.v = Imaged(Variant, cell)

    def run_tool(self, **kw):
        seen = {}

        def fake_run(argv, **k):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "out", "")
        with mock.patch.dict("os.environ", {"SSL_CERT_FILE": "/host/ca.pem", "AGENT": "m"}), \
                mock.patch.object(base.subprocess, "run", fake_run):
            r = self.v.tool(["kind", "get", "clusters"], **kw)
        return r, seen["argv"]

    def test_the_argv_is_a_docker_run_of_the_image_with_the_socket_and_the_roots(self):
        r, argv = self.run_tool(env={"KUBECONFIG": str(self.ws / "k")}, network="kind")
        self.assertEqual(r.stdout, "out")
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        self.assertTrue(argv[argv.index("--name") + 1].startswith("fae-tool-cell-x-"))
        self.assertIn("--user", argv)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", argv)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        for p in (str(ROOT), str(self.ws)):
            self.assertIn(f"{p}:{p}", mounts)
        self.assertEqual(argv[argv.index("--network") + 1], "kind")
        self.assertEqual(argv[argv.index("-w") + 1], str(self.ws))
        self.assertEqual(argv[argv.index("--add-host") + 1], "host.docker.internal:host-gateway")
        env = dict(a.split("=", 1) for i, a in enumerate(argv) if i and argv[i - 1] == "-e")
        self.assertEqual(env["KUBECONFIG"], str(self.ws / "k"))
        self.assertEqual(env["REPO_ROOT"], str(ROOT))
        self.assertEqual(env["USER"], _image.CONTAINER_USER)
        self.assertEqual(env["HOME"], str(self.ws / ".tool-home"))
        self.assertEqual(env["AGENT"], "m")
        self.assertNotIn("SSL_CERT_FILE", env)
        self.assertEqual(argv[-4:], ["fae-x-imaged:abc", "kind", "get", "clusters"])

    def test_no_network_when_none_is_named(self):
        _, argv = self.run_tool()
        self.assertNotIn("--network", argv)

    def test_every_call_names_a_distinct_container(self):
        _, a = self.run_tool()
        _, b = self.run_tool()
        self.assertNotEqual(a[a.index("--name") + 1], b[b.index("--name") + 1])

    def test_the_reaper_knows_the_prefix(self):
        from fae.conduct import zombies
        d = mock.Mock()
        d.verifier_class.return_value.PREFIXES = {}
        with mock.patch.object(zombies._shared, "definition", return_value=d), \
                mock.patch("fae.cell.variants.registry", return_value={}):
            self.assertIn(("container", "fae-tool-"), zombies._prefixes())


if __name__ == "__main__":
    unittest.main()
