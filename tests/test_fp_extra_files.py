"""The fingerprint covers every .py of the engine's cell package, subpackages
included, and the experiment package (fae/experiment/) — found where the
engine is, never under the experiment root."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)
from fae.experiment import config as C

ENGINE = Path(C.__file__).resolve().parent.parent
CELL = ENGINE / "cell"


class TestFpExtraFiles(unittest.TestCase):
    def test_every_module_of_the_cell_package_is_hashed_subpackages_included(self):
        got = C._fp_extra_files(tempfile.mkdtemp(), []).split()
        want = (sorted(str(p) for p in CELL.rglob("*.py"))
                + sorted(str(p) for p in (ENGINE / "experiment").rglob("*.py")))
        self.assertEqual(got, want)
        self.assertTrue(any("/fae/cell/contrib/" in f for f in got))

    def test_a_root_holding_its_own_fae_tree_does_not_replace_the_engine(self):
        root = Path(tempfile.mkdtemp())
        (root / "fae" / "cell").mkdir(parents=True)
        (root / "fae" / "cell" / "impostor.py").write_text("")
        got = C._fp_extra_files(str(root), []).split()
        self.assertNotIn(str(root / "fae" / "cell" / "impostor.py"), got)
        self.assertIn(str(CELL / "cell.py"), got)

    def test_the_configuration_module_is_hashed(self):
        """It sets every value a verify reads: an edit to it mid-cell must
        change the fingerprint like an edit to the cell package does."""
        got = C._fp_extra_files(tempfile.mkdtemp(), []).split()
        self.assertIn(str(ENGINE / "experiment" / "config.py"), got)


if __name__ == "__main__":
    unittest.main()
