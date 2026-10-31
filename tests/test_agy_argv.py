"""agy's invocation: with stream_agent on (the default) it writes a
stream-json transcript, as claude does, so its turns, tokens and result
event are on record; off, it prints the agent's text only."""
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401

from fae.cell.cell import Cell


class TestAgyArgv(unittest.TestCase):
    def argv(self, stream):
        d = Path(tempfile.mkdtemp())
        prompt = d / "PROMPT.md"
        prompt.write_text("task\n")
        conf = {"AGENT_CLI": "agy", "AGENT_MODEL": "Gemini 3.8 Flash (High)",
                "STREAM_AGENT": stream}
        argv = Cell.agent_argv(conf, "cid1", "/ws/art", d / "home", prompt)
        return argv[argv.index("agy"):]

    def test_stream_agent_on_writes_stream_json(self):
        tail = self.argv("1")
        self.assertEqual(tail[tail.index("--output-format") + 1], "stream-json")
        self.assertEqual(tail[-2:], ["--print", "task\n"])

    def test_it_runs_the_confined_agent(self):
        tail = self.argv("1")
        self.assertEqual(tail[tail.index("--agent") + 1], "fae")

    def test_stream_agent_off_prints_text(self):
        self.assertNotIn("--output-format", self.argv(""))


if __name__ == "__main__":
    unittest.main()
