"""Seeding a cell workspace.

What a cell is measured on is decided here: which files its variant hands the
agent, which it may author, and what the manifest says the seed was. These pin
the parts a port can get wrong silently.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT, runs

ROOT = Path(ROOT)

from fae.cell import config as cfgmod      # noqa: E402
from fae.cell import prepare               # noqa: E402


class PrepareTestCase(unittest.TestCase):

    CID = "testpy_high_beta_apidocs_T1_r1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws_root = Path(self._tmp.name)
        self.cfg = cfgmod.load(ROOT)
        # a fresh prepare writes the lock plane's log; never the real one
        self.transitions = self.ws_root / "transitions.log"
        env = mock.patch.dict(os.environ, {"TRANSITIONS_LOG": str(self.transitions)})
        env.start()
        self.addCleanup(env.stop)

    def seed(self, variant="beta_apidocs", task="T1",
             cid=None, **kw):
        return prepare.prepare(cid or self.CID, task, variant, "1",
                               workspaces=self.ws_root, root=ROOT,
                               cfg=self.cfg, **kw)


class TestTheSeededSurface(PrepareTestCase):

    def test_it_seeds_the_task_and_the_docs(self):
        ws = self.seed()
        a = ws / "artifacts"
        self.assertTrue((a / "TODO.md").is_file())
        self.assertTrue((a / "docs" / "project.md").is_file())
        self.assertTrue((a / "docs" / "beta.md").is_file())
        self.assertTrue((ws / "PROMPT.md").is_file())

    def test_a_later_template_directory_wins(self):
        common, overlay = runs.experiment.definition().variant("beta_apidocs").TEMPLATE
        both = {p.relative_to(common) for p in common.rglob("*") if p.is_file()} & \
               {p.relative_to(overlay) for p in overlay.rglob("*") if p.is_file()}
        if not both:
            self.skipTest("no file exists in both template directories")
        ws = self.seed()
        for rel in both:
            self.assertEqual((ws / "artifacts" / rel).read_bytes(),
                             (overlay / rel).read_bytes(), rel)

    def test_each_input_lands_at_its_workspace_path(self):
        cls = runs.experiment.definition().variant("beta_apidocs")
        ws = self.seed()
        for rel, src in cls.INPUTS.items():
            self.assertEqual((ws / "artifacts" / rel).read_bytes(), src.read_bytes(), rel)

    def test_the_manifest_covers_every_seeded_file(self):
        ws = self.seed()
        a = ws / "artifacts"
        on_disk = {p.relative_to(a).as_posix() for p in a.rglob("*")
                   if p.is_file() and ".git/" not in p.relative_to(a).as_posix()
                   and p.name != ".skeleton_manifest" and p.name != ".gitignore"}
        listed = {l.split("\t")[0]
                  for l in (ws / ".skeleton_manifest").read_text().splitlines()}
        self.assertTrue(on_disk <= listed, f"unlisted: {on_disk - listed}")

    def test_the_manifest_is_outside_the_agents_mount(self):
        ws = self.seed()
        self.assertTrue((ws / ".skeleton_manifest").is_file())
        self.assertFalse((ws / "artifacts" / ".skeleton_manifest").exists())

    def test_the_manifest_records_lines_and_sha(self):
        import hashlib
        ws = self.seed()
        a = ws / "artifacts"
        for line in (ws / ".skeleton_manifest").read_text().splitlines():
            rel, lines, sha = line.split("\t")
            data = (a / rel).read_bytes()
            self.assertEqual(int(lines), data.count(b"\n"), rel)
            self.assertEqual(sha, hashlib.sha256(data).hexdigest(), rel)

    def test_the_manifest_is_deterministic(self):
        # Ordering must not depend on the machine's locale, which is what a
        # shell `sort` gives.
        first = (self.seed() / ".skeleton_manifest").read_text()
        second_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        ws2 = prepare.prepare(self.CID, "T1", "beta_apidocs", "1",
                              workspaces=second_root, root=ROOT, cfg=self.cfg)
        self.assertEqual((ws2 / ".skeleton_manifest").read_text(),
                         first)

    def test_the_workspace_is_the_agents_own_git_repo(self):
        ws = self.seed()
        a = ws / "artifacts"
        self.assertTrue((a / ".git").is_dir())
        log = subprocess.run(["git", "-C", str(a), "log", "--oneline"],
                             capture_output=True, text=True)
        self.assertIn("seed:", log.stdout)
        branch = subprocess.run(["git", "-C", str(a), "branch", "--show-current"],
                                capture_output=True, text=True)
        self.assertEqual(branch.stdout.strip(), "task/T1")


class TestFixedFilesAreSeededReadOnly(PrepareTestCase):
    """A deterrent, not the guarantee — restore-and-judge is. But the modes
    decide what an agent hits at the moment it tries to write."""

    def test_docs_are_read_only(self):
        ws = self.seed()
        for rel in ("TODO.md", "docs/project.md"):
            mode = (ws / "artifacts" / rel).stat().st_mode & 0o777
            self.assertEqual(mode, 0o444, rel)

    def test_the_authorable_app_tree_stays_writable(self):
        ws = self.seed()
        app = [p for p in (ws / "artifacts" / "app").rglob("*") if p.is_file()]
        self.assertTrue(app)
        for p in app:
            self.assertTrue(p.stat().st_mode & 0o200, p)

    def test_a_file_the_variant_lets_the_agent_author_stays_writable(self):
        # the seal keys on the variant's AUTHORING_SURFACE: the declared file stays
        # writable while the rest of the skeleton is read-only
        ws = self.seed()
        m = ws / "artifacts" / "declaration.toml"
        self.assertTrue(m.stat().st_mode & 0o200)
        self.assertEqual((ws / "artifacts" / "TODO.md").stat().st_mode & 0o777, 0o444)


    def test_a_resume_lifts_an_old_seal_off_the_authorable_file(self):
        # A workspace sealed by an earlier prepare that read declaration.toml as
        # fixed keeps that mode until something opens it; preparing the same
        # cell again (a resume) must, and must keep the fixed files sealed.
        ws = self.seed()
        m = ws / "artifacts" / "declaration.toml"
        m.chmod(0o444)
        (ws / "artifacts" / "TODO.md").chmod(0o644)
        again = self.seed()
        self.assertEqual(again, ws)
        self.assertTrue(os.access(m, os.W_OK))
        self.assertEqual((ws / "artifacts" / "TODO.md").stat().st_mode & 0o777, 0o444)

class TestWhatTheVariantHands(PrepareTestCase):
    """The variant file is the whole of what the agent is given: what it does
    not name is not seeded, and what it names must exist."""

    def test_each_variant_gets_its_own_doc(self):
        ws = self.seed(variant="alpha_howto", cid="testpy_high_alpha_howto_T1_r1")
        cls = runs.experiment.definition().variant("alpha_howto")
        self.assertEqual((ws / "artifacts" / "docs" / "alpha.md").read_bytes(),
                         cls.INPUTS["docs/alpha.md"].read_bytes())

    def test_an_unknown_variant_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            self.seed(variant="beta_howto", cid="testpy_high_beta_howto_T1_r1")

    def test_a_missing_input_is_refused_not_skipped(self):
        cls = runs.experiment.definition().variant("beta_apidocs")
        inputs = {**cls.INPUTS, "docs/beta.md": Path(self._tmp.name) / "gone.md"}
        with mock.patch.object(cls, "INPUTS", inputs):
            with self.assertRaisesRegex(FileNotFoundError, "docs/beta.md"):
                self.seed()

    def test_an_input_the_template_also_provides_is_refused(self):
        cls = runs.experiment.definition().variant("beta_apidocs")
        inputs = {**cls.INPUTS, "declaration.toml": cls.INPUTS["TODO.md"]}
        with mock.patch.object(cls, "INPUTS", inputs):
            with self.assertRaisesRegex(FileNotFoundError, "template also provides"):
                self.seed()

    def test_the_variant_reaches_cell_env(self):
        env = (self.seed() / "cell.env").read_text()
        self.assertIn("VARIANT=beta_apidocs\n", env)
        self.assertIn("ATTEMPT_BUDGET=10", env)
        self.assertNotIn("REFERENCE=", env)

    def test_a_reference_cell_gets_the_known_answer(self):
        cls = runs.experiment.definition().variant("beta_apidocs")
        ws = self.seed(reference=True, cid="testpy_high_beta_apidocs_T1_r2")
        for p in cls.REFERENCE.rglob("*"):
            if p.is_file():
                rel = p.relative_to(cls.REFERENCE)
                self.assertEqual((ws / "artifacts" / rel).read_bytes(), p.read_bytes(), rel)
        self.assertIn("REFERENCE=1\n", (ws / "cell.env").read_text())


class TestTheImplementationIsRecorded(PrepareTestCase):

    def test_cell_env_carries_the_impl_it_was_prepared_with(self):
        from fae.cell import IMPL
        ws = self.seed(impl=IMPL)
        self.assertIn(f"IMPL={IMPL}\n", (ws / "cell.env").read_text())


class TestSafeWipe(PrepareTestCase):
    """Two-stage: nothing is deleted, it is moved for review."""

    def test_it_moves_rather_than_deletes(self):
        ws = self.seed()
        (ws / "marker").write_text("x")
        dest = prepare.safe_wipe(ws, self.ws_root)
        self.assertFalse(ws.exists())
        self.assertTrue((dest / "marker").is_file())
        self.assertIn(".to_be_deleted", str(dest))

    def test_it_refuses_a_path_outside_the_workspaces_dir(self):
        with self.assertRaises(ValueError):
            prepare.safe_wipe(Path("/tmp/elsewhere_r1"), self.ws_root)

    def test_it_refuses_a_basename_that_is_not_a_cell_id(self):
        d = self.ws_root / "not-a-cell"
        d.mkdir()
        with self.assertRaises(ValueError):
            prepare.safe_wipe(d, self.ws_root)

    def test_it_refuses_a_relative_path(self):
        with self.assertRaises(ValueError):
            prepare.safe_wipe(Path("workspaces/x_r1"), self.ws_root)

    def test_fresh_reseeds_through_it(self):
        ws = self.seed()
        (ws / "artifacts" / "authored.py").write_text("agent work\n")
        self.seed(fresh=True)
        self.assertFalse((ws / "artifacts" / "authored.py").exists())
        self.assertTrue(any(self.ws_root.joinpath(".to_be_deleted").iterdir()))

    def test_a_wiped_workspace_retires_its_cell_in_the_transitions_log(self):
        # the id now names a new cell; the conformance replay must see where
        # the old one ended
        self.seed()
        self.seed(fresh=True)
        lines = [l.split("\t") for l in self.transitions.read_text().splitlines()]
        self.assertEqual([(l[1], l[2]) for l in lines], [("Retire", self.CID)])
        self.assertIn(".to_be_deleted", lines[0][3])

    def test_a_fresh_prepare_of_nothing_retires_nothing(self):
        self.seed(fresh=True)
        self.assertFalse(self.transitions.exists())


class TestIdempotence(PrepareTestCase):

    def test_re_preparing_does_not_clobber_authored_work(self):
        ws = self.seed()
        authored = ws / "artifacts" / "app" / "provisioning_impl.py"
        authored.write_text("# the agent's answer\n")
        (ws / "iterations.log").write_text("existing ledger\n")
        self.seed()
        self.assertEqual(authored.read_text(), "# the agent's answer\n")
        self.assertEqual((ws / "iterations.log").read_text(), "existing ledger\n")


class TestOneImplementation(unittest.TestCase):
    """prepare() is the one seeding; the driver calls it directly and
    `cli.py experiment prepare` (Experiment.prepare) asks each cell of the
    matrix to prepare itself."""

    def test_the_driver_calls_the_module(self):
        src = (ROOT / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def prepare(self"):]
        self.assertIn("_prepare.prepare(", body[:body.index("\n    def ")])

    def test_experiment_prepare_asks_each_cell_to_prepare_itself(self):
        src = (ROOT / "fae" / "experiment.py").read_text()
        body = src[src.index("    def prepare(self"):src.index("    def init(self")]
        self.assertIn(".prepare(fresh=fresh)", body)
        self.assertNotIn("_prepare.prepare(", body)
        self.assertNotIn("subprocess", body)

    def test_it_is_in_the_guarded_surface(self):
        from fae.cell import config as cfgmod
        cfgmod._cache.clear()
        extras = cfgmod.load(ROOT).values.get("FP_EXTRA_FILES", "")
        self.assertIn("fae/cell/prepare.py", extras)


if __name__ == "__main__":
    unittest.main()
