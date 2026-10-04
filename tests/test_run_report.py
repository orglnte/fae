"""results run-report: the cells sealed since the run started, by variant,
and the ones still working — verdicts through the one ledger parser."""
import os
import time
import unittest

from _ctx import runs, OrchTmpCase
from fae.experiment.scoring import run_report

TS = "2026-07-30T09:00:00Z"


def _ev(name, *fields):
    return "\t".join((TS, name) + fields)


class TestTheRunReport(OrchTmpCase):
    def _cell(self, cid, *lines, sealed=True, loop=False, sealed_at=None):
        ws = self.ws / cid
        ws.mkdir(parents=True)
        (ws / "iterations.log").write_text("".join(l + "\n" for l in lines))
        if sealed:
            at = time.gmtime(sealed_at if sealed_at is not None else time.time())
            (ws / ".sealed").write_text(f"sealed={time.strftime('%Y-%m-%dT%H:%M:%SZ', at)}"
                                        "\tverdict=green\tattempts=1\tby=test\n")
        if loop:
            (ws / ".loop").touch()
        return ws

    def test_completed_by_variant_and_in_progress(self):
        g = "sonnet_high_alpha_apidocs_T1_r1"
        self._cell(g, _ev("ITER", "fail", "attempt=1 stage=x"),
                   _ev("ITER", "green", "attempt=2 shapes=all"), _ev("END", g, "green=true"))
        b = "sonnet_high_alpha_apidocs_T1_r2"
        self._cell(b, _ev("ITER", "fail", "attempt=1 stage=x"),
                   _ev("ITER", "budget", "attempt=2"), _ev("END", b, "green=false"))
        w = "haiku_high_beta_apidocs_T1_r3"
        self._cell(w, _ev("ITER", "fail", "attempt=1 stage=x"), sealed=False, loop=True)
        done, live = run_report.collect(None)
        self.assertEqual(done["alpha_apidocs"], {"done": 2, "green": 1, "budget": 1, "itg": [2]})
        self.assertEqual(live, [("haiku", "beta_apidocs", 3, 1, "?")])
        text = run_report.report(None)
        self.assertIn("alpha", text)
        self.assertIn("2.0", text)
        self.assertIn("total: 2 done (1 green, 1 budget)", text)
        self.assertIn("IN PROGRESS (1)", text)

    def test_the_run_boundary_is_conducts_start(self):
        old = "sonnet_high_alpha_apidocs_T1_r1"
        self._cell(old, _ev("ITER", "green", "attempt=1 shapes=all"), _ev("END", old, "green=true"),
                   sealed_at=time.time() - 3600)
        (self.conduct / "conduct.pid").write_text("1")
        os.utime(self.conduct / "conduct.pid", (time.time() - 60, time.time() - 60))
        new = "sonnet_high_alpha_apidocs_T1_r2"
        self._cell(new, _ev("ITER", "green", "attempt=1 shapes=all"), _ev("END", new, "green=true"))
        done, _ = run_report.collect(run_report.run_start())
        self.assertEqual(done["alpha_apidocs"]["done"], 1)
        self.assertEqual(run_report.collect(None)[0]["alpha_apidocs"]["done"], 2)
        self.assertEqual(run_report._parse_since("100"), 100.0)
        self.assertEqual(run_report._parse_since("2000-01-01T00:00:00+00:00"), 946684800.0)

if __name__ == "__main__":
    unittest.main()
