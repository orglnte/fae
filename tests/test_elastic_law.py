"""The elastic_resource law judges from what the policy can see.

The deadline runs from the store's SATURATION (the first tick inside a spike
whose p99 reaches the threshold), never from the load generator's clock: when
a capped store tips over under a ramp is the host's doing, and a spike that
never tips it is a rig fault (void), not a verdict against the policy."""
import importlib.util
import json
import os
import subprocess
import sys
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


law = _load("law")

# one spike: ramp from t=8 over 20s, hold 30s, down 3s -> window [8, 61]
SCHED = {"shape": "S", "events": [{"kind": "spike", "t0": 8.0, "t1": 61.0}]}
BB_S = {"shape": "BBS", "events": [{"kind": "blip", "t0": 8.0, "t1": 14.0},
                                   {"kind": "blip", "t0": 22.0, "t1": 28.0},
                                   {"kind": "spike", "t0": 36.0, "t1": 89.0}]}


def rows(total, saturated_from=None, mounted=(), p99_hot=800.0):
    """A synthetic per-second trace: p99 crosses at `saturated_from` (and
    stays high until the cache is up), the substrate confirms the cache
    over each [a, b) in `mounted`."""
    out = []
    for ts in range(total):
        up = any(a <= ts < b for a, b in mounted)
        hot = saturated_from is not None and ts >= saturated_from and not up
        out.append({"ts": float(ts), "p99": p99_hot if hot else 12.0, "up": up})
    return out


def judge(rows_, sched, errors=0, **kw):
    params = dict(sat_p99=150.0, max_mount=30.0, max_pct=0.0)
    params.update(kw)
    return law.judge(rows_, {}, 6000, errors, sched, **params)


class TestTheDeadlineRunsFromSaturation(unittest.TestCase):

    def test_an_early_saturation_and_a_prompt_mount_pass(self):
        v, why = judge(rows(100, saturated_from=14, mounted=[(22, 70)]), SCHED)
        self.assertEqual((v, why), ("true", []))

    def test_a_late_saturation_is_the_hosts_and_the_same_reaction_still_passes(self):
        # the store tipped over 30s into the spike; the policy took 8s from
        # there, exactly as in the case above
        v, why = judge(rows(100, saturated_from=38, mounted=[(46, 70)]), SCHED)
        self.assertEqual((v, why), ("true", []))

    def test_a_mount_past_the_deadline_after_saturation_fails(self):
        v, why = judge(rows(100, saturated_from=14, mounted=[(45, 70)]), SCHED)
        self.assertEqual(v, "false")
        self.assertRegex(why[0], r"mount 1 at ts=45 outside spike 1's window \[5\.\.44\]")
        self.assertIn("saturated at ts=14", why[0])

    def test_the_schedules_clock_no_longer_decides(self):
        # under the old law this mount, 38s after the spike began, was late;
        # measured from the saturation it is 8s
        v, _ = judge(rows(100, saturated_from=38, mounted=[(46, 70)]), SCHED, max_mount=10.0)
        self.assertEqual(v, "true")


