"""The fleet console groups cells by their variant's LABEL (its id when the
file names none) and shows the variant id on the row, a retired variant
included: variants that share a label stay apart on screen."""
import unittest
from unittest import mock

from _ctx import runs


def _variant(vid, label=""):
    return type("V", (), {"ID": vid, "LABEL": label or vid})


def _state(cid, variant):
    return dict(cid=cid, agent=cid.split("_")[0], agent_model="5", state="DONE",
                why="green", att=2, budget=10, live="-", shape="6/6", hist="green",
                detail="", variant=variant, task="T1", rep=1,
                green_at=2, taint=False, alerts_open=0, alert_last="", noedit=0)


class TestTheVariantLabels(unittest.TestCase):
    def _render(self, states, variants):
        with mock.patch.object(runs.render.state, "all_states", return_value=(states, {}, [])), \
             mock.patch.object(runs.render.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.render.state, "heartbeat", return_value=None), \
             mock.patch.object(runs.render.zombies, "find_zombies", return_value=[]), \
             mock.patch.object(runs.render, "queued_summary", return_value=[]), \
             mock.patch.object(runs.queues_module.Queues, "weekly_line", return_value=""), \
             mock.patch.object(runs.render.common, "definition") as d:
            d.return_value.variants = variants
            return runs.render.render()

    def test_the_label_groups_and_the_row_names_the_variant(self):
        variants = {"beta_sealed_apidocs": _variant("beta_sealed_apidocs", "Alpha-B"),
                    "gamma_sealed_apidocs": _variant("gamma_sealed_apidocs"),
                    "old_access_apidocs": _variant("old_access_apidocs", "Alpha-old")}
        states = [_state("m1_high_beta_sealed_apidocs_T1_r1", "beta_sealed_apidocs"),
                  _state("m2_high_gamma_sealed_apidocs_T1_r1", "gamma_sealed_apidocs"),
                  _state("m3_high_old_access_apidocs_T1_r1", "old_access_apidocs")]
        out = self._render(states, variants)
        beta = next(l for l in out.splitlines() if l.split()[:2] == ["Alpha-B", "m1"])
        self.assertIn("beta_sealed_apidocs T1 r1", beta)
        gamma = next(l for l in out.splitlines()
                     if l.split()[:2] == ["gamma_sealed_apidocs", "m2"])
        self.assertIn("gamma_sealed_apidocs T1 r1", gamma)
        old = next(l for l in out.splitlines() if l.split()[:2] == ["Alpha-old", "m3"])
        self.assertIn("old_access_apidocs T1 r1", old)


if __name__ == "__main__":
    unittest.main()
