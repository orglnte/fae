"""experiment smoke judges a reference cell by its ledger, whatever the verifier's
metrics carry."""
import json
import tempfile
import unittest
from pathlib import Path

from _ctx import runs


def _verdict(ws):
    return runs.experiment._experiment.Experiment.smoke_verdict(runs.experiment.exp().cell(ws.name, workspaces=ws.parent))


def _cell(ledger_lines, metrics=None):
    ws = Path(tempfile.mkdtemp())
    cid = "ref_high_smoke_alpha_reference_T1_r1"
    (ws / "iterations.log").write_text("".join(
        f"2026-09-26T00:00:0{i}Z\t{line.format(cid=cid)}\n" for i, line in enumerate(ledger_lines)))
    if metrics is not None:
        (ws / "metrics.json").write_text(json.dumps(metrics))
    return ws


GREEN = ["PREPARED\t{cid}\tby=prepare_cell", "START\t{cid}\tattempt=1",
         "ITER\tgreen\tattempt=1 verify_s=1 tree=abc", "END\t{cid}\tgreen=true"]
FAILED = ["PREPARED\t{cid}\tby=prepare_cell", "START\t{cid}\tattempt=1",
          "ITER\tbudget\tattempt=1 stage=cases verify_s=1 tree=abc", "END\t{cid}\tgreen=false"]


class TestSmokeVerdict(unittest.TestCase):
    def test_a_green_ledger_is_green_whatever_the_metrics_hold(self):
        green, why = _verdict(_cell(GREEN, {"cases": 8, "passed": 8}))
        self.assertTrue(green, why)
        self.assertIn("attempt 1", why)

    def test_a_failed_ledger_is_not_green_even_with_green_looking_metrics(self):
        green, _ = _verdict(_cell(FAILED, {"e2e_green": True}))
        self.assertFalse(green)

    def test_the_metrics_still_localize_a_break(self):
        green, why = _verdict(
            _cell(FAILED, {"e2e_green": True, "scaling_ok": False, "scaling_why": "no mount"}))
        self.assertFalse(green)
        self.assertIn("SCALING FAILED", why)

    def test_no_metrics_is_a_harness_break(self):
        self.assertIn("NO METRICS", _verdict(_cell(FAILED))[1])


if __name__ == "__main__":
    unittest.main()
