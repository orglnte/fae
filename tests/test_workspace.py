"""The Workspace (fae/experiment.py): the cells' folders of a root and the
scheduling plane beside them. Which folders are cells is the cell-id
grammar's question; the plane is the root's whatever the workspace path."""
import tempfile
import unittest
from pathlib import Path

from _ctx import runs

from fae.experiment import Experiment, Workspace


class TestTheCells(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "ws"
        for d in ("sonnet_high_beta_apidocs_T1_r2", "sonnet_high_beta_apidocs_T1_r10",
                  "haiku_high_alpha_howto_T1_r1", ".queues", "not-a-cell"):
            (self.path / d).mkdir(parents=True)
        (self.path / "sonnet_high_beta_apidocs_T1_r1").write_text("a file is no cell")
        self.w = Workspace(self.root, self.path, parse=runs.parse_cell_id)

    def test_only_cell_folders_are_cells_and_they_come_sorted(self):
        self.assertEqual(self.w.cells(), ["haiku_high_alpha_howto_T1_r1",
                                          "sonnet_high_beta_apidocs_T1_r10",
                                          "sonnet_high_beta_apidocs_T1_r2"])

    def test_a_root_with_no_workspace_has_no_cells(self):
        self.assertEqual(Workspace(self.root, self.root / "none",
                                   parse=runs.parse_cell_id).cells(), [])

    def test_selection_is_anchored_at_token_boundaries(self):
        self.assertEqual(self.w.select("r1"), ["haiku_high_alpha_howto_T1_r1"])   # not r10
        self.assertEqual(self.w.select("son"), [])
        self.assertEqual(self.w.select("sonnet_high_beta_apidocs_T1_r10"),
                         ["sonnet_high_beta_apidocs_T1_r10"])
        self.assertEqual(self.w.select("haiku", "r2"), ["haiku_high_alpha_howto_T1_r1",
                                                        "sonnet_high_beta_apidocs_T1_r2"])

    def test_a_cell_lives_on_the_workspaces_plane(self):
        c = self.w.cell("sonnet_high_beta_apidocs_T1_r2")
        self.assertEqual(c.ws, self.path / "sonnet_high_beta_apidocs_T1_r2")
        self.assertEqual(c.locks, self.root / "workspaces.nosync" / ".locks")
        self.assertEqual(c._transitions_log(),
                         self.root / "workspaces.nosync" / "transitions.log")

    def test_a_cell_named_by_what_it_runs_may_not_exist_yet(self):
        c = self.w.cell("sonnet_high_beta_apidocs_T1_r9", "T1", "beta_apidocs", 9)
        self.assertEqual((c.variant, c.rep, c.ws.exists()), ("beta_apidocs", 9, False))


class TestTheExperiment(unittest.TestCase):
    def test_its_definition_is_the_one_this_process_runs(self):
        self.assertIs(Experiment().definition, runs.experiment.definition())

    def test_the_drivers_experiment_is_on_the_drivers_workspace(self):
        e = runs.experiment.current()
        self.assertEqual(e.workspace.path, runs.experiment.workspace().path)
        self.assertEqual(e.workspace.locks, runs.experiment.workspace().locks)
        self.assertEqual(e.workspace.transitions, runs.experiment.workspace().transitions)


if __name__ == "__main__":
    unittest.main()
