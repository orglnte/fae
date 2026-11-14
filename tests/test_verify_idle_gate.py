"""A scored verify on a remote verify host starts only once that host is
idle: its CPU busy share is read before each arrangement, read again after a
wait while it is busy, and an arrangement whose host stays busy is refunded
with every reading and an ALERT. A smoke verify and the local daemon are
never gated."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell import image as _image
from fae.cell import verify_host as _vh
from fae.cell import verify_timings as _timings
from fae.cell.verify import Ctx, Verifier, run_verifier, take_events
from fae.cell.verify_host import VerifyHost
from fae.cell.verify_timings import HostTimings
from fae.experiment import config as config_mod
from fae.experiment.config import Config

H = "ssh://operator@verify1"


class IdleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out = self.root / "ws" / "cell-x"
        (self.out / "artifacts").mkdir(parents=True)
        self.ctx = Ctx(root=str(self.root), experiment_dir=str(self.root), workspace=str(self.out),
                       artifacts=str(self.out / "artifacts"), out=str(self.out), cid="cell-x",
                       task="T1", variant="only", arrangement="BSB")
        self.conf = Config({}, {})
        self.infra = SimpleNamespace(conf=self.conf, variant="only")
        self.section = HostTimings(host=H, date="d", fae_commit="a", experiment_commit="b")
        self.slept = []

    def run_on(self, readings, host=H):
        seen = {}

        def for_variant(variant, definition, conf, **kw):
            seen["image"] = True
            raise RuntimeError("stop here")
        busy = mock.Mock(side_effect=list(readings))
        with mock.patch.object(_timings, "calibration_of", return_value=(self.section, None)), \
                mock.patch.object(_image, "for_variant", side_effect=for_variant), \
                mock.patch.object(VerifyHost, "ensure_up", return_value=True), \
                mock.patch.object(VerifyHost, "busy_share", busy), \
                mock.patch.object(_vh.time, "sleep", side_effect=self.slept.append):
            v = run_verifier(self.ctx, self.infra, host=VerifyHost(host))
        return v, seen, busy.call_count


class TestAnIdleHost(IdleCase):
    def test_is_read_once_and_the_verify_goes_on(self):
        v, seen, reads = self.run_on([0.03])
        self.assertEqual((v.stage, reads, self.slept), ("verifier-image", 1, []))
        self.assertTrue(seen["image"])

    def test_a_host_that_becomes_idle_after_a_wait_is_verified(self):
        v, seen, reads = self.run_on([0.40, 0.05])
        self.assertEqual((v.stage, reads, self.slept), ("verifier-image", 2, [60.0]))
        self.assertIn("busy 40% (max 10%)", (self.out / "verifier.log").read_text())


class TestAHostThatStaysBusy(IdleCase):
    def test_is_refunded_with_every_reading_and_nothing_runs(self):
        v, seen, reads = self.run_on([0.40, 0.30, 0.25, 0.12])
        self.assertEqual((v.ok, v.charge, v.stage, v.stand_down), (False, False, "host-busy", ()))
        self.assertEqual(reads, 4)
        self.assertEqual(self.slept, [60.0, 60.0, 60.0])
        self.assertIn("0.40 0.30 0.25 0.12", v.why)
        self.assertEqual(seen, {})

    def test_writes_an_alert_the_host_appends_to_the_ledger(self):
        self.run_on([0.5] * 4)
        (stamp, event, fields), = take_events(self.out)
        self.assertEqual(event, "ALERT")
        self.assertTrue(fields[0].startswith("HOST-BUSY BSB busy=0.50 0.50 0.50 0.50"), fields)

    def test_the_threshold_wait_and_readings_are_the_configs(self):
        self.conf.values.update(VERIFY_IDLE_MAX="0.5", VERIFY_IDLE_WAIT_S="5",
                                VERIFY_IDLE_RETRIES="1")
        v, _, reads = self.run_on([0.6, 0.55])
        self.assertEqual((v.stage, reads, self.slept), ("host-busy", 2, [5.0]))

    def test_a_busy_share_at_the_threshold_is_busy(self):
        v, _, _ = self.run_on([0.10] * 4)
        self.assertEqual(v.stage, "host-busy")

    def test_its_verifier_outputs_are_not_expected(self):
        self.assertIn("host-busy", Verifier.NOT_RUN_STAGES)


class TestNotGated(IdleCase):
    def test_a_smoke_verify(self):
        self.conf.values["SMOKE"] = "1"
        v, _, reads = self.run_on([0.9] * 4)
        self.assertEqual((v.stage, reads), ("verifier-image", 0))

    def test_the_local_daemon(self):
        self.section = HostTimings(host="local", date="d", fae_commit="a", experiment_commit="b")
        v, _, reads = self.run_on([0.9] * 4, host="")
        self.assertEqual((v.stage, reads), ("verifier-image", 0))


class TestAnUnreadableHost(IdleCase):
    def test_is_a_refunded_host_fault(self):
        v, seen, _ = self.run_on([RuntimeError("op@verify1: /proc/stat unreadable: denied")])
        self.assertEqual((v.charge, v.stage), (False, "verifier-host"))
        self.assertIn("unreadable", v.why)
        self.assertEqual(seen, {})


class TestTheBusyShare(unittest.TestCase):
    def test_is_read_twice_over_one_ssh(self):
        out = "cpu 100 0 100 700 100 0 0 0\ncpu 250 0 250 1600 100 0 0 0\n"
        with mock.patch.object(_vh.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0, stdout=out,
                                                            stderr="")) as run:
            busy = VerifyHost(H).busy_share()
        self.assertAlmostEqual(busy, 300 / 1200)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", "operator@verify1"])
        self.assertIn("sleep 5", argv[4])

    def test_an_unreadable_stat_raises(self):
        with mock.patch.object(_vh.subprocess, "run",
                               return_value=SimpleNamespace(returncode=255, stdout="",
                                                            stderr="denied")):
            with self.assertRaisesRegex(RuntimeError, "unreadable: denied"):
                VerifyHost(H).busy_share()

    def test_the_engine_defaults(self):
        d = config_mod.ENGINE_DEFAULTS
        self.assertEqual((d["VERIFY_IDLE_MAX"], d["VERIFY_IDLE_WAIT_S"], d["VERIFY_IDLE_RETRIES"]),
                         ("0.10", "60", "3"))


if __name__ == "__main__":
    unittest.main()
