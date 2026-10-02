"""An infra that dies under a measurement is a rig fault, not a verdict:
the cell probes the arm's infra before every arrangement and again after
a charged fail, and voids the arrangement (stage "infra", refunded)
instead of sealing what a corpse measured."""
import subprocess
import unittest
from unittest import mock

from _ctx import ROOT

from fae.cell import cell
from fae.cell import experiment as _experiment
from fae.cell.variants import base
from fae.cell.verify import Verdict


class TestTheProbeClassifiesADaemon(unittest.TestCase):
    def _run(self, rc, stderr="", timeout=False):
        if timeout:
            return mock.Mock(side_effect=subprocess.TimeoutExpired("docker", 15))
        return mock.Mock(return_value=mock.Mock(returncode=rc, stderr=stderr))

    def test_an_answering_daemon_is_alive(self):
        with mock.patch.object(base, "_run", self._run(0)):
            self.assertTrue(base.daemon_answers(["docker", "version"]))

    def test_a_hard_connect_failure_is_dead(self):
        for err in ("Cannot connect to the Docker daemon at tcp://127.0.0.1:26852",
                    "error during connect: ... connection refused",
                    "dial unix /var/run/docker.sock: no such file or directory"):
            with mock.patch.object(base, "_run", self._run(1, err)):
                self.assertFalse(base.daemon_answers(["docker", "version"]), err)

    def test_a_slow_daemon_is_alive_and_so_is_any_other_error(self):
        # a fail under a slow daemon stays a fail: only "gone" refunds
        with mock.patch.object(base, "_run", self._run(0, timeout=True)):
            self.assertTrue(base.daemon_answers(["docker", "version"]))
        with mock.patch.object(base, "_run", self._run(1, "permission denied")):
            self.assertTrue(base.daemon_answers(["docker", "version"]))


class TestTheDefaultIsNotAlive(unittest.TestCase):
    def test_an_arm_without_a_probe_voids_rather_than_assumes(self):
        class Bare(base.Variant):
            ARM = "bare"
        self.assertFalse(Bare(mock.Mock(cid="c", ws="/nonexistent", root=ROOT, conf=None,
                                        condition="apidocs")).infra_alive())

    def test_the_fixture_seam_is_alive(self):
        self.assertTrue(base.NoopVariant(mock.Mock(cid="c", ws="/nonexistent", root=ROOT,
                                                     conf=None, condition="apidocs")).infra_alive())


class TestTheTwoGates(unittest.TestCase):
    """Cell.verify around the runner: dead before -> void without spending
    the verifier; charged fail then dead -> void; alive -> the verdict stands."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        (d / "ws" / "c" / "artifacts").mkdir(parents=True)
        self.c = cell.Cell("c", workspaces=d / "ws", root=ROOT)
        self.alive = [True]
        self.c._treatment = mock.Mock(infra_alive=lambda: self.alive[0])

    def verify_with(self, verdict, alive_after, measured=None):
        calls = []

        def runner(ctx, variant, timeout_s, log_dir=None):
            calls.append(ctx)
            self.alive[0] = alive_after
            return verdict
        vcls = mock.Mock(REQUIRED_OUTPUTS=(), NOT_RUN_STAGES=frozenset(), MEASURED_STAGES=measured, FILES=(), FEEDBACK_LOGS=())
        with mock.patch.object(cell, "run_verifier", runner), \
                mock.patch.object(cell.Cell, "expected_fp", new_callable=mock.PropertyMock,
                                  return_value=""), \
                mock.patch.object(_experiment, "current",
                                  return_value=mock.Mock(exclusive=None,
                                                         verifier_class=lambda: vcls)):
            r = self.c.verify(shape="A")
        return r, calls

    def test_dead_before_voids_without_running_the_verifier(self):
        self.alive[0] = False
        r, calls = self.verify_with(Verdict(ok=True), alive_after=False)
        self.assertEqual(calls, [])
        self.assertEqual((r.green, r.stage_failed, r.charge), (False, "infra", False))

    def test_a_charged_fail_with_the_infra_dead_after_is_a_void(self):
        r, calls = self.verify_with(Verdict(ok=False, stage="scaling", charge=True), alive_after=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual((r.stage_failed, r.charge), ("infra", False))

    def test_a_charged_fail_with_the_infra_alive_stands(self):
        r, _ = self.verify_with(Verdict(ok=False, stage="scaling", charge=True), alive_after=True)
        self.assertEqual((r.stage_failed, r.charge), ("scaling", True))

    def test_only_the_verifiers_measured_stages_are_covered_when_it_names_them(self):
        r, _ = self.verify_with(Verdict(ok=False, stage="deploy", charge=True),
                                alive_after=False, measured={"e2e", "scaling"})
        self.assertEqual((r.stage_failed, r.charge), ("deploy", True))
        r, _ = self.verify_with(Verdict(ok=False, stage="e2e", charge=True),
                                alive_after=False, measured={"e2e", "scaling"})
        self.assertEqual((r.stage_failed, r.charge), ("infra", False))

    def test_a_green_is_never_second_guessed(self):
        r, _ = self.verify_with(Verdict(ok=True), alive_after=False)
        self.assertTrue(r.green)


if __name__ == "__main__":
    unittest.main()
