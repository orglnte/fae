"""A verify runs only on a calibrated host, with that host's calibrated load
values; on any other host it stands the cell down with the command that
calibrates it."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell import image as _image
from fae.cell import verify_timings as _timings
from fae.cell.verify import Ctx, run_verifier
from fae.cell.verify_host import VerifyHost
from fae.cell.verify_timings import FILE, HostTimings, VerifyTimings, calibration_of
from fae.experiment.config import Config

H = "ssh://operator@verify1"


class GateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out = self.root / "ws" / "cell-x"
        (self.out / "artifacts").mkdir(parents=True)
        self.ctx = Ctx(root=str(self.root), experiment_dir=str(self.root), workspace=str(self.out),
                       artifacts=str(self.out / "artifacts"), out=str(self.out), cid="cell-x",
                       task="T1", variant="only", arrangement="BSB")
        self.conf = Config({"LOAD_TOP_RPS": "360", "OTHER": "x"},
                           {"LOAD_TOP_RPS": "360", "OTHER": "x"})
        self.infra = SimpleNamespace(conf=self.conf, variant="only")

    def run_on(self, section):
        seen = {}

        def for_variant(variant, definition, conf, **kw):
            seen["conf"] = conf
            raise RuntimeError("stop here")
        with mock.patch.object(_timings, "calibration_of", return_value=section), \
                mock.patch.object(_image, "for_variant", side_effect=for_variant):
            return run_verifier(self.ctx, self.infra, host=VerifyHost("")), seen


class TestAnUncalibratedHost(GateCase):
    def test_stands_the_cell_down_with_the_command_and_runs_nothing(self):
        why = "verify host local is not calibrated: run `python3 cli.py experiment calibrate --host local`"
        v, seen = self.run_on((None, why))
        self.assertEqual((v.ok, v.charge, v.stage), (False, False, "verifier-host"))
        self.assertEqual(v.stand_down, ("uncalibrated-verify-host",))
        self.assertIn("experiment calibrate --host local", v.why)
        self.assertEqual(seen, {})
        self.assertIn("not calibrated", (self.out / "verifier.log").read_text())


class TestACalibratedHost(GateCase):
    def test_its_load_values_are_the_verifys(self):
        section = HostTimings(host="local", date="d", fae_commit="a", experiment_commit="b",
                              load={"LOAD_TOP_RPS": "266"})
        v, seen = self.run_on((section, None))
        self.assertEqual(v.stage, "verifier-image")
        conf = seen["conf"]
        self.assertEqual(conf.get("LOAD_TOP_RPS"), "266")
        self.assertEqual(conf.exported["LOAD_TOP_RPS"], "266")
        self.assertEqual(conf.get("OTHER"), "x")
        self.assertEqual(self.conf.get("LOAD_TOP_RPS"), "360")


class TestCalibrationOf(unittest.TestCase):
    def test_no_section_names_the_command(self):
        with tempfile.TemporaryDirectory() as td:
            found, why = calibration_of(td, VerifyHost(H))
        self.assertIsNone(found)
        self.assertIn(f"`python3 cli.py experiment calibrate --host {H}`", why)

    def test_an_unreadable_file_is_the_reason(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / FILE).write_text("[hosts\n")
            found, why = calibration_of(td, VerifyHost(H))
        self.assertIsNone(found)
        self.assertIn("unreadable", why)

    def test_the_hosts_section(self):
        with tempfile.TemporaryDirectory() as td:
            section = HostTimings(host=H, date="d", fae_commit="a", experiment_commit="b")
            VerifyTimings().with_host(section).write(td)
            self.assertEqual(calibration_of(td, VerifyHost(H)), (section, None))


class TestConfigWithValues(unittest.TestCase):
    def test_a_key_is_exported_only_where_it_already_was(self):
        c = Config({"A": "1", "B": "2"}, {"A": "1"}).with_values({"A": "9", "B": "8", "C": "7"})
        self.assertEqual((c.get("A"), c.get("B"), c.get("C")), ("9", "8", "7"))
        self.assertEqual(c.exported, {"A": "9"})


if __name__ == "__main__":
    unittest.main()
