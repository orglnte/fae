"""mount_delay_s for a scaler with no observable decision runs from the
saturation of the spike the mount answers, not from the run's first slow
tick: in an arrangement that opens with blips, a blip's queue crosses the
saturation threshold long before the spike does."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

CONTRIB = Path(ROOT) / "fae" / "cell" / "contrib" / "elastic_resource"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, CONTRIB / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


k6 = _load("k6")

BBS = {"shape": "BBS", "events": [{"kind": "blip", "t0": 8.0, "t1": 30.0},
                                  {"kind": "blip", "t0": 38.0, "t1": 60.0},
                                  {"kind": "spike", "t0": 68.0, "t1": 121.0}]}
SSB = {"shape": "SSB", "events": [{"kind": "spike", "t0": 8.0, "t1": 50.0},
                                  {"kind": "spike", "t0": 60.0, "t1": 102.0},
                                  {"kind": "blip", "t0": 110.0, "t1": 130.0}]}


def trace(total, hot=(), mounted=(), app_only=()):
    """Per second: p99 800 ms over each [a, b) in `hot`, the cache mounted
    (app and substrate) over each [a, b) in `mounted`, the app alone claiming
    it over each [a, b) in `app_only`."""
    out = []
    for ts in range(total):
        inside = lambda spans: any(a <= ts < b for a, b in spans)  # noqa: E731
        up = inside(mounted)
        out.append({"ts_s": float(ts), "p99_ms": 800.0 if inside(hot) else 12.0,
                    "cache_mounted": up or inside(app_only), "substrate_cache_up": up})
    return out


class MountDelayCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def files(self, rows, events, sched=None):
        c, e, s = self.dir / "trace.csv", self.dir / "trace_events.json", self.dir / "schedule.json"
        with c.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        e.write_text(json.dumps({"sat_p99_ms": 150.0, **events}))
        if sched is not None:
            s.write_text(json.dumps(sched))
        return str(c), str(e), str(s) if sched is not None else ""


class TestTheSpikeSaturationAnchorsTheDelay(MountDelayCase):

    def test_a_blip_that_saturates_first_does_not_anchor_it(self):
        # the run's first slow tick is in blip 1 (ts 15); the spike's at ts 80
        rows = trace(130, hot=[(15, 18), (80, 89)], mounted=[(89, 120)])
        c, e, s = self.files(rows, {"saturation_epoch": 1015.0, "mount_epoch": 1089.0,
                                    "decision_epoch": None}, BBS)
        self.assertEqual(k6.mount_delay_s(c, e, s), "9.0")

    def test_without_a_schedule_the_first_saturation_anchors_it(self):
        rows = trace(130, hot=[(80, 89)], mounted=[(89, 120)])
        c, e, _ = self.files(rows, {"saturation_epoch": 1080.0, "mount_epoch": 1089.5,
                                    "decision_epoch": None})
        self.assertEqual(k6.mount_delay_s(c, e), "9.5")

    def test_an_observable_decision_still_anchors_it(self):
        rows = trace(130, hot=[(15, 18), (80, 89)], mounted=[(89, 120)])
        c, e, s = self.files(rows, {"saturation_epoch": 1015.0, "mount_epoch": 1089.0,
                                    "decision_epoch": 1086.5}, BBS)
        self.assertEqual(k6.mount_delay_s(c, e, s), "2.5")

    def test_no_mount_in_the_spike_is_no_delay(self):
        # mounted on a blip and released before the spike: not the spike's mount
        rows = trace(130, hot=[(15, 18), (80, 120)], mounted=[(18, 30)])
        c, e, s = self.files(rows, {"saturation_epoch": 1015.0, "mount_epoch": 1018.0,
                                    "decision_epoch": None}, BBS)
        self.assertEqual(k6.mount_delay_s(c, e, s), "")

    def test_the_app_alone_claiming_a_mount_is_not_one(self):
        rows = trace(130, hot=[(80, 92)], mounted=[(92, 120)], app_only=[(84, 92)])
        c, e, s = self.files(rows, {"saturation_epoch": 1080.0, "mount_epoch": 1092.0,
                                    "decision_epoch": None}, BBS)
        self.assertEqual(k6.mount_delay_s(c, e, s), "12.0")


class TestEachSpikePairsWithItsOwnMount(unittest.TestCase):

    def rows(self, **kw):
        return [{"ts": r["ts_s"], "p99": r["p99_ms"],
                 "mounted": r["cache_mounted"] and r["substrate_cache_up"]}
                for r in trace(140, **kw)]

    def test_two_spikes_two_delays(self):
        rows = self.rows(hot=[(20, 26), (72, 80)], mounted=[(26, 52), (80, 100)])
        self.assertEqual(k6.spike_mount_delays(rows, [w for w in SSB["events"] if w["kind"] == "spike"], 150.0),
                         [6.0, 8.0])

    def test_a_mount_held_over_from_the_first_spike_is_not_the_seconds(self):
        rows = self.rows(hot=[(20, 26), (72, 80)], mounted=[(26, 100)])
        self.assertEqual(k6.spike_mount_delays(rows, [w for w in SSB["events"] if w["kind"] == "spike"], 150.0),
                         [6.0, None])

    def test_a_spike_that_never_saturates_has_no_delay(self):
        rows = self.rows(hot=[(20, 26)], mounted=[(26, 52), (80, 100)])
        self.assertEqual(k6.spike_mount_delays(rows, [w for w in SSB["events"] if w["kind"] == "spike"], 150.0),
                         [6.0, None])


if __name__ == "__main__":
    unittest.main()
