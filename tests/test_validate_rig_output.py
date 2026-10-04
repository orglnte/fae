"""results validate on a verify that left a required output unwritten
(ALERT RIG-OUTPUT): a taint unless the attempt was judged again after it."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs, at_workspace, use_workspace

from fae.cell import ledger
from fae.experiment.scoring.validate import rig_output_findings

CID = "m_high_arm_cond_T1_r1"


def ledger_of(*events):
    ws = Path(tempfile.mkdtemp())
    for event, *fields in events:
        ledger.append(ws, event, *fields)
    return ws, (ws / "iterations.log").read_text()


ALERT = ("ALERT", CID, "attempt=2", "RIG-OUTPUT the verify left no resources.json")


class TestRigOutputRule(unittest.TestCase):
    def test_an_attempt_never_judged_after_the_alert_taints(self):
        _, text = ledger_of(("ITER", "fail", "attempt=1 stage=e2e"), ALERT)
        taints, warns = rig_output_findings(text)
        self.assertEqual(warns, [])
        self.assertEqual(len(taints), 1)
        self.assertIn("attempt 2", taints[0])
        self.assertIn("resources.json", taints[0])

    def test_an_attempt_judged_after_the_alert_warns(self):
        _, text = ledger_of(ALERT, ("ITER", "green", "attempt=2 e2e=7/7"))
        taints, warns = rig_output_findings(text)
        self.assertEqual(taints, [])
        self.assertEqual(len(warns), 1)
        self.assertIn("attempt 2", warns[0])

    def test_another_attempt_judged_later_does_not_clear_it(self):
        _, text = ledger_of(ALERT, ("ITER", "green", "attempt=20 e2e=7/7"))
        taints, _ = rig_output_findings(text)
        self.assertEqual(len(taints), 1)

    def test_a_judgment_before_the_alert_does_not_clear_it(self):
        _, text = ledger_of(("ITER", "fail", "attempt=2 stage=e2e"), ALERT)
        taints, _ = rig_output_findings(text)
        self.assertEqual(len(taints), 1)

    def test_a_clean_ledger_has_no_findings(self):
        _, text = ledger_of(("ITER", "green", "attempt=1 e2e=7/7"))
        self.assertEqual(rig_output_findings(text), ([], []))


class TestTheValidatorAppliesIt(unittest.TestCase):
    def test_the_cell_is_tainted_under_rule_set_10(self):
        ws, _ = ledger_of(("ITER", "fail", "attempt=1 stage=e2e"), ALERT)
        definition = mock.Mock(taint_rules=None)
        definition.report_text.return_value = ""
        with mock.patch.object(runs.experiment, "definition", return_value=definition), \
             at_workspace(ws.parent, ws.parent):
            doc = runs.validate_ws(ws)
        self.assertEqual((doc["verdict"], doc["rule_set"]), ("TAINTED", 10))
        self.assertEqual(json.loads((ws / "validation.json").read_text())["verdict"], "TAINTED")


if __name__ == "__main__":
    unittest.main()
