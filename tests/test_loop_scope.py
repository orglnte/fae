"""The process table holds every experiment's cell loops on this machine; a
conduct counts, pauses and kills only its own: a loop whose WORKSPACES_DIR
is neither this experiment's scored root nor its smoke root is another's."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import patch_plane, use_workspace

import fae.experiment
from fae.conduct import _host as state


class TestTheLoopScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        use_workspace(self, self.ws)
        patch_plane(self, self.ws)

    def scan(self, lines):
        with mock.patch.object(state, "sh", return_value="\n".join(lines)):
            return state.loop_parents()

    def line(self, pid, workspaces=None, agent="sonnet", smoke=False, variant="alpha_apidocs"):
        env = (f"WORKSPACES_DIR={workspaces} " if workspaces else "") + \
              f"AGENT={agent} EFFORT=high" + (" SMOKE=1" if smoke else "")
        return f" {pid} python3 -m fae.cell T1 {variant} 1 {env}"

    def test_its_own_scored_and_smoke_loops_count(self):
        smoke = fae.experiment.exp().smoke_workspaces.resolve()
        got = self.scan([self.line(11, self.ws), self.line(12, smoke, agent="ref", smoke=True)])
        self.assertEqual(got, {"sonnet_high_alpha_apidocs_T1_r1": 11,
                               "ref_high_smoke_alpha_apidocs_T1_r1": 12})

    def test_another_experiments_loop_is_left_out(self):
        self.assertEqual(self.scan([self.line(13, "/elsewhere/ws-smoke.nosync", agent="ref",
                                              smoke=True)]), {})

    def test_a_loop_that_names_no_root_still_counts(self):
        self.assertEqual(self.scan([self.line(14)]), {"sonnet_high_alpha_apidocs_T1_r1": 14})


if __name__ == "__main__":
    unittest.main()
