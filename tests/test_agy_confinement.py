"""An agy agent runs confined (fae/cell/clients/agy.py): the cell's home
carries the custom agent `fae`, whose tool list leaves out the web, the
browser, MCP, plugins and self-defined subagents; the command line selects
it; and a step calling any other tool interrupts the cell."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401
from fae.cell import clients
from fae.cell.clients import agy
from fae.cell.cell import Cell
from fae.experiment import config as C

INIT = {"event": "init", "conversation_id": "c1",
        "init": {"model": "m", "cwd": "/workspace", "tools": ["run_command", "view_file"],
                 "permission_mode": "always-proceed"}}

AGY = clients.of("agy")


def write_agent(home):
    p = Path(home) / agy.AGENT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(agy.AGENT_MD)


class Case(unittest.TestCase):

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)


class TestTheAgentFile(Case):

    def test_the_kept_tools_leave_out_the_web_the_browser_mcp_and_new_subagents(self):
        for tool in ("search_web", "read_url_content", "open_browser_url", "read_browser_page",
                     "browser_click_element", "browser_subagent", "execute_browser_javascript",
                     "call_mcp_tool", "search_marketplace", "define_subagent"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, agy.TOOLS)
                self.assertNotIn(tool, agy.AGENT_MD)
        for tool in ("run_command", "view_file", "write_to_file", "replace_file_content"):
            self.assertIn(tool, agy.TOOLS)

    def test_it_is_the_main_agent_and_lists_its_tools(self):
        self.assertIn("mainAgent: true\n", agy.AGENT_MD)
        self.assertIn(f"tools: [{', '.join(agy.TOOLS)}]\n", agy.AGENT_MD)

    def test_an_edited_or_missing_agent_is_a_breach(self):
        home = self.d / ".agent-gemini"
        with self.assertRaises(clients.Breach):
            AGY.check_home(home)
        write_agent(home)
        AGY.check_home(home)
        p = home / agy.AGENT_FILE
        p.write_text(p.read_text().replace("run_command", "run_command, search_web"))
        with self.assertRaises(clients.Breach):
            AGY.check_home(home)

    def test_staging_writes_it_into_the_cells_home(self):
        home = self.d / "signin"
        (home / agy.TOKEN).parent.mkdir(parents=True)
        (home / agy.TOKEN).write_text("t")
        dest = self.d / "ws" / ".agent-gemini"
        AGY.stage_home({"AGENT_HOME": str(home)}, str(dest), ROOT)
        AGY.check_home(dest)


def step(i, tool):
    return {"event": "step_update", "step_update": {
        "conversation_id": "c1", "step_index": i, "state": "DONE", "step_type": "tool",
        "tool_name": tool, "tool_info": {"name": tool, "parameters": {}}}}


class TestTheStepsAreTheProof(Case):
    """agy's init line lists its default tools whatever the agent runs, so
    it proves nothing; the tools the session's steps call do."""

    def test_an_init_listing_the_defaults_and_only_kept_tools_called_is_confined(self):
        init = {**INIT, "init": {**INIT["init"], "tools": ["search_web", "view_file"]}}
        self.assertEqual(AGY.breach(init, ["view_file", "run_command"]), "")

    def test_a_call_to_any_other_tool_is_named(self):
        for tool in ("search_web", "browser_get_dom", "a_tool_agy_added_later"):
            with self.subTest(tool=tool):
                self.assertEqual(AGY.breach(INIT, ["view_file", tool]),
                                 f"tools called [{tool!r}]")

    def test_an_init_line_without_its_body_is_a_breach(self):
        for init in ({}, {"event": "init"}):
            with self.subTest(init=init):
                self.assertIn("init line without", AGY.breach(init, []))

    def test_the_watch_finds_agys_init_line_and_the_tools_its_steps_call(self):
        log = self.d / "agent.attempt-1.log"
        lines = [INIT, step(1, "view_file"), step(2, "run_command"), step(3, "view_file")]
        text = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in lines)
        log.write_text(text[:-20])
        w = clients.Watch(log)
        self.assertEqual(w.poll(), INIT)
        self.assertEqual(w.tools_used, ["view_file", "run_command"])
        with log.open("a") as f:
            f.write(text[-20:] + json.dumps(step(4, "search_web")) + "\n")
        w.poll()
        self.assertEqual(w.tools_used, ["view_file", "run_command", "search_web"])


class TestABreachInterruptsTheAgent(Case):

    def run_agent(self, events, then):
        cell = Cell.__new__(Cell)
        cell.cid = "cid1"
        cell.conf = C.Config({"AGENT_CLI": "agy", "AGENT_MODEL": "m", "AGENT_IMAGE": "img",
                              "AGENT_TIMEOUT_S": "30"}, {})
        log = self.d / "agent.attempt-1.log"
        script = "; ".join(f"echo '{json.dumps(e)}'; sleep 1.5" for e in events)
        with log.open("w") as f:
            rc = cell._run_bounded(["bash", "-c", f"{script}; {then}"], f, None, watch=log)
        return cell, rc, log.read_text()

    def test_an_agent_that_calls_a_web_tool_after_its_init_is_killed(self):
        cell, rc, text = self.run_agent([INIT, step(1, "view_file"), step(2, "search_web")],
                                        "sleep 20; echo finished")
        self.assertEqual(rc, Cell.AGENT_UNCONFINED)
        self.assertIn("search_web", cell._breach)
        self.assertNotIn("finished", text)

    def test_a_confined_agent_runs_to_its_end(self):
        _, rc, text = self.run_agent([INIT, step(1, "view_file")], "echo finished")
        self.assertEqual(rc, 0)
        self.assertIn("finished", text)


if __name__ == "__main__":
    unittest.main()
