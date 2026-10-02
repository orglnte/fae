"""conduct: the unified scheduler. Everything it touches is mocked; nothing
here starts a process. Fixtures patch runs.common.WS / runs.common.ORCH to a temp tree, the
same convention as test_operator_control.py.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import runs, OrchTmpCase, ROOT


class ConductCase(OrchTmpCase):
    def setUp(self):
        super().setUp()   # temp tree + WS/ORCH/RESPAWN_BOOK/TRANSITIONS_LOG/RECONCILE_LOG
        self.spawned = []
        self.live = {}
        self.patches = [
            # per-pid conduct logs land in the tmp tree, not the repo
            mock.patch.dict(runs.os.environ,
                            {"CONDUCT_LOG_DIR": self._tmp.name}),
            mock.patch.object(runs.state, "loop_parents", side_effect=lambda: dict(self.live)),
            mock.patch.object(runs.state, "containers", return_value=set()),
            mock.patch.object(runs.ops, "prestart_clean", lambda cid: None),
            mock.patch.object(runs.state, "pause_lock", side_effect=lambda cid: None),
            mock.patch.object(runs.ops, "_spawn_detached", side_effect=self._spawn),
            mock.patch.object(runs.conduct.image, "ensure_current", return_value=True),
            mock.patch.object(runs.time, "sleep", self._tick),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.spawn_rc = None            # None = alive
        self.per_model = 8              # off unless a test lowers it
        self.rounds = 0
        self.max_rounds = 4

    def _spawn(self, argv, env, cid, what="spawn"):
        self.spawned.append(cid)
        if self.spawn_rc is None:
            self.live[cid] = 4242       # becomes a live loop
        return self.spawn_rc

    CADENCE = 7             # conduct's poll interval in these tests — chosen
                            # to collide with nothing (mutex settles with
                            # sleep(1.0), so counting s==1 ended the run
                            # before iteration 2 ever happened)

    def _tick(self, s):
        if s != self.CADENCE:
            return          # mutex settle / de-herd sleeps, not the loop
        self.rounds += 1
        if self.rounds >= self.max_rounds:
            raise KeyboardInterrupt     # end the test run

    def q(self, model, specs):
        for spec in specs:
            runs.queue.enqueue(model, spec)

    def pending(self, model):
        """The lane's pending specs, in admission order."""
        return [runs.queue.read_spec(p) for p in runs.queue.lane_specs(model)]

    def claimed(self, model):
        return [runs.queue.spec_cid(p) for p in runs.queue.running_specs(model)]

    def spec(self, treatment="beta", condition="apidocs", rep=1):
        return {"task": "T1", "treatment": treatment, "condition": condition,
                "rep": rep, "budget": 10, "fresh": False}

    def run_conduct(self, n=5, supervise=0):
        """supervise=0 keeps the scheduler tests pure-scheduler; the
        supervision tests below mock the pass and turn it on."""
        out = io.StringIO()
        # Preflight shells out to docker; these tests are about scheduling.
        with contextlib.redirect_stdout(out), \
                contextlib.suppress(KeyboardInterrupt), \
                mock.patch.object(runs.conduct, "_conduct_preflight", return_value=True):
            runs.conduct.conduct(SimpleNamespace(limit=n, interval=self.CADENCE,
                                         per_model=self.per_model,
                                         supervise_interval=supervise))
        return out.getvalue()


class TestFairness(ConductCase):
    def test_round_robin_interleaves_lanes(self):
        self.q("aaa", [self.spec(rep=r) for r in (1, 2)])
        self.q("bbb", [self.spec(rep=r) for r in (1, 2)])
        self.run_conduct(n=4)
        models = [c.split("_")[0] for c in self.spawned]
        self.assertEqual(sorted(models[:2]), ["aaa", "bbb"],
                         f"first two admissions came from one lane: {models}")

    def test_global_cap_is_respected(self):
        self.q("aaa", [self.spec(rep=r) for r in range(1, 9)])
        self.run_conduct(n=3)
        self.assertEqual(len(self.live), 3)

    def test_exits_when_backlog_empty_and_no_loops(self):
        out = self.run_conduct()
        self.assertIn("backlog empty", out)


class TestStarvationGuard(ConductCase):
    def test_lane_with_no_live_cell_admits_first(self):
        """The starved lane sorts LAST alphabetically, so only the
        fewest-live-cells ordering can put it first."""
        self.live["aaa_high_beta_apidocs_T1_r9"] = 1
        self.q("aaa", [self.spec()])
        self.q("zzz", [self.spec(rep=2)])
        self.run_conduct(n=3)
        self.assertTrue(self.spawned and self.spawned[0].startswith("zzz_"),
                        f"starved lane must admit first: {self.spawned}")


class TestPerModelCap(ConductCase):
    def test_default_one_cell_per_model(self):
        self.per_model = 1
        self.q("aaa", [self.spec(rep=r) for r in (1, 2, 3)])
        self.run_conduct(n=5)
        self.assertEqual(len(self.spawned), 1,
                         f"one lane must hold one live cell: {self.spawned}")

    def test_every_non_empty_lane_gets_exactly_one(self):
        self.per_model = 1
        for lane in ("aaa", "bbb", "ccc"):
            self.q(lane, [self.spec(rep=r) for r in (1, 2, 3)])
        self.run_conduct(n=3)
        self.assertEqual(sorted(c.split("_")[0] for c in self.spawned),
                         ["aaa", "bbb", "ccc"], self.spawned)
        for lane in ("aaa", "bbb", "ccc"):
            self.assertEqual(len(self.claimed(lane)), 1)

    def test_cap_mismatch_warns(self):
        self.per_model = 1
        self.q("aaa", [self.spec()])
        self.q("bbb", [self.spec()])
        out = self.run_conduct(n=5)
        self.assertIn("WARNING: global cap 5 != 2", out)

    def test_no_warning_when_the_cap_is_the_lane_count(self):
        self.per_model = 1
        self.q("aaa", [self.spec()])
        self.q("bbb", [self.spec()])
        out = self.run_conduct(n=2)
        self.assertNotIn("WARNING", out)


