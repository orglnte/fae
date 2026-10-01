"""fae/cell/surface.py: the authorable surface — recorded at seed time,
sealed, healed from the skeleton before judging, asserted before the gate."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)
from fae.cell.surface import MANIFEST, Surface, authorable


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class SurfaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.art = root / "artifacts"
        self.skel = root / "skeleton"
        _w(self.art / "Dockerfile", "FROM x\n")
        _w(self.art / "app" / "main.py", "app\n")
        _w(self.art / "app" / "provisioning_impl.py", "todo\n")
        _w(self.art / "declaration.toml", "[service]\n")
        _w(self.skel / "common" / "Dockerfile", "FROM x\n")
        _w(self.skel / "common" / "app" / "main.py", "app\n")
        _w(self.skel / "beta" / "declaration.toml", "[service]\n")
        self.s = Surface(self.art, "beta")


class TestRecordAndSeal(SurfaceCase):
    def test_record_lists_every_file_but_the_manifest(self):
        rows = self.s.record()
        self.assertEqual([r for r, _, _ in rows],
                         ["Dockerfile", "app/main.py", "app/provisioning_impl.py", "declaration.toml"])
        self.assertEqual(self.s.rows(), rows)
        self.assertTrue((self.art / MANIFEST).is_file())

    def test_seal_marks_fixed_files_read_only_and_opens_the_authorable_one(self):
        rows = self.s.record()
        (self.art / "declaration.toml").chmod(0o444)
        self.s.seal(rows)
        self.assertFalse(os.stat(self.art / "Dockerfile").st_mode & stat.S_IWUSR)
        self.assertTrue(os.stat(self.art / "declaration.toml").st_mode & stat.S_IWUSR)
        self.assertTrue(os.stat(self.art / "app" / "main.py").st_mode & stat.S_IWUSR)


class TestHeal(SurfaceCase):
    def test_a_changed_fixed_file_is_restored_and_rebaselined(self):
        self.s.record()
        (self.art / "Dockerfile").chmod(0o644)
        (self.art / "Dockerfile").write_text("FROM y\n")
        _w(self.skel / "common" / "Dockerfile", "FROM x2\n")      # the skeleton evolved
        self.assertEqual(self.s.heal(self.skel / "common", self.skel / "beta"), ["Dockerfile"])
        self.assertEqual((self.art / "Dockerfile").read_text(), "FROM x2\n")
        self.assertFalse(os.stat(self.art / "Dockerfile").st_mode & stat.S_IWUSR)
        self.assertEqual(self.s.check(), [])                        # row re-baselined

    def test_the_overlay_wins_over_common(self):
        _w(self.skel / "common" / "declaration.toml", "common\n")
        self.s = Surface(self.art, "alpha")                  # declaration is fixed for alpha
        self.s.record()
        (self.art / "declaration.toml").write_text("edited\n")
        _w(self.skel / "keda" / "declaration.toml", "overlay\n")
        self.assertEqual(self.s.heal(self.skel / "common", self.skel / "keda"), ["declaration.toml"])
        self.assertEqual((self.art / "declaration.toml").read_text(), "overlay\n")

    def test_an_authorable_edit_is_left_alone(self):
        self.s.record()
        (self.art / "app" / "provisioning_impl.py").write_text("done\n")
        self.assertEqual(self.s.heal(self.skel / "common", self.skel / "beta"), [])
        self.assertEqual((self.art / "app" / "provisioning_impl.py").read_text(), "done\n")


class TestCheck(SurfaceCase):
    def test_modified_and_deleted_fixed_files_are_named(self):
        _w(self.art / "README.md", "seed\n")
        self.s.record()
        (self.art / "Dockerfile").write_text("FROM y\n")
        (self.art / "README.md").unlink()
        (self.art / "app" / "main.py").unlink()          # app/ is authorable: not a violation
        self.assertEqual(self.s.check(), ["Dockerfile (modified)", "README.md (deleted)"])

    def test_authorable_edits_pass(self):
        self.s.record()
        (self.art / "declaration.toml").write_text("id = 1\n")
        (self.art / "app" / "provisioning_impl.py").write_text("done\n")
        self.assertEqual(self.s.check(), [])

    def test_no_manifest_is_nothing_to_check(self):
        self.assertEqual(self.s.check(), [])


if __name__ == "__main__":
    unittest.main()
