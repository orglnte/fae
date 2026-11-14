"""The law's switches are off unless asked for, and off is the law as it
was: every judge call test_elastic_law makes returns exactly what it
returned before the switches existed (GOLDEN: a hash of each call's
inputs -> a hash of its verdict and reasons). On, the calibrated tolerance
widens each spike's mount and release limits by its own seconds, and only
those."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT

import test_elastic_law as T
from fae.cell import image as _image
from fae.cell import verify_timings as _timings
from fae.cell.contrib.elastic_resource import calibration
from fae.cell.verify import Ctx, run_verifier, verify_argv
from fae.cell.verify_host import VerifyHost
from fae.cell.verify_timings import HostTimings, Reference
from fae.experiment import config as config_mod
from fae.experiment.config import Config

law = T.law

GOLDEN = {
    "00bad61adec78f7f": "6dae56d9f6d4a081",
    "0bcc772411329fcd": "6dae56d9f6d4a081",
    "0f48ba962f4eb1c0": "36989bd13529ec5b",
    "1b30978afc73499e": "7414599766d9ee71",
    "1ba91336aa7ad4e1": "8265bfbdac28d29f",
    "1cedd7e0ab0885a6": "11d7702ff33f95f5",
    "25458815b97f7e5d": "8265bfbdac28d29f",
    "33788767871d8616": "5ea07528858a0124",
    "35092a73a3881606": "923190ab1d94d4d8",
    "3b88ce75d8738c3b": "37c97c7c3b9e6814",
    "3f0f9ce1d302fdd1": "f19ceb707450cb98",
    "41b98d989fcf6b1c": "6dae56d9f6d4a081",
    "46ad4e1dfc02e429": "760052291d44a2e6",
    "4a2c0268759f3cf5": "37c97c7c3b9e6814",
    "50b887d63bea0a00": "11396e6cd5a56be9",
    "518497d7804d7b57": "11396e6cd5a56be9",
    "5521d9bebdb970b9": "6dae56d9f6d4a081",
    "66e4ff6a74206dec": "6dae56d9f6d4a081",
    "6dd7c7bfa26e696d": "8265bfbdac28d29f",
    "6e71b2ceec42668b": "0496c326d6d574e5",
    "79b4004ab2f59062": "6dae56d9f6d4a081",
    "8045529aef6af186": "fbbe973fa1046e26",
    "8c26985e2def3ec7": "99ae9f46f8add560",
    "9797cfaf61271467": "e5fe962bca6f1a7e",
    "9da80a0f1438222f": "fa8a1752aa793227",
    "9e0c1e7fc9daad29": "37c97c7c3b9e6814",
    "a73824eca62ed117": "923190ab1d94d4d8",
    "aa57af1475350e56": "760052291d44a2e6",
    "ad26cfca50a7b294": "183b32cbee48812a",
    "be15c2441c68819e": "8265bfbdac28d29f",
    "c252a9a74bc7ab9d": "bad702dc74e274d3",
    "c8f13623f619e38d": "bc126871601ca017",
    "ca139da8d3b7afc8": "c69c0800d107cb7d",
    "ca57f9987cc198a0": "6dae56d9f6d4a081",
    "d54ee9f648b13ce2": "6dae56d9f6d4a081",
    "e3a8785cb4b392f4": "6dae56d9f6d4a081",
    "f71ca3818573604e": "d31eed398df7fba1",
    "f90102291d67f0eb": "17e4671bd9398c4e",
}


def _hash(x):
    return hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()[:16]


class TestOffIsTheLawAsItWas(unittest.TestCase):
    def calls(self, **extra):
        """{input hash: output hash} of every judge call test_elastic_law
        makes, each judged with `extra` added to its keywords."""
        orig, seen = law.judge, {}

        def rec(*a, **kw):
            out = orig(*a, **kw, **extra)
            seen[_hash([list(a), kw])] = _hash(list(out))
            return out
        suite = unittest.defaultTestLoader.loadTestsFromModule(T)
        with mock.patch.object(law, "judge", rec), open(os.devnull, "w") as null:
            result = unittest.TextTestRunner(stream=null, verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        return seen

    def test_every_fixture_judges_as_before(self):
        self.assertEqual(self.calls(), GOLDEN)

    def test_explicitly_off_and_zero_tolerance_judge_the_same(self):
        self.assertEqual(self.calls(mount_tol=(), release_tol=()), GOLDEN)
        self.assertEqual(self.calls(mount_tol=(0.0, 0.0, 0.0), release_tol=(0.0, 0.0, 0.0)),
                         GOLDEN)

    def test_main_passes_no_switch_when_none_is_set(self):
        self.assertEqual(law.switches({}), {})
        self.assertEqual(law.switches({"LAW_CALIBRATED_TOLERANCE": "0",
                                       "FAE_CALIBRATION": "{}"}), {})


class TestATolerance(unittest.TestCase):
    def test_widens_the_mount_deadline_by_its_seconds_only(self):
        r = T.rows(100, saturated_from=14, mounted=[(46, 70)])     # 2s past [5..44]
        self.assertEqual(T.judge(r, T.SCHED)[0], "false")
        self.assertEqual(T.judge(r, T.SCHED, mount_tol=(2.0,)), ("true", []))
        v, why = T.judge(r, T.SCHED, mount_tol=(1.0,))
        self.assertEqual(v, "false")
        self.assertIn("outside spike 1's window [4..45]", why[0])

    def test_widens_the_start_of_the_mount_window(self):
        r = T.rows(100, saturated_from=14, mounted=[(4, 70)])       # 1s before [5..44]
        for t in range(14, 20):
            r[t]["p99"] = 800.0
        self.assertIn("mount 1 at ts=4 outside", T.judge(r, T.SCHED)[1][0])
        self.assertEqual(T.judge(r, T.SCHED, mount_tol=(1.0,)), ("true", []))

    def test_moves_where_an_episode_counts_as_an_earlier_spikes(self):
        # up from 1s before the window and absorbing the spike: held, and
        # charged, without the tolerance; the spike's own, so a void, with it
        r = T.rows(100, saturated_from=14, mounted=[(4, 70)])
        self.assertIn("never released before spike 1", T.judge(r, T.SCHED)[1][0])
        self.assertEqual(T.judge(r, T.SCHED, mount_tol=(1.0,))[0], "void")

    def test_widens_a_mid_spike_release(self):
        r = T.rows(100, saturated_from=14, mounted=[(22, 56)])      # 2s before 58
        self.assertIn("released MID-spike", T.judge(r, T.SCHED)[1][0])
        self.assertEqual(T.judge(r, T.SCHED, release_tol=(2.0,)), ("true", []))

    def test_widens_a_late_release(self):
        r = T.rows(170, saturated_from=20, mounted=[(28, 85), (90, 130)])   # 4s past 81
        self.assertIn("released too late (ts=85", T.judge(r, T.SSB)[1][0])
        self.assertEqual(T.judge(r, T.SSB, release_tol=(4.0,)), ("true", []))
        self.assertEqual(T.judge(r, T.SSB, release_tol=(3.0,))[0], "false")

    def test_is_per_spike(self):
        r = T.rows(170, saturated_from=20, mounted=[(28, 85), (90, 130)])
        self.assertEqual(T.judge(r, T.SSB, release_tol=(0.0, 9.0))[0], "false")

    def test_a_mount_tolerance_leaves_a_release_alone(self):
        r = T.rows(100, saturated_from=14, mounted=[(22, 56)])
        self.assertEqual(T.judge(r, T.SCHED, mount_tol=(9.0,))[0], "false")

    def test_widens_the_canonical_deadline(self):
        r = T.rows(100, saturated_from=30, mounted=[(62, 90)])      # 32s > 30s
        self.assertEqual(T.judge(r, None)[0], "false")
        self.assertEqual(T.judge(r, None, mount_tol=(2.0,)), ("true", []))

    def test_never_turns_a_void_into_a_verdict(self):
        r = T.rows(100, saturated_from=None, mounted=[(46, 70)])
        self.assertEqual(T.judge(r, T.SCHED, mount_tol=(9.0,), release_tol=(9.0,))[0], "void")


class TestTheTolerancesFromCalibration(unittest.TestCase):
    SPREADS = {"spike1_mount_s": 1.5, "spike1_release_s": 0.5, "spike1_sat_s": 4.0,
               "spike2_release_s": 2.0, "arrangement_s": 30.0, "host_busy_p95": 1.0}

    def test_are_k_times_each_spikes_mount_and_release_spread(self):
        self.assertEqual(calibration.tolerances(self.SPREADS, 2),
                         ((3.0, 0.0), (1.0, 4.0)))

    def test_no_spike_values_is_no_tolerance(self):
        self.assertEqual(calibration.tolerances({"arrangement_s": 3.0}, 2), ((), ()))

    def test_main_takes_them_from_the_engines_reference_when_switched_on(self):
        ref = json.dumps({"spreads": self.SPREADS})
        self.assertEqual(law.switches({"LAW_CALIBRATED_TOLERANCE": "1", "FAE_CALIBRATION": ref,
                                       "LAW_TOLERANCE_K": "3"}),
                         {"mount_tol": (4.5, 0.0), "release_tol": (1.5, 6.0)})
        self.assertEqual(law.switches({"LAW_CALIBRATED_TOLERANCE": "1", "FAE_CALIBRATION": ref}),
                         {"mount_tol": (3.0, 0.0), "release_tol": (1.0, 4.0)})

    def test_switched_on_without_a_reference_is_no_tolerance(self):
        self.assertEqual(law.switches({"LAW_CALIBRATED_TOLERANCE": "1"}), {})

    def test_the_script_judges_with_them(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with (d / "trace.csv").open("w") as f:
                f.write("ts_s,p99_ms,infra_cache_up\n")
                for r in T.rows(100, saturated_from=14, mounted=[(46, 70)]):
                    f.write(f"{r['ts']},{r['p99']},{r['up']}\n")
            (d / "events.json").write_text(json.dumps(T.STARTS))
            (d / "schedule.json").write_text(json.dumps(T.SCHED))
            env = dict(os.environ, PYTHONPATH=str(ROOT), VERIFY_SAT_P99_MS="150",
                       FAE_CALIBRATION=json.dumps({"spreads": {"spike1_mount_s": 1.0}}))

            def run(**kw):
                p = subprocess.run([sys.executable, str(T.CONTRIB / "law.py"), str(d / "trace.csv"),
                                    str(d / "events.json"), "6000", "0", str(d / "schedule.json")],
                                   capture_output=True, text=True, env=dict(env, **kw))
                self.assertEqual(p.returncode, 0, p.stderr)
                return p.stdout.strip()
            self.assertTrue(run().startswith("false|mount 1 at ts=46"))
            self.assertEqual(run(LAW_CALIBRATED_TOLERANCE="1"), "true|")


class TestTheEngineHandsTheReference(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        out = root / "ws" / "cell-x"
        (out / "artifacts").mkdir(parents=True)
        self.ctx = Ctx(root=str(root), experiment_dir=str(root), workspace=str(out),
                       artifacts=str(out / "artifacts"), out=str(out), cid="cell-x",
                       task="T1", variant="only", arrangement="BSB")
        self.ref = Reference("only", "BSB", values={"spike1_mount_s": 9.0},
                             spreads={"spike1_mount_s": 1.0}, spread="measured", n=3)

    def test_the_verify_container_gets_it_as_json(self):
        with mock.patch("fae.cell.verify.verify_mounts", return_value=[]):
            argv = verify_argv(self.ctx, "img", Config({}, {}), environ={}, reference=self.ref)
            plain = verify_argv(self.ctx, "img", Config({}, {}), environ={})
        env = argv[argv.index("-e", argv.index("FAE_CALIBRATION=" + json.dumps(
            {"arrangement": "BSB", "n": 3, "spread": "measured", "spreads": {"spike1_mount_s": 1.0},
             "values": {"spike1_mount_s": 9.0}, "variant": "only"}, sort_keys=True)) - 1)]
        self.assertEqual(env, "-e")
        self.assertFalse(any(str(a).startswith("FAE_CALIBRATION=") for a in plain))

    def test_a_scored_verify_passes_its_hosts_reference_for_the_arrangement(self):
        section = HostTimings(host="local", date="d", fae_commit="a", experiment_commit="b",
                              references=(self.ref,))
        seen = {}

        def argv(ctx, image, conf=None, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")
        infra = SimpleNamespace(conf=Config({}, {}), variant="only")
        with mock.patch.object(_timings, "calibration_of", return_value=(section, None)), \
                mock.patch.object(_image, "for_variant", return_value="img"), \
                mock.patch.object(_image, "remove_container"), \
                mock.patch("fae.cell.verify.verify_argv", argv):
            v = run_verifier(self.ctx, infra, host=VerifyHost(""))
        self.assertEqual(v.stage, "verifier-host")
        self.assertEqual(seen["reference"], self.ref)

    def test_the_switch_is_off_by_default_and_exported_only_when_set(self):
        d = config_mod.ENGINE_DEFAULTS
        self.assertEqual((d["LAW_CALIBRATED_TOLERANCE"], d["LAW_TOLERANCE_K"]), ("", "2"))
        with tempfile.TemporaryDirectory() as td:
            conf = config_mod.load(td, env={})
            self.assertNotIn("LAW_CALIBRATED_TOLERANCE", conf.exported)
            self.assertEqual(conf.exported["LAW_TOLERANCE_K"], "2")
            (Path(td) / "fae.toml").write_text('[rig]\nLAW_CALIBRATED_TOLERANCE = "1"\n')
            self.assertEqual(config_mod.load(td, env={}).exported["LAW_CALIBRATED_TOLERANCE"], "1")


if __name__ == "__main__":
    unittest.main()