class TestFinishedSpecsAreRetired(ConductCase):
    """Admission retires a terminal cell's spec, but only for a lane it is
    about to admit from — a lane at its per-model cap is never scanned, and a
    cell resumed by hand finishes while its spec is still in the queue."""

    def _done_cell(self, cid):
        d = self.ws / cid
        (d / "artifacts").mkdir(parents=True, exist_ok=True)
        (d / "iterations.log").write_text(
            "2026-08-15T09:00:00Z\tITER\tgreen\tattempt=1 shapes=all\n"
            "2026-08-15T09:00:00Z\tEND\tcid\tgreen=true\n")
        (d / "cell.env").write_text("ATTEMPT_BUDGET=10\nMODEL_VERSION=5\n")

    def test_a_done_cells_spec_leaves_the_lane(self):
        cid = "sonnet_high_beta_apidocs_T1_r1"
        m, t, v, task, rep = runs.parse_cell_id(cid)
        runs.queue.enqueue(m, dict(task=task, treatment=t, condition=v, rep=int(rep),
                             budget=10, fresh=False))
        self._done_cell(cid)
        with mock.patch.object(runs.state, "containers", return_value=set()):
            runs.supervise._retire_finished_specs()
        self.assertEqual(runs.queue.lane_specs(m), [])
        self.assertTrue((runs.common.ORCH / "done" / m / f"{cid}.json").exists())

    def test_an_unfinished_cells_spec_stays(self):
        cid = "sonnet_high_beta_apidocs_T1_r1"
        m, t, v, task, rep = runs.parse_cell_id(cid)
        runs.queue.enqueue(m, dict(task=task, treatment=t, condition=v, rep=int(rep),
                             budget=10, fresh=False))
        d = self.ws / cid
        (d / "artifacts").mkdir(parents=True, exist_ok=True)
        (d / "iterations.log").write_text(
            "2026-08-15T09:00:00Z\tITER\tfail\tattempt=1 stage=scaling\n")
        (d / "cell.env").write_text("ATTEMPT_BUDGET=10\nMODEL_VERSION=5\n")
        with mock.patch.object(runs.state, "containers", return_value=set()):
            runs.supervise._retire_finished_specs()
        self.assertEqual(len(runs.queue.lane_specs(m)), 1)

    def test_resume_retires_it_too(self):
        """`cell resume` is what leaves these behind — it respawns without
        going through admission, so the spec is never claimed."""
        cid = "sonnet_high_beta_apidocs_T1_r1"
        m, t, v, task, rep = runs.parse_cell_id(cid)
        runs.queue.enqueue(m, dict(task=task, treatment=t, condition=v, rep=int(rep),
                             budget=10, fresh=False))
        self._done_cell(cid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(runs.state, "loop_parents", return_value={}), \
                mock.patch.object(runs.state, "containers", return_value=set()):
            runs.ops.resume(SimpleNamespace(selectors=[cid], force=False))
        self.assertIn("spec retired", out.getvalue())
        self.assertEqual(runs.queue.lane_specs(m), [])

    def test_resume_claims_the_spec_it_respawns(self):
        """A running cell whose spec is still queued reads as backlog, and
        conduct can admit it a second time. The claim is one rename(2), so the
        two cannot both win."""
        cid = "sonnet_high_beta_apidocs_T1_r1"
        m, t, v, task, rep = runs.parse_cell_id(cid)
        runs.queue.enqueue(m, dict(task=task, treatment=t, condition=v, rep=int(rep),
                             budget=10, fresh=False))
        d = self.ws / cid
        (d / "artifacts").mkdir(parents=True, exist_ok=True)
        (d / "iterations.log").write_text(
            "2026-08-15T09:00:00Z\tITER\tfail\tattempt=1 stage=scaling\n")
        (d / "cell.env").write_text("ATTEMPT_BUDGET=10\nMODEL_VERSION=5\n")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(runs.state, "loop_parents", return_value={}), \
                mock.patch.object(runs.state, "containers", return_value=set()), \
                mock.patch.object(runs.ops, "refresh_cell_creds"), \
                mock.patch.object(runs.ops, "prestart_clean"), \
                mock.patch.object(runs.ops, "_spawn_detached", return_value=None):
            runs.ops.resume(SimpleNamespace(selectors=[cid], force=False))
        self.assertEqual(runs.queue.lane_specs(m), [], "spec still in the lane")
        self.assertTrue((runs.common.ORCH / "running" / m / f"{cid}.json").exists())

    def test_a_spawn_that_never_starts_gives_the_spec_back(self):
        cid = "sonnet_high_beta_apidocs_T1_r1"
        m, t, v, task, rep = runs.parse_cell_id(cid)
        runs.queue.enqueue(m, dict(task=task, treatment=t, condition=v, rep=int(rep),
                             budget=10, fresh=False))
        d = self.ws / cid
        (d / "artifacts").mkdir(parents=True, exist_ok=True)
        (d / "iterations.log").write_text(
            "2026-08-15T09:00:00Z\tITER\tfail\tattempt=1 stage=scaling\n")
        (d / "cell.env").write_text("ATTEMPT_BUDGET=10\nMODEL_VERSION=5\n")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(runs.state, "loop_parents", return_value={}), \
                mock.patch.object(runs.state, "containers", return_value=set()), \
                mock.patch.object(runs.ops, "refresh_cell_creds"), \
                mock.patch.object(runs.ops, "prestart_clean"), \
                mock.patch.object(runs.ops, "_spawn_detached", return_value=3):
            runs.ops.resume(SimpleNamespace(selectors=[cid], force=False))
        self.assertEqual(len(runs.queue.lane_specs(m)), 1, "spec was not returned")
        self.assertFalse((runs.common.ORCH / "running" / m / f"{cid}.json").exists())

    def test_a_preview_moves_nothing(self):
        cid = "sonnet_high_beta_apidocs_T1_r1"
        m, t, v, task, rep = runs.parse_cell_id(cid)
        runs.queue.enqueue(m, dict(task=task, treatment=t, condition=v, rep=int(rep),
                             budget=10, fresh=False))
        self._done_cell(cid)
        with mock.patch.object(runs.state, "containers", return_value=set()):
            runs.supervise._retire_finished_specs(dry=True)
        self.assertEqual(len(runs.queue.lane_specs(m)), 1)


class TestPreflight(unittest.TestCase):
    """A fresh clone and a reset Docker VM look identical from conduct: no
    agent image, and every spawn dying at preflight until there is one."""

    def _run(self, rcs, image_ok=True):
        calls = []

        def fake(cmd, *a, **k):
            calls.append(cmd)
            key = " ".join(cmd[:3])
            return mock.Mock(returncode=rcs.get(key, 0))

        self.ensured = mock.Mock(return_value=image_ok)
        with mock.patch.object(runs.subprocess, "run", side_effect=fake), \
                mock.patch.object(runs.conduct.image, "ensure_agent", self.ensured), \
                contextlib.redirect_stdout(io.StringIO()):
            return runs.conduct._conduct_preflight(), calls

    def test_a_dead_daemon_stops_conduct_before_it_admits(self):
        ok, calls = self._run({"docker info": 1})
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "nothing else runs once docker is out")
        self.ensured.assert_not_called()

    def test_the_agent_image_is_ensured_base_and_layer(self):
        # missing base built, clients current, the experiment's layer over
        # it: one call, fae/driver/image.py's (its own tests pin the steps)
        ok, _ = self._run({})
        self.assertTrue(ok)
        self.ensured.assert_called_once()

    def test_a_present_image_pulls_nothing(self):
        ok, calls = self._run({})
        self.assertTrue(ok)
        self.assertFalse(any(c[:2] == ["docker", "pull"] for c in calls))

    def test_every_arm_is_probed_and_a_refusal_is_a_note(self):
        # The experiment's substrate (its daemons, images, tools) is the
        # variants' own preflight; conduct runs it and reports, never installs.
        with mock.patch.object(runs.rig, "_probe_arms", return_value=2) as probe:
            ok, calls = self._run({})
        self.assertTrue(ok, "a refused arm is a note, not a stop")
        self.assertEqual(probe.call_count, 1)
        self.assertFalse(any(c[:2] == ["docker", "pull"] or c[0] == "which" for c in calls))

    def test_a_failed_build_stops_conduct(self):
        ok, _ = self._run({}, image_ok=False)
        self.assertFalse(ok)


