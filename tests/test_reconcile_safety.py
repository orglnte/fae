"""Guards against reconcile starting work nobody ordered, and against a
preview writing state.

Money is at stake in both: an unordered respawn is a paid agent run, and a
dry-run that flags a cell silently removes it from supervision.

`runs.common.WS` / `runs.common.ORCH` are patched to a TemporaryDirectory in every test that
writes. Nothing touches the live workspace tree.
"""
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _ctx import runs, ROOT

TS = "2026-07-30T09:00:00Z"


class TestNeverStarted(unittest.TestCase):
    """A prepared-but-never-launched workspace must never be respawned —
    audit finding 13. The predicate went dead when prepare_cell.sh started
    writing a v2 PREPARED birth event, because it tested `events == 0`."""

    def st(self, **kw):
        base = dict(state="CRASHED", events=0, att=0, prepared=False)
        base.update(kw)
        return base

    def test_v1_ledger_with_no_events_is_never_started(self):
        self.assertTrue(runs.state.never_started(self.st()))

    def test_v2_prepared_only_workspace_is_never_started(self):
        """THE REGRESSION: prepare_cell.sh:107 writes PREPARED, ledger.parse
        counts it, so events == 1. `cli.py experiment prepare` seeds the whole matrix and
        launches nothing — every one of those would have been respawned as a
        paid agent run."""
        self.assertTrue(runs.state.never_started(
            self.st(events=1, prepared=True, att=0)))

    def test_a_cell_that_logged_start_is_NOT_never_started(self):
        """Keeps the 2026-07-25 fix intact: an attempt that started but never
        finished has an empty ITER history, and counting history instead of
        events stranded six paused-then-crashed cells."""
        self.assertFalse(runs.state.never_started(
            self.st(events=3, prepared=True, att=0)))

    def test_a_cell_with_a_judged_attempt_is_NOT_never_started(self):
        self.assertFalse(runs.state.never_started(
            self.st(events=5, prepared=True, att=2)))

    def test_att_is_not_consulted(self):
        """cell_state reassigns att to the in-flight attempt NUMBER for
        non-terminal cells, so it is never 0. A predicate keyed on att == 0
        would be permanently false — the way this guard died the first time."""
        self.assertTrue(runs.state.never_started(
            self.st(events=1, prepared=True, att=1)))

    def test_non_crashed_states_are_never_never_started(self):
        for state in ("RUNNING", "DONE", "PAUSED", "WAITING"):
            with self.subTest(state=state):
                self.assertFalse(runs.state.never_started(
                    self.st(state=state, events=1, prepared=True)))

    def test_prepared_flag_is_required_not_just_a_single_event(self):
        """One event that is NOT the birth event means something happened."""
        self.assertFalse(runs.state.never_started(
            self.st(events=1, prepared=False, att=0)))


class TestCellStateExposesPrepared(unittest.TestCase):
    """never_started reads st['prepared']; ledger.parse has always returned it
    and nothing consumed it, which is how the guard went dead unnoticed.

    NB cell_state is NOT a pure function of the path it is given: pause_lock
    and the container/loop lookups resolve the cid against the module-global
    WS. Without patching it, a temp workspace whose cid collides with a real
    one reports the REAL cell's pause state. Patched here for isolation.
    """

    def test_prepared_workspace_reports_prepared(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.common, "WS", Path(d)):
            ws = Path(d) / "sonnet_high_beta_apidocs_T1_r1"
            (ws / "artifacts").mkdir(parents=True)
            (ws / "iterations.log").write_text(
                f"{TS}\tPREPARED\t{ws.name}\tby=prepare_cell\n")
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            st = runs.state.cell_state(ws, {}, set())
        self.assertIsNotNone(st)
        self.assertTrue(st["prepared"])
        self.assertEqual(st["events"], 1)
        # NB st["att"] is the IN-FLIGHT attempt NUMBER for a non-terminal cell
        # (cell_state:458, max(att + 1, 1)), not a count of judged attempts —
        # it is 1 here, never 0. never_started must not test it.
        self.assertEqual(st["att"], 1)
        self.assertTrue(runs.state.never_started(st))

    def test_a_workspace_with_attempts_reports_not_only_prepared(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.common, "WS", Path(d)):
            ws = Path(d) / "sonnet_high_beta_apidocs_T1_r1"
            (ws / "artifacts").mkdir(parents=True)
            (ws / "iterations.log").write_text(
                f"{TS}\tPREPARED\t{ws.name}\tby=prepare_cell\n"
                f"{TS}\tSTART\tattempt=1\n"
                f"{TS}\tITER\tfail\tattempt=1 stage=scaling\n")
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            st = runs.state.cell_state(ws, {}, set())
        self.assertTrue(st["prepared"])
        self.assertEqual(st["att"], 1)
        self.assertFalse(runs.state.never_started(st))


class TestAgentProgress(unittest.TestCase):
    """Liveness of the AGENT, which the loop's heartbeat cannot show: a loop
    blocked on a wedged container ticks exactly like a healthy one."""

    def test_received_bytes_growing_means_alive(self):
        v, why = runs.supervise._agent_progressing("c", {"c": (2000.0, 0.0)},
                                         {"c": {"rx": 1000.0}})
        self.assertTrue(v)
        self.assertIn("rx +1000B", why)

    def test_cpu_alone_can_confirm_but_flat_cpu_cannot_refute(self):
        v, _ = runs.supervise._agent_progressing("c", {"c": (1000.0, 12.0)},
                                       {"c": {"rx": 1000.0}})
        self.assertTrue(v, "busy CPU with flat rx is still alive")
        v, _ = runs.supervise._agent_progressing("c", {"c": (1000.0, 0.0)},
                                       {"c": {"rx": 1000.0}})
        self.assertFalse(v, "idle CPU with flat rx is the flat case")

    def test_a_first_reading_is_unknown_not_dead(self):
        v, _ = runs.supervise._agent_progressing("c", {"c": (1000.0, 0.0)}, {})
        self.assertIsNone(v)

    def test_no_container_reading_is_unknown_not_dead(self):
        v, _ = runs.supervise._agent_progressing("c", {}, {"c": {"rx": 1000.0}})
        self.assertIsNone(v)

    def test_docker_size_strings_parse(self):
        self.assertEqual(runs.supervise._bytes("59.8MB"), 59800000)
        self.assertEqual(runs.supervise._bytes("266kB"), 266000)
        self.assertEqual(runs.supervise._bytes("1KiB"), 1024)
        self.assertEqual(runs.supervise._bytes("0B"), 0)
        self.assertIsNone(runs.supervise._bytes("--"))

    def test_the_book_survives_a_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.common, "ORCH", Path(d)):
            (Path(d) / "agent-io.json").write_text("{not json")
            self.assertEqual(runs.supervise._agent_io_book(), {})