class TestASpikeThatNeverSaturatesIsAVoid(unittest.TestCase):

    def test_no_tick_over_the_threshold_is_void_with_the_numbers(self):
        v, why = judge(rows(100, saturated_from=None), SCHED)
        self.assertEqual(v, "void")
        self.assertRegex(why[0], r"spike 1 never saturated the store \(p99 max 12ms < 150ms")
        self.assertIn("raise [load] top", why[0])

    def test_the_void_is_decided_before_any_other_clause(self):
        # no mount at all, 40% errors: still a void, none of that is judged
        v, why = judge(rows(100, saturated_from=None), SCHED, errors=2400)
        self.assertEqual(v, "void")
        self.assertEqual(len(why), 1)

    def test_saturation_outside_the_spike_does_not_count(self):
        # a blip's aftermath saturates at t=15 and recovers; the spike itself never does
        r = rows(100, saturated_from=None)
        for t in range(15, 19):
            r[t]["p99"] = 900.0
        v, why = judge(r, BB_S)
        self.assertEqual(v, "void")
        self.assertIn("spike 1 never saturated", why[0])

    def test_the_threshold_is_the_experiments(self):
        v, _ = judge(rows(100, saturated_from=14, mounted=[(22, 70)], p99_hot=200.0),
                     SCHED, sat_p99=500.0)
        self.assertEqual(v, "void")

    def test_the_script_prints_void_on_its_own_line(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with (d / "trace.csv").open("w") as f:
                f.write("ts_s,p99_ms,substrate_cache_up\n")
                for ts in range(100):
                    f.write(f"{ts}.0,12.0,False\n")
            (d / "events.json").write_text("{}")
            (d / "schedule.json").write_text(json.dumps(SCHED))
            p = subprocess.run([sys.executable, str(CONTRIB / "law.py"), str(d / "trace.csv"),
                                str(d / "events.json"), "6000", "0", str(d / "schedule.json")],
                               capture_output=True, text=True,
                               env=dict(os.environ, VERIFY_SAT_P99_MS="150"))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(p.stdout.startswith("void|spike 1 never saturated"), p.stdout)


class TestTheOtherClausesStillHold(unittest.TestCase):

    def test_a_mount_during_a_blip_is_a_false_positive(self):
        r = rows(100, saturated_from=42, mounted=[(10, 20), (50, 95)])
        v, why = judge(r, BB_S)
        self.assertEqual(v, "false")
        self.assertIn("expected 1 mount episode(s) (one per spike), saw 2", why[0])

    def test_a_release_mid_spike_fails(self):
        v, why = judge(rows(100, saturated_from=14, mounted=[(22, 40)]), SCHED)
        self.assertEqual(v, "false")
        self.assertTrue(any("released MID-spike" in w for w in why), why)

    def test_errors_are_judged_after_a_saturated_run(self):
        v, why = judge(rows(100, saturated_from=14, mounted=[(22, 70)]), SCHED, errors=6)
        self.assertEqual(v, "false")
        self.assertIn("load errors 6/6000", why[0])

    def test_canonical_mode_measures_from_saturation_too(self):
        v, why = judge(rows(100, saturated_from=30, mounted=[(50, 90)]), None)
        self.assertEqual((v, why), ("true", []))
        v, why = judge(rows(100, saturated_from=30, mounted=[(70, 90)]), None)
        self.assertIn("saturation->mount 40s > 30s", why[0])
        v, why = judge(rows(100, saturated_from=None, mounted=[(50, 90)]), None)
        self.assertEqual(v, "void")


class TestTheScheduleRampsTheSpike(unittest.TestCase):

    def test_the_spike_is_a_ramp_then_a_hold(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, str(CONTRIB / "load_shape.py"), d],
                               capture_output=True, text=True,
                               env=dict(os.environ, VERIFY_SHAPE="BS", DISC_T_RAMP="20"))
            self.assertEqual(p.returncode, 0, p.stderr)
            sched = json.loads((Path(d) / "schedule.json").read_text())
            stages = [(s["target"], s["duration"]) for s in sched["stages"]]
            self.assertIn(("SPIKE", "20s"), stages, "the ramp")
            i = stages.index(("SPIKE", "20s"))
            self.assertEqual(stages[i + 1], ("SPIKE", "30s"), "the hold at the top")
            spike = [e for e in sched["events"] if e["kind"] == "spike"][0]
            self.assertEqual(spike["t1"] - spike["t0"], 53.0)

    def test_a_blip_window_is_baseline_for_the_main_generator(self):
        # the blip is a separate generator's, started at the window's t0 and
        # stopped by its own rule; these stages give it the ramp plus a margin
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, str(CONTRIB / "load_shape.py"), d],
                           capture_output=True, text=True, check=True,
                           env=dict(os.environ, VERIFY_SHAPE="BS", DISC_T_RAMP="20"))
            sched = json.loads((Path(d) / "schedule.json").read_text())
            targets = {s["target"] for s in sched["stages"]}
            self.assertEqual(targets, {"BASELINE", "SPIKE"}, "no blip level of its own")
            blip = [e for e in sched["events"] if e["kind"] == "blip"][0]
            self.assertEqual(blip["t1"] - blip["t0"], 22.0)
            self.assertIn(("BASELINE", "22s"), [(s["target"], s["duration"]) for s in sched["stages"]])

    def test_a_declared_hold_replaces_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, str(CONTRIB / "load_shape.py"), d],
                           capture_output=True, text=True, check=True,
                           env=dict(os.environ, VERIFY_SHAPE="S", DISC_T_RAMP="5", DISC_T_SPIKE="12"))
            sched = json.loads((Path(d) / "schedule.json").read_text())
            stages = [(s["target"], s["duration"]) for s in sched["stages"]]
            self.assertEqual(stages[1:3], [("SPIKE", "5s"), ("SPIKE", "12s")])


if __name__ == "__main__":
    unittest.main()
