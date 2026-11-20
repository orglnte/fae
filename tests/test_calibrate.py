"""`experiment calibrate`: an idle host, every reference green on the whole
gate, the guard refunded, and only then the host's section written."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell.cell import Cell
from fae.cell.verify import Verifier
from fae.cell.verify_host import VerifyHost, VerifyHostProbe
from fae.cell.verify_timings import FILE, VerifyTimings
from fae.experiment import calibrate as cal
from fae.experiment import config as config_mod

H = "ssh://operator@verify1"


class Probe(VerifyHostProbe):
    def measure(self, host, conf, log=print):
        return {"store_qps": 96.0}


class V(Verifier):
    CALIBRATION_KEYS = ("LOAD_TOP_RPS",)
    CALIBRATION_GUARD = {"variant": None, "env": {"LOAD_TOP_RPS": 60}, "stage": "load-shape"}

    def verify(self, ctx):
        raise NotImplementedError

    @classmethod
    def calibration_values(cls, arrangement_dir):
        return json.loads((Path(arrangement_dir) / "values.json").read_text())

    @classmethod
    def estimated_spread(cls, name, value):
        return 1.0


def cell_at(ws):
    return SimpleNamespace(ws=ws, archived_runs=lambda: Cell.archived_runs(SimpleNamespace(ws=ws)))


class Fixture(unittest.TestCase):
    ARRANGEMENTS = ("BSB", "SSB")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.green = True
        self.guard_state = ("refunded", "load-shape")
        self.calls = []
        d = SimpleNamespace(verifier_class=lambda: V, verify_host_probe=lambda: Probe,
                            active=["v1", "v2"], gate=SimpleNamespace(arrangements=self.ARRANGEMENTS))
        conf = {"VERIFY_DOCKER_HOST": H, "LOAD_TOP_RPS": "266"}
        self.exp = SimpleNamespace(config=lambda: conf, definition=d, root=self.root,
                                   smoke_cells=self.smoke_cells)
        patches = [mock.patch.object(VerifyHost, "ensure_up", return_value=True),
                   mock.patch.object(cal, "busy_fraction", side_effect=lambda h: self.busy),
                   mock.patch.object(cal, "commit_of", return_value="abc1234")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.busy = 0.02

    def smoke_cells(self, chosen, rep=1, full_gate=False, env=None):
        self.calls.append((tuple(chosen), rep, full_gate, dict(env or {})))
        out = []
        for vid in chosen:
            ws = self.root / "ws" / f"{vid}_r{rep}"
            arr = ws / "arrangements"
            if full_gate:
                for i, a in enumerate(self.ARRANGEMENTS, 1):
                    d = arr / f"{i:02d}-a1-{a}-{'green' if self.green else 'charged'}"
                    d.mkdir(parents=True)
                    (d / "values.json").write_text(json.dumps(
                        {"sat_s": 6.0 + rep, "mount_s": 9.0, f"only_r{rep}": 1.0}))
            else:
                d = arr / f"01-a1-BBS-{self.guard_state[0]}"
                d.mkdir(parents=True)
                (d / "verdict.json").write_text(json.dumps(
                    {"end_state": self.guard_state[0], "stage": self.guard_state[1]}))
            out.append((vid, self.green, "GREEN" if self.green else "SCALING FAILED", cell_at(ws)))
        return out

    def run_it(self, repeats=1):
        return cal.calibrate(self.exp, H, repeats=repeats, log=lambda m: None)


class TestAWrittenSection(Fixture):
    def test_one_run_estimates_the_spread(self):
        self.run_it()
        h = VerifyTimings.read(self.root).host(H)
        self.assertEqual(h.probe, {"store_qps": 96.0})
        self.assertEqual(h.load, {"LOAD_TOP_RPS": "266"})
        self.assertEqual((h.fae_commit, h.experiment_commit), ("abc1234", "abc1234"))
        r = h.reference("v2", "SSB")
        self.assertEqual((r.spread, r.n), ("estimated", 1))
        self.assertEqual(r.values, {"sat_s": 7.0, "mount_s": 9.0, "only_r1": 1.0})
        self.assertEqual(r.spreads["sat_s"], 1.0)
        self.assertEqual(len(h.references), 4)

    def test_repeats_measure_it_over_the_names_every_run_has(self):
        self.run_it(repeats=2)
        r = VerifyTimings.read(self.root).host(H).reference("v1", "BSB")
        self.assertEqual((r.spread, r.n), ("measured", 2))
        self.assertEqual(r.values, {"sat_s": 7.5, "mount_s": 9.0})
        self.assertAlmostEqual(r.spreads["sat_s"], 0.7071, places=3)
        self.assertEqual(r.spreads["mount_s"], 0.0)

    def test_every_run_is_pinned_to_the_host_and_runs_the_whole_gate(self):
        self.run_it()
        refs = [c for c in self.calls if c[2]]
        self.assertEqual(refs, [(("v1", "v2"), 1, True, {"VERIFY_DOCKER_HOST": H})])

    def test_the_guard_runs_its_env_on_the_first_variant_once(self):
        self.run_it()
        guard = [c for c in self.calls if not c[2]]
        self.assertEqual(guard, [(("v1",), 2, False, {"VERIFY_DOCKER_HOST": H, "LOAD_TOP_RPS": "60"})])

    def test_another_hosts_section_is_kept(self):
        (self.root / FILE).write_text('[hosts.local]\ndate = "d"\nfae_commit = "a"\n'
                                      'experiment_commit = "b"\n')
        self.run_it()
        self.assertEqual(sorted(VerifyTimings.read(self.root).hosts), ["local", H])


class TestNothingIsWritten(Fixture):
    def refused(self, why):
        with self.assertRaises(cal.CalibrationError) as ctx:
            self.run_it()
        self.assertIn(why, str(ctx.exception))
        self.assertFalse((self.root / FILE).exists())

    def test_on_a_busy_host(self):
        self.busy = 0.35
        self.refused("is busy (35% >= 10%)")
        self.assertEqual(self.calls, [])

    def test_when_a_reference_is_not_green(self):
        self.green = False
        self.refused("reference v1 (run 1) not green")

    def test_when_the_guard_was_judged(self):
        self.guard_state = ("charged", "scaling")
        self.refused("was not refunded at stage 'load-shape'")

    def test_for_a_host_outside_the_pool(self):
        with self.assertRaises(cal.CalibrationError) as ctx:
            cal.calibrate(self.exp, "ssh://operator@elsewhere", log=lambda m: None)
        self.assertIn("is not a verify host of this config", str(ctx.exception))


class TestPieces(unittest.TestCase):
    def test_cpu_busy_counts_idle_and_iowait_as_free(self):
        before = "cpu 100 0 100 700 100 0 0 0".split()
        after = "cpu 150 0 150 1500 100 0 0 0".split()
        self.assertAlmostEqual(cal.cpu_busy(before, after), 100 / 900)

    def test_an_unlabelled_gate_arrangement_is_seed(self):
        with tempfile.TemporaryDirectory() as td:
            for n in ("01-a1-seed-green", "02-a1-BSB-green", "03-a1-SSB-charged"):
                (Path(td) / "arrangements" / n).mkdir(parents=True)
            self.assertEqual(sorted(cal.arrangement_dirs(cell_at(Path(td)))), ["BSB", "seed"])

    def test_an_arrangements_seconds_are_its_start_to_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            for n, end, started, ended in (("01-a1-BSB-green", "green", "2026-11-14T10:00:00Z",
                                            "2026-11-14T10:06:40Z"),
                                           ("02-a1-SSB-refunded", "refunded", "2026-11-14T10:07:00Z",
                                            "2026-11-14T10:08:00Z"),
                                           ("03-a1-SBS-green", "green", None, None)):
                d = Path(td) / "arrangements" / n
                d.mkdir(parents=True)
                (d / "verdict.json").write_text(json.dumps({"end_state": end, "started": started,
                                                            "ended": ended}))
            self.assertEqual(cal.arrangement_seconds(cell_at(Path(td))), {"BSB": 400.0})

    def test_the_environment_pins_the_verify_host(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "fae.toml").write_text('[rig]\nVERIFY_DOCKER_HOST = "ssh://a, ssh://b"\n')
            conf = config_mod.load(td, env={"VERIFY_DOCKER_HOST": "ssh://b"})
            self.assertEqual(conf.get("VERIFY_DOCKER_HOST"), "ssh://b")
            self.assertEqual(config_mod.load(td, env={}).get("VERIFY_DOCKER_HOST"), "ssh://a, ssh://b")
            local_conf = config_mod.load(td, env={"VERIFY_DOCKER_HOST": ""})
            self.assertIsNone(local_conf.get("VERIFY_DOCKER_HOST"))
            pool = VerifyHost.pool(local_conf)
            self.assertEqual([(h.docker_host, h.remote) for h in pool], [("", False)])


if __name__ == "__main__":
    unittest.main()
