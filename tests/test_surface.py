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
    def test_record_lists_every_file_and_writes_outside_the_agents_mount(self):
        rows = self.s.record()
        self.assertEqual([r for r, _, _ in rows],
                         ["Dockerfile", "app/main.py", "app/provisioning_impl.py", "declaration.toml"])
        self.assertEqual(self.s.rows(), rows)
        self.assertTrue((self.art.parent / MANIFEST).is_file())
        self.assertFalse((self.art / MANIFEST).exists())

    def test_a_manifest_left_inside_artifacts_is_adopted(self):
        rows = self.s.record()
        (self.art.parent / MANIFEST).rename(self.art / MANIFEST)
        self.assertEqual(Surface(self.art, "beta").rows(), rows)
        self.assertTrue((self.art.parent / MANIFEST).is_file())
        self.assertFalse((self.art / MANIFEST).exists())

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


class TestTheManifestIsNotTheAgents(SurfaceCase):
    def test_a_fixed_file_and_a_forged_row_inside_artifacts_are_still_healed(self):
        self.s.record()
        (self.art / "Dockerfile").chmod(0o644)
        (self.art / "Dockerfile").write_text("FROM evil\n")
        forged = "".join(l.replace(l.split("\t")[2], "0" * 64) + "\n"
                         for l in (self.art.parent / MANIFEST).read_text().splitlines())
        (self.art / MANIFEST).write_text(forged)
        self.assertEqual(self.s.heal(self.skel / "common", self.skel / "beta"), ["Dockerfile"])
        self.assertEqual((self.art / "Dockerfile").read_text(), "FROM x\n")


class TestEvict(SurfaceCase):
    def test_a_file_outside_the_surface_is_moved_out_and_kept(self):
        self.s.record()
        _w(self.art / "conftest.py", "import sys\n")
        _w(self.art / "tests" / "test_a.py", "def test(): pass\n")
        dest = self.art.parent / ".out-of-surface" / "attempt-1"
        self.assertEqual(self.s.evict(dest), ["conftest.py", "tests/test_a.py"])
        self.assertFalse((self.art / "conftest.py").exists())
        self.assertFalse((self.art / "tests").exists())
        self.assertEqual((dest / "tests" / "test_a.py").read_text(), "def test(): pass\n")
        self.assertEqual(self.s.check(), [])

    def test_authorable_new_files_stay(self):
        self.s.record()
        _w(self.art / "app" / "new_module.py", "x = 1\n")
        self.assertEqual(self.s.evict(self.art.parent / "out"), [])
        self.assertTrue((self.art / "app" / "new_module.py").is_file())

    def test_the_rigs_git_and_tool_caches_are_left_alone(self):
        self.s.record()
        _w(self.art / ".git" / "HEAD", "ref\n")
        _w(self.art / ".gitignore", "*.pyc\n")
        _w(self.art / "app" / "__pycache__" / "main.cpython-312.pyc", "x")
        _w(self.art / ".pytest_cache" / "v" / "lastfailed", "{}")
        self.assertEqual(self.s.evict(self.art.parent / "out"), [])

    def test_a_forged_manifest_inside_artifacts_is_itself_a_stray(self):
        self.s.record()
        _w(self.art / MANIFEST, "Dockerfile\t1\tforged\n")
        self.assertEqual(self.s.strays(), [MANIFEST])


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

    def test_a_stray_left_after_heal_is_named(self):
        self.s.record()
        _w(self.art / "sitecustomize.py", "import os\n")
        self.assertEqual(self.s.check(), ["sitecustomize.py (outside surface)"])

    def test_no_manifest_is_nothing_to_check(self):
        self.assertEqual(self.s.check(), [])


if __name__ == "__main__":
    unittest.main()