class TestTheEpochSeedIsComplete(unittest.TestCase):
    """A reset records the state the replay resumes from. Anything it omits is
    a cell seeded into a state it cannot legally leave: the omitted verify lock
    made a mid-verify cell's own VerifyFail illegal, and collapsing the waiting
    phases to `idle` made a lock-queued cell's AcquireVerify illegal too."""

    def test_every_model_variable_is_written(self):
        c = dict(cid="x", outcome="green", intent="run", loop="verify",
                 attempts=3, slot=True, verify=True)
        self.assertEqual(runs.rig._epoch_fields(c),
                         "outcome=green intent=run loop=verify attempts=3 "
                         "slot=true verify=true")

    def test_a_slot_in_hand_means_the_attempt_started(self):
        # AcquireSlot is enabled only while the slot is NOT held, so a cell
        # seeded `idle` holding one can never legally reach `agent` — and
        # every verify it then runs replays as illegal.
        for phase in ("agent", "verify-lock", "limit", "arm-lock", "setup", ""):
            self.assertEqual(runs.rig._loop_of_phase(phase, True), "agent", phase)

    def test_verify_is_its_own_state(self):
        self.assertEqual(runs.rig._loop_of_phase("verify", True), "verify")

    def test_no_slot_yet_is_the_idle_window(self):
        # Between Spawn and AcquireSlot, which is the only place `idle` is
        # reachable from.
        for phase in ("setup", "arm-lock", "slot-wait", ""):
            self.assertEqual(runs.rig._loop_of_phase(phase, False), "idle", phase)

    def test_lock_holders_are_read_from_the_mutex_files(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.common, "ORCH", Path(d)):
            slots = Path(d) / "work-slots"
            (slots / "slot-1").mkdir(parents=True)
            (slots / "slot-1" / "holder").write_text("cell-a\n4242\n123\n")
            (slots / "slot-2").mkdir()
            (Path(d) / "verify-lock").mkdir()
            (Path(d) / "verify-lock" / "holder").write_text("cell-b\n99\n123\n")
            self.assertEqual(runs.rig._slot_holders(), {"cell-a"})
            self.assertEqual(runs.rig._verify_holder(), "cell-b")

    def test_no_locks_held_reads_empty(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.common, "ORCH", Path(d)):
            self.assertEqual(runs.rig._slot_holders(), set())
            self.assertEqual(runs.rig._verify_holder(), "")


class TestTheConformanceWindow(unittest.TestCase):
    """selftest judges live-trace events at or after a declared instant. The
    file narrows what counts as evidence, so a bad value must widen the check,
    never silently disable it."""

    def _since(self, body):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "conformance-since"
            f.write_text(body)
            with mock.patch.object(runs.rig, "CONFORMANCE_SINCE", f):
                return runs.rig.conformance_since()

    def test_a_declared_instant_is_passed_through(self):
        self.assertEqual(self._since("# why\n2026-08-13T17:04:00Z\n"),
                         ["--since", "2026-08-13T17:04:00Z"])

    def test_no_file_means_the_whole_trace(self):
        with mock.patch.object(runs.rig, "CONFORMANCE_SINCE",
                               Path("/nonexistent/conformance-since")):
            self.assertEqual(runs.rig.conformance_since(), [])

    def test_comments_only_means_the_whole_trace(self):
        self.assertEqual(self._since("# nothing declared yet\n"), [])

    def test_a_malformed_instant_is_refused(self):
        self.assertEqual(self._since("yesterday\n"), [])

    def test_a_future_instant_is_refused(self):
        # It would judge nothing and report green — the worst kind of pass.
        self.assertEqual(self._since("2099-01-01T00:00:00Z\n"), [])


class TestNoEditReachesTheStatus(unittest.TestCase):
    """A NOEDIT attempt is charged like any other, so if nothing renders it the
    loss is invisible — which is how 18 of them went unnoticed."""

    def _state(self, *lines):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.common, "WS", Path(d)):
            ws = Path(d) / "sonnet_high_beta_apidocs_T1_r1"
            (ws / "artifacts").mkdir(parents=True)
            (ws / "iterations.log").write_text("".join(l + "\n" for l in lines))
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            return runs.state.cell_state(ws, {}, set())

    def test_the_count_and_last_detail_are_exposed(self):
        st = self._state(
            f"{TS}\tNOEDIT\tcid\tattempt=1\tINVESTIGATE — agent said: Error: 503",
            f"{TS}\tITER\tfail\tattempt=1 stage=no-edit")
        self.assertEqual(st["noedit"], 1)
        self.assertIn("503", st["noedit_last"])

    def test_it_fills_an_otherwise_empty_detail_column(self):
        # A finished cell has nothing else to say in that column.
        st = self._state(
            f"{TS}\tNOEDIT\tcid\tattempt=1\tINVESTIGATE — agent said: Error: 503",
            f"{TS}\tITER\tfail\tattempt=1 stage=no-edit",
            f"{TS}\tEND\tcid\tgreen=false")
        self.assertIn("NOEDIT", st["detail"])

    def test_a_halt_cause_still_wins_the_detail_column(self):
        st = self._state(
            f"{TS}\tNOEDIT\tcid\tattempt=1\tINVESTIGATE — agent said: Error: 503",
            f"{TS}\tITER\tfail\tattempt=1 stage=no-edit",
            f"{TS}\tHALT\tcid\tattempt=2\tinfra: docker unreachable")
        self.assertIn("docker unreachable", st["detail"])


