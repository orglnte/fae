"""A claude agent runs confined (fae/cell/confinement.py): a setup-token
credential, the limits in its home and on its command line, and an init line
that shows no MCP server and no denied tool — or the cell is interrupted."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT
from fae.cell import confinement
from fae.cell.cell import Cell
from fae.experiment import config as C

TOKEN = confinement.TOKEN_PREFIX + "abc"
CONFINED = {"type": "system", "subtype": "init", "mcp_servers": [],
            "skills": ["code-review", "simplify"], "tools": ["Bash", "Edit", "Read"]}


class Case(unittest.TestCase):

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def conf(self, home):
        return C.Config({"AGENT_CLI": "claude", "AGENT_MODEL": "m", "AGENT_IMAGE": "img",
                         "AGENT_HOME": str(home), "AGENT_TIMEOUT_S": "30"}, {})

    def home(self, **files):
        home = self.d / "creds"
        home.mkdir(exist_ok=True)
        for name, body in files.items():
            (home / name).write_text(body)
        return home


class TestTheCredentialIsASetupToken(Case):

    def test_a_token_is_staged_as_settings_only_and_stays_out_of_the_cells_home(self):
        home = self.home(**{".oauth_token": TOKEN + "\n", ".credentials.json": "{}"})
        dest = self.d / ".agent-claude"
        C.stage_agent(self.conf(home), "claude", dest, ROOT)
        self.assertEqual([p.name for p in dest.iterdir()], ["settings.json"])
        confinement.check_home(dest)

    def test_a_login_alone_is_refused(self):
        home = self.home(**{".credentials.json": "{}"})
        with self.assertRaisesRegex(RuntimeError, "setup-token"):
            C.stage_agent(self.conf(home), "claude", self.d / ".agent-claude", ROOT)

    def test_a_token_that_is_not_a_setup_token_is_refused(self):
        home = self.home(**{".oauth_token": "sk-ant-api03-abc\n"})
        with self.assertRaisesRegex(RuntimeError, "not a `claude setup-token` token"):
            C.stage_agent(self.conf(home), "claude", self.d / ".agent-claude", ROOT)


class TestTheHomeCarriesTheSettings(Case):

    def staged(self):
        dest = self.d / ".agent-claude"
        dest.mkdir()
        confinement.stage(dest)
        return dest

    def test_the_settings_deny_the_web_connectors_and_skill_sync(self):
        s = json.loads((self.staged() / "settings.json").read_text())
        self.assertEqual(s["permissions"]["deny"], ["WebSearch", "WebFetch"])
        self.assertIs(s["disableClaudeAiConnectors"], True)
        self.assertIs(s["syncClaudeAiSkills"], False)

    def test_an_edited_setting_or_a_login_in_the_home_is_a_breach(self):
        dest = self.staged()
        s = json.loads((dest / "settings.json").read_text())
        s["syncClaudeAiSkills"] = True
        (dest / "settings.json").write_text(json.dumps(s))
        with self.assertRaises(confinement.Breach):
            confinement.check_home(dest)
        confinement.stage(dest)
        confinement.check_home(dest)
        (dest / ".credentials.json").write_text("{}")
        with self.assertRaises(confinement.Breach):
            confinement.check_home(dest)


class TestTheCommandLine(Case):

    def test_the_token_is_named_on_the_command_line_never_its_value(self):
        home = self.home(**{".oauth_token": TOKEN})
        prompt = self.d / "PROMPT.md"
        prompt.write_text("task\n")
        argv = Cell.agent_argv(self.conf(home), "cid1", "/ws/art", self.d / "home", prompt)
        self.assertEqual(argv[argv.index("-e") + 1], confinement.TOKEN_ENV)
        self.assertFalse(any(TOKEN in a for a in argv), argv)

    def test_it_drops_mcp_and_the_web_keeps_skills_and_a_flag_closes_the_tool_list(self):
        prompt = self.d / "PROMPT.md"
        prompt.write_text("task\n")
        argv = Cell.agent_argv(self.conf(self.d), "cid1", "/ws/art", self.d / "home", prompt)
        tail = argv[argv.index("claude"):]
        self.assertIn("--strict-mcp-config", tail)
        self.assertNotIn("--disable-slash-commands", tail)
        self.assertNotIn("--mcp-config", tail)
        self.assertEqual(tail[tail.index("--disallowedTools") + 1], "WebSearch,WebFetch")
        self.assertTrue(tail[tail.index("--disallowedTools") + 2].startswith("--"), tail)


class TestTheInitLineIsTheProof(Case):

    def test_a_confined_session_with_built_in_skills_shows_nothing_denied(self):
        self.assertEqual(confinement.breach(CONFINED), "")

    def test_each_denied_thing_is_named(self):
        for key, value, word in (("mcp_servers", [{"name": "claude.ai Gmail"}], "claude.ai Gmail"),
                                 ("tools", ["Bash", "WebFetch"], "WebFetch"),
                                 ("tools", ["WebSearch"], "WebSearch"),
                                 ("tools", ["mcp__x__y"], "mcp__x__y")):
            with self.subTest(key=key, value=value):
                self.assertIn(word, confinement.breach({**CONFINED, key: value}))

    def test_an_init_line_that_cannot_prove_confinement_is_a_breach(self):
        for init in ({}, {"tools": []}, {"mcp_servers": []}):
            with self.subTest(init=init):
                self.assertIn("init line without", confinement.breach(init))

    def test_the_watch_reads_only_complete_lines_as_the_transcript_grows(self):
        log = self.d / "agent.attempt-1.log"
        w = confinement.Watch(log)
        self.assertIsNone(w.poll())
        line = json.dumps(CONFINED)
        log.write_text("noise\n" + line[:20])
        self.assertIsNone(w.poll())
        with log.open("a") as f:
            f.write(line[20:] + "\n")
        self.assertEqual(w.poll(), CONFINED)

    def test_a_malformed_init_line_reads_as_empty_and_so_as_a_breach(self):
        log = self.d / "agent.attempt-1.log"
        log.write_text('{"type":"system","subtype":"init", broken\n')
        self.assertIn("init line without", confinement.breach(confinement.Watch(log).poll()))


class TestABreachInterruptsTheAgent(Case):
    """The runner reads the init line while the agent runs and kills it on a
    breach; a script stands in for the docker client."""

    def run_agent(self, init, then):
        cell = Cell.__new__(Cell)
        cell.cid = "cid1"
        cell.conf = self.conf(self.d)
        log = self.d / "agent.attempt-1.log"
        with log.open("w") as f:
            rc = cell._run_bounded(["bash", "-c", f"echo '{json.dumps(init)}'; {then}"],
                                   f, None, watch=log)
        return cell, rc, log.read_text()

    def test_an_unconfined_agent_is_killed_at_its_init_line(self):
        cell, rc, text = self.run_agent({**CONFINED, "tools": ["WebSearch"]},
                                        "sleep 20; echo finished")
        self.assertEqual(rc, Cell.AGENT_UNCONFINED)
        self.assertIn("WebSearch", cell._breach)
        self.assertNotIn("finished", text)

    def test_a_confined_agent_runs_to_its_end(self):
        _, rc, text = self.run_agent(CONFINED, "echo finished")
        self.assertEqual(rc, 0)
        self.assertIn("finished", text)

    def test_an_agent_that_ran_without_an_init_line_is_unconfined(self):
        cell = Cell.__new__(Cell)
        cell.cid = "cid1"
        cell.conf = self.conf(self.d)
        log = self.d / "agent.attempt-1.log"
        with log.open("w") as f:
            rc = cell._run_bounded(["bash", "-c", "echo did some work"], f, None, watch=log)
        self.assertEqual(rc, Cell.AGENT_UNCONFINED)
        self.assertIn("without writing an init line", cell._breach)

    def test_an_agent_that_wrote_nothing_is_left_to_the_fault_checks(self):
        cell = Cell.__new__(Cell)
        cell.cid = "cid1"
        cell.conf = self.conf(self.d)
        log = self.d / "agent.attempt-1.log"
        with log.open("w") as f:
            rc = cell._run_bounded(["bash", "-c", "exit 3"], f, None, watch=log)
        self.assertEqual(rc, 3)

    def test_an_interrupt_kills_the_agent_it_was_waiting_on(self):
        cell = Cell.__new__(Cell)
        cell.cid = "cid1"
        cell.conf = self.conf(self.d)
        log = self.d / "agent.attempt-1.log"
        procs = []
        real = subprocess.Popen

        def popen(*a, **k):
            procs.append(real(*a, **k))
            return procs[-1]

        def interrupt(*a, **k):
            raise KeyboardInterrupt
        with log.open("w") as f, mock.patch.object(subprocess, "Popen", popen), \
                mock.patch.object(confinement.Watch, "poll", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                cell._run_bounded(["sleep", "30"], f, None, watch=log)
        self.assertIsNotNone(procs[0].poll())


if __name__ == "__main__":
    unittest.main()
