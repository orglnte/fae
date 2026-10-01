"""Per-attempt checkpoints: real git, real trees, in a temp workspace.

These exercise the Checkpoints class (fae/cell/checkpoints.py) itself rather
than reading it, because every claim here is about what git actually does —
where the repo lands, what the index touches, which changes move the tree hash.
A source-text test would assert the intent and miss the behaviour.

The tree hash is also the no-edit oracle and the equivalence key the bash and
Python cell implementations will be diffed on, so each property is stated with
its inverse: an edited tree must NOT hash the same, and an ignored file must.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae.cell.checkpoints import Checkpoints


def ckpt(cmd, ws, *rest):
    cp = Checkpoints(ws)
    if cmd == "init":
        cp.init()
        return ""
    if cmd == "tree":
        return cp.tree()
    if cmd == "commit":
        return cp.commit(rest[0])
    if cmd == "restore":
        return cp.restore(rest[0])
    raise AssertionError(f"unknown ckpt command {cmd!r}")


class CheckpointTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "cell"
        self.art = self.ws / "artifacts"
        (self.art / "app").mkdir(parents=True)
        self.write("app/main.py", "print('v1')\n")
        # What seed_skeleton leaves behind: the agent's OWN repo, and its
        # ignore list. Both are part of the surface these tests are about.
        self.write(".gitignore", "__pycache__/\n*.pyc\n.skeleton_manifest\n*.log\n")
        self.git("init", "-q")
        self.git("config", "user.email", "agent@test.local")
        self.git("config", "user.name", "Agent")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")

    def write(self, rel, text):
        p = self.art / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.art), *args],
                              capture_output=True, text=True, check=True).stdout


class TestTheRepoStaysOutOfTheWorkTree(CheckpointTestCase):
    """The hazard being designed out: a checkpoint repo INSIDE artifacts/ is
    itself an edit, so every checkpoint would look like agent work — which is
    how the mtime-based no-edit guard used to be defeated."""

    def test_the_checkpoint_repo_is_a_sibling_of_artifacts(self):
        ckpt("init", str(self.ws))
        self.assertTrue((self.ws / ".attempts.git").is_dir())
        self.assertFalse((self.art / ".attempts.git").exists())

    def test_committing_writes_nothing_inside_artifacts(self):
        before = sorted(p.relative_to(self.art).as_posix()
                        for p in self.art.rglob("*") if ".git/" not in
                        p.relative_to(self.art).as_posix())
        ckpt("commit", str(self.ws), "pre attempt 1")
        after = sorted(p.relative_to(self.art).as_posix()
                       for p in self.art.rglob("*") if ".git/" not in
                       p.relative_to(self.art).as_posix())
        self.assertEqual(before, after)

    def test_the_agents_own_history_and_index_are_untouched(self):
        head = self.git("rev-parse", "HEAD").strip()
        status = self.git("status", "--porcelain")
        ckpt("commit", str(self.ws), "pre attempt 1")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)
        self.assertEqual(self.git("status", "--porcelain"), status,
                         "the checkpoint staged files in the agent's index")

    def test_the_agents_own_commits_do_not_move_the_tree(self):
        # The property the no-edit guard depends on. It holds because `.git`
        # at the ROOT of a work tree is reserved and git never stages it —
        # measured, not assumed (with info/exclude emptied, this case still
        # passes, so it is not what protects it; the nested case below is).
        t0 = ckpt("tree", str(self.ws))
        self.write("app/scratch.py", "x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "agent work")
        self.git("rm", "-q", "app/scratch.py")
        self.git("commit", "-q", "-m", "agent reverts")
        self.assertEqual(ckpt("tree", str(self.ws)), t0,
                         "the agent's own git writes moved the tree hash")

    def test_a_repo_the_agent_creates_in_a_SUBDIRECTORY_is_excluded(self):
        # This one is info/exclude's actual job. A nested repo is staged as a
        # GITLINK whose value is the nested HEAD, so without the exclude every
        # commit the agent makes inside it moves our tree hash — and a no-edit
        # attempt would read as authored work. Inverse measured 2026-08-18:
        # with info/exclude emptied, this assertion fails.
        t0 = ckpt("tree", str(self.ws))
        nested = self.art / "app"
        subprocess.run(["git", "-C", str(nested), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(nested), "-c", "user.email=a@b",
                        "-c", "user.name=a", "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(nested), "-c", "user.email=a@b",
                        "-c", "user.name=a", "commit", "-q", "-m", "nested"],
                       check=True)
        self.assertEqual(ckpt("tree", str(self.ws)), t0,
                         "a nested agent repo is being recorded as a gitlink")


class TestTheTreeHashIsTheNoEditOracle(CheckpointTestCase):

    def test_an_untouched_tree_hashes_the_same(self):
        self.assertEqual(ckpt("tree", str(self.ws)), ckpt("tree", str(self.ws)))

    def test_an_edited_file_moves_the_hash(self):
        before = ckpt("tree", str(self.ws))
        self.write("app/main.py", "print('v2')\n")
        self.assertNotEqual(ckpt("tree", str(self.ws)), before)

    def test_a_new_file_moves_the_hash(self):
        before = ckpt("tree", str(self.ws))
        self.write("app/extra.py", "y = 2\n")
        self.assertNotEqual(ckpt("tree", str(self.ws)), before)

    def test_a_deleted_file_moves_the_hash(self):
        before = ckpt("tree", str(self.ws))
        (self.art / "app" / "main.py").unlink()
        self.assertNotEqual(ckpt("tree", str(self.ws)), before)

    def test_logs_and_caches_are_not_authoring(self):
        # The mtime guard counted these; a *.log write is not a code change,
        # and treating it as one refunds an attempt that produced nothing.
        before = ckpt("tree", str(self.ws))
        self.write("run.log", "noise\n")
        self.write("__pycache__/main.cpython-311.pyc", "\x00")
        self.assertEqual(ckpt("tree", str(self.ws)), before)

    def test_identical_content_hashes_identically_across_workspaces(self):
        # The property the bash/Python equivalence diff rests on: same content,
        # same hash, regardless of who wrote it or when.
        other = Path(self._tmp.name) / "cell2"
        (other / "artifacts" / "app").mkdir(parents=True)
        (other / "artifacts" / "app" / "main.py").write_text("print('v1')\n")
        (other / "artifacts" / ".gitignore").write_text(
            (self.art / ".gitignore").read_text())
        self.assertEqual(ckpt("tree", str(other)), ckpt("tree", str(self.ws)))


class TestTheCheckpointChain(CheckpointTestCase):

    def _log(self):
        return subprocess.run(
            ["git", "log", "--format=%s %T"],
            cwd=str(self.ws),
            env={"GIT_DIR": str(self.ws / ".attempts.git"),
                 "GIT_WORK_TREE": str(self.art), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, check=True).stdout.strip().splitlines()

    def test_each_checkpoint_is_a_commit_on_the_previous_one(self):
        ckpt("commit", str(self.ws), "pre attempt 1")
        self.write("app/main.py", "print('v2')\n")
        ckpt("commit", str(self.ws), "post attempt 1 green=false")
        self.assertEqual([l.split(" ")[0] for l in self._log()], ["post", "pre"])

    def test_an_unchanged_tree_is_still_committed(self):
        # "the agent changed nothing" and "the harness failed to checkpoint"
        # must not look the same in the history.
        ckpt("commit", str(self.ws), "pre attempt 1")
        ckpt("commit", str(self.ws), "post attempt 1 green=false")
        self.assertEqual(len(self._log()), 2)

    def test_commit_returns_the_tree_it_committed(self):
        t = ckpt("commit", str(self.ws), "pre attempt 1")
        self.assertEqual(t, ckpt("tree", str(self.ws)))
        self.assertRegex(t, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