class TestTheModelLearnsAboutKilledLoops(unittest.TestCase):
    """A loop announces its death through its EXIT trap, and SIGKILL skips it.
    Supervision has to close that gap or the model keeps a dead loop alive,
    the cell's next legitimate Spawn replays as illegal, and every later event
    for it cascades."""

    CID = "g36f_high_beta_apidocs_T1_r4"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = Path(self._tmp.name)
        self.log = self.orch / "transitions.log"
        self.p = [mock.patch.object(runs.common, "ORCH", self.orch),
                  mock.patch.object(runs.common, "TRANSITIONS_LOG", self.log),
                  mock.patch.object(runs.common, "RECONCILE_LOG", self.orch / "r.log")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self._tmp.cleanup()

    OLD = "2026-08-13T09:00:00Z"

    def _reconcile(self, last, loop_pid=None, in_box=False, out_age=9999,
                   dry=False, ts=OLD, terminal=False):
        return runs.supervise._reconcile_dead_loop(
            self.CID, loop_pid, in_box, out_age,
            (last, ts) if last else None, dry, terminal=terminal)

    def _emitted(self):
        return [l.split("\t")[1] for l in
                self.log.read_text().splitlines()] if self.log.exists() else []

    def test_a_vanished_loop_gets_its_crash(self):
        self.assertTrue(self._reconcile("Pause"))
        self.assertEqual(self._emitted(), ["Crash"])

    def test_it_does_not_repeat_on_the_next_sweep(self):
        # The Crash it wrote is what stops it writing another — a second Crash
        # is not ENABLED and would itself be a violation.
        self.assertTrue(self._reconcile("Pause"))
        self.assertFalse(self._reconcile("Crash"))
        self.assertEqual(self._emitted(), ["Crash"])

    def test_a_live_loop_is_left_alone(self):
        self.assertFalse(self._reconcile("Spawn", loop_pid=4242))
        self.assertFalse(self._reconcile("Spawn", in_box=True))
        self.assertEqual(self._emitted(), [])

    def test_a_recently_written_agent_log_is_the_canary(self):
        # A loop re-execs (the FP re-pin) and is briefly pidless. Fresh output
        # means it is between boundaries, not dead.
        self.assertFalse(self._reconcile("Spawn", out_age=5))
        self.assertEqual(self._emitted(), [])

    def test_a_cell_the_model_already_thinks_idle_is_left_alone(self):
        for last in ("Crash", "Kill", "ReleaseSlot", "VerifyGreen",
                     "StandDown", "EPOCH"):
            self.assertFalse(self._reconcile(last), last)
        self.assertEqual(self._emitted(), [])

    def test_every_clearing_action_matches_the_spec(self):
        # Crash after a loop is already `none` is not ENABLED, so this set has
        # to name the spec's actions that assign loop = "none". VerifyFail is
        # the exception: it clears only on the budget branch, and the terminal
        # guard covers that case instead.
        spec = (Path(runs.ROOT) / ".tla" / "Runs.tla").read_text()
        clears = {m for m in re.findall(r"^(\w+)\(c\) ==", spec, re.M)
                  if re.search(rf"^{m}\(c\) ==(?:(?!^\w+\(c\) ==).)*?"
                               r"""loop' = \[loop EXCEPT !\[c\] = "none"\]""",
                               spec, re.M | re.S)}
        missing = clears - runs.common.LOOP_CLEARED_BY - {"VerifyFail"}
        self.assertFalse(missing,
                         f"spec clears the loop in {missing}, which the "
                         f"reconcile would then Crash a second time")

    def test_a_cell_with_a_verdict_is_left_alone(self):
        # VerifyFail on the last attempt ends the loop; so does VerifyGreen.
        self.assertFalse(self._reconcile("VerifyFail", terminal=True))
        self.assertEqual(self._emitted(), [])
        self.assertTrue(self._reconcile("VerifyFail", terminal=False),
                        "mid-budget the loop is still `agent` and can vanish")

    def test_intent_only_actions_do_not_hide_the_real_last_action(self):
        # Pausing and resuming a crashed cell leaves it crashed; reading Resume
        # as the last action would make the model think a loop still lives.
        self.log.write_text(
            f"{TS}\tCrash\t{self.CID}\tloop-vanished\n"
            f"{TS}\tPause\t{self.CID}\treason=limit-wall\n"
            f"{TS}\tResume\t{self.CID}\t\n")
        self.assertEqual(runs.common._last_transitions()[self.CID][0], "Crash")

    def test_a_cell_that_is_starting_right_now_is_left_alone(self):
        # No pid yet, no container, no agent log — indistinguishable from a
        # corpse except that its Spawn was seconds ago.
        now = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
        self.assertFalse(self._reconcile("Spawn", out_age=None, ts=now))
        self.assertEqual(self._emitted(), [])

    def test_an_epoch_that_seeded_a_live_loop_is_not_a_clearing_event(self):
        self.log.write_text(
            f"{TS}\tEPOCH\t{self.CID}\toutcome=none intent=run loop=idle attempts=0\n")
        self.assertEqual(runs.common._last_transitions()[self.CID][0], "EPOCH-live")
        self.log.write_text(
            f"{TS}\tEPOCH\t{self.CID}\toutcome=none intent=run loop=none attempts=0\n")
        self.assertEqual(runs.common._last_transitions()[self.CID][0], "EPOCH")

    def test_a_cell_with_no_history_is_left_alone(self):
        self.assertFalse(self._reconcile(None))
        self.assertEqual(self._emitted(), [])

    def test_dry_run_reports_without_writing(self):
        self.assertTrue(self._reconcile("Pause", dry=True))
        self.assertEqual(self._emitted(), [])

    def test_only_a_branch_that_kills_the_loop_may_emit_a_crash(self):
        # A branch that merely OBSERVES a crash runs again on every sweep, so
        # emitting there wrote one legal Crash and an illegal one every sweep
        # after it. The two survivors kill the loop themselves, which is what
        # makes their Crash both true and once-only.
        src = (Path(ROOT) / "fae" / "driver" / "supervise.py").read_text()
        body = src[src.index("def _supervise_pass"):]
        emits = re.findall(r'_emit_transition\("Crash", cid, "([^"]+)"\)', body)
        self.assertEqual(sorted(emits), ["limit-wall", "silent-hang"])


class TestDryRunWritesNothing(unittest.TestCase):
    """--dry-run is a preview. It must not leave state behind."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = self.root / "ws"
        self.orch = self.ws / ".orch"
        self.orch.mkdir(parents=True)
        self.cid = "sonnet_high_beta_apidocs_T1_r1"
        (self.ws / self.cid).mkdir()
        self.patches = [mock.patch.object(runs.common, "WS", self.ws),
                        mock.patch.object(runs.common, "ORCH", self.orch),
                        mock.patch.object(runs.ops, "RESPAWN_BOOK",
                                          self.orch / "respawns.json"),
                        mock.patch.object(runs.common, "TRANSITIONS_LOG",
                                          self.orch / "transitions.log"),
                        # unpatched, _rec_log appends TEST lines into the
                        # LIVE .orch/reconcile.log (observed 2026-08-12)
                        mock.patch.object(runs.common, "RECONCILE_LOG",
                                          self.orch / "reconcile.log")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def st(self):
        return dict(cid=self.cid, agent="sonnet", variant="beta_apidocs", task="T1", rep=1, budget="10",
                    state="CRASHED", why="loop", events=3, att=1,
                    prepared=True, detail="")

    def test_dry_run_does_not_write_reconcile_flagged(self):
        """reconcile.flagged makes every future reconcile skip the cell until a
        human resumes it. A preview that writes it disables supervision of a
        cell the operator only meant to look at."""
        runs.ops.RESPAWN_BOOK.write_text(
            '{"%s": %d}' % (self.cid, runs.ops.MAX_RESPAWNS))
        runs.ops._respawn(self.st(), dry=True)
        self.assertFalse((self.ws / self.cid / "reconcile.flagged").exists())

    def test_real_run_does_write_reconcile_flagged(self):
        runs.ops.RESPAWN_BOOK.write_text(
            '{"%s": %d}' % (self.cid, runs.ops.MAX_RESPAWNS))
        runs.ops._respawn(self.st(), dry=False)
        self.assertTrue((self.ws / self.cid / "reconcile.flagged").exists())

    def test_dry_run_does_not_spawn_or_charge_the_respawn_budget(self):
        before = runs.ops._respawn_count(self.cid)
        runs.ops._respawn(self.st(), dry=True)
        self.assertEqual(runs.ops._respawn_count(self.cid), before)

    def test_dry_run_writes_no_transition(self):
        runs.ops._respawn(self.st(), dry=True)
        self.assertFalse(runs.common.TRANSITIONS_LOG.exists(),
                         "a preview wrote to the trace the TLA+ check replays")


class TestSpawnReportsEarlyDeath(unittest.TestCase):
    """run_cell.sh refuses a run before installing its tee (exit 1/2/3/42/43).
    Both spawn paths sent stderr to DEVNULL and printed success anyway.

    The sink also has to OUTLIVE the probe: it is the driver's stderr for the
    whole run, so a crash long after the probe window still has somewhere to
    land."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = Path(self._tmp.name) / ".orch"
        self.orch.mkdir(parents=True)
        self.p = mock.patch.object(runs.common, "ORCH", self.orch)
        self.p.start()
        self._probe = runs.ops.SPAWN_PROBE_S
        runs.ops.SPAWN_PROBE_S = 0.4

    def tearDown(self):
        runs.ops.SPAWN_PROBE_S = self._probe
        self.p.stop()
        self._tmp.cleanup()

    def test_immediate_failure_returns_the_exit_code(self):
        rc = runs.ops._spawn_detached(
            ["bash", "-c", 'echo "FATAL prepare_cell: wrong seed doc" >&2; exit 3'],
            dict(os.environ), "cell-x", "spawn")
        self.assertEqual(rc, 3)

    def test_a_live_child_keeps_its_stderr_sink(self):
        rc = runs.ops._spawn_detached(["bash", "-c", "sleep 5"],
                                  dict(os.environ), "cell-y", "spawn")
        self.assertIsNone(rc)
        self.assertTrue((self.orch / "cell.cell-y.err").exists(),
                        "a surviving driver has nowhere to write a traceback")

    def test_a_survivors_later_output_still_lands_in_it(self):
        # The regression that mattered: the child outlives the probe, then
        # writes. Deleting the file on survival sent that into an unlinked
        # inode.
        runs.ops._spawn_detached(
            ["bash", "-c", 'sleep 0.8; echo "died later" >&2'],
            dict(os.environ), "cell-w", "spawn")
        kept = self.orch / "cell.cell-w.err"
        for _ in range(40):
            if "died later" in kept.read_text():
                break
            time.sleep(0.1)
        self.assertIn("died later", kept.read_text())

    def test_stderr_is_kept_for_inspection_on_failure(self):
        runs.ops._spawn_detached(["bash", "-c", 'echo "boom" >&2; exit 43'],
                             dict(os.environ), "cell-z", "spawn")
        kept = self.orch / "cell.cell-z.err"
        self.assertTrue(kept.exists())
        self.assertIn("boom", kept.read_text())

    def test_spawning_a_stopped_cell_records_the_resume(self):
        # `stop` writes .paused and emits Pause; a spawn that starts the cell
        # anyway must lift it, or the model keeps intent='paused' and every
        # later event on the cell replays as an illegal transition.
        cid = "cell-paused"
        (runs.common.WS / cid).mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, runs.common.WS / cid, True)
        (runs.common.WS / cid / ".paused").write_text("stopped by=operator\n")
        emitted = []
        # The Resume is gated on the LEDGER pause, exactly as replay judges
        # it — so the fixture seeds the Pause `stop` emits, into a patched
        # log (this class patches ORCH only; the live ledger must stay clean).
        with mock.patch.object(runs.common, "TRANSITIONS_LOG",
                               self.orch / "transitions.log"):
            runs.common._emit_transition("Pause", cid, "reason=stopped")
            with mock.patch.object(runs.common, "_emit_transition",
                                   side_effect=lambda a, c, *r: emitted.append((a, c))):
                runs.ops._spawn_detached(["bash", "-c", "exit 0"], dict(os.environ),
                                     cid, "spawn")
        self.assertIn(("Resume", cid), emitted)
        self.assertFalse((runs.common.WS / cid / ".paused").exists())

    def test_a_cancelled_cell_is_never_resumed_by_a_spawn(self):
        cid = "cell-cancelled"
        (runs.common.WS / cid).mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, runs.common.WS / cid, True)
        (runs.common.WS / cid / ".paused").write_text("killed by=operator\n")
        (runs.common.WS / cid / ".cancelled").write_text("by=operator\n")
        emitted = []
        with mock.patch.object(runs.common, "_emit_transition",
                               side_effect=lambda a, c, *r: emitted.append((a, c))):
            runs.ops._spawn_detached(["bash", "-c", "exit 0"], dict(os.environ),
                                 cid, "spawn")
        self.assertNotIn(("Resume", cid), emitted)
        self.assertTrue((runs.common.WS / cid / ".paused").exists())

    def test_it_never_creates_a_workspace(self):
        # The sink must not be minted under WS: a spawn precedes prepare, so
        # writing there invents a workspace for a cell that never ran — and
        # every scanner that walks WS then sees it.
        runs.ops._spawn_detached(["bash", "-c", "exit 3"],
                             dict(os.environ), "cell-never", "spawn")
        self.assertFalse((runs.common.WS / "cell-never").exists())


