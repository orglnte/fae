"""A cell's agy home: the sign-in and agy's identity files, nothing else,
plus a settings file and the confined agent FAE writes. The sign-in home also holds agy's conversation
memory, the sign-in session's log and its settings (an agentMode, say);
copying those would start every g38f cell from whatever they say."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae.cell import confinement
from fae.experiment import config as C


class TestAgyHome(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        self.home = self.d / "home"
        self.dest = self.d / "ws" / ".agent-gemini"

    def put(self, rel, text="x"):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def stage(self):
        C.stage_agent({"AGENT_HOME": str(self.home)}, "agy", str(self.dest), ROOT)

    def test_only_the_sign_in_and_identity_files_reach_the_cell(self):
        for rel in C.AGY_LOGIN:
            self.put(rel)
        for rel in ("antigravity-cli/conversation_summaries.db",
                    "antigravity-cli/implicit/a.pb", "antigravity-cli/log/cli-1.log",
                    "antigravity-cli/jetski_state.pbtxt", "antigravity-cli/bin/webm_encoder",
                    "config/mcp_config.json", "GEMINI.md"):
            self.put(rel)
        self.stage()
        got = {str(p.relative_to(self.dest)) for p in self.dest.rglob("*") if p.is_file()}
        self.assertEqual(got, set(C.AGY_LOGIN) | {C.AGY_SETTINGS, confinement.AGY_AGENT_FILE})

    def test_the_settings_are_fae_s_own_whatever_the_home_holds(self):
        self.put(C.AGY_TOKEN)
        self.put(C.AGY_SETTINGS, json.dumps({"agentMode": "auto"}))
        self.stage()
        self.assertEqual(json.loads((self.dest / C.AGY_SETTINGS).read_text()), {})

    def test_a_home_without_the_sign_in_halts_at_staging(self):
        self.put("antigravity-cli/log/cli-1.log")
        with self.assertRaisesRegex(RuntimeError, "no signed-in agy home"):
            self.stage()

    def test_a_restage_drops_what_the_last_attempt_left(self):
        self.put(C.AGY_TOKEN)
        self.stage()
        (self.dest / "antigravity-cli" / "conversation_summaries.db").write_text("x")
        self.stage()
        self.assertFalse((self.dest / "antigravity-cli" / "conversation_summaries.db").exists())


if __name__ == "__main__":
    unittest.main()
