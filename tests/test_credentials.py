"""experiment credentials: one credential per agent (a CLI), shared by the
models it runs, set up from a hidden prompt, written owner-only; experiment
run checks every agent's first."""
from __future__ import annotations

import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)
import fae.experiment
from fae.cell import confinement
from fae.experiment import credentials

TOKEN = confinement.TOKEN_PREFIX + "abc"


class Case(unittest.TestCase):

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.runs = []
        p = mock.patch.object(credentials, "_image_absent", return_value="")
        self.absent = p.start()
        self.addCleanup(p.stop)

    def cred(self, cli, name=None):
        return credentials.Credential(name or cli, cli, self.d / cli, "img")

    def docker(self, argv, **_):
        self.runs.append(argv)


class TestOneCredentialPerAgent(Case):

    def test_the_experiments_agents_each_have_one_and_the_scripted_agent_none(self):
        d = fae.experiment.exp().definition
        creds = credentials.of(d, self.d, {}, "img")
        self.assertEqual(sorted(creds), sorted(n for n, a in d.agents.items() if a["cli"] != "testagent"))
        self.assertEqual(creds["claude"].home, self.d / ".agent-home/.claude")
        moved = credentials.of(d, self.d, {"agents": {"claude": {"home": "/elsewhere"}}}, "img")
        self.assertEqual(moved["claude"].home, Path("/elsewhere"))


class TestClaude(Case):

    def test_setup_runs_setup_token_and_writes_the_token_owner_only(self):
        c = self.cred("claude")
        why = c.setup(ask=lambda _: TOKEN, run=self.docker, say=lambda *_: None)
        self.assertEqual(why, "")
        self.assertEqual(self.runs, [["docker", "run", "-it", "--rm", "img", "claude", "setup-token"]])
        token = c.home / confinement.TOKEN_FILE
        self.assertEqual(token.read_text(), TOKEN + "\n")
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(c.home.stat().st_mode), 0o700)

    def test_a_login_found_there_is_moved_aside_never_deleted(self):
        c = self.cred("claude")
        c.home.mkdir()
        (c.home / ".credentials.json").write_text("{}")
        c.setup(ask=lambda _: TOKEN, run=self.docker, say=lambda *_: None)
        self.assertFalse((c.home / ".credentials.json").exists())
        self.assertEqual(len(list((c.home / ".to_be_deleted").rglob(".credentials.json"))), 1)

    def test_a_pasted_value_that_is_not_a_setup_token_writes_nothing(self):
        c = self.cred("claude")
        why = c.setup(ask=lambda _: "sk-ant-api03-x", run=self.docker, say=lambda *_: None)
        self.assertIn("not a `claude setup-token` token", why)
        self.assertFalse((c.home / confinement.TOKEN_FILE).exists())

    def test_missing_names_why(self):
        self.assertIn("setup-token", self.cred("claude").missing())

    def test_an_absent_agent_image_is_named_before_anything_is_asked(self):
        self.absent.return_value = "the agent image img is not built"
        asked = []
        why = self.cred("claude").setup(ask=asked.append, run=self.docker, say=lambda *_: None)
        self.assertIn("not built", why)
        self.assertEqual((asked, self.runs), ([], []))

    def test_a_lax_existing_file_is_owner_only_before_the_secret_lands(self):
        c = self.cred("opencode")
        c.home.mkdir()
        key = c.home / "opencode.key"
        key.write_text("old\n")
        key.chmod(0o644)
        c.setup(ask=lambda _: "NEW", run=self.docker, say=lambda *_: None)
        self.assertEqual(key.read_text(), "NEW\n")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

    def test_a_symlink_where_the_secret_goes_is_refused(self):
        c = self.cred("opencode")
        c.home.mkdir()
        (c.home / "opencode.key").symlink_to(self.d / "elsewhere")
        with self.assertRaises(OSError):
            c.setup(ask=lambda _: "NEW", run=self.docker, say=lambda *_: None)
        self.assertFalse((self.d / "elsewhere").exists())


class TestOpencodeAndAgy(Case):

    def test_opencode_writes_the_key_owner_only(self):
        c = self.cred("opencode")
        self.assertIn("no API key", c.missing())
        self.assertEqual(c.setup(ask=lambda _: "KEY", run=self.docker, say=lambda *_: None), "")
        key = c.home / "opencode.key"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

    def test_agy_signs_in_with_its_home_mounted(self):
        c = self.cred("agy")
        self.assertIn("no signed-in agy home", c.missing())
        c.setup(ask=lambda _: "", run=self.docker, say=lambda *_: None)
        self.assertIn(f"{c.home}:/home/node/.gemini", self.runs[0])


class TestEnsure(Case):

    def creds(self):
        claude = self.cred("claude")
        claude.home.mkdir()
        (claude.home / confinement.TOKEN_FILE).write_text(TOKEN)
        return {"claude": claude, "opencode": self.cred("opencode"), "agy": self.cred("agy")}

    def test_without_a_terminal_the_missing_are_reported_not_asked(self):
        asked = []
        left = credentials.ensure(self.creds(), interactive=False, confirm=asked.append)
        self.assertEqual(sorted(left), ["agy", "opencode"])
        self.assertEqual(asked, [])

    def test_an_excluded_agent_is_not_checked(self):
        left = credentials.ensure(self.creds(), exclude=["agy", "opencode"], interactive=False)
        self.assertEqual(left, {})

    def test_an_unknown_exclusion_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no agent"):
            credentials.ensure(self.creds(), exclude=["gemini"], interactive=False)

    def test_at_a_terminal_a_yes_sets_it_up(self):
        creds = self.creds()
        left = credentials.ensure(creds, exclude=["agy"], interactive=True,
                                  confirm=lambda _: "y", ask=lambda _: "KEY",
                                  run=self.docker, say=lambda *_: None)
        self.assertEqual(left, {})
        self.assertEqual((creds["opencode"].home / "opencode.key").read_text(), "KEY\n")


if __name__ == "__main__":
    unittest.main()