if __name__ == "__main__":
    unittest.main()


class SweepCase(unittest.TestCase):
    """One supervision sweep over one workspace, with the world mocked.
    Nothing here starts a process or touches the live tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        self.orch = self.ws / ".orch"
        self.orch.mkdir(parents=True)
        self.cid = "sonnet_high_beta_apidocs_T1_r1"
        self.cell = self.ws / self.cid
        self.cell.mkdir()
        (self.cell / "iterations.log").touch()
        self.patches = [
            # _teardown_cell waits for the loop to exit, and these suites
            # patch os.kill with a bare Mock — every pid then looks alive
            # forever and the full grace is burned in a unit test.
            mock.patch.object(runs.ops, "_await_exit", return_value=True),
            mock.patch.object(runs.common, "WS", self.ws),
            mock.patch.object(runs.common, "ORCH", self.orch),
            mock.patch.object(runs.ops, "RESPAWN_BOOK", self.orch / "respawns.json"),
            mock.patch.object(runs.common, "TRANSITIONS_LOG", self.orch / "trans.log"),
            mock.patch.object(runs.common, "RECONCILE_LOG", self.orch / "rec.log"),
            mock.patch.object(runs.state, "loop_pids", return_value={}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def sweep(self, st, *, live=None, boxes=(), ledger_doc=None, dry=False,
              only="", ps="", validate_error=None):
        doc = {"reverify_active": False, "gate": 0, "att": 1, "events": 3, "verdict": None}
        doc.update(ledger_doc or {})
        # only the cell dir is a cell: .orch lives under WS and cell_state
        # returns None for it in production
        with mock.patch.object(runs.state, "cell_state",
                               side_effect=lambda w, *a: st
                               if Path(w).name == self.cid else None), \
             mock.patch.object(runs.state, "loop_parents", return_value=live or {}), \
             mock.patch.object(runs.state, "containers", return_value=set(boxes)), \
             mock.patch.object(runs.ledger, "parse", return_value=doc), \
             mock.patch.object(runs.common, "sh", return_value=ps), \
             mock.patch.object(runs.taint, "_validate_cell",
                               return_value={"verdict": "VALID", "taints": []},
                               side_effect=validate_error) as val, \
             mock.patch.object(runs.supervise, "subprocess") as sub, \
             mock.patch.object(runs.os, "kill") as kill:
            runs.supervise._supervise_pass(dry=dry, only=only)
        return val, sub, kill

    def st(self, state="CRASHED", why="loop", **kw):
        d = dict(cid=self.cid, state=state, why=why, detail="", agent="sonnet",
                 variant="beta_apidocs", task="T1",
                 rep="1", budget=10, shape="3/6")
        d.update(kw)
        return d


class TestSweepClassification(SweepCase):
    def test_a_done_cell_is_validated_once(self):
        val, _, _ = self.sweep(self.st(state="DONE", why="green"))
        val.assert_called_once()

    def test_a_cell_whose_validation_raises_does_not_stop_the_sweep(self):
        # one bad metrics.json must not take the scheduler down with it
        runs.supervise._VALIDATION_FAILED.clear()
        self.addCleanup(runs.supervise._VALIDATION_FAILED.clear)
        for _ in range(2):
            val, _, _ = self.sweep(self.st(state="DONE", why="green"),
                                   validate_error=TypeError("str / str"))
            val.assert_called_once()
        log = (self.orch / "rec.log").read_text()
        self.assertEqual(log.count("validation FAILED"), 1, log)
        self.assertIn("TypeError: str / str", log)

    def test_a_cancelled_cell_is_not_validated(self):
        val, _, _ = self.sweep(self.st(state="DONE", why="cancelled"))
        val.assert_not_called()

    def test_a_flagged_cell_is_left_alone(self):
        (self.cell / "reconcile.flagged").touch()
        val, _, kill = self.sweep(self.st(state="DONE", why="green"))
        val.assert_not_called()
        kill.assert_not_called()

    def test_an_operator_pause_stops_the_sweep_touching_the_cell(self):
        (self.cell / ".paused").write_text("manual by=operator\n")
        val, _, kill = self.sweep(self.st(state="DONE", why="green"))
        val.assert_not_called()
        kill.assert_not_called()

    def _crashed_sweeps(self, *, queued, n):
        with mock.patch.object(runs.ops, "_claimed", return_value=False), \
             mock.patch.object(runs.supervise.queue, "lane_has", return_value=queued):
            for _ in range(n):
                self.sweep(self.st(state="CRASHED", why="loop", events=5))
        log = self.orch / "rec.log"
        return log.read_text() if log.exists() else ""

    def test_a_crashed_cell_waiting_in_its_lane_is_not_reported_every_sweep(self):
        log = self._crashed_sweeps(queued=True, n=3)
        self.assertNotIn("CRASHED/", log)
        self.assertNotIn("unclaimed", log)

    def test_a_crashed_cell_nobody_will_restart_is_reported(self):
        log = self._crashed_sweeps(queued=False, n=1)
        self.assertIn(f"{self.cid} CRASHED/loop", log)
        self.assertIn(f"{self.cid} unclaimed", log)

    def test_only_restricts_the_sweep(self):
        val, _, _ = self.sweep(self.st(state="DONE", why="green"), only="haiku")
        val.assert_not_called()

    def test_an_orphan_loop_on_a_terminal_cell_is_killed_and_torn_down(self):
        old = time.time() - 10 * runs.supervise.T_HANG
        os.utime(self.cell / "iterations.log", (old, old))
        _, sub, kill = self.sweep(self.st(state="DONE", why="green"),
                                  live={self.cid: 4242})
        kill.assert_called_once()
        self.assertTrue(sub.run.called, "infra teardown must be explicit")

    def test_a_fresh_orphan_gets_its_teardown_grace(self):
        _, sub, kill = self.sweep(self.st(state="DONE", why="green"),
                                  live={self.cid: 4242})
        kill.assert_not_called()

    def test_an_agent_crash_asks_for_a_human(self):
        _, _, kill = self.sweep(self.st(why="agent", detail="creds expired"))
        kill.assert_not_called()
        self.assertIn("needs a", (self.orch / "rec.log").read_text())

    def test_a_stranded_reverify_repairs_the_ledger(self):
        old = time.time() - 10 * runs.supervise.T_HANG
        os.utime(self.cell / "iterations.log", (old, old))
        self.sweep(self.st(state="DONE", why="green"),
                   ledger_doc={"reverify_active": True})
        self.assertIn("stranded mid-gate",
                      (self.cell / "iterations.log").read_text())

    def test_a_stranded_reverify_is_not_repaired_in_dry_run(self):
        old = time.time() - 10 * runs.supervise.T_HANG
        os.utime(self.cell / "iterations.log", (old, old))
        self.sweep(self.st(state="DONE", why="green"),
                   ledger_doc={"reverify_active": True}, dry=True)
        self.assertEqual((self.cell / "iterations.log").read_text(), "")

    def test_a_live_reverify_process_is_not_stranded(self):
        old = time.time() - 10 * runs.supervise.T_HANG
        os.utime(self.cell / "iterations.log", (old, old))
        self.sweep(self.st(state="DONE", why="green"),
                   ledger_doc={"reverify_active": True},
                   ps="python3 /x/cli.py cell reverify cell")
        self.assertEqual((self.cell / "iterations.log").read_text(), "")

    def test_a_stalled_but_growing_cell_is_left_running(self):
        old = time.time() - 3 * runs.supervise.T_STALL
        os.utime(self.cell / "iterations.log", (old, old))
        (self.cell / "agent.attempt-1.log").write_text("x" * 100)
        _, _, kill = self.sweep(self.st(state="RUNNING", why="agent"),
                                live={self.cid: 4242},
                                boxes={f"fae-agent-{self.cid}"})
        kill.assert_not_called()
        self.assertIn("STALL-BUT-ALIVE", (self.orch / "rec.log").read_text())


class TestVerifyHeldAlert(SweepCase):
    """verify-lock is one global mutex: a cell stuck inside it queues every
    other lane behind it (2026-08-15 haiku_beta r19 — a single hung
    HTTP request in the build under test held it for 43min). The sweep
    should surface that in the cell's own ledger, same shape as
    HOST-OVERLOADED, once it has run past the threshold."""

    def setUp(self):
        super().setUp()
        self.addCleanup(runs.supervise._VERIFY_ALERTED.clear)
        runs.supervise._VERIFY_ALERTED.clear()

    def _acquire(self, seconds_ago):
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        line = f"{ts:%Y-%m-%dT%H:%M:%SZ}\tAcquireVerify\t{self.cid}\tdeploy0=0\n"
        (self.orch / "trans.log").write_text(line)

    def test_a_verify_held_past_the_threshold_is_alerted(self):
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"))
        body = (self.cell / "iterations.log").read_text()
        self.assertIn("ALERT", body)
        self.assertIn("VERIFY-SLOW", body)
        self.assertIn("verify-lock", body)

    def test_a_fresh_verify_is_not_alerted(self):
        self._acquire(30)
        self.sweep(self.st(state="RUNNING", why="verify"))
        self.assertEqual((self.cell / "iterations.log").read_text(), "")

    def test_the_alert_fires_once_per_episode(self):
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"))
        self.sweep(self.st(state="RUNNING", why="verify"))
        body = (self.cell / "iterations.log").read_text()
        self.assertEqual(body.count("VERIFY-SLOW"), 1)

    def test_a_fresh_episode_after_the_lock_was_released_alerts_again(self):
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"))
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 60)   # new AcquireVerify ts
        self.sweep(self.st(state="RUNNING", why="verify"))
        body = (self.cell / "iterations.log").read_text()
        self.assertEqual(body.count("VERIFY-SLOW"), 2)

    def _mark(self, seconds_ago, kind="SHAPE", note="attempt=1\tBSB pass"):
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        with (self.cell / "iterations.log").open("a") as f:
            f.write(f"{ts:%Y-%m-%dT%H:%M:%SZ}\t{kind}\t{self.cid}\t{note}\n")

    def test_a_green_gate_progressing_through_its_arrangements_is_not_slow(self):
        """Six arrangements under one lock hold it ~15 min; the newest mark
        is what a stuck arrangement inflates, not the total."""
        self._acquire(3 * runs.supervise.VERIFY_HELD_ALERT_S)
        self._mark(2 * runs.supervise.VERIFY_HELD_ALERT_S, "VERIFY_READY", "held=10s")
        self._mark(60)
        self.sweep(self.st(state="RUNNING", why="verify"))
        self.assertNotIn("VERIFY-SLOW", (self.cell / "iterations.log").read_text())

    def test_a_stuck_arrangement_is_slow_even_after_earlier_progress(self):
        self._acquire(3 * runs.supervise.VERIFY_HELD_ALERT_S)
        self._mark(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"))
        body = (self.cell / "iterations.log").read_text()
        self.assertIn("VERIFY-SLOW", body)
        self.assertIn("no progress for", body)

    def test_a_mark_from_the_previous_verify_does_not_count(self):
        self._mark(10 * runs.supervise.VERIFY_HELD_ALERT_S)      # an older gate's mark
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"))
        self.assertIn("VERIFY-SLOW", (self.cell / "iterations.log").read_text())

    def test_progress_after_an_alert_rearms_it(self):
        live = {self.cid: 4242}      # alive both sweeps: no Crash reconciled
        self._acquire(3 * runs.supervise.VERIFY_HELD_ALERT_S)
        self._mark(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"), live=live)
        self._mark(runs.supervise.VERIFY_HELD_ALERT_S + 10)      # a later mark, also stale
        self.sweep(self.st(state="RUNNING", why="verify"), live=live)
        self.assertEqual((self.cell / "iterations.log").read_text().count("VERIFY-SLOW"), 2)

    def test_dry_run_writes_nothing_and_does_not_suppress_the_real_alert(self):
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="RUNNING", why="verify"), dry=True)
        self.assertEqual((self.cell / "iterations.log").read_text(), "")
        self.sweep(self.st(state="RUNNING", why="verify"))
        self.assertIn("VERIFY-SLOW", (self.cell / "iterations.log").read_text())

    def test_a_terminal_cell_is_never_alerted(self):
        self._acquire(runs.supervise.VERIFY_HELD_ALERT_S + 30)
        self.sweep(self.st(state="DONE", why="green"))
        self.assertNotIn("VERIFY-SLOW",
                         (self.cell / "iterations.log").read_text())


class TestAStaleArmSlotSidecarIsNotAZombieForever(unittest.TestCase):
    """An arm slot's holder note names the infra its last holder had.
    Once that infra is gone the note is bookkeeping, not a zombie: the
    finder must drop the note instead of re-reporting a reap that cannot
    happen on every tick."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.orch = Path(self._tmp.name) / "orch"
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()
        slots = self.orch / "arm-alpha.slots"
        slots.mkdir(parents=True)
        self.slot = slots / "slot-2"
        self.slot.touch()
        (slots / "slot-2.holder").write_text("sonnet_high_alpha_apidocs_T1_r6 54638 1\n")
        self.patches = [
            mock.patch.object(runs.common, "ORCH", self.orch),
            mock.patch.object(runs.common, "WS", self.ws),
            mock.patch.object(runs.state, "loop_parents", return_value={}),
            mock.patch.object(runs.zombies, "_leaked_lock_holders", return_value=[]),
            mock.patch.object(runs.state, "containers", return_value=[]),
            mock.patch.object(runs.zombies, "_cluster_map", return_value={}),
            mock.patch.object(runs.zombies, "_strays", return_value=[]),
            mock.patch.object(runs.state, "_lock_is_held", return_value=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _found(self, existing):
        with mock.patch.object(runs.zombies, "_containers_all", return_value=set(existing)), \
             mock.patch.object(runs.common, "sh", return_value=""):
            return [z for z in runs.zombies.find_zombies() if z[0] == "container"]

    def test_a_note_naming_nothing_that_exists_is_dropped_and_reports_nothing(self):
        self.assertEqual(self._found([]), [])
        self.assertFalse((self.slot.parent / "slot-2.holder").exists())

    def test_a_note_naming_a_real_leftover_is_still_a_zombie(self):
        found = self._found(["fae-dind-sonnet_high_alpha_apidocs_T1_r6"])
        self.assertEqual([z[1] for z in found], ["fae-dind-sonnet_high_alpha_apidocs_T1_r6"])
        self.assertTrue((self.slot.parent / "slot-2.holder").exists())

class TestVerifyWedgedStandsTheCellDown(SweepCase):
    """The verify lock is GLOBAL, so a holder that never finishes stalls every
    other lane for as long as it takes a human to notice. Alerting is not
    enough on its own: the alert fires and the fleet keeps waiting."""

    def setUp(self):
        super().setUp()
        for d in (runs.supervise._VERIFY_ALERTED, runs.supervise._VERIFY_WEDGED):
            self.addCleanup(d.clear)
            d.clear()

    def _acquire(self, seconds_ago):
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        (self.orch / "trans.log").write_text(
            f"{ts:%Y-%m-%dT%H:%M:%SZ}\tAcquireVerify\t{self.cid}\tdeploy0=0\n")

    # ABSOLUTE seconds against PINNED thresholds. Deriving the hold from the
    # constant (VERIFY_WEDGED_S + 60) makes the test move with it, so raising
    # the threshold to disable escalation raises the simulated hold too and
    # the test passes either way.
    ALERT_S, WEDGE_S = 300, 14400

    def _sweep_wedged(self, held_s, dry=False):
        self._acquire(held_s)
        with mock.patch.object(runs.supervise, "VERIFY_HELD_ALERT_S", self.ALERT_S), \
             mock.patch.object(runs.supervise, "VERIFY_WEDGED_S", self.WEDGE_S), \
             mock.patch.object(runs.ops, "_teardown_cell") as td, \
             mock.patch.object(runs.supervise, "_reclaim"):
            self.sweep(self.st(state="RUNNING", why="verify"), dry=dry)
        return td

    def test_the_shipped_thresholds_are_what_these_tests_assume(self):
        self.assertEqual(runs.supervise.VERIFY_HELD_ALERT_S, self.ALERT_S)
        self.assertEqual(runs.supervise.VERIFY_WEDGED_S, self.WEDGE_S)

    def test_past_the_wedge_threshold_the_cell_is_torn_down(self):
        td = self._sweep_wedged(self.WEDGE_S + 60)
        td.assert_called_once()
        self.assertEqual(td.call_args.kwargs["reason"], "verify-wedged")
        body = (self.cell / "iterations.log").read_text()
        self.assertIn("VERIFY-WEDGED", body)
        self.assertIn("this attempt is lost", body)

    def test_between_the_two_thresholds_it_only_alerts(self):
        td = self._sweep_wedged(self.ALERT_S + 60)
        td.assert_not_called()
        body = (self.cell / "iterations.log").read_text()
        self.assertIn("VERIFY-SLOW", body)
        self.assertNotIn("VERIFY-WEDGED", body)

    def test_a_dry_run_stands_nothing_down(self):
        td = self._sweep_wedged(self.WEDGE_S + 60, dry=True)
        td.assert_not_called()
        self.assertFalse((self.cell / "iterations.log").read_text().strip())

    def test_it_acts_once_per_episode(self):
        self._acquire(self.WEDGE_S + 60)
        with mock.patch.object(runs.supervise, "VERIFY_HELD_ALERT_S", self.ALERT_S), \
             mock.patch.object(runs.supervise, "VERIFY_WEDGED_S", self.WEDGE_S), \
             mock.patch.object(runs.ops, "_teardown_cell") as td, \
             mock.patch.object(runs.supervise, "_reclaim"):
            for _ in range(3):
                self.sweep(self.st(state="RUNNING", why="verify"))
        td.assert_called_once()


class TestArmStuckMeasuresProgressNotLiveness(SweepCase):
    """A held arm slot is ended only when the cell is BOTH overaged and not
    progressing. `not progressing` is phase_age: the heartbeat's own age says
    the ticker is alive, and the ticker rewrites .loop every HB_TICK for as
    long as the loop lives, so a wedged-but-alive cell reads as freshly
    beating and the rule can never fire for the case it exists to catch."""

    def setUp(self):
        super().setUp()
        self.addCleanup(runs.common._ARM_ALERTED.clear)
        runs.common._ARM_ALERTED.clear()

    def _sweep_arm(self, *, slot_age, phase_age, phase="verify"):
        st = self.st(state="RUNNING", why="agent")
        st["variant"] = "alpha_apidocs"
        with mock.patch.object(runs.state, "_arm_slot_of", return_value=slot_age), \
             mock.patch.object(runs.state, "heartbeat",
                               return_value={"phase_age": phase_age,
                                             "age": 1.0, "phase": phase}), \
             mock.patch.object(runs.ops, "_teardown_cell") as td, \
             mock.patch.object(runs.supervise, "_reclaim"):
            self.sweep(st)
        return td

    def test_a_beating_ticker_does_not_protect_a_stalled_phase(self):
        # age=1.0 (the ticker just wrote .loop) while the PHASE has not moved.
        td = self._sweep_arm(slot_age=runs.supervise.ARM_HELD_ALERT_S + 60,
                             phase_age=runs.supervise.ARM_STALL_S + 60)
        td.assert_called_once()
        self.assertEqual(td.call_args.kwargs["reason"], "arm-stuck")

    def test_a_queued_cell_is_waiting_not_stalled(self):
        """The wait phases are someone else's time: the verify-lock holder,
        a provider wall. A 30-minute queue behind six-arrangement greens is
        the fleet's normal shape, not a wedge."""
        for phase in sorted(runs.state.WAIT_PHASES):
            with self.subTest(phase=phase):
                td = self._sweep_arm(slot_age=runs.supervise.ARM_HELD_ALERT_S * 3,
                                     phase_age=runs.supervise.ARM_STALL_S * 3, phase=phase)
                td.assert_not_called()

    def test_a_progressing_cell_is_left_alone_however_long_it_holds(self):
        td = self._sweep_arm(slot_age=runs.supervise.ARM_HELD_ALERT_S * 3,
                             phase_age=10)
        td.assert_not_called()

    def test_a_young_slot_is_left_alone_even_when_stalled(self):
        td = self._sweep_arm(slot_age=10, phase_age=runs.supervise.ARM_STALL_S + 60)
        td.assert_not_called()

    def test_a_long_agent_call_is_not_a_stall(self):
        # One agent call runs the better part of an hour with the phase
        # unchanged throughout. Reading that as "not progressing" stands down
        # a cell that is simply working.
        td = self._sweep_arm(slot_age=runs.supervise.ARM_HELD_ALERT_S + 60,
                             phase_age=runs.supervise.ARM_STALL_S * 3, phase="agent")
        td.assert_not_called()


class TestLeakedLockHolders(unittest.TestCase):
    """A lock held while nothing entitled to hold it is running. Both locks
    are held in-process — by a cell loop, or by `cli.py experiment verb|cell
    reverify|experiment smoke` verifying outside one; either held with no such process means an
    inherited fd was never released, and the next verify blocks on it while
    holding the GLOBAL verify lock.

    The holder cannot be named while a legitimate one runs — every driver
    opens all of its candidate lock files — so the absence of an entitled
    process is what makes the remaining fd holders attributable.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(runs.common, "ORCH", self.orch)
        p.start(); self.addCleanup(p.stop)
        (self.orch / "rig-lock").write_text("")

    def _find(self, *, held, entitled_running, fd_pids, alive=True, loops=None):
        with mock.patch.object(runs.mutex, "probe_held", return_value=held), \
             mock.patch.object(runs.common, "sh",
                               side_effect=lambda a, **k:
                                   ("999\n" if entitled_running else "")
                                   if a[0] == "pgrep" else ""), \
             mock.patch.object(runs.zombies, "_fd_holders", return_value=fd_pids), \
             mock.patch.object(runs.zombies, "_pid_alive", return_value=alive), \
             mock.patch.object(runs.state, "loop_parents", return_value=loops or {}):
            return runs.zombies._leaked_lock_holders()

    def test_a_driver_holding_the_lock_from_another_root_is_not_a_leak(self):
        # A cell run by hand in ws-test.nosync holds the rig lock in-process;
        # loop_parents() (default root only) does not see it. Its own argv
        # names the driver, and that is what entitles it.
        def ps(a, **k):
            if a[0] == "ps":
                return "python3 -m fae.cell T1 alpha reference 2 --stub /tmp/x\n"
            return ""
        with mock.patch.object(runs.mutex, "probe_held", return_value=True), \
             mock.patch.object(runs.common, "sh", side_effect=ps), \
             mock.patch.object(runs.zombies, "_fd_holders", return_value=[4242]), \
             mock.patch.object(runs.zombies, "_pid_alive", return_value=True), \
             mock.patch.object(runs.state, "loop_parents", return_value={}):
            self.assertEqual(runs.zombies._leaked_lock_holders(), [])

    def test_a_held_lock_with_nothing_entitled_is_a_leak(self):
        self.assertIn(("rig-lock", 4242),
                      self._find(held=True, entitled_running=False,
                                 fd_pids=[4242]))

    def test_a_live_verifier_means_the_holder_is_legitimate(self):
        # The whole safety argument: while `cli.py experiment verb` (or cell
        # reverify, or experiment smoke) runs, the fd holders include it and its
        # children, and none may be killed.
        self.assertEqual(self._find(held=True, entitled_running=True,
                                    fd_pids=[4242]), [])

    def test_the_out_of_loop_verifiers_are_the_ones_named(self):
        for argv in ("python3 cli.py experiment verb bench --reps 3",
                     "python3 cli.py cell reverify x --all",
                     "python3 cli.py experiment smoke --only alpha"):
            self.assertRegex(argv, runs.zombies._VERIFY_HOLDER_ARGV)
            with mock.patch.object(runs.common, "sh", return_value=argv + "\n"):
                self.assertTrue(runs.zombies._is_driver_pid(4242), argv)
        for argv in ("python3 cli.py experiment status", "python3 cli.py experiment run",
                     "python3 cli.py rig reverify x",    # wrong group: not a real invocation
                     "python3 cli.py experiment bench"):  # an experiment command runs only through verb
            self.assertNotRegex(argv, runs.zombies._VERIFY_HOLDER_ARGV)

    def test_a_live_python_cell_holds_the_rig_lock_itself(self):
        # The cell loop takes the rig lock IN-PROCESS — there is no verifier
        # process to pgrep for. A predicate that names only the out-of-loop
        # verifiers reads every cell verify as a leak, and the reaper kills
        # the cell mid-verify.
        self.assertEqual(
            self._find(held=True, entitled_running=False, fd_pids=[4242],
                       loops={"opus_high_beta_apidocs_T1_r12": 4242}),
            [])

    def test_with_no_loop_at_all_it_is_still_a_leak(self):
        # The invert: the fix must not disable the check, only widen who
        # counts as entitled.
        self.assertIn(("rig-lock", 4242),
                      self._find(held=True, entitled_running=False,
                                 fd_pids=[4242], loops={}))

    def test_an_unheld_lock_is_never_a_leak(self):
        self.assertEqual(self._find(held=False, entitled_running=False,
                                    fd_pids=[4242]), [])

    def test_a_dead_pid_is_dropped(self):
        self.assertEqual(self._find(held=True, entitled_running=False,
                                    fd_pids=[4242], alive=False), [])


class TestTheGateColumnIsLiveDuringAVerify(unittest.TestCase):
    """GATE shows the arrangements passed so far in the attempt RUNNING NOW.

    The ledger's `gate` is the last JUDGED attempt and cannot move while a
    verify is in flight, so a six-shape gate taking twenty minutes displayed a
    frozen number the whole time, next to a live one in another column.
    """

    L = dict(att=2, gate=3, rev_pass=0, reverify_active=False,
             live_shape_pass=4, events=5, prepared=True, noedit=0,
             noedit_last="", alerts=0, alert_last="", verdict=None,
             green_at=None, last_ev="", last_line="", iters=[], iter_notes=[])

    def _shape(self, phase, **over):
        led = dict(self.L, gate_n=6); led.update(over)
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "sonnet_high_beta_apidocs_T1_r1"
            ws.mkdir()
            (ws / "cell.env").write_text(
                "TASK=T1\nVARIANT=beta_apidocs\n"
                "REP=1\nATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            (ws / "iterations.log").write_text("")
            with mock.patch.object(runs.ledger, "parse", return_value=led), \
                 mock.patch.object(runs.ledger, "hist", return_value=[]), \
                 mock.patch.object(runs.state, "heartbeat",
                                   return_value={"phase": phase, "pid": 1,
                                                 "phase_age": 5, "attempt": "2"}):
                st = runs.state.cell_state(ws, {}, set())
        return st["shape"] if st else None

    def test_mid_verify_shows_the_running_attempts_count(self):
        self.assertEqual(self._shape("verify"), "4/6*")

    def test_outside_a_verify_shows_the_last_judged_gate(self):
        self.assertEqual(self._shape("agent"), "3/6")

    def test_a_reverify_keeps_its_own_counter(self):
        self.assertEqual(
            self._shape("verify", reverify_active=True, rev_pass=2), "2/6")


class TestSpawnTagsSmokeCells(unittest.TestCase):
    """A SMOKE=1 spawn that computes the unsmoke cid guards one identity
    (duplicate-loop check, prestart clean, stderr sink) while the cell
    process, which honors SMOKE via load_config, runs under another."""

    def test_the_spawn_cid_carries_the_smoke_flag(self):
        import inspect
        src = inspect.getsource(runs.ops.spawn)
        m = re.search(r"cid = common\.cell_id\(([^)]*)\)", src)
        self.assertIsNotNone(m)
        self.assertIn("smoke=", m.group(1))


class TestRespawnKeepsTheCellsImplementation(unittest.TestCase):
    """_respawn hardcoded the bash driver: any resumed py cell silently became
    a bash cell from that attempt on. The respawn must ask the cell which
    implementation it records (cell.env IMPL, via _impl_of)."""

    def test_the_respawn_argv_comes_from_the_recorded_impl(self):
        import inspect
        src = inspect.getsource(runs.ops._respawn)
        self.assertIn("_cell_argv(st[", src)
        self.assertIn('"-m", "fae.cell"', inspect.getsource(runs.ops._cell_argv))

    def test_impl_of_reads_the_cell_env(self):
        with tempfile.TemporaryDirectory() as d:
            cid = "opus_high_beta_apidocs_T1_r96"
            (Path(d) / cid).mkdir()
            (Path(d) / cid / "cell.env").write_text("TASK=T1\nIMPL=py\n")
            with mock.patch.object(runs.common, "WS", Path(d)):
                self.assertEqual(runs._impl_of(cid), "py")
