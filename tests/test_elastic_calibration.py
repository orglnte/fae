"""The elastic-resource contrib's calibration values: per spike, where the
store saturated, how soon the resource mounted, how soon after the traffic
it was released."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell.contrib.elastic_resource import calibration as cal

T0 = 1_000_000.0
SCHED = {"events": [{"kind": "blip", "t0": 2.0, "t1": 6.0},
                    {"kind": "spike", "t0": 10.0, "t1": 30.0},
                    {"kind": "spike", "t0": 40.0, "t1": 60.0}]}


def trace(sat=(16, 46), up=((22, 35), (52, 64)), last=70, done_late=0):
    """One line per second from the generator's start: p99 crosses 150ms at
    each `sat` second, the cache is up over each `up` window; k6_done reaches
    k6_sent `done_late` seconds after each spike's scheduled end."""
    rows = []
    for s in range(last + 1):
        p99 = 200.0 if s in sat else 20.0
        cache = any(a <= s < b for a, b in up)
        sent = s * 10
        done = sent - (10 * done_late if done_late and s in range(30, 30 + done_late) else 0)
        rows.append({"epoch": T0 + s, "offered_rps": 100 + s, "p99_ms": p99,
                     "infra_cache_up": str(cache), "k6_sent": sent, "k6_done": done})
    return rows


def arrangement(td, rows):
    d = Path(td)
    with (d / "trace.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (d / "trace_events.json").write_text(json.dumps({"k6_start_epoch": T0}))
    (d / "schedule.json").write_text(json.dumps(SCHED))
    return d


class TestValues(unittest.TestCase):
    def values(self, **kw):
        with tempfile.TemporaryDirectory() as td:
            return cal.values_from(arrangement(td, trace(**kw)), sat_p99=150)

    def test_each_spike_is_placed_on_its_own_window(self):
        v = self.values()
        self.assertEqual(v, {"spike1_lag_s": 0.0, "spike1_sat_s": 6.0, "spike1_sat_rps": 116.0,
                             "spike1_mount_s": 6.0, "spike1_release_s": 5.0,
                             "spike2_lag_s": 0.0, "spike2_sat_s": 6.0, "spike2_sat_rps": 146.0,
                             "spike2_mount_s": 6.0, "spike2_release_s": 4.0})

    def test_the_release_runs_from_the_observed_end_of_the_traffic(self):
        v = self.values(done_late=4)
        self.assertEqual(v["spike1_lag_s"], 4.0)
        self.assertEqual(v["spike1_release_s"], 1.0)

    def test_what_the_run_never_reached_is_left_out(self):
        v = self.values(sat=(16,), up=((22, 35),))
        self.assertNotIn("spike2_sat_s", v)
        self.assertNotIn("spike2_mount_s", v)
        v = self.values(up=((22, 80),), last=70)
        self.assertIn("spike1_mount_s", v)
        self.assertNotIn("spike1_release_s", v)
        self.assertNotIn("spike2_mount_s", v)

    def test_one_run_is_estimated_at_the_traces_grain(self):
        self.assertEqual(cal.estimated_spread("spike1_mount_s", 6.0), 1.0)


if __name__ == "__main__":
    unittest.main()