class TestClaims(ConductCase):
    """A spec's state is where it lives: queue/ -> running/ -> done/, one
    rename each, so an interrupted conduct can neither lose nor double it."""

    def test_admission_moves_the_spec_into_running(self):
        self.q("aaa", [self.spec()])
        self.run_conduct()
        cid = "aaa_high_beta_apidocs_T1_r1"
        self.assertEqual(self.claimed("aaa"), [cid])
        self.assertEqual(self.pending("aaa"), [])

    def test_a_claim_whose_loop_died_is_restarted(self):
        self.q("aaa", [self.spec()])
        self.max_rounds = 2
        self.run_conduct()
        self.live.clear()                      # loop vanished between polls
        self.max_rounds, self.rounds = 2, 0
        self.run_conduct()
        self.assertEqual(len(self.spawned), 2, self.spawned)
        self.assertEqual(len(self.claimed("aaa")), 1, "claim must be kept")

    def test_a_claim_cannot_be_overwritten(self):
        """rename would drop the existing claim silently, and two claims on
        one cid means two cells."""
        self.q("aaa", [self.spec()])
        p = runs.queue.lane_specs("aaa")[0]
        runs.queue.claim("aaa", p)
        runs.queue.enqueue("aaa", self.spec())
        # the dedupe normally prevents this; force the collision
        d = runs.queue.lane_dir("aaa"); d.mkdir(parents=True, exist_ok=True)
        dup = d / "099999.aaa_high_beta_apidocs_T1_r1.json"
        dup.write_text(json.dumps(self.spec()) + "\n")
        with self.assertRaises(FileExistsError):
            runs.queue.claim("aaa", dup)

    def test_an_unclaimed_crashed_cell_is_left_to_the_operator(self):
        """Supervision must not invent claims: doing so restarted every
        crashed workspace on disk, ignoring caps and lane cooldowns."""
        cid = "aaa_high_beta_apidocs_T1_r7"
        (self.ws / cid).mkdir(parents=True)
        st = {"cid": cid, "state": "CRASHED", "why": "loop", "model": "aaa",
              "treatment": "beta", "condition": "apidocs", "task": "T1",
              "rep": "7", "budget": 10}
        with mock.patch.object(runs.common, "RECONCILE_LOG", self.orch / "rec.log"):
            runs.supervise._reclaim(st, dry=False)
        self.assertEqual(runs.queue.running_specs("aaa"), [])
        self.assertEqual(self.pending("aaa"), [])

    def _reclaim_log(self, cid):
        st = {"cid": cid, "state": "CRASHED", "why": "loop", "model": "aaa"}
        log = self.orch / "rec.log"
        with mock.patch.object(runs.common, "RECONCILE_LOG", log):
            runs.supervise._reclaim(st, dry=False)
        return log.read_text() if log.exists() else ""

    def test_a_crashed_cell_whose_spec_waits_in_its_lane_is_not_told_to_resume(self):
        """Its spec is queued: admission restarts it; `cell resume` would
        only jump it to the lane front."""
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        self.q("aaa", [self.spec()])
        self.assertNotIn("unclaimed", self._reclaim_log(cid))

    def test_triage_says_a_queued_crashed_cell_waits_for_admission(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        ws = self.ws / cid
        ws.mkdir(parents=True)
        with mock.patch.object(runs.state, "live_loops", return_value=set()):
            self.assertIn("cell resume", runs.state.cell_liveness(ws, set())[2])
            self.q("aaa", [self.spec()])
            self.assertEqual(runs.state.cell_liveness(ws, set()),
                             ("CRASHED", "loop", "no loop — queued, admission resumes it"))

    def test_a_crashed_cell_with_no_spec_anywhere_is_told_to_resume(self):
        cid = "aaa_high_beta_apidocs_T1_r7"
        (self.ws / cid).mkdir(parents=True)
        self.assertIn(f"{cid} unclaimed", self._reclaim_log(cid))

    def test_a_cooling_lane_does_not_restart_its_claim(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        self.q("aaa", [self.spec()])
        self.run_conduct()
        self.live.clear()
        runs.weekly._cooldown_file("aaa").write_text(f"{int(runs.time.time()) + 9999} x\n")
        self.max_rounds, self.rounds = 2, 0
        self.run_conduct()
        self.assertEqual(len(self.spawned), 1, "restarted a walled lane's cell")

    def test_a_live_cell_without_a_claim_is_adopted(self):
        """A cell outliving the conduct that started it would otherwise leave
        its lane reading as free, and the lane would run a second cell."""
        cid = "aaa_high_beta_apidocs_T1_r9"
        (self.ws / cid).mkdir(parents=True)
        self.live[cid] = 4242
        self.per_model = 1
        self.q("aaa", [self.spec()])
        with mock.patch.object(runs.state, "cell_state",
                               return_value={"cid": cid, "state": "RUNNING",
                                             "why": "agent", "model": "aaa",
                                             "treatment": "beta",
                                             "condition": "apidocs", "task": "T1",
                                             "rep": "9", "budget": 10}):
            out = self.run_conduct()
        self.assertIn("adopted 1 live cell", out)
        self.assertEqual(self.claimed("aaa"), [cid])
        self.assertEqual(self.spawned, [], "the lane already had its cell")

    def test_a_finished_cell_releases_its_claim(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        self.q("aaa", [self.spec()])
        self.run_conduct()
        self.live.clear()
        self.max_rounds, self.rounds = 2, 0
        with mock.patch.object(runs.state, "cell_state",
                               return_value={"cid": cid, "state": "DONE",
                                             "why": "green"}):
            self.run_conduct()
        self.assertEqual(self.claimed("aaa"), [])
        self.assertTrue((self.orch / "done" / "aaa" / f"{cid}.json").exists())

    def test_repairs_are_bounded_then_flagged(self):
        (self.ws / "aaa_high_beta_apidocs_T1_r1").mkdir(parents=True)
        self.q("aaa", [self.spec()])
        self.run_conduct()
        for _ in range(runs.ops.MAX_RESPAWNS + 1):
            self.live.clear()
            self.max_rounds, self.rounds = 2, 0
            out = self.run_conduct()
        cid = "aaa_high_beta_apidocs_T1_r1"
        self.assertIn("FLAGGED", out)
        self.assertTrue((self.ws / cid / "reconcile.flagged").exists())
        self.assertEqual(self.claimed("aaa"), [], "a flagged claim is handed back")
        self.assertEqual(len(self.pending("aaa")), 1)


class TestGates(ConductCase):
    def test_alpha_is_admitted_while_another_holds_the_arm(self):
        """Arm serialization happens in-cell: the admitted cell parks on the
        arm lock and takes it the moment it frees. Holding the spec back
        instead left the arm idle between cells and could idle a whole lane
        whose backlog was all alpha."""
        self.live["x_high_alpha_apidocs_T1_r9"] = 1
        self.q("aaa", [self.spec(treatment="alpha")])
        self.run_conduct()
        self.assertEqual(len(self.spawned), 1, self.spawned)
        self.assertEqual(self.pending("aaa"), [])

    def test_flagged_cell_is_not_admitted(self):
        """The flag quarantines from ADMISSION too: repair stops requeueing a
        flagged cell, but a spec already in the lane would respawn it right
        past the flag."""
        cid = runs.cell_id("aaa", "beta", "apidocs", 1, "T1")
        (self.ws / cid).mkdir(parents=True)
        (self.ws / cid / "reconcile.flagged").touch()
        self.q("aaa", [self.spec()])
        self.run_conduct()
        self.assertEqual(self.spawned, [])
        rows = self.pending("aaa")
        self.assertEqual(len(rows), 1, "the spec must stay queued for the "
                                       "operator's resume")

    def test_done_cell_spec_is_skipped(self):
        cid = runs.cell_id("aaa", "beta", "apidocs", 1, "T1")
        (self.ws / cid).mkdir(parents=True)
        with mock.patch.object(runs.state, "cell_state",
                               return_value={"cid": cid, "state": "DONE", "why": "green"}):
            self.q("aaa", [self.spec()])
            self.run_conduct()
        self.assertEqual(self.spawned, [])

    def test_paused_specs_are_left_in_place(self):
        """A paused cell's spec is skipped where it lies — nothing is
        rotated, so lane order still means what it says."""
        with mock.patch.object(runs.state, "pause_lock",
                               side_effect=lambda cid: "manual"):
            self.q("aaa", [self.spec(rep=1), self.spec(rep=2)])
            self.run_conduct()
        rows = self.pending("aaa")
        self.assertEqual([r["rep"] for r in rows], [1, 2])
        self.assertEqual(self.spawned, [])


class TestASubstrateHaltSpendsARepair(ConductCase):
    """A driver that halts on its substrate at admission (exit 45) is not
    "owned meanwhile": it is a repair, counted toward the cap, so a dead
    daemon cannot be respawned into forever."""

    def test_exit_45_is_counted_and_does_not_freeze_the_lane(self):
        self.spawn_rc = 45
        self.q("aaa", [self.spec(rep=1)])
        out = self.run_conduct()
        self.assertIn("substrate HALT at admission", out)
        self.assertNotIn("FROZEN", out)
        cid = self.spawned[0]
        self.assertGreaterEqual(runs.ops._respawn_count(cid), 1)


class TestAGenericCrashSpendsARepair(ConductCase):
    """An uncaught driver exception (exit 47) used to collide with LOCK_EXIT
    (43) and skip the repair budget entirely, so a deterministic host fault
    respawned forever, uncounted. It must be treated like a substrate HALT:
    counted, not frozen."""

    def test_exit_47_is_counted_and_does_not_freeze_the_lane(self):
        self.spawn_rc = runs.common.GENERIC_CRASH_EXIT_RC
        self.q("aaa", [self.spec(rep=1)])
        out = self.run_conduct()
        self.assertIn("crash at admission", out)
        self.assertNotIn("FROZEN", out)
        cid = self.spawned[0]
        self.assertGreaterEqual(runs.ops._respawn_count(cid), 1)

class TestSystemicFreeze(ConductCase):
    def test_immediate_systemic_death_freezes_the_lane(self):
        self.spawn_rc = 42
        self.q("aaa", [self.spec(rep=1), self.spec(rep=2)])
        out = self.run_conduct()
        self.assertIn("FROZEN", out)
        self.assertEqual(len(self.spawned), 1, "a frozen lane must not be re-tried")
        rows = self.pending("aaa")
        self.assertEqual([r["rep"] for r in rows], [1, 2],
                         "the failed spec goes back to the FRONT")

    def test_loop_lock_collision_skips_without_freezing(self):
        self.spawn_rc = 43
        self.q("aaa", [self.spec(rep=1)])
        out = self.run_conduct()
        self.assertNotIn("FROZEN", out)
        self.assertEqual(self.pending("aaa"), [],
                         "a 43 spec stays claimed, not requeued")

    def test_pause_exit_in_the_spawn_window_keeps_the_claim(self):
        """A pause landing between claim and spawn exits with PAUSE_EXIT
        (44): cell-specific like 43, so the claim STANDS and the next
        converge decides — asserting only 'not frozen' passed even when 44
        fell through to the unknown-rc branch."""
        self.spawn_rc = runs.PAUSE_EXIT_RC
        self.q("aaa", [self.spec(rep=1)])
        out = self.run_conduct()
        self.assertNotIn("FROZEN", out)
        self.assertEqual(self.pending("aaa"), [], "the claim must stand")
        self.assertEqual(len(self.claimed("aaa")), 1)

    def test_unknown_death_requeues_front_without_freezing(self):
        self.spawn_rc = 97
        self.q("aaa", [self.spec(rep=1)])
        out = self.run_conduct()
        self.assertNotIn("FROZEN", out)
        self.assertIn("lane NOT frozen", out)
        rows = self.pending("aaa")
        self.assertEqual([r["rep"] for r in rows], [1],
                         "the spec must stay at the head for a later round")


class TestSupervision(ConductCase):
    """conduct is the ONE controller: the supervision sweep runs inside its
    loop. Repair requeues; admission spawns — never the sweep itself."""

    def _run(self, supervise, rounds=3):
        self.max_rounds = rounds
        with mock.patch.object(runs.supervise, "_supervise_pass") as sp, \
             mock.patch.object(runs.zombies, "find_zombies", return_value=[]) as fz, \
             mock.patch.object(runs.zombies, "reap_zombies", return_value=[]):
            self.q("aaa", [self.spec()])
            self.run_conduct(supervise=supervise)
        return sp, fz

    def test_sweep_runs_on_the_first_iteration(self):
        """A conduct starting after an outage must validate/repair BEFORE
        admitting, so the sweep fires immediately, not after the interval."""
        sp, _ = self._run(supervise=3600)
        self.assertEqual(sp.call_count, 1)
        sp.assert_called_with(dry=False)

    def test_zero_disables_supervision(self):
        sp, fz = self._run(supervise=0)
        sp.assert_not_called()
        fz.assert_not_called()

    def test_zombies_reaped_only_on_second_sighting(self):
        z = ("container", "fae-agent-x", "x", "no live loop")
        self.max_rounds = 3
        reaped = []
        with mock.patch.object(runs.supervise, "_supervise_pass"), \
             mock.patch.object(runs.zombies, "find_zombies", return_value=[z]), \
             mock.patch.object(runs.zombies, "reap_zombies",
                               side_effect=lambda zs: reaped.extend(zs) or []):
            self.q("aaa", [self.spec()])
            # interval elapses every round (time not mocked; sweep gate uses
            # supervise=0.000001s so every iteration sweeps)
            self.run_conduct(supervise=0.000001)
        self.assertTrue(reaped, "a persistent zombie was never reaped")
        self.assertEqual(reaped[0], z)


class TestLimitCooldown(ConductCase):
    def test_cooling_lane_is_not_admitted_from(self):
        self.q("aaa", [self.spec()])
        runs.weekly._cooldown_file("aaa").write_text(f"{int(runs.time.time()) + 9999} x\n")
        self.run_conduct()
        self.assertEqual(self.spawned, [], "admitted from a cooling lane")

    def test_expired_cooldown_lifts_locks_and_admits(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        (self.ws / cid / ".paused").write_text("limit-wall by=conduct\n")
        self.q("aaa", [self.spec()])
        runs.weekly._cooldown_file("aaa").write_text(f"{int(runs.time.time()) - 5} x\n")
        with mock.patch.object(runs.common, "TRANSITIONS_LOG",
                               self.orch / "transitions.log"):
            out = self.run_conduct()
        self.assertIn("cooldown expired", out)
        self.assertFalse(runs.weekly._cooldown_file("aaa").exists())
        self.assertIsNone(runs.state.pause_lock(cid))
        self.assertEqual(self.spawned, [cid])


class TestNarration(ConductCase):
    def test_liveness_tick_appears_on_a_quiet_fleet(self):
        self.live["aaa_high_beta_apidocs_T1_r9"] = 1
        self.max_rounds = 12
        out = self.run_conduct()
        self.assertIn("alive —", out)

    def test_green_line_carries_attempt_and_gate_detail(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        self.live[cid] = 1
        seq = [([dict(cid=cid, state="RUNNING", why="agent")], 0, 0),
               ([dict(cid=cid, state="DONE", why="green")], 0, 0)]
        with mock.patch.object(runs.state, "all_states",
                               side_effect=lambda *a, **k:
                               seq.pop(0) if seq else ([], 0, 0)), \
             mock.patch.object(runs.ledger, "parse",
                               return_value=dict(green_at=4, att=4, gate=6)):
            out = self.run_conduct()
        self.assertIn("GREEN", out)
        self.assertIn("(attempt 4/4, gate 6/6)", out)


class TestReapingBelongsToConduct(unittest.TestCase):
    """Supervision is conduct's, and the operator's on request. No other code
    path may end a process or delete substrate the fleet is using."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        self.orch = self.ws / ".orch"
        self.orch.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(runs.common, "WS", self.ws),
                  mock.patch.object(runs.common, "ORCH", self.orch)):
            p.start(); self.addCleanup(p.stop)

    Z = ("container", "fae-agent-x", "x", "no live loop")

    def _watch_one_pass(self, zs):
        """One refresh of the live console, then out."""
        out = io.StringIO()
        with mock.patch.object(runs.zombies, "find_zombies", return_value=zs), \
             mock.patch.object(runs.zombies, "reap_zombies") as reap, \
             mock.patch.object(runs.render, "render", return_value="TABLE"), \
             mock.patch.object(runs.os, "system"), \
             mock.patch.object(runs.time, "sleep", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(out):
            runs.render.watch(SimpleNamespace(flat=False, running_only=False, interval=1))
        return reap, out.getvalue()

    def test_the_console_never_reaps(self):
        reap, _ = self._watch_one_pass([self.Z])
        reap.assert_not_called()

    def test_the_console_still_shows_what_it_found(self):
        # Removing the action must not remove the visibility.
        _, printed = self._watch_one_pass([self.Z])
        self.assertIn("fae-agent-x", printed)

    def test_a_clean_fleet_prints_no_zombie_section(self):
        _, printed = self._watch_one_pass([])
        self.assertNotIn("ZOMBIES", printed)

    def test_a_spawn_does_not_sweep_the_fleet(self):
        with mock.patch.object(runs.state, "containers", return_value=set()), \
             mock.patch.object(runs.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.subprocess, "Popen") as popen:
            runs.ops.prestart_clean("some_cell")
        popen.assert_not_called()

    def test_a_spawn_still_clears_its_own_leftovers(self):
        cid = "some_cell"
        (self.ws / cid).mkdir(parents=True)
        (self.ws / cid / ".loop").write_text("pid=999999 phase=agent\n")
        removed = []
        with mock.patch.object(runs.state, "containers",
                               return_value={f"fae-agent-{cid}"}), \
             mock.patch.object(runs.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.state, "heartbeat", return_value=None), \
             mock.patch.object(runs.subprocess, "run",
                               side_effect=lambda a, **k: removed.append(a)):
            runs.ops.prestart_clean(cid)
        self.assertIn(["docker", "rm", "-f", f"fae-agent-{cid}"], removed)
        self.assertFalse((self.ws / cid / ".loop").exists())


if __name__ == "__main__":
    unittest.main()


class TestConvergeBranches(ConductCase):
    """The paths a claim can take when its loop is gone, other than a plain
    restart."""

    def _converge(self, frozen=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runs.conduct._converge_running(frozen if frozen is not None else set())
        return out.getvalue()

    def test_a_paused_claim_is_handed_back_to_the_lane(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        self.q("aaa", [self.spec()])
        runs.queue.claim("aaa", runs.queue.lane_specs("aaa")[0])
        with mock.patch.object(runs.state, "pause_lock", side_effect=lambda c: "manual"):
            self._converge()
        self.assertEqual(self.claimed("aaa"), [], "paused claim was kept")
        self.assertEqual(len(self.pending("aaa")), 1)
        self.assertEqual(self.spawned, [])

    def test_an_unreadable_claim_is_shelved(self):
        d = runs.queue.rundir("aaa"); d.mkdir(parents=True)
        (d / "aaa_high_beta_apidocs_T1_r1.json").write_text("{not json\n")
        self._converge()
        self.assertEqual(runs.queue.running_specs("aaa"), [])
        self.assertEqual(len(list((self.orch / "backups").glob("unreadable-*.json"))), 1)

    def test_a_systemic_repair_death_freezes_the_lane(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        self.q("aaa", [self.spec()])
        runs.queue.claim("aaa", runs.queue.lane_specs("aaa")[0])
        self.spawn_rc = 42
        frozen = set()
        out = self._converge(frozen)
        self.assertIn("FROZEN", out)
        self.assertEqual(frozen, {"aaa"})
        self.assertEqual(len(self.claimed("aaa")), 1, "the claim stands")

    def test_a_repair_refused_as_already_owned_keeps_the_claim(self):
        cid = "aaa_high_beta_apidocs_T1_r1"
        (self.ws / cid).mkdir(parents=True)
        self.q("aaa", [self.spec()])
        runs.queue.claim("aaa", runs.queue.lane_specs("aaa")[0])
        self.spawn_rc = 43
        self._converge()
        self.assertEqual(len(self.claimed("aaa")), 1)


class TestDiagnose(ConductCase):
    """conduct diagnose is the read-only view of the same judgment: it must
    print the picture and change nothing."""

    def _diagnose(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(runs.zombies, "find_zombies",
                               return_value=[("container", "fae-agent-x",
                                              "x", "no live loop")]), \
             mock.patch.object(runs.zombies, "janitor_lines", return_value=[]), \
             mock.patch.object(runs.supervise, "_supervise_pass") as sp:
            runs.supervise.conduct_diagnose(SimpleNamespace())
        return out.getvalue(), sp

    def test_reports_supervision_zombies_and_admission(self):
        self.q("aaa", [self.spec()])
        out, sp = self._diagnose()
        sp.assert_called_once_with(dry=True)
        self.assertIn("SUPERVISION (dry run)", out)
        self.assertIn("fae-agent-x", out)
        self.assertIn("would admit", out)
        self.assertIn("conduct DOWN", out)

    def test_changes_nothing(self):
        self.q("aaa", [self.spec(rep=r) for r in (1, 2)])
        before = {p.name for p in runs.queue.lane_specs("aaa")}
        self._diagnose()
        self.assertEqual({p.name for p in runs.queue.lane_specs("aaa")}, before)
        self.assertEqual(runs.queue.running_specs(), [])
        self.assertEqual(self.spawned, [])

    def test_shows_a_parked_lane_and_a_cooling_lane(self):
        self.q("aaa", [self.spec()])
        self.q("bbb", [self.spec()])
        runs.queue.park_lane("aaa")
        runs.weekly._cooldown_file("bbb").write_text(f"{int(runs.time.time()) + 9999} x\n")
        out, _ = self._diagnose()
        self.assertIn("parked", out)
        self.assertIn("limit-cooling", out)

    def test_shows_a_lane_held_at_its_cap(self):
        self.q("aaa", [self.spec(rep=r) for r in (1, 2)])
        runs.queue.claim("aaa", runs.queue.lane_specs("aaa")[0])
        out, _ = self._diagnose()
        self.assertIn("HELD at 1/lane", out)


class TestSpawnAndAdoptEdges(ConductCase):
    def test_a_fresh_spec_sets_FRESH_in_the_environment(self):
        seen = {}
        with mock.patch.object(runs.ops, "_spawn_detached",
                               side_effect=lambda argv, env, cid, what:
                               seen.update(env) or None):
            runs.ops._spawn_spec("aaa", dict(self.spec(), fresh=True),
                             "aaa_high_beta_apidocs_T1_r1", "test")
        self.assertEqual(seen.get("FRESH"), "1")

    def test_adoption_skips_a_cell_with_no_state(self):
        """A live loop whose workspace says nothing must not get a fabricated
        claim — the claim would describe a cell that does not exist."""
        self.live["aaa_high_beta_apidocs_T1_r5"] = 4242
        with mock.patch.object(runs.state, "cell_state", return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                runs.conduct._adopt_live_cells()
        self.assertEqual(runs.queue.running_specs(), [])
        self.assertNotIn("adopted", out.getvalue())


class TestArmSlotWinnersDoNotReap(unittest.TestCase):
    """An orphaned cluster/sidecar/daemon must stay visible in zombies/TRIAGE
    until conduct reaps it; a slot winner silently deleting it converts a
    dead cell's missing teardown into an invisible event."""

    def test_the_slot_winner_invokes_no_reconcile(self):
        src = (runs.ROOT / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def acquire_slots(self"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertNotIn("_arm_reconcile", body)
        for line in body.splitlines():
            code = line.split("#", 1)[0]
            self.assertNotIn("runs.py", code,
                             f"the slot winner executes runs.py: {line.strip()}")
            self.assertNotIn("cli.py", code,
                             f"the slot winner executes cli.py: {line.strip()}")
            self.assertNotIn("reap", code)

    def test_the_mechanism_is_gone_from_runs(self):
        for f in [runs.ROOT / "fae" / "cli.py"] + sorted((runs.ROOT / "driver").glob("*.py")):
            self.assertNotIn("def _arm_reconcile", f.read_text(), f.name)


class TestConductLiftsItsOwnStandDowns(ConductCase):
    """A pause conduct wrote (arm-stuck, verify-wedged, silent-hang,
    phase-stalled-*) is lifted by conduct after STANDDOWN_COOL_S, one repair
    per lift, FLAGGED when the budget is spent. Operator pauses are never
    touched; limit-wall stays with its lane cooldown."""

    CID = "aaa_high_beta_apidocs_T1_r1"

    def stood_down(self, reason="arm-stuck", who="conduct", age_s=3600):
        (self.ws / self.CID).mkdir(parents=True)
        at = runs.datetime.fromtimestamp(runs.time.time() - age_s,
                                         runs.timezone.utc)
        (self.ws / self.CID / ".paused").write_text(
            f"{reason} by={who} at={at:%Y-%m-%dT%H:%M:%SZ}\n")
        self.q("aaa", [self.spec()])

    def run_lift(self):
        with mock.patch.object(runs.common, "TRANSITIONS_LOG",
                               self.orch / "transitions.log"):
            return self.run_conduct()

    def test_a_cooled_stand_down_is_lifted_and_costs_a_repair(self):
        self.stood_down()
        runs.common._ARM_ALERTED.add(self.CID)
        out = self.run_lift()
        self.assertIn("lifted", out)
        self.assertFalse((self.ws / self.CID / ".paused").exists())
        self.assertEqual(self.spawned, [self.CID])
        self.assertEqual(runs.ops._respawn_count(self.CID), 1)
        self.assertNotIn(self.CID, runs.common._ARM_ALERTED)

    def test_a_fresh_stand_down_waits_out_the_cool_off(self):
        self.stood_down(age_s=10)
        out = self.run_lift()
        self.assertNotIn("lifted", out)
        self.assertTrue((self.ws / self.CID / ".paused").exists())

    def test_every_conduct_reason_is_lifted(self):
        for reason in ("verify-wedged", "silent-hang", "phase-stalled-setup"):
            with self.subTest(reason=reason):
                (self.ws / self.CID / ".paused").parent.mkdir(
                    parents=True, exist_ok=True)
                (self.ws / self.CID / ".paused").write_text(
                    f"{reason} by=conduct at=2026-01-01T00:00:00Z\n")
                self.q("aaa", [self.spec()]) if not self.pending("aaa") else None
                self.spawned.clear()
                self.assertIn("lifted", self.run_lift())
                self.assertFalse((self.ws / self.CID / ".paused").exists())

    def test_an_operator_pause_is_never_lifted(self):
        self.stood_down(reason="manual", who="operator")
        out = self.run_lift()
        self.assertNotIn("lifted", out)
        self.assertTrue((self.ws / self.CID / ".paused").exists())

    def test_a_limit_wall_is_the_cooldowns_not_this_lifts(self):
        self.stood_down(reason="limit-wall")
        runs.weekly._cooldown_file("aaa").write_text(
            f"{int(runs.time.time()) + 9999} x\n")
        out = self.run_lift()
        self.assertNotIn("lifted", out)
        self.assertTrue((self.ws / self.CID / ".paused").exists())

    def test_a_spent_budget_flags_instead_of_lifting(self):
        self.stood_down()
        for _ in range(runs.ops.MAX_RESPAWNS):
            runs.ops._respawn_count(self.CID, bump=True)
        out = self.run_lift()
        self.assertIn("FLAGGED", out)
        self.assertTrue((self.ws / self.CID / ".paused").exists())
        self.assertTrue((self.ws / self.CID / "reconcile.flagged").exists())
        self.assertEqual(self.spawned, [])

    def test_a_cancelled_cell_is_left_alone(self):
        self.stood_down()
        (self.ws / self.CID / ".cancelled").write_text("x\n")
        out = self.run_lift()
        self.assertNotIn("lifted", out)
        self.assertTrue((self.ws / self.CID / ".paused").exists())

    def test_pause_meta_reads_reason_author_and_time(self):
        (self.ws / self.CID).mkdir(parents=True)
        (self.ws / self.CID / ".paused").write_text(
            "arm-stuck by=conduct at=2026-08-25T20:02:17Z\n")
        reason, who, at = runs.state.pause_meta(self.CID)
        self.assertEqual((reason, who), ("arm-stuck", "conduct"))
        self.assertEqual(int(at), 1787688137)
        (self.ws / self.CID / ".paused").write_text("manual\n")
        self.assertEqual(runs.state.pause_meta(self.CID), ("manual", None, None))
        self.assertIsNone(runs.state.pause_meta("nope"))

    def test_a_walled_cell_is_exempt_from_arm_stuck(self):
        """The wall branch owns a cell in the limit phase; ARM-STUCK must
        not stand it down first (it would lose the lane cooldown)."""
        src = (Path(ROOT) / "fae" / "driver" / "supervise.py").read_text()
        head = src[:src.index("ARM-STUCK held")]
        self.assertIn('if _phase == "agent" or _phase in state.WAIT_PHASES:', head[-2500:])
        self.assertEqual(runs.state.WAIT_PHASES,
                         {"arm-lock", "verify-lock", "limit", "slot-wait"})


class TestWeeklyBudgetLanes(ConductCase):
    """The claude lanes share one weekly cap. Conduct reads the CLI's own
    rate_limit_event, holds the budget lanes past the threshold, and
    releases them when the remainder is about to expire."""

    WARN = ('{"type":"rate_limit_event","rate_limit_info":{"status":'
            '"allowed_warning","resetsAt":%d,"rateLimitType":"seven_day",'
            '"utilization":%s,"isUsingOverage":false,"surpassedThreshold":0.75}}')
    REJECT = ('{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
              '"resetsAt":%d,"rateLimitType":"seven_day","overageStatus":'
              '"rejected","isUsingOverage":false}}')
    FIVE_H = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",'
              '"resetsAt":%d,"rateLimitType":"five_hour","isUsingOverage":false}}')

    def setUp(self):
        super().setUp()
        self.now = runs.time.time()
        self.reset = int(self.now + 3 * 86400)
        self.patches.append(mock.patch.object(runs.weekly, "BUDGET_LANES", ["fable", "opus"]))
        self.patches[-1].start()
        self.addCleanup(self.patches[-1].stop)

    def log(self, cid, *lines, mtime=None):
        ws = self.ws / cid
        ws.mkdir(parents=True, exist_ok=True)
        p = ws / "agent.attempt-1.log"
        p.write_text("\n".join(lines) + "\n")
        if mtime is not None:
            runs.os.utime(p, (mtime, mtime))
        return p

    def lanes(self, *models):
        for m in models:
            self.q(m, [self.spec()])

    def test_the_newest_seven_day_event_is_the_reading(self):
        self.log("sonnet_high_beta_apidocs_T1_r1",
                 self.FIVE_H % self.reset, self.WARN % (self.reset, "0.77"),
                 mtime=self.now - 100)
        self.log("opus_high_beta_apidocs_T1_r1",
                 self.WARN % (self.reset, "0.83"), mtime=self.now - 50)
        st = runs.weekly.weekly_cap_observe(now=self.now)
        self.assertEqual(st["utilization"], 0.83)
        self.assertEqual(st["resets_at"], self.reset)
        self.assertEqual(st["source_cid"], "opus_high_beta_apidocs_T1_r1")

    def test_rejected_is_the_cap_and_five_hour_is_ignored(self):
        self.log("sonnet_high_beta_apidocs_T1_r1", self.REJECT % self.reset)
        self.assertEqual(runs.weekly.weekly_cap_observe(now=self.now)["utilization"], 1.0)
        self.log("sonnet_high_beta_apidocs_T1_r2", self.FIVE_H % self.reset,
                 mtime=self.now + 10)
        st = runs.weekly.weekly_cap_observe(now=self.now + 20)
        self.assertEqual(st["utilization"], 1.0, "a five_hour event is not a reading")

    def test_a_reading_from_before_the_reset_says_nothing(self):
        st = dict(utilization=0.96, resets_at=self.now - 10, seen_at=0, hold=[])
        self.assertEqual(runs.weekly.weekly_reading(st, self.now), (None, self.now - 10))

    def test_past_the_threshold_the_budget_lanes_are_held_and_alerted(self):
        self.lanes("fable", "opus", "sonnet")
        st = dict(utilization=0.78, resets_at=self.reset, seen_at=0, hold=[])
        out = []
        runs.weekly.weekly_budget_apply(st, self.now, out=out.append)
        self.assertTrue(runs.queue.lane_dir("fable", parked=True).is_dir())
        self.assertTrue(runs.queue.lane_dir("opus", parked=True).is_dir())
        self.assertFalse(runs.queue.lane_dir("sonnet", parked=True).is_dir())
        self.assertEqual(st["hold"], ["fable", "opus"])
        self.assertEqual(len(out), 1)
        self.assertIn("ALERT weekly cap 78%", out[0])
        self.assertIn("parked fable, opus", out[0])
        runs.weekly.weekly_budget_apply(st, self.now + 60, out=out.append)
        self.assertEqual(len(out), 1, "the hold is announced once")

    def test_below_the_threshold_nothing_moves(self):
        self.lanes("fable")
        for util in (None, 0.5, 0.74):
            st = dict(utilization=util, resets_at=self.reset, seen_at=0, hold=[])
            out = []
            runs.weekly.weekly_budget_apply(st, self.now, out=out.append)
            self.assertFalse(runs.queue.lane_dir("fable", parked=True).is_dir(), util)
            self.assertEqual(out, [])

    def test_an_operator_park_is_not_conducts_to_hold_or_release(self):
        self.lanes("fable", "opus")
        runs.queue.park_lane("fable")
        st = dict(utilization=0.9, resets_at=self.reset, seen_at=0, hold=[])
        runs.weekly.weekly_budget_apply(st, self.now, out=lambda s: None)
        self.assertEqual(st["hold"], ["opus"])
        runs.weekly.weekly_budget_apply(st, self.reset - 3600, out=lambda s: None)
        self.assertTrue(runs.queue.lane_dir("fable", parked=True).is_dir(),
                        "the operator's park survived the release")
        self.assertFalse(runs.queue.lane_dir("opus", parked=True).is_dir())

    def test_released_within_a_day_of_the_reset_and_not_reheld(self):
        self.lanes("fable")
        runs.queue.park_lane("fable")
        st = dict(utilization=0.9, resets_at=self.reset, seen_at=0, hold=["fable"])
        out = []
        runs.weekly.weekly_budget_apply(st, self.reset - 20 * 3600, out=out.append)
        self.assertFalse(runs.queue.lane_dir("fable", parked=True).is_dir())
        self.assertEqual(st["hold"], [])
        self.assertIn("released fable", out[0])
        runs.weekly.weekly_budget_apply(st, self.reset - 19 * 3600, out=out.append)
        self.assertFalse(runs.queue.lane_dir("fable", parked=True).is_dir(),
                         "no re-hold inside the release window")

    def test_released_after_the_reset_passed(self):
        self.lanes("opus")
        runs.queue.park_lane("opus")
        st = dict(utilization=1.0, resets_at=self.now - 5, seen_at=0, hold=["opus"])
        runs.weekly.weekly_budget_apply(st, self.now, out=lambda s: None)
        self.assertFalse(runs.queue.lane_dir("opus", parked=True).is_dir())

    def test_an_empty_budget_lane_is_held_by_creating_its_parked_dir(self):
        st = dict(utilization=0.8, resets_at=self.reset, seen_at=0, hold=[])
        runs.weekly.weekly_budget_apply(st, self.now, out=lambda s: None)
        self.assertTrue(runs.queue.lane_dir("fable", parked=True).is_dir())
        runs.queue.enqueue("fable", self.spec())
        self.assertEqual(runs.queue.lane_specs("fable"), [], "enqueue lands in the parked lane")

    def test_the_hold_survives_a_reload_and_resume_clears_it(self):
        st = dict(utilization=0.8, resets_at=self.reset, seen_at=0, hold=[])
        runs.weekly.weekly_budget_apply(st, self.now, out=lambda s: None)
        self.assertEqual(runs.weekly.weekly_load()["hold"], ["fable", "opus"])
        out = []
        runs.weekly.weekly_hold_clear("fable", out=out.append)
        self.assertEqual(runs.weekly.weekly_load()["hold"], ["opus"])
        self.assertIn("cleared", out[0])

    def test_the_status_line(self):
        self.assertEqual(runs.weekly.weekly_line(self.now), "claude weekly: unknown")
        runs.weekly.weekly_save(dict(utilization=0.83, resets_at=self.reset, seen_at=1,
                              event_at=1, hold=["fable"]))
        line = runs.weekly.weekly_line(self.now)
        self.assertIn("claude weekly: 83%", line)
        self.assertIn("resets", line)
        self.assertIn("hold: fable", line)
        runs.weekly.weekly_save(dict(utilization=None, resets_at=None, seen_at=1,
                              event_at=1, hold=[]))
        self.assertEqual(runs.weekly.weekly_line(self.now), "claude weekly: <75%")


class TestMemoryPressureMonitor(unittest.TestCase):
    """conduct reads host memory pressure straight from the kernel every
    supervision pass and status surfaces it — a heavy cell fleet on a
    thrashing box invites an OOM kill, so it must be visible and logged."""

    def test_mem_pressure_reports_a_band_and_swap(self):
        mp = runs.common.mem_pressure()
        self.assertIn("label", mp)
        self.assertIn(mp["level"], (0, 1, 2, 4))
        self.assertGreaterEqual(mp["swap_total_mb"], mp["swap_used_mb"])

    def test_conduct_logs_on_the_rising_edge_into_pressure(self):
        # WARNING/CRITICAL is logged once when it is first seen, not every pass.
        runs.supervise._MEM_ALERTED["level"] = 1
        crit = {"label": "CRITICAL", "level": 4, "avail_pct": 8,
                "used_gb": 14.7, "total_gb": 16.0,
                "swap_used_mb": 40000.0, "swap_total_mb": 41000.0}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(runs.common, "WS", Path(d)), \
             mock.patch.object(runs.common, "mem_pressure", return_value=crit), \
             mock.patch.object(runs.common, "host_sleep_observe", lambda *a, **k: None), \
             mock.patch.object(runs.state, "loop_pids", return_value={}), \
             mock.patch.object(runs.state, "containers", return_value=set()), \
             mock.patch.object(runs.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.common, "_last_transitions", return_value={}), \
             mock.patch.object(runs.supervise, "_agent_io", return_value={}), \
             mock.patch.object(runs.supervise, "_agent_io_book", return_value={}), \
             mock.patch.object(runs.common, "_rec_log") as rec:
            runs.supervise._supervise_pass(dry=True)
            runs.supervise._supervise_pass(dry=True)   # second pass must NOT re-log
        msgs = [c.args[0] for c in rec.call_args_list]
        hits = [m for m in msgs if "MEMORY PRESSURE" in m]
        self.assertEqual(len(hits), 1, msgs)
        self.assertIn("CRITICAL", hits[0])

    def test_status_footer_carries_the_mem_line(self):
        fake = {"label": "WARN", "level": 2, "avail_pct": 40,
                "used_gb": 9.6, "total_gb": 16.0,
                "swap_used_mb": 9000.0, "swap_total_mb": 10000.0}
        with mock.patch.object(runs.common, "mem_pressure", return_value=fake), \
             mock.patch.object(runs.state, "all_states", return_value=([], {}, set())), \
             mock.patch.object(runs.render, "queued_summary", return_value=[]), \
             mock.patch.object(runs.weekly, "weekly_line", return_value=""), \
             mock.patch.object(runs.state, "containers", return_value=set()), \
             mock.patch.object(runs.state, "loop_pids", return_value={}), \
             mock.patch.object(runs.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.zombies, "find_zombies", return_value=[]):
            out = runs.render.render()
        self.assertIn("pressure=WARN", out)
        self.assertIn("9.6/16.0GB used (40% avail)", out)


if __name__ == "__main__":
    unittest.main()
