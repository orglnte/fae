"""A remote verify host's CPU busy share is a mark on a scored arrangement,
never its verdict: calibration records each reference arrangement's 95th
percentile (`host_busy_p95`), and an arrangement whose host stayed more than
0.15 over it for 5 consecutive readings gets an ALERT HOST-SPIKE and a
`host_spike` field in its archived verdict.json."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

import fae.experiment
from fae.cell import cell
from fae.cell import verify_timings as _timings
from fae.cell.verify import Verdict
from fae.cell.verify_host import VerifyHost, busy_p95, busy_readings, busy_spike
from fae.cell.verify_timings import HOST_BUSY_P95, HostTimings, Reference

import test_calibrate

H = "ssh://operator@verify1"


def load_lines(shares, t0=1000.0):
    return "".join(f"{t0 + i:.3f},{b:.3f}\n" for i, b in enumerate(shares))


class TestTheReadings(unittest.TestCase):
    def test_a_file_is_read_as_epoch_and_share_skipping_a_torn_line(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "host-load.csv"
            f.write_text("1000.0,0.250\n1001.0,0.5\n1002.0,0.")
            self.assertEqual(busy_readings(f), [(1000.0, 0.25), (1001.0, 0.5), (1002.0, 0.0)])
            f.write_text("1000.0,0.250\ngarbage\n")
            self.assertEqual(busy_readings(f), [(1000.0, 0.25)])
            self.assertEqual(busy_readings(Path(td) / "none.csv"), [])

    def test_the_p95_is_the_nearest_rank(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "host-load.csv"
            f.write_text(load_lines([i / 100 for i in range(1, 101)]))
            self.assertEqual(busy_p95(f), 0.95)
            f.write_text(load_lines([0.3]))
            self.assertEqual(busy_p95(f), 0.3)
            f.write_text("")
            self.assertIsNone(busy_p95(f))


class TestASpike(unittest.TestCase):
    def readings(self, shares):
        return [(1000.0 + i, b) for i, b in enumerate(shares)]

    def test_five_consecutive_readings_over_the_threshold(self):
        s = busy_spike(self.readings([0.1, 0.5, 0.6, 0.9, 0.5, 0.5, 0.1]), 0.4)
        self.assertEqual(s, {"seconds": 5, "peak": 0.9, "threshold": 0.4})

    def test_four_are_not_one(self):
        self.assertIsNone(busy_spike(self.readings([0.5, 0.5, 0.5, 0.5, 0.1, 0.5]), 0.4))

    def test_a_reading_at_the_threshold_breaks_a_stretch(self):
        self.assertIsNone(busy_spike(self.readings([0.5, 0.5, 0.4, 0.5, 0.5, 0.5]), 0.4))

    def test_stretches_add_up_and_one_running_to_the_end_counts(self):
        s = busy_spike(self.readings([0.5] * 5 + [0.1] + [0.7] * 6), 0.4)
        self.assertEqual(s, {"seconds": 11, "peak": 0.7, "threshold": 0.4})


class TestCalibrationRecordsTheP95(test_calibrate.Fixture):
    def smoke_cells(self, chosen, rep=1, full_gate=False, env=None):
        out = super().smoke_cells(chosen, rep, full_gate, env)
        if full_gate:
            for vid, *_ in out:
                for d in (self.root / "ws" / f"{vid}_r{rep}" / "arrangements").iterdir():
                    if "BSB" in d.name:
                        (d / cell.Cell.HOST_LOAD).write_text(load_lines([0.2] * 19 + [0.6]))
        return out

    def test_beside_the_verifiers_values_where_the_host_was_sampled(self):
        self.run_it()
        h = _timings.VerifyTimings.read(self.root).host(H)
        self.assertEqual(h.reference("v1", "BSB").values[HOST_BUSY_P95], 0.2)
        self.assertNotIn(HOST_BUSY_P95, h.reference("v1", "SSB").values)


class TestAMarkedArrangement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        (d / "ws" / "c" / "artifacts").mkdir(parents=True)
        self.c = cell.Cell("c", workspaces=d / "ws", root=ROOT)
        self.c._infra = mock.Mock(alive=lambda: True)
        self.c._attempt = 3
        self.c.verify_host = VerifyHost(H)
        self.ws = self.c.ws
        self.p95 = 0.25
        self.shares = [0.2] * 10 + [0.5] * 6 + [0.2] * 10

    def section(self):
        values = {"sat_s": 6.0} if self.p95 is None else {HOST_BUSY_P95: self.p95}
        return HostTimings(host=H, date="d", fae_commit="a", experiment_commit="b",
                           references=(Reference(self.c.variant, "BSB", values=values, spreads={}),))

    def verify(self, verdict, out_dir=None, stale=False):
        def runner(ctx, infra, timeout_s, log_dir=None, host=None):
            f = Path(log_dir) / cell.Cell.HOST_LOAD
            f.write_text(load_lines(self.shares))
            if stale:
                os.utime(f, (1, 1))
            return verdict
        vcls = mock.Mock(REQUIRED_OUTPUTS=(), NOT_RUN_STAGES=frozenset(), MEASURED_STAGES=None,
                         FILES=(), FEEDBACK_LOGS=())
        definition = mock.Mock(exclusive=None, verifier_class=lambda: vcls)
        with mock.patch.object(cell, "run_verifier", runner), \
                mock.patch.object(cell.Cell, "expected_fp", new_callable=mock.PropertyMock,
                                  return_value=""), \
                mock.patch.object(_timings, "calibration_of", return_value=(self.section(), None)), \
                mock.patch.object(fae.experiment._experiment.Experiment, "definition",
                                  new_callable=mock.PropertyMock, return_value=definition):
            return self.c.verify(shape=verdict.arrangement, out_dir=out_dir)

    def record(self, base=None):
        d, = sorted(((base or self.ws) / "arrangements").iterdir())
        return json.loads((d / "verdict.json").read_text())

    def alerts(self):
        f = self.ws / "iterations.log"
        return [l for l in (f.read_text().splitlines() if f.exists() else []) if "\tALERT\t" in l]

    def test_carries_the_spike_and_an_alert_and_keeps_its_verdict(self):
        r = self.verify(Verdict(ok=True, arrangement="BSB"))
        self.assertTrue(r.green)
        rec = self.record()
        self.assertEqual((rec["ok"], rec["charge"], rec["stage"], rec["end_state"]),
                         (True, True, "", "green"))
        self.assertEqual(rec["host_spike"], {"seconds": 6, "peak": 0.5, "threshold": 0.4})
        alert, = self.alerts()
        self.assertIn("attempt=3", alert)
        self.assertIn("HOST-SPIKE BSB verify host busy over 0.40 for 6s, peak 0.50", alert)

    def test_a_charged_verdict_stays_charged(self):
        r = self.verify(Verdict(ok=False, stage="scaling", arrangement="BSB"))
        self.assertEqual((r.green, r.charge, r.stage_failed), (False, True, "scaling"))
        self.assertIn("host_spike", self.record())

    def test_under_the_threshold_is_no_mark(self):
        self.shares = [0.2] * 10 + [0.4] * 6
        self.verify(Verdict(ok=True, arrangement="BSB"))
        self.assertNotIn("host_spike", self.record())
        self.assertEqual(self.alerts(), [])

    def test_no_calibrated_value_is_no_mark(self):
        self.p95 = None
        self.verify(Verdict(ok=True, arrangement="BSB"))
        self.assertNotIn("host_spike", self.record())

    def test_another_arrangement_has_no_reference_and_no_mark(self):
        self.verify(Verdict(ok=True, arrangement="SSB"))
        self.assertNotIn("host_spike", self.record())

    def test_no_calibration_section_is_no_mark(self):
        self.section = lambda: None
        self.verify(Verdict(ok=True, arrangement="BSB"))
        self.assertNotIn("host_spike", self.record())

    def test_an_earlier_runs_readings_are_no_mark(self):
        self.verify(Verdict(ok=True, arrangement="BSB"), stale=True)
        self.assertNotIn("host_spike", self.record())

    def test_a_smoke_verify_is_not_marked(self):
        with mock.patch.object(type(self.c.conf), "get",
                               lambda conf, k, d=None: "1" if k == "SMOKE" else conf.values.get(k, d)):
            self.verify(Verdict(ok=True, arrangement="BSB"))
        self.assertNotIn("host_spike", self.record())

    def test_a_reverify_records_the_mark_but_writes_no_ledger_line(self):
        out = Path(self._tmp.name) / "reverify"
        out.mkdir()
        self.verify(Verdict(ok=True, arrangement="BSB"), out_dir=out)
        self.assertIn("host_spike", self.record(out))
        self.assertEqual(self.alerts(), [])


if __name__ == "__main__":
    unittest.main()
