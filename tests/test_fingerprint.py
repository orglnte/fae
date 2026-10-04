"""fae/cell/rig.py:fp() — the fingerprint that pins a cell: well formed,
deterministic, moved by every guarded byte, fatal on a missing guarded file."""
import os
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae.experiment import config as _config  # noqa: E402
from fae.cell import rig  # noqa: E402


class TestTheFingerprint(unittest.TestCase):
    """fp: 64-hex, deterministic, sensitive to every guarded byte, FATAL on
    a missing FP_EXTRA_FILES entry (a silently skipped file would quietly
    shrink the guarded surface)."""

    @classmethod
    def setUpClass(cls):
        cls.extras = _config.load(ROOT).values.get("FP_EXTRA_FILES", "")

    def fp(self, extra=""):
        return rig.fp(ROOT, {"FP_EXTRA_FILES": f"{self.extras} {extra}".strip(),
                             "EXPERIMENT_DIR": os.environ["EXPERIMENT_DIR"]})

    def test_deterministic_and_well_formed(self):
        fp1 = self.fp()
        self.assertRegex(fp1, r"^[0-9a-f]{64}$")
        self.assertEqual(self.fp(), fp1)

    def test_every_guarded_byte_moves_the_hash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write("guarded\n")
            probe = f.name
        self.addCleanup(os.unlink, probe)
        with_probe = self.fp(extra=probe)
        Path(probe).write_text("guarded, changed\n")
        self.assertNotEqual(self.fp(extra=probe), with_probe)

    def test_a_missing_guarded_file_is_fatal_not_skipped(self):
        with self.assertRaises(RuntimeError) as cm:
            self.fp(extra="/nonexistent/guarded.sh")
        self.assertIn("FATAL _fp", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
