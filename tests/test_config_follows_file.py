"""fae/experiment/config.py load(): the cached Config is the file's as it is now."""
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.experiment import config as config_mod


class TestTheConfigFollowsItsFile(unittest.TestCase):
    """A process that outlives an edit of fae.toml (the conduct) reads the
    edited values on its next load, and an unchanged file is not rebuilt."""

    def test_an_edit_is_seen_and_no_edit_is_cached(self):
        with tempfile.TemporaryDirectory() as td:
            toml = Path(td) / "fae.toml"
            toml.write_text('[slots]\narm_keda_access = 1\n')
            first = config_mod.load(td, env={})
            self.assertIs(config_mod.load(td, env={}), first)
            toml.write_text('[slots]\narm_keda_access = 3\n')
            self.assertEqual(config_mod.load(td, env={}).get("ARM_SLOTS_KEDA_ACCESS"), "3")
            self.assertEqual(first.get("ARM_SLOTS_KEDA_ACCESS"), "1")


if __name__ == "__main__":
    unittest.main()
