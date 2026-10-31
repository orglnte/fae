"""A cell starts an agent client only through `clients.of`, which refuses a
client that does not provide every guarantee AgentClient names
(fae/cell/clients/base.py); a finished attempt's transcript is read through
`clients.reader`, whatever the client."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401
from fae.cell import clients
from fae.cell.cell import Cell
from fae.cell.clients.claude import Claude
from fae.experiment import config as C


class TestOnlyACompleteClientStarts(unittest.TestCase):

    def test_claude_agy_and_the_test_agent_provide_everything(self):
        for cli in ("claude", "agy", "testagent"):
            with self.subTest(cli=cli):
                client = clients.of(cli)
                self.assertEqual(client.NAME, cli)
                self.assertTrue(client.HOME.startswith(".agent-"))
                self.assertTrue(client.MOUNT.startswith("/home/node/"))

    def test_a_client_missing_a_guarantee_is_refused_naming_it(self):
        no_proof = type("NoProof", (clients.AgentClient,),
                        {k: v for k, v in vars(Claude).items()
                         if k not in ("breach", "__abstractmethods__")})
        self.assertEqual(clients.missing(no_proof), ["breach"])
        self.assertEqual(clients.missing(Claude), [])

    def test_a_client_without_its_home_is_refused(self):
        homeless = type("Homeless", (Claude,), {"HOME": ""})
        self.assertEqual(clients.missing(homeless), ["HOME"])

    def test_opencode_is_refused_until_it_proves_its_confinement(self):
        with self.assertRaisesRegex(clients.Incomplete, "opencode client cannot start: "
                                                        "it does not provide breach"):
            clients.of("opencode")

    def test_an_unknown_client_is_named(self):
        with self.assertRaisesRegex(ValueError, "no agent client 'gemini-cli'"):
            clients.of("gemini-cli")

    def test_the_experiments_clis_are_the_clients(self):
        import fae.experiment._experiment as E
        self.assertEqual(E.AGENT_CLIS, clients.NAMES)


class TestACellRefusesAnIncompleteClient(unittest.TestCase):

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_staging_an_opencode_cell_raises_before_anything_is_written(self):
        cell = Cell.__new__(Cell)
        cell.cid, cell.ws, cell.root = "cid1", self.d / "ws", ROOT
        cell._conf = C.Config({"AGENT_CLI": "opencode", "AGENT_HOME": str(self.d)}, {})
        with self.assertRaises(clients.Incomplete):
            cell.stage_agent()
        self.assertFalse((self.d / "ws").exists())


class TestEveryClientsTranscriptCanBeRead(unittest.TestCase):

    def test_a_reader_exists_for_every_client_even_one_that_cannot_start(self):
        log = Path(tempfile.mkdtemp()) / "agent.attempt-1.log"
        log.write_text("text only\n")
        for cli in clients.NAMES:
            with self.subTest(cli=cli):
                self.assertIsNone(clients.reader(cli).usage(log))
                self.assertIsNone(clients.reader(cli).client_seconds(log))
        self.assertIsNone(clients.reader("-"))


if __name__ == "__main__":
    unittest.main()
