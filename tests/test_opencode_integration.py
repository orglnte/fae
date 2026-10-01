"""opencode (OSS models via OpenRouter): the third agent CLI.

The model map, the docker-run argv and the credential staging are Python now
(fae/cell/config.py); these pin the opencode branch of each.
"""
import os
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

from fae.cell import config as C

TOML = C._toml(str(ROOT))


class TestAgentCliSelection(unittest.TestCase):
    def test_the_three_clis_are_selected_by_model_key(self):
        for key, cli in (("gemini", "agy"), ("dsv4f", "opencode"),
                         ("dsv4p", "opencode"), ("kimi", "opencode"),
                         ("sonnet", "claude")):
            self.assertEqual(C.model_map(key, TOML)[0], cli, key)

    def test_unknown_model_keys_still_fall_through_to_claude(self):
        cli, model = C.model_map("some-unlisted-id", TOML)
        self.assertEqual(cli, "claude")
        self.assertEqual(model, "some-unlisted-id")


def _opencode_conf(home):
    v = {"AGENT_CLI": "opencode", "AGENT_MODEL": "opencode-go/deepseek-v4-flash",
         "AGENT_IMAGE": "fae-agent:latest", "AGENT_HOME": str(home)}
    return C.Config(v, {})


class TestOpencodeContainment(unittest.TestCase):
    def _argv(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        (home / ".opencode").mkdir()
        (home / ".opencode" / "opencode.key").write_text("KEY123\n")
        prompt = home / "PROMPT.md"
        prompt.write_text("do the task\n")
        return C.build_agent_argv(_opencode_conf(home), "cid1", "/ws/art",
                                  home / ".agent-opencode", prompt)

    def test_config_is_mounted_writable(self):
        argv = self._argv()
        self.assertIn("-v", argv)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertTrue(any(m.endswith(":/home/node/.config/opencode") for m in mounts))
        self.assertFalse(any(":ro" in m and "opencode" in m for m in mounts))

    def test_mutable_state_is_never_mounted(self):
        self.assertNotIn(".local/share/opencode", " ".join(self._argv()))

    def test_the_key_is_injected_from_the_key_file(self):
        argv = self._argv()
        self.assertIn("-e", argv)
        self.assertTrue(any(a == "OPENCODE_API_KEY=KEY123" for a in argv))

    def test_container_keeps_the_kill_handle_name(self):
        self.assertIn("fae-agent-cid1", self._argv())


class TestStaging(unittest.TestCase):
    def test_stage_refuses_a_dest_that_is_not_a_per_cell_home(self):
        with self.assertRaises(RuntimeError):
            C.stage_agent(_opencode_conf(Path("/nowhere")), "opencode",
                          "/tmp/not-a-cell-home", ROOT)

    def test_staged_config_pins_temperature_zero_and_no_sharing(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        dest = d / ".agent-opencode"
        C.stage_agent(_opencode_conf(d), "opencode", str(dest), ROOT)
        body = (dest / "opencode.json").read_text()
        self.assertIn('"temperature": 0', body)
        self.assertIn('"share": "disabled"', body)


if __name__ == "__main__":
    unittest.main()
