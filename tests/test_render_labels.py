"""The fleet console names an arm's family by the variant's LABEL (its TECH
when it has none), and gives a retired arm its access/sealed half like any
other: arms that share a TECH stay apart on screen, and a retired cell does
not read as an unknown arm."""
import unittest
from unittest import mock

from _ctx import runs


class _Variant:
    TECH = "tech"
    LABEL = ""


def _variant(tech, label=""):
    return type("V", (_Variant,), {"TECH": tech, "LABEL": label})


def _state(cid, treatment):
    return dict(cid=cid, model=cid.split("_")[0], model_version="5", state="DONE",
                why="green", att=2, budget=10, live="-", shape="6/6", hist="green",
                detail="", treatment=treatment, condition="apidocs", task="T1", rep=1,
                green_at=2, taint=False, alerts_open=0, alert_last="", noedit=0)


class TestTheArmLabels(unittest.TestCase):
    def _render(self, states, variants, matrix):
        with mock.patch.object(runs.render.state, "all_states", return_value=(states, {}, [])), \
             mock.patch.object(runs.render.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.render.state, "heartbeat", return_value=None), \
             mock.patch.object(runs.render.zombies, "find_zombies", return_value=[]), \
             mock.patch.object(runs.render, "queued_summary", return_value=[]), \
             mock.patch.object(runs.render.weekly, "weekly_line", return_value=""), \
             mock.patch.object(runs.render.common, "definition") as d:
            d.return_value.matrix = matrix
            d.return_value.variants = variants
            return runs.render.render()

    def _row(self, out, rep_text):
        return next(l for l in out.splitlines() if rep_text in l)

    def test_a_label_names_the_family_and_a_retired_arm_keeps_its_half(self):
        variants = {"beta_sealed": _variant("alpha", "Alpha-B"),
                    "gamma_sealed": _variant("alpha"),
                    "old_access": _variant("alpha", "Alpha-old")}
        states = [_state("m1_high_beta_sealed_apidocs_T1_r1", "beta_sealed"),
                  _state("m2_high_gamma_sealed_apidocs_T1_r1", "gamma_sealed"),
                  _state("m3_high_old_access_apidocs_T1_r1", "old_access")]
        out = self._render(states, variants, matrix={"beta_sealed": ["apidocs"],
                                                     "gamma_sealed": ["apidocs"]})
        beta = next(l for l in out.splitlines() if l.split()[:2] == ["Alpha-B", "m1"])
        self.assertIn("sealed·apidocs", beta)
        gamma = next(l for l in out.splitlines() if l.split()[:2] == ["alpha", "m2"])
        self.assertIn("sealed·apidocs", gamma)
        old = next(l for l in out.splitlines() if l.split()[:2] == ["Alpha-old", "m3"])
        self.assertIn("access·apidocs", old)
        self.assertNotIn("?·", out)


if __name__ == "__main__":
    unittest.main()
