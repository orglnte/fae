"""An infra class owns its files in the cell's folder: the endpoints its setup
records (ENV_FILE) and the state it keeps (RUN_DIR). The verify reads them
through the infra, never by name."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell.infra.base import Infra


class _Recording(Infra):
    ENV_FILE = "x.env"
    RUN_DIR = ".x-state"


class _Narrow(_Recording):
    VERIFY_ADOPTS = ("NAMESPACE",)


def _infra(cls, ws):
    cell = SimpleNamespace(cid="c", ws=ws, root=ws, conf=None)
    return cls(SimpleNamespace(), cell)


class TestTheInfraOwnsItsFiles(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_what_setup_records_reads_back(self):
        i = _infra(_Recording, self.ws)
        i.record_env([("DOCKER_HOST", "tcp://d:2375"), ("NAMESPACE", "ns")])
        self.assertEqual(i.env(), {"DOCKER_HOST": "tcp://d:2375", "NAMESPACE": "ns"})
        self.assertEqual(i.env_file, self.ws / "x.env")

    def test_quotes_and_export_are_read_as_a_shell_would(self):
        (self.ws / "x.env").write_text("export A='1'\nB=\"two\"\nnot a line\n")
        self.assertEqual(_infra(_Recording, self.ws).env(), {"A": "1", "B": "two"})

    def test_the_verify_adopts_only_what_the_class_names(self):
        i = _infra(_Narrow, self.ws)
        i.record_env([("KUBECONFIG", "/sandbox"), ("NAMESPACE", "ns")])
        self.assertEqual(i.verify_env(), {"NAMESPACE": "ns"})
        self.assertEqual(_infra(_Recording, self.ws).verify_env(),
                         {"KUBECONFIG": "/sandbox", "NAMESPACE": "ns"})

    def test_nothing_recorded_is_nothing_adopted(self):
        self.assertEqual(_infra(_Recording, self.ws).env(), {})
        self.assertEqual(_infra(Infra, self.ws).env(), {})

    def test_the_run_dir_is_the_classes(self):
        self.assertEqual(_infra(_Recording, self.ws).run_dir, self.ws / ".x-state")
        self.assertIsNone(_infra(Infra, self.ws).run_dir)


if __name__ == "__main__":
    unittest.main()
