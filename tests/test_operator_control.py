"""Operator control must actually control: selectors, the exactly-1 cell
verbs (pause/resume/stop), and the conduct bulk verbs (pause/resume/stop).

Every test patches runs.experiment.workspace().path and the scheduling plane to a TemporaryDirectory. Nothing here
reads or writes the live workspace tree, and nothing starts a process.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fae.cell.cell import Cell

from _ctx import runs, OrchTmpCase, at_workspace

CIDS = [
    "sonnet_high_beta_apidocs_T1_r1",
    "sonnet_high_beta_apidocs_T1_r10",
    "sonnet_high_alpha_howto_T1_r2",
    "haiku_high_beta_apidocs_T1_r1",
]


class OperatorTestCase(OrchTmpCase):
    """Base fixture.

    NO TEST HERE MAY START A PROCESS. resume() calls _respawn(), which Popens
    harness/run_cell.sh — a unit test that reaches it launches real cells
    against whatever workspace tree it is pointed at. _respawn and
    _spawn_detached are therefore stubbed out for every test in this file, and
    the stub records its calls so a test can assert that nothing was launched.
    """

    def setUp(self):
        super().setUp()   # temp tree + WS and the scheduling plane patched
        for c in CIDS:
            (self.ws / c).mkdir()
        self.spawned = []
        self.patches = [
            # teardown_cell waits for the loop to exit, and these suites
            # patch os.kill with a bare Mock — every pid then looks alive
            # forever and the full grace is burned in a unit test.
            mock.patch.object(Cell, "_gone", return_value=True),
            # never launch anything from a unit test
            mock.patch.object(runs.conduct.Conduct, "respawn",
                              side_effect=lambda st, dry, **k: self.spawned.append(st["cid"])),
            mock.patch.object(runs.conduct.Conduct, "_spawn",
                              side_effect=lambda *a, **k: self.spawned.append(a[2]) or True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def queue(self, agent, specs):
        for spec in specs:
            runs.queues.enqueue(agent, spec)

    def pending(self, agent):
        return [runs.queues.read_spec(p) for p in runs.queues.lane_specs(agent)]

    def parked_pending(self, agent):
        return [runs.queues.read_spec(p)
                for p in runs.queues.specs_in(runs.queues.lane_dir(agent, parked=True))]


class TestSelectorIsAnchored(OperatorTestCase):
    """`sel in c` made every short selector fleet-wide and made a full cid
    match its own higher reps. kill has no confirmation prompt."""

    def test_all_matches_everything(self):
        self.assertEqual(sorted(runs.experiment.workspace().select("all")), sorted(CIDS))

    def test_model_still_matches_its_cells(self):
        got = runs.experiment.workspace().select("sonnet")
        self.assertEqual(len(got), 3)
        self.assertTrue(all(c.startswith("sonnet_") for c in got))

    def test_arm_token_run_still_matches(self):
        self.assertEqual(runs.experiment.workspace().select("alpha"),
                         ["sonnet_high_alpha_howto_T1_r2"])

    def test_exact_cid_matches_only_itself(self):
        self.assertEqual(
            runs.experiment.workspace().select("sonnet_high_beta_apidocs_T1_r1"),
            ["sonnet_high_beta_apidocs_T1_r1"])

    def test_r1_does_NOT_match_r10(self):
        """THE REGRESSION: `..._r1` is a substring of `..._r10`, so killing
        rep 1 would have taken reps 10-19 with it. Reps past nine are real —
        4-6 were queued and trimmed."""
        got = runs.experiment.workspace().select("sonnet_high_beta_apidocs_T1_r1")
        self.assertNotIn("sonnet_high_beta_apidocs_T1_r10", got)

    def test_a_whole_token_legitimately_matches_many(self):
        """`T1` IS a complete token, so matching every T1 cell is correct — and
        anchoring cannot change that. What made `kill T1` dangerous was the
        absence of a preview, which is why kill now has --dry-run. Recorded
        here so nobody "fixes" it into surprising behaviour."""
        self.assertEqual(sorted(runs.experiment.workspace().select("T1")), sorted(CIDS))

    def test_rep_token_matches_only_that_rep(self):
        """`r1` selects rep 1 and NOT rep 10 — the substring bug that made a
        full cid unsafe against its own siblings."""
        got = runs.experiment.workspace().select("r1")
        self.assertIn("sonnet_high_beta_apidocs_T1_r1", got)
        self.assertIn("haiku_high_beta_apidocs_T1_r1", got)
        self.assertNotIn("sonnet_high_beta_apidocs_T1_r10", got)

    def test_partial_token_does_not_match(self):
        self.assertEqual(runs.experiment.workspace().select("son"), [])
        self.assertEqual(runs.experiment.workspace().select("apidoc"), [])


class TestQueuedCells(OperatorTestCase):
    """Workspace.queued_cells: every cell with a pending spec, the ones with
    no workspace folder included, which select_cells cannot see."""

    def test_finds_specs_with_no_workspace(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2)])
        self.assertEqual(runs.experiment.workspace().queued_cells("all"),
                         ["sonnet_high_alpha_apidocs_T1_r2"])

    def test_includes_specs_that_already_have_a_workspace(self):
        self.queue("sonnet", [dict(task="T1", variant="beta_apidocs", rep=1)])
        self.assertEqual(runs.experiment.workspace().queued_cells("all"),
                         ["sonnet_high_beta_apidocs_T1_r1"])

    def test_selector_applies_and_is_anchored(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2)])
        self.queue("haiku", [dict(task="T1", variant="beta_howto", rep=3)])
        self.assertEqual(len(runs.experiment.workspace().queued_cells("sonnet")), 1)
        self.assertEqual(len(runs.experiment.workspace().queued_cells("haiku")), 1)
        self.assertEqual(len(runs.experiment.workspace().queued_cells("T1")), 2)   # whole token
        self.assertEqual(runs.experiment.workspace().queued_cells("son"), [])      # partial: no match

    def test_unreadable_spec_file_is_survived(self):
        d = runs.queues.lane_dir("sonnet"); d.mkdir(parents=True)
        (d / "100000.sonnet_high_beta_apidocs_T1_r9.json").write_text("{not json\n")
        self.assertEqual(runs.experiment.workspace().queued_cells("all"),
                         ["sonnet_high_beta_apidocs_T1_r9"])



class TestQueuedSummaryCountsOnlyRealBacklog(OperatorTestCase):
    """"QUEUED — not yet started" must mean it. A cell restarted by resume or
    reconcile while its spec is still queued is running, not pending: conduct
    pops that spec and drops it on the duplicate-loop guard."""

    def test_the_headline_breaks_pending_specs_down_by_state(self):
        """Every pending spec is counted; the breakdown says what each is
        waiting on, so a lane full of paused work cannot read as backlog."""
        fresh = dict(task="T1", variant="alpha_apidocs",
                     rep=7, budget=10, fresh=False)
        self.queue("sonnet", [fresh])
        m, v, task, rep = runs.parse_cell_id(CIDS[0])
        self.queue("sonnet", [dict(task=task, variant=v,
                                   rep=int(rep), budget=10, fresh=False)])
        (self.ws / CIDS[0] / ".paused").write_text("manual by=operator\n")
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            head = runs.render.queued_summary()[0]
        self.assertIn("QUEUED (2)", head)
        self.assertIn("1 fresh", head)
        self.assertIn("1 paused", head)

    def test_a_lane_says_how_many_of_its_specs_are_paused(self):
        """`next:` names the head of the lane, and a pause makes it
        unadmittable — without the count the lane reads as ready to go for as
        long as the lock stands. Counted the same way as the headline, so the
        lane numbers add up to it."""
        m, v, task, rep = runs.parse_cell_id(CIDS[0])
        self.queue("sonnet", [dict(task=task, variant=v,
                                   rep=int(rep), budget=10, fresh=False)])
        (self.ws / CIDS[0] / ".paused").write_text("manual by=operator\n")
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            lines = runs.render.queued_summary()
        lane = next(l for l in lines if l.strip().startswith("sonnet"))
        self.assertIn("[1 paused]", lane)
        self.assertIn("1 paused", lines[0], "the headline must agree")

    def test_a_lane_with_nothing_paused_says_nothing(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7, budget=10)])
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            lane = next(l for l in runs.render.queued_summary()
                        if l.strip().startswith("sonnet"))
        self.assertNotIn("paused", lane)

    def test_a_running_cell_is_not_counted_as_queued(self):
        cid = CIDS[0]
        m, v, task, rep = runs.parse_cell_id(cid)
        self.queue("sonnet", [dict(task=task, variant=v,
                                   rep=int(rep), budget=10)])
        with mock.patch.object(runs.host, "loop_parents", return_value={cid: 4242}):
            lines = runs.render.queued_summary()
        head = lines[0]
        self.assertIn("QUEUED (1)", head, head)
        self.assertIn("1 running", head, "a live cell's spec must say so")

    def test_a_pending_cell_with_no_loop_still_counts(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2, budget=10)])
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            lines = runs.render.queued_summary()
        self.assertIn("QUEUED (1)", lines[0])
        self.assertIn("1 fresh", lines[0])

    def test_a_parked_lane_is_shown_tagged_paused(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2, budget=10)])
        runs.queues.park_lane("sonnet")
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            lines = runs.render.queued_summary()
        row = next(l for l in lines if "sonnet" in l)
        self.assertIn("[PAUSED]", row)


class TestConductStop(OperatorTestCase):
    """The hard halt stops what is RUNNING. A scope of AGENT names touches
    only those lanes and leaves conduct up; `all` also TERMs conduct.

    It does NOT touch queues: the backlog is not run state, and a stop that
    emptied it made an operator's halt indistinguishable from discarding the
    experiment. It confirms before acting."""

    def _stop(self, scope, live=None, yes=True):
        with mock.patch.object(runs.host, "loop_parents", return_value=live or {}), \
             mock.patch.object(runs.host, "containers", return_value=[]), \
             mock.patch.object(runs.conduct.Conduct, "request_pause") as rp:
            runs.conduct.Conduct().stop(mock.Mock(scope=scope, yes=yes))
        return rp

    def test_the_backlog_survives_a_blanket_stop(self):
        specs = [dict(task="T1", variant="alpha_apidocs",
                      rep=r) for r in (2, 3)]
        self.queue("sonnet", specs)
        self._stop(["all"])
        self.assertEqual(sorted(s["rep"] for s in self.pending("sonnet")), [2, 3],
                         "a stop emptied the queue")
        self.assertEqual(list((self.queues / "backups").glob("stopped-*.json")), [],
                         "specs were shelved; a stop should not move them")

    def test_a_parked_lane_keeps_its_backlog(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2)])
        runs.queues.park_lane("sonnet")
        self._stop(["all"])
        self.assertEqual(len(self.parked_pending("sonnet")), 1)

    def test_scoped_stop_pauses_only_its_lanes_cells(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2)])
        self.queue("haiku", [dict(task="T1", variant="beta_howto", rep=3)])
        rp = self._stop(["haiku"])
        self.assertEqual(len(self.pending("sonnet")), 1)
        self.assertEqual(len(self.pending("haiku")), 1,
                         "the scoped lane's backlog was cleared")
        paused = rp.call_args[0][0]
        self.assertTrue(all(c.startswith("haiku_") for c in paused), paused)

    def test_it_refuses_without_a_confirmation(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=2)])
        with mock.patch.object(runs.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch.object(runs.conduct.Conduct, "request_pause") as rp, \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=[]):
            runs.conduct.Conduct().stop(mock.Mock(scope=["all"], yes=False))
        rp.assert_not_called()

    def test_a_non_terminal_without_yes_does_nothing(self):
        with mock.patch.object(runs.sys.stdin, "isatty", return_value=False), \
             mock.patch.object(runs.conduct.Conduct, "request_pause") as rp, \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=[]):
            runs.conduct.Conduct().stop(mock.Mock(scope=["all"], yes=False))
        rp.assert_not_called()

    def test_the_warning_names_the_loops_it_will_kill(self):
        live = {"haiku_high_beta_howto_T1_r3": 4242}
        buf = io.StringIO()
        with mock.patch.object(runs.host, "loop_parents", return_value=live), \
             mock.patch.object(runs.host, "containers", return_value=[]), \
             mock.patch.object(runs.conduct.Conduct, "request_pause"), \
             mock.patch.object(Cell, "take_down"), \
             contextlib.redirect_stdout(buf):
            runs.conduct.Conduct().stop(mock.Mock(scope=["all"], yes=True))
        out = buf.getvalue()
        self.assertIn("WARNING", out)
        self.assertIn("MID-ATTEMPT", out)
        self.assertIn("haiku_high_beta_howto_T1_r3", out)

    def test_scoped_stop_leaves_conduct_running(self):
        (self.conduct / "conduct.pid").write_text("4242 cap=7")
        self.queue("haiku", [])
        sent = []
        with mock.patch.object(runs.os, "kill",
                               lambda pid, sig: sent.append((pid, sig))):
            self._stop(["haiku"])
        self.assertNotIn((4242, runs.signal.SIGTERM), sent,
                         "a scoped stop must not TERM the scheduler")
        self.assertTrue((self.conduct / "conduct.pid").exists())



class TestStopConductor(OperatorTestCase):
    """A SIGKILLed conduct never reaches its own pidfile.unlink(), so a stale
    file can name a pid the OS has since reused — TERMing that blind would
    hit an unrelated process."""

    def test_stale_pidfile_is_cleared_without_signalling(self):
        (self.conduct / "conduct.pid").write_text("999999 cap=7")

        real_kill = runs.os.kill

        def probe_dead(pid, sig):
            if sig == 0:
                raise ProcessLookupError
            raise AssertionError("signalled a dead pid")

        with mock.patch.object(runs.os, "kill", probe_dead):
            self.assertFalse(runs.conduct.Conduct().stop_conductor())
        self.assertFalse((self.conduct / "conduct.pid").exists())

    def test_live_conduct_is_termed_and_pidfile_cleared(self):
        (self.conduct / "conduct.pid").write_text("4242 cap=7")
        sent = []
        with mock.patch.object(runs.os, "kill",
                               lambda pid, sig: sent.append((pid, sig))):
            self.assertTrue(runs.conduct.Conduct().stop_conductor())
        self.assertIn((4242, runs.signal.SIGTERM), sent)
        self.assertFalse((self.conduct / "conduct.pid").exists())

    def test_garbage_pidfile_is_cleared(self):
        (self.conduct / "conduct.pid").write_text("not-a-pid")
        self.assertFalse(runs.conduct.Conduct().stop_conductor())
        self.assertFalse((self.conduct / "conduct.pid").exists())


class TestQueueParkUnpark(OperatorTestCase):

    def test_park_and_unpark_roundtrip(self):
        self.queue("sonnet", [dict(task="T1", variant="beta_apidocs", rep=1)])
        self.assertEqual(runs.queues.park_lane("sonnet"), "parked")
        self.assertFalse(runs.queues.lane_dir("sonnet").exists())
        self.assertTrue(runs.queues.lane_dir("sonnet", parked=True).is_dir())
        self.assertEqual(runs.queues.park_lane("sonnet"), "already")
        self.assertEqual(runs.queues.unpark_lane("sonnet"), "resumed")
        self.assertTrue(runs.queues.lane_dir("sonnet").is_dir())

    def test_unpark_refuses_to_clobber_a_conflicting_live_file(self):
        self.queue("sonnet", [dict(task="T1", variant="beta_apidocs", rep=1)])
        runs.queues.park_lane("sonnet")
        runs.queues.lane_dir("sonnet").mkdir(parents=True)   # hand-made live lane
        self.assertEqual(runs.queues.unpark_lane("sonnet"), "conflict")
        self.assertTrue(runs.queues.lane_dir("sonnet", parked=True).is_dir())

    def test_park_without_a_queue_reports_empty(self):
        self.assertEqual(runs.queues.park_lane("nosuch"), "empty")



class TestExactlyOneCellRule(OperatorTestCase):
    """A cell verb acts directly iff it matches exactly ONE cell (operator,
    2026-08-12). Anything wider must go through the conduct verbs — one
    spawner, one cap owner, no burst race."""

    def test_pause_refuses_a_multi_match(self):
        with self.assertRaises(SystemExit):
            runs.cli.pause(mock.Mock(selectors=["sonnet"], reason="manual"))

    def test_resume_refuses_a_multi_match(self):
        with self.assertRaises(SystemExit):
            runs.cli.resume(mock.Mock(selectors=["sonnet"], force=False))

    def test_stop_refuses_a_multi_match(self):
        with self.assertRaises(SystemExit):
            runs.cli.stop_cells(mock.Mock(selectors=["sonnet"], cancel=False,
                                      dry_run=False))

    def test_spawn_refuses_a_rep_list(self):
        with self.assertRaises(SystemExit):
            runs.cli.spawn(mock.Mock(agent="sonnet", variant="beta_apidocs", rep="2,3", task="T1",
                                 budget=10, fresh=False))

    def _spawn_one(self, image_ready=True, variant="beta_apidocs", run_up=False, ignore=False):
        from fae.cell import Cell
        from fae.conduct import Conduct
        args = mock.Mock(agent="sonnet", variant=variant, rep="1", task="T1", budget=10,
                         fresh=False, dangerously_ignore_slots=ignore)
        out = io.StringIO()
        with mock.patch.object(Cell, "ready_image", return_value=image_ready) as ready, \
             mock.patch.object(Cell, "prepare"), \
             mock.patch.object(Conduct, "pid", return_value=4242 if run_up else None), \
             mock.patch.object(runs.conduct.Conduct, "_spawn", return_value=None) as spawned, \
             mock.patch.object(Cell, "prestart_clean"), \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             contextlib.redirect_stdout(out):
            try:
                runs.cli.spawn(args)
            except SystemExit as e:
                out.write(str(e))
        return ready, spawned, out.getvalue()

    def test_spawn_readies_the_agent_image_before_the_cell_starts(self):
        ready, spawned, _ = self._spawn_one(True)
        ready.assert_called_once()
        spawned.assert_called_once()

    def test_spawn_refuses_when_the_agent_image_cannot_be_built(self):
        # a cell whose agent image is missing charges every attempt to the agent
        _, spawned, _ = self._spawn_one(False)
        spawned.assert_not_called()

    def test_a_manual_spawn_runs_without_slots(self):
        from fae.cell import Cell
        _, spawned, _ = self._spawn_one()
        env = spawned.call_args.args[1]
        self.assertEqual(env.get(Cell.IGNORE_SLOTS_ENV), "1")
        self.assertNotIn(Cell.SLOT_FDS_ENV, env)

    def test_spawn_refuses_while_the_run_is_up(self):
        _, spawned, out = self._spawn_one(run_up=True)
        spawned.assert_not_called()
        self.assertIn("--dangerously-ignore-slots", out)

    def test_spawn_while_the_run_is_up_needs_the_flag(self):
        _, spawned, _ = self._spawn_one(run_up=True, ignore=True)
        spawned.assert_called_once()

    def test_a_variant_with_a_lock_warns_about_its_shared_resource(self):
        _, spawned, out = self._spawn_one(variant="alpha_apidocs")
        spawned.assert_called_once()
        self.assertIn("WARNING: alpha_apidocs uses the shared resource 'alpha'", out)


class TestStopCells(OperatorTestCase):
    """stop = hard halt, RESUMABLE (PAUSED·stopped): no .cancelled unless
    --cancel, which is the terminal verdict."""

    def _stop(self, cid, cancel=False, live=None):
        live = live or {}
        with mock.patch.object(runs.host, "loop_parents", return_value=live), \
             mock.patch.object(Cell, "loop_pid", lambda c: live.get(c.cid)), \
             mock.patch.object(Cell, "_gone", staticmethod(lambda pid, grace: True)), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=[]), \
             mock.patch.object(runs.host, "cell_state",
                               return_value=dict(cid=cid, state="RUNNING",
                                                 why="agent",
                                                 variant="beta_apidocs")), \
             mock.patch.object(runs.subprocess, "run"), \
             mock.patch.object(runs.os, "kill"):
            runs.cli.stop_cells(mock.Mock(selectors=[cid], cancel=cancel,
                                      dry_run=False))
            runs.conduct.Conduct().act_on_requests()

    def test_default_stop_is_resumable(self):
        cid = CIDS[0]
        self._stop(cid)
        self.assertFalse((self.ws / cid / ".cancelled").exists(),
                         "a plain stop must not write the terminal verdict")
        self.assertTrue((self.ws / cid / ".paused").read_text()
                        .startswith("stopped"))

    def test_cancel_writes_the_terminal_verdict(self):
        cid = CIDS[0]
        self._stop(cid, cancel=True)
        self.assertTrue((self.ws / cid / ".cancelled").exists())
        self.assertTrue((self.ws / cid / ".paused").read_text()
                        .startswith("killed"))

    def test_stop_scrubs_the_cells_queued_spec_with_backup(self):
        cid = CIDS[0]
        m, v, task, rep = runs.parse_cell_id(cid)
        self.queue("sonnet", [dict(task=task, variant=v,
                                   rep=int(rep))])
        self._stop(cid)
        self.assertEqual(self.pending("sonnet"), [],
                         "the stopped cell's spec survived in queue")
        self.assertEqual(len(list((self.queues / "backups").glob("stopped-*.json"))), 1)

    def test_stop_kills_the_loops_whole_session_and_tears_the_bring_up_down(self):
        """The verify container dies with the kill, but what the arrangement
        provisioned outside it (a per-verify cluster, a daemon's leases) does
        not: the variant's verify_teardown runs for the cell's last
        arrangement, in a fresh container of the same image."""
        from fae.cell import verify as _verify
        cid = CIDS[0]
        (self.ws / cid / "artifacts").mkdir(parents=True, exist_ok=True)
        groups, torn = [], []
        with mock.patch.object(runs.host, "loop_parents", return_value={cid: 4242}), \
             mock.patch.object(Cell, "loop_pid", lambda c: 4242), \
             mock.patch.object(Cell, "_gone", staticmethod(lambda pid, grace: False)), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=[]), \
             mock.patch.object(runs.host, "cell_state",
                               return_value=dict(cid=cid, state="RUNNING",
                                                 why="agent", variant="beta_apidocs")), \
             mock.patch.object(runs.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(_verify, "run_teardown",
                               side_effect=lambda ctx, infra, **kw: torn.append((ctx, infra))), \
             mock.patch.object(runs.os, "getpgid", return_value=7777), \
             mock.patch.object(runs.os, "killpg",
                               side_effect=lambda pg, sig: groups.append((pg, sig))), \
             mock.patch.object(runs.os, "kill"):
            runs.cli.stop_cells(mock.Mock(selectors=[cid], cancel=False, dry_run=False))
            runs.conduct.Conduct().act_on_requests()
        self.assertEqual(groups, [(7777, runs.signal.SIGKILL)])
        self.assertEqual(len(torn), 1)
        ctx, infra = torn[0]
        self.assertEqual((ctx.cid, ctx.variant, ctx.artifacts, ctx.out),
                         (cid, "beta_apidocs", str(self.ws / cid / "artifacts"),
                          str(self.ws / cid / ".verify-out")))
        self.assertEqual(infra.variant.ID, "beta_apidocs")
        self.assertIsInstance(infra, infra.variant.INFRA)

    def test_plain_stop_of_a_live_loop_emits_crash_not_kill(self):
        """Trace conformance: Pause leaves the model's loop alive; the SIGKILL
        must land as Crash(c) or the eventual resume Spawn replays as a
        violation. Kill is reserved for --cancel."""
        cid = CIDS[0]
        self._stop(cid, live={cid: 4242})
        log = (self.plane / "transitions.log").read_text()
        self.assertIn("Crash", log)
        self.assertNotIn("Kill", log)


class TestConductResume(OperatorTestCase):
    """Bulk resume never spawns: it lifts locks and requeues at the FRONT;
    conduct admits under its caps. Standing operator decisions survive a
    blanket resume (the 2026-07-24 resurrection guard)."""

    def _paused(self, cid, reason):
        (self.ws / cid / ".paused").write_text(f"{reason} by=operator\n")

    def _st(self, cid, state="CRASHED", why="loop"):
        m, v, task, rep = runs.parse_cell_id(cid)
        return dict(cid=cid, state=state, why=why, agent=m, variant=v,
                    task=task, rep=rep, budget=10)

    def _resume(self, scope, live=None, states=None):
        """Cells default to DONE (no requeue); a test names the ones it wants
        interrupted via `states`. experiment resume requeues EVERY loop-less
        non-terminal cell — crashed included, it is the bulk human act — so
        an all-CRASHED default would requeue the whole fixture."""
        st_map = states or {}

        def _cs(ws, loops, boxes):
            cid = Path(ws).name
            return st_map.get(cid, self._st(cid, state="DONE", why="green"))

        with mock.patch.object(runs.host, "loop_parents", return_value=live or {}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=[]), \
             mock.patch.object(runs.host, "cell_state", side_effect=_cs):
            runs.conduct.Conduct().resume(mock.Mock(scope=scope))

    def test_no_loop_is_ever_spawned(self):
        for c in CIDS:
            self._paused(c, "drain")
        self._resume(["all"])
        self.assertEqual(self.spawned, [],
                         "experiment resume spawned a loop — that is conduct's job")

    def test_drain_pause_is_lifted_and_requeued_at_front(self):
        cid = CIDS[0]
        self._paused(cid, "drain")
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        self._resume(["all"], states={cid: self._st(cid)})
        self.assertFalse((self.ws / cid / ".paused").exists())
        rows = self.pending("sonnet")
        self.assertEqual(rows[0]["variant"], "beta_apidocs",
                         "the resumed cell's spec must go to the FRONT")
        self.assertEqual(len(rows), 2)

    def test_roster_pause_survives_a_blanket_resume(self):
        cid = CIDS[0]
        self._paused(cid, "roster")
        self._resume(["all"])
        self.assertTrue((self.ws / cid / ".paused").exists(),
                        "experiment resume all resurrected a standing roster pause")

    def test_naming_the_model_lifts_a_roster_pause(self):
        cid = CIDS[0]
        self._paused(cid, "roster")
        self._resume(["sonnet"])
        self.assertFalse((self.ws / cid / ".paused").exists())

    def test_naming_the_model_keeps_the_drivers_contract_stand_down(self):
        cid = CIDS[0]
        (self.ws / cid / ".paused").write_text(
            "contract by=driver at=2026-08-29T06:45:23Z\nteardown: the ledger survived\n")
        self._resume(["sonnet"])
        self.assertTrue((self.ws / cid / ".paused").exists(),
                        "experiment resume sonnet resurrected a contract stand-down")

    def test_requeue_deduplicates_against_an_existing_spec(self):
        cid = CIDS[0]
        m, v, task, rep = runs.parse_cell_id(cid)
        self._paused(cid, "drain")
        self.queue("sonnet", [dict(task=task, variant=v,
                                   rep=int(rep))])
        self._resume(["all"], states={cid: self._st(cid)})
        rows = self.pending("sonnet")
        self.assertEqual(len(rows), 1, "the spec was doubled")

    def test_cancelled_cells_stay_dead(self):
        cid = CIDS[0]
        self._paused(cid, "killed")
        (self.ws / cid / ".cancelled").write_text("killed by=operator\n")
        self._resume(["all"])
        self.assertTrue((self.ws / cid / ".paused").exists())
        self.assertFalse(self.pending("sonnet"),
                         "a cancelled cell was requeued")

    def test_live_loops_are_not_requeued(self):
        cid = CIDS[0]
        self._paused(cid, "drain")
        self._resume(["all"], live={cid: 4242},
                     states={cid: self._st(cid, state="RUNNING", why="agent")})
        self.assertFalse(self.pending("sonnet"),
                         "a cell with a live loop was requeued")

    def test_parked_lanes_are_unparked(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        runs.queues.park_lane("sonnet")
        self._resume(["all"])
        self.assertTrue(runs.queues.lane_dir("sonnet").is_dir())
        self.assertFalse(runs.queues.lane_dir("sonnet", parked=True).exists())



class TestParseResetHint(unittest.TestCase):
    def test_seconds_minutes_hours(self):
        self.assertEqual(runs.supervise._parse_reset_hint("retry in 65s"), 65)
        self.assertEqual(runs.supervise._parse_reset_hint("try again in 5 minutes"), 300)
        self.assertEqual(runs.supervise._parse_reset_hint("resets after 2 hours"), 7200)

    def test_no_hint_returns_none(self):
        self.assertIsNone(runs.supervise._parse_reset_hint(
            "Error: Individual quota reached. Please upgrade your subscription"))

    def test_day_scale_hint(self):
        self.assertEqual(runs.supervise._parse_reset_hint(
            "Weekly usage limit reached. Resets in 3 days."), 3 * 86400)

    def test_clock_time_hint(self):
        """The Claude Code CLI's own wording: no 'in'/'after', a clock time
        instead of a duration, always UTC."""
        with mock.patch.object(runs.supervise, "datetime") as m:
            m.now.return_value = datetime(2026, 8, 15, 19, 45, tzinfo=timezone.utc)
            hint = runs.supervise._parse_reset_hint(
                "You've hit your session limit · resets 7:40pm (UTC)")
        # 7:40pm has passed for a 19:45 "now" -> rolls to tomorrow, 23h55m out
        self.assertEqual(hint, 23 * 3600 + 55 * 60)

    def test_clock_time_hint_still_ahead_today(self):
        with mock.patch.object(runs.supervise, "datetime") as m:
            m.now.return_value = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)
            hint = runs.supervise._parse_reset_hint(
                "You've hit your session limit · resets 7:40pm (UTC)")
        self.assertEqual(hint, 5 * 3600 + 40 * 60)


class TestWaitReasonReadsTheNewestSnapshot(unittest.TestCase):
    """wait_reason's text becomes the lane cooldown's reset hint, so reading a
    stale snapshot parks the lane on a wall that already expired. The filename
    sorts wrong twice: the attempt number is lexical, and the wait suffix is a
    bare HHMMSS that wraps at midnight.
    (2026-08-16: sonnet parked 19h on attempt 6's message, read during
    attempt 10; fable parked 9h on the previous day's.)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _snap(self, name, result, mtime):
        p = self.ws / name
        p.write_text(json.dumps({"type": "result", "result": result}) + "\n")
        os.utime(p, (mtime, mtime))
        return p

    def test_a_higher_attempt_number_wins_over_lexical_order(self):
        self._snap("agent.attempt-6.wait-100614.log", "stale: resets 10:40am", 1000)
        self._snap("agent.attempt-10.wait-133400.log", "current: resets 3:40pm", 2000)
        self.assertIn("current", runs.experiment.current().cell(self.ws.name, workspaces=self.ws.parent).wait_reason())

    def test_a_suffix_that_wrapped_midnight_does_not_win(self):
        self._snap("agent.attempt-4.wait-220929.log", "stale: resets 12:40am", 1000)
        self._snap("agent.attempt-4.wait-100607.log", "current: resets 10:40am", 2000)
        self.assertIn("current", runs.experiment.current().cell(self.ws.name, workspaces=self.ws.parent).wait_reason())

    def test_no_snapshots_is_empty(self):
        self.assertEqual(runs.experiment.current().cell(self.ws.name, workspaces=self.ws.parent).wait_reason(), "")


class TestLimitWall(OperatorTestCase):
    """A quota-walled cell holds its arm lock and work slot while burning
    nothing. Supervision stands it down (cooperative pause), requeues it,
    and cools the lane until the provider's hint or the default."""

    def _claim(self, cid):
        """Put the cell under conduct's management, as admission would."""
        d = runs.queues.rundir(cid.split("_", 1)[0])
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.json").write_text(json.dumps(
            {"task": "T1", "variant": "beta_apidocs",
             "rep": 1, "budget": 10, "fresh": False}) + "\n")

    def _sweep(self, cid, detail="Error: Individual quota reached.", dry=False):
        (self.ws / cid / "iterations.log").touch()
        st = dict(cid=cid, state="WAITING", why="limit", detail=detail,
                  agent="sonnet", variant="beta_apidocs",
                  task="T1", rep="1", budget=10)

        def _cs(ws, loops, boxes):
            c = Path(ws).name
            return st if c == cid else None

        with mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "loop_parents", return_value={cid: 4242}), \
             mock.patch.object(runs.host, "cell_state", side_effect=_cs):
            runs.supervise.supervise_pass(runs.supervise.Alerts(), dry=dry)

    def test_walled_cell_is_stood_down_and_lane_cooled(self):
        cid = CIDS[0]
        self._claim(cid)
        self._sweep(cid)
        self.assertEqual(runs.experiment.current().cell(cid).pause_reason, "limit-wall")
        self.assertEqual([runs.queues.spec_cid(p) for p in runs.queues.running_specs("sonnet")],
                         [cid], "the spec stays claimed for the lane's retry")
        until = runs.queues.cooldown_until("sonnet")
        import time as _t
        self.assertGreater(until, _t.time() + 3600, "default cooldown ~3h")

    def test_provider_hint_sets_the_cooldown(self):
        cid = CIDS[0]
        self._sweep(cid, detail="rate limited, retry in 90s")
        import time as _t
        until = runs.queues.cooldown_until("sonnet")
        self.assertLess(abs(until - _t.time() - 90), 10)

    def test_dry_run_writes_nothing(self):
        cid = CIDS[0]
        self._sweep(cid, dry=True)
        self.assertIsNone(runs.experiment.current().cell(cid).pause_reason)
        self.assertEqual(runs.queues.cooldown_until("sonnet"), 0)
        self.assertEqual(self.pending("sonnet"), [])
        self.assertEqual(runs.queues.running_specs("sonnet"), [])

    def test_transient_connection_fault_is_not_a_wall(self):
        """The `limit` phase also covers retryable API faults; standing a
        lane down hours for a ConnectionRefused idles a healthy agent."""
        cid = CIDS[0]
        for detail in ("API Error: Unable to connect to API (ConnectionRefused)",
                       "API Error: Connection closed mid-response. The response"):
            self._sweep(cid, detail=detail)
            self.assertIsNone(runs.experiment.current().cell(cid).pause_reason, detail)
            self.assertEqual(runs.queues.cooldown_until("sonnet"), 0, detail)

    def test_wall_inside_a_stuck_agent_cools_the_lane(self):
        """opencode logs its weekly limit and then retries INSIDE the process
        for days: no output growth, no exit. The hang branch must read the
        wall out of the attempt log instead of charging repair budget."""
        import time as _t
        cid = CIDS[0]
        self._claim(cid)
        ws = self.ws / cid
        (ws / "iterations.log").touch()
        (ws / "agent.attempt-1.log").write_text(
            "level=ERROR message=\"stream error\" error.error=\"AI_APICallError: "
            "Weekly usage limit reached. Resets in 3 days.\"\n")
        old = _t.time() - 9000
        os.utime(ws / "iterations.log", (old, old))
        os.utime(ws / "agent.attempt-1.log", (old, old))
        st = dict(cid=cid, state="RUNNING", why="agent", detail="",
                  agent="sonnet", variant="beta_apidocs",
                  task="T1", rep="1", budget=10)
        with mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "containers",
                               return_value={f"fae-agent-{cid}"}), \
             mock.patch.object(runs.host, "cell_state",
                               side_effect=lambda w, l, b:
                               st if Path(w).name == cid else None), \
             mock.patch.object(runs.host, "heartbeat",
                               return_value={"age": 1e9}), \
             mock.patch.object(runs.subprocess, "run"):
            runs.supervise.supervise_pass(runs.supervise.Alerts(), dry=False)
        until = runs.queues.cooldown_until("sonnet")
        self.assertGreater(until, _t.time() + 2 * 86400, "no day-scale cooldown")
        self.assertEqual([runs.queues.spec_cid(p) for p in runs.queues.running_specs("sonnet")],
                         [cid], "the spec stays claimed while the lane cools")
        self.assertFalse((ws / "reconcile.flagged").exists())

    def test_quota_wall_detector(self):
        self.assertTrue(runs.supervise._is_quota_wall("Individual quota reached."))
        self.assertTrue(runs.supervise._is_quota_wall("429 Too Many Requests"))
        self.assertTrue(runs.supervise._is_quota_wall("rate limited, retry in 90s"))
        self.assertFalse(runs.supervise._is_quota_wall("ConnectionRefused"))
        self.assertFalse(runs.supervise._is_quota_wall("Connection closed mid-response"))

    def test_quota_wall_detector_matches_the_claude_cli_wording(self):
        """Matches none of the other branches: 'hit your ... limit' is not
        'limit reached/exceeded', and 'resets 7:40pm' has no 'in'/'at' for
        the relative-duration branch. A claude-backed lane retried this in
        cell forever, never cooling, until this pattern was added."""
        self.assertTrue(runs.supervise._is_quota_wall(
            "You've hit your session limit · resets 7:40pm (UTC)"))
        self.assertTrue(runs.supervise._is_quota_wall(
            "You've hit your usage limit · resets 2:40pm (UTC)"))
        self.assertFalse(runs.supervise._is_quota_wall("Connection refused"))


class TestLaneWrites(OperatorTestCase):
    """Where a spec lands matters as much as that it lands: writing into a
    parked lane's live directory would resurrect admission from a lane the
    operator paused."""

    def spec(self, rep=1):
        return dict(task="T1", variant="beta_apidocs",
                    rep=rep, budget=10, fresh=False)

    def test_enqueue_into_a_parked_lane_stays_parked(self):
        self.queue("sonnet", [self.spec(rep=5)])
        runs.queues.park_lane("sonnet")
        runs.queues.enqueue("sonnet", self.spec(rep=6))
        self.assertFalse(runs.queues.lane_dir("sonnet").exists(),
                         "a live lane reappeared beside the parked one")
        self.assertEqual(len(self.parked_pending("sonnet")), 2)
        self.assertEqual(runs.queues.lane_specs("sonnet"), [],
                         "a parked lane offers nothing to admission")

    def test_dedupe_sees_a_claimed_spec(self):
        self.queue("sonnet", [self.spec()])
        runs.queues.claim("sonnet", runs.queues.lane_specs("sonnet")[0])
        self.assertIsNone(runs.queues.enqueue("sonnet", self.spec()),
                          "a running cell must not be queued underneath itself")

    def test_dedupe_sees_a_parked_spec(self):
        self.queue("sonnet", [self.spec()])
        runs.queues.park_lane("sonnet")
        self.assertIsNone(runs.queues.enqueue("sonnet", self.spec()))

    def test_unpark_reports_a_lane_that_was_never_parked(self):
        self.assertEqual(runs.queues.unpark_lane("sonnet"), "not-paused")


class TestOneCellGoesThroughTheRun(OperatorTestCase):
    """`cell pause` and `cell stop` queue a request; the run acts on it, or
    the CLI does when no run is up."""

    def test_a_pause_is_queued_then_acted_on(self):
        cid = CIDS[0]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runs.cli.pause(mock.Mock(selectors=[cid], reason="roster")), cid)
            self.assertFalse((self.ws / cid / ".paused").exists())
            self.assertEqual([r["verb"] for _, r in runs.queues.requests()], ["pause"])
            runs.conduct.Conduct().act_on_requests()
        self.assertTrue((self.ws / cid / ".paused").read_text().startswith("roster by=operator"))
        self.assertEqual(runs.queues.requests(), [])

    def test_while_the_run_is_up_the_request_waits_for_it(self):
        cid = CIDS[0]
        with contextlib.redirect_stdout(io.StringIO()):
            runs.cli.pause(mock.Mock(selectors=[cid], reason="manual"))
            with mock.patch.object(runs.conduct.Conduct, "pid", return_value=os.getpid() + 1):
                runs.conduct.Conduct().act_on_requests()
        self.assertFalse((self.ws / cid / ".paused").exists())
        self.assertEqual(len(runs.queues.requests()), 1)


class TestStopRemovesTheClaim(OperatorTestCase):
    """A stopped cell whose CLAIM survived would be restarted by the next
    converge — the same resurrection the queued-spec scrub prevents."""

    def test_stopping_a_running_cell_shelves_its_claim(self):
        cid = CIDS[0]
        self.queue("sonnet", [dict(task="T1", variant="beta_apidocs", rep=1, budget=10,
                                   fresh=False)])
        runs.queues.claim("sonnet", runs.queues.lane_specs("sonnet")[0])
        with mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state",
                               return_value=dict(cid=cid, state="RUNNING",
                                                 why="agent",
                                                 variant="beta_apidocs")), \
             mock.patch.object(runs.subprocess, "run"), \
             mock.patch.object(runs.os, "kill"):
            runs.cli.stop_cells(mock.Mock(selectors=[cid], cancel=False,
                                      dry_run=False))
            runs.conduct.Conduct().act_on_requests()
        self.assertEqual(runs.queues.running_specs("sonnet"), [],
                         "the claim survived a stop")
        self.assertEqual(len(list((self.queues / "backups").glob("stopped-*.json"))), 1)


class TestConductPause(OperatorTestCase):

    def test_partial_parks_and_pauses(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        with mock.patch.object(runs.conduct.Conduct, "request_pause") as rp:
            runs.conduct.Conduct().pause(mock.Mock(scope=["sonnet"], admission_only=False,
                                         dry_run=False, interval=1))
        self.assertTrue(runs.queues.lane_dir("sonnet", parked=True).is_dir())
        paused = rp.call_args[0][0]
        self.assertTrue(paused and
                        all(c.startswith("sonnet_") for c in paused), paused)

    def test_admission_only_leaves_running_cells_alone(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        with mock.patch.object(runs.conduct.Conduct, "request_pause") as rp:
            runs.conduct.Conduct().pause(mock.Mock(scope=["sonnet"], admission_only=True,
                                         dry_run=False, interval=1))
        self.assertTrue(runs.queues.lane_dir("sonnet", parked=True).is_dir())
        rp.assert_not_called()

    def test_admission_only_is_rejected_fleet_wide(self):
        with self.assertRaises(SystemExit):
            runs.conduct.Conduct().pause(mock.Mock(scope=["all"], admission_only=True,
                                         dry_run=False, interval=1))


class TestResumeRespectsPerModelCap(OperatorTestCase):
    """Recovery must not out-run the scheduler: conduct admits 1 live cell
    per agent, so a resume that respawns a second one runs the agent 2-wide.
    Locks are still lifted; only the RESPAWN defers. --force pushes past."""

    def _resume(self, cid, force=False, live=None):
        args = mock.Mock(selectors=[cid], force=force)
        with mock.patch.object(runs.host, "loop_parents", return_value=live or {}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=[]), \
             mock.patch.object(runs.host, "cell_state",
                               return_value=dict(cid=cid, state="CRASHED",
                                                 why="loop")), \
             mock.patch.object(Cell, "refresh_creds"), \
             mock.patch.object(runs.conduct.Conduct, "respawn") as rs:
            runs.cli.resume(args)
        return rs

    def test_respawn_deferred_when_model_at_cap(self):
        cid = CIDS[0]                       # sonnet cell
        other = "sonnet_high_beta_apidocs_T1_r9"
        (self.ws / cid / ".paused").write_text("manual by=operator\n")
        rs = self._resume(cid, live={other: 4242})
        rs.assert_not_called()
        self.assertFalse((self.ws / cid / ".paused").exists(),
                         "the pause lock must be lifted even when deferring")

    def test_force_respawns_past_the_cap(self):
        cid = CIDS[0]
        other = "sonnet_high_beta_apidocs_T1_r9"
        rs = self._resume(cid, force=True, live={other: 4242})
        rs.assert_called_once()

    def test_other_models_loops_do_not_count(self):
        cid = CIDS[0]
        rs = self._resume(cid, live={"haiku_high_beta_apidocs_T1_r1": 7})
        rs.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestPendingKind(OperatorTestCase):
    """The status breakdown is derived, never stored: each kind comes from a
    different source of truth (workspace markers, ledger, live processes)."""

    def kind(self, cid, live=()):
        return runs.render._pending_kind(cid, set(live))

    def test_no_workspace_is_fresh(self):
        self.assertEqual(self.kind("sonnet_high_beta_apidocs_T1_r99"),
                         "fresh")

    def test_flagged_beats_everything(self):
        cid = CIDS[0]
        (self.ws / cid / "reconcile.flagged").touch()
        (self.ws / cid / ".paused").write_text("manual by=operator\n")
        self.assertEqual(self.kind(cid, [cid]), "flagged")

    def test_paused_beats_running(self):
        cid = CIDS[0]
        (self.ws / cid / ".paused").write_text("manual by=operator\n")
        self.assertEqual(self.kind(cid, [cid]), "paused")

    def test_a_live_loop_is_running(self):
        self.assertEqual(self.kind(CIDS[0], [CIDS[0]]), "running")

    def test_terminal_is_done(self):
        cid = CIDS[0]
        with mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "DONE",
                                             "why": "green"}):
            self.assertEqual(self.kind(cid), "done")

    def test_seeded_but_never_launched_is_prepared(self):
        cid = CIDS[0]
        with mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "CRASHED",
                                             "why": "loop"}), \
             mock.patch.object(runs.Cell, "never_started", return_value=True):
            self.assertEqual(self.kind(cid), "prepared")

    def test_a_stopped_cell_is_interrupted(self):
        cid = CIDS[0]
        with mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "CRASHED",
                                             "why": "loop"}), \
             mock.patch.object(runs.Cell, "never_started", return_value=False):
            self.assertEqual(self.kind(cid), "interrupted")


class TestQueuedSummaryDisplay(OperatorTestCase):
    def test_a_cooling_lane_carries_its_limit_tag(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        runs.queues.cooldown_file("sonnet").write_text(
            f"{int(runs.time.time()) + 9999} quota\n")
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            row = next(l for l in runs.render.queued_summary() if "sonnet" in l)
        self.assertIn("[LIMIT until", row)

    def test_an_empty_lane_directory_is_not_a_row(self):
        runs.queues.lane_dir("sonnet").mkdir(parents=True)
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            self.assertEqual(runs.render.queued_summary(), [])

    def test_an_unreadable_head_spec_drops_the_row_not_the_count(self):
        d = runs.queues.lane_dir("sonnet"); d.mkdir(parents=True)
        (d / "100000.sonnet_high_beta_apidocs_T1_r9.json").write_text("{bad\n")
        with mock.patch.object(runs.host, "loop_parents", return_value={}):
            self.assertEqual(runs.render.queued_summary(), [])


class TestConductPauseDryRun(OperatorTestCase):
    """--dry-run must describe the window without opening it."""

    def _dry(self, scope, admission_only=False, live=None):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(runs.host, "loop_parents", return_value=live or {}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.conduct.Conduct, "request_pause") as rp, \
             mock.patch.object(runs.conduct.Conduct, "stop_conductor") as sc:
            runs.conduct.Conduct().pause(mock.Mock(scope=scope, admission_only=admission_only,
                                         dry_run=True, interval=1))
        return out.getvalue(), rp, sc

    def test_partial_preview_touches_nothing(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        out, rp, sc = self._dry(["sonnet"], live={CIDS[0]: 4242})
        self.assertIn("would pause", out)
        self.assertIn("would park queue[sonnet]", out)
        self.assertIn("would leave conduct running", out)
        rp.assert_not_called()
        sc.assert_not_called()
        self.assertTrue(runs.queues.lane_dir("sonnet").is_dir(), "the lane was parked")

    def test_partial_preview_says_when_cells_are_left_alone(self):
        out, _, _ = self._dry(["sonnet"], admission_only=True)
        self.assertIn("--admission-only", out)

    def test_an_already_parked_lane_is_not_offered_again(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        runs.queues.park_lane("sonnet")
        out, _, _ = self._dry(["sonnet"])
        self.assertNotIn("would park", out)

    def test_full_preview_names_conduct_and_the_backlog(self):
        (self.conduct / "conduct.pid").write_text("4242 cap=7")
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        out, rp, sc = self._dry(["all"], live={CIDS[0]: 4242})
        self.assertIn("would stop conduct", out)
        self.assertIn("queued spec(s) in place", out)
        rp.assert_not_called()
        sc.assert_not_called()

    def test_full_preview_counts_only_specs_with_no_workspace(self):
        (self.conduct / "conduct.pid").write_text("4242 cap=7")
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7),
                              dict(task="T1", variant="beta_apidocs", rep=1)])
        out, _, _ = self._dry(["all"], live={CIDS[0]: 4242})
        self.assertIn("would leave 1 queued spec(s) in place", out)

    def test_unknown_model_is_refused(self):
        with self.assertRaises(SystemExit):
            self._dry(["nosuchmodel"])

    def test_admission_only_is_rejected_fleet_wide(self):
        with self.assertRaises(SystemExit):
            self._dry(["all"], admission_only=True)


class TestConductPauseFullWindow(OperatorTestCase):
    """`experiment pause all` is the FP-edit window: conduct stops FIRST, then
    every cell is asked to stop, then it waits for zero loops."""

    def test_stops_conduct_pauses_all_and_waits_for_quiet(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(runs.conduct.Conduct, "stop_conductor", return_value=True) as sc, \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "sh", return_value=""), \
             mock.patch.object(runs.conduct.Conduct, "request_pause") as rp:
            runs.conduct.Conduct().pause(mock.Mock(scope=["all"], admission_only=False,
                                         dry_run=False, interval=1))
        sc.assert_called_once()
        self.assertEqual(rp.call_args[0][1], "drain")
        self.assertIn("DRAIN COMPLETE", out.getvalue())

    def test_waits_while_an_fp_pinned_verifier_runs(self):
        """A reverify holds a pinned fingerprint even with no cell loop; the
        window is not open until it too is gone."""
        import io, contextlib
        seen = ["python3 /x/cli.py cell reverify cell", ""]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(runs.conduct.Conduct, "stop_conductor", return_value=False), \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "sh", side_effect=lambda *a, **k: seen.pop(0) if seen else ""), \
             mock.patch.object(runs.time, "sleep", lambda s: None), \
             mock.patch.object(runs.conduct.Conduct, "request_pause"):
            runs.conduct.Conduct().pause(mock.Mock(scope=["all"], admission_only=False,
                                         dry_run=False, interval=1))
        self.assertIn("FP-pinned process", out.getvalue())
        self.assertIn("DRAIN COMPLETE", out.getvalue())


class TestVerbEdges(OperatorTestCase):
    """The branches an operator hits when the target is not a plain live
    cell: previews, terminal cells, unknown lanes, standing decisions."""

    def out_of(self, fn, *a, **kw):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*a, **kw)
        return buf.getvalue()

    # --- cell stop -------------------------------------------------------
    def test_stop_says_so_when_nothing_matches(self):
        out = self.out_of(runs.cli.stop_cells,
                          mock.Mock(selectors=["nosuchcell"], cancel=False,
                                    dry_run=False))
        self.assertIn("no cells match", out)

    def test_stop_dry_run_previews_a_cell(self):
        cid = CIDS[0]
        with mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "RUNNING",
                                             "why": "agent"}):
            out = self.out_of(runs.cli.stop_cells,
                              mock.Mock(selectors=[cid], cancel=True,
                                        dry_run=True))
        self.assertIn("would cancel", out)
        self.assertFalse((self.ws / cid / ".paused").exists())

    def test_stop_dry_run_previews_a_queued_spec(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        with mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state", return_value=None):
            out = self.out_of(runs.cli.stop_cells,
                              mock.Mock(selectors=[runs.cell_id("sonnet", "alpha_apidocs", 7)],
                                        cancel=True, dry_run=True))
        self.assertIn("would drop queued spec", out)
        self.assertIn("Nothing done (--dry-run)", out)
        self.assertEqual(len(self.pending("sonnet")), 1, "preview shelved a spec")

    def test_stop_leaves_a_finished_cell_alone(self):
        cid = CIDS[0]
        with mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "DONE",
                                             "why": "green"}):
            out = self.out_of(runs.cli.stop_cells,
                              mock.Mock(selectors=[cid], cancel=False,
                                        dry_run=False))
        self.assertIn("already DONE", out)
        self.assertFalse((self.ws / cid / ".paused").exists())

    # --- cell resume -----------------------------------------------------
    def test_resume_lifts_a_lock_without_respawning_a_live_cell(self):
        cid = CIDS[0]
        (self.ws / cid / ".paused").write_text("manual by=operator\n")
        with mock.patch.object(runs.host, "loop_parents", return_value={cid: 4242}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "RUNNING",
                                             "why": "agent"}):
            out = self.out_of(runs.cli.resume, mock.Mock(selectors=[cid], force=False))
        self.assertIn("pause lifted (no respawn", out)
        self.assertFalse((self.ws / cid / ".paused").exists())
        self.assertEqual(self.spawned, [])

    def test_resume_does_not_soften_a_cancel(self):
        cid = CIDS[0]
        (self.ws / cid / ".paused").write_text("killed by=operator\n")
        with mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "DONE",
                                             "why": "cancelled"}):
            self.out_of(runs.cli.resume, mock.Mock(selectors=[cid], force=False))
        self.assertTrue((self.ws / cid / ".paused").exists(),
                        "a cancel must survive a named resume of a DONE cell")

    # --- experiment stop / resume ------------------------------------------
    def test_conduct_stop_refuses_an_unknown_lane(self):
        with self.assertRaises(SystemExit):
            runs.conduct.Conduct().stop(mock.Mock(scope=["nosuchmodel"]))

    def test_conduct_resume_refuses_an_unknown_lane(self):
        with self.assertRaises(SystemExit):
            runs.conduct.Conduct().resume(mock.Mock(scope=["nosuchmodel"]))

    def test_conduct_stop_terms_conduct_and_the_scope_loops(self):
        (self.conduct / "conduct.pid").write_text("4242 cap=7")
        killed = []
        with mock.patch.object(runs.host, "loop_parents",
                               return_value={CIDS[0]: 111, CIDS[3]: 222}), \
             mock.patch.object(runs.host, "containers",
                               return_value={f"fae-agent-{CIDS[0]}"}), \
             mock.patch.object(runs.conduct.Conduct, "request_pause") as pause, \
             mock.patch.object(runs.subprocess, "run") as sub, \
             mock.patch.object(runs.os, "kill",
                               side_effect=lambda p, s: killed.append((p, s))):
            out = self.out_of(runs.conduct.Conduct().stop, mock.Mock(scope=["all"]))
        self.assertIn("conduct stopped (TERM)", out)
        # Cells stand down cooperatively and exit through their own teardown
        # trap, which is the only path that releases infra in order. A
        # signal is the escalation, not the opening move.
        paused = {c for call in pause.call_args_list for c in call.args[0]}
        self.assertLessEqual({CIDS[0], CIDS[3]}, paused,
                             "both live loops must be stood down")
        self.assertIn((4242, runs.signal.SIGTERM), killed,
                      "the conductor itself is still TERMed")
        self.assertTrue(sub.called, "the scope's infra is torn down")

    def test_conduct_stop_survives_a_loop_that_already_exited(self):
        with mock.patch.object(runs.host, "loop_parents", return_value={CIDS[0]: 111}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.conduct.Conduct, "request_pause"), \
             mock.patch.object(runs.os, "kill", side_effect=ProcessLookupError):
            out = self.out_of(runs.conduct.Conduct().stop, mock.Mock(scope=["all"]))
        self.assertIn("stopped [all]", out)

    def test_conduct_resume_refuses_to_merge_a_conflicting_lane(self):
        self.queue("sonnet", [dict(task="T1", variant="alpha_apidocs", rep=7)])
        runs.queues.park_lane("sonnet")
        runs.queues.lane_dir("sonnet").mkdir(parents=True)
        with mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state", return_value=None):
            out = self.out_of(runs.conduct.Conduct().resume, mock.Mock(scope=["sonnet"]))
        self.assertIn("refusing to clobber", out)

    def test_conduct_resume_clears_a_flag_and_a_corrupt_budget_book(self):
        cid = CIDS[0]
        (self.ws / cid / "reconcile.flagged").touch()
        (self.ws / cid / ".paused").write_text("drain by=conduct\n")
        (self.conduct / "reconcile.respawns.json").write_text("{not json")
        with mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state",
                               return_value=dict(cid=cid, state="CRASHED",
                                                 why="loop", agent="sonnet",
                                                 variant="beta_apidocs", task="T1",
                                                 rep="1", budget=10)):
            out = self.out_of(runs.conduct.Conduct().resume, mock.Mock(scope=["sonnet"]))
        self.assertIn("flag cleared", out)
        self.assertFalse((self.ws / cid / "reconcile.flagged").exists())


class TestStandingStateSurvivesBulkVerbs(OperatorTestCase):
    """Operator decisions are not run state: a bulk stop must report the
    pauses it is leaving in place rather than quietly clearing them."""

    def test_conduct_stop_names_the_cells_that_stay_paused(self):
        import io, contextlib
        for c in CIDS[:2]:
            (self.ws / c / ".paused").write_text("roster by=operator\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch.object(runs.host, "loop_parents", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.conduct.Conduct, "request_pause"):
            runs.conduct.Conduct().stop(mock.Mock(scope=["all"]))
        out = buf.getvalue()
        self.assertIn("stay paused", out)
        self.assertIn("cli.py experiment resume all", out)

    def test_stopping_a_cell_whose_loop_already_exited(self):
        cid = CIDS[0]
        with mock.patch.object(runs.host, "loop_parents", return_value={cid: 111}), \
             mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "cell_state",
                               return_value={"cid": cid, "state": "RUNNING",
                                             "why": "agent",
                                             "variant": "beta_apidocs"}), \
             mock.patch.object(runs.subprocess, "run"), \
             mock.patch.object(runs.os, "kill", side_effect=ProcessLookupError):
            runs.cli.stop_cells(mock.Mock(selectors=[cid], cancel=False,
                                      dry_run=False))
            runs.conduct.Conduct().act_on_requests()
        self.assertTrue((self.ws / cid / ".paused").exists())


class TestResumeAnswersTheLedgerNotTheFile(OperatorTestCase):
    """A Resume the model admits must answer a ledger Pause. The .paused FILE
    is only the request: an operator can remove it by hand (starving the old
    file-existence test), and a raw file write that no driver ever honored
    means the model never left 'run' — either way, gating the Resume on the
    file makes the replay lie."""

    def _log(self, *lines):
        (self.plane / "transitions.log").write_text(
            "".join(f"2026-08-22T10:00:0{i}Z\t{a}\t{c}\t{x}\n"
                    for i, (a, c, x) in enumerate(lines)))

    def _resumes(self):
        f = self.plane / "transitions.log"
        return [l for l in f.read_text().splitlines()
                if l.split("\t")[1] == "Resume"] if f.exists() else []

    def test_resume_fires_on_a_ledger_pause_even_without_the_file(self):
        cid = CIDS[0]
        self._log(("Pause", cid, "reason=manual"))
        # no .paused file — the operator removed it by hand
        runs.experiment.current().cell(cid).unpause()
        self.assertEqual(len(self._resumes()), 1)

    def test_no_resume_when_the_ledger_never_paused(self):
        cid = CIDS[0]
        self._log(("Admit", cid, ""))
        (self.ws / cid / ".paused").write_text("raw file, never honored\n")
        runs.experiment.current().cell(cid).unpause()
        self.assertEqual(self._resumes(), [])
        self.assertFalse((self.ws / cid / ".paused").exists())

    def test_an_epoch_seeded_pause_counts(self):
        cid = CIDS[0]
        self._log(("EPOCH", cid, "outcome=none intent=paused loop=none"))
        runs.experiment.current().cell(cid).unpause()
        self.assertEqual(len(self._resumes()), 1)

    def test_a_ledger_resume_clears_the_pause(self):
        cid = CIDS[0]
        self._log(("Pause", cid, ""), ("Resume", cid, ""))
        runs.experiment.current().cell(cid).unpause()
        self.assertEqual(len(self._resumes()), 1)   # only the pre-existing one


class TestConductRefusesAnAlternativeRoot(OperatorTestCase):
    """The backlog lives in the global .queues, so a conduct pointed at another
    workspace root would drain the SCORED queue into it. The test root is
    spawn-by-hand only; the scheduler refuses it outright."""

    def test_refuses_before_touching_anything(self):
        with mock.patch.dict(os.environ, {"WORKSPACES_DIR": "/x/ws-test.nosync"}), \
             at_workspace(plane=self.conduct / "never"):
            rc = runs.conduct.Conduct().run(SimpleNamespace(limit=1))
        self.assertEqual(rc, 2)
        self.assertFalse((self.conduct / "never").exists())

    def test_the_scored_root_is_the_default(self):
        with mock.patch.dict(os.environ, {"WORKSPACES_DIR": str(runs.conduct._default_ws())}):
            self.assertTrue(runs.conduct._ws_is_default())
        env = {k: v for k, v in os.environ.items() if k != "WORKSPACES_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(runs.conduct._ws_is_default())


class TestTheWeeklyWallCoolsTheLane(OperatorTestCase):
    """The most common wall in the corpus (290 occurrences) matched neither
    the wall detector nor the hint parser, and wait_reason cut the hint off
    at 70 characters; a walled cell retried in-cell holding its arm."""

    def test_weekly_wording_is_a_wall(self):
        self.assertTrue(runs.supervise._is_quota_wall(
            "You've hit your weekly limit · resets 12am (UTC)"))
        self.assertTrue(runs.supervise._is_quota_wall(
            "Error: Individual quota reached. Please upgrade your subscription "
            "to increase your limits. Resets in 126h54m56s."))

    def test_compound_and_midnight_hints_parse(self):
        self.assertEqual(runs.supervise._parse_reset_hint("Resets in 126h54m56s."),
                         126 * 3600 + 54 * 60 + 56)
        with mock.patch.object(runs.supervise, "datetime") as m:
            m.now.return_value = datetime(2026, 8, 25, 20, 2, 17,
                                          tzinfo=timezone.utc)
            self.assertEqual(runs.supervise._parse_reset_hint(
                "You've hit your weekly limit · resets 12am (UTC)"),
                3 * 3600 + 57 * 60 + 43)

    def test_wait_reason_keeps_the_hint_at_the_end_of_a_long_message(self):
        ws = self.ws / CIDS[0]
        ws.mkdir(parents=True, exist_ok=True)
        long = ("Error: Individual quota reached. Please upgrade your "
                "subscription to increase your limits. Resets in 126h54m56s.")
        (ws / "agent.attempt-3.wait-120000.log").write_text(long + "\n")
        got = runs.experiment.current().cell(ws.name, workspaces=ws.parent).wait_reason()
        self.assertIn("126h54m56s", got)
        self.assertEqual(runs.supervise._parse_reset_hint(got), 126 * 3600 + 54 * 60 + 56)

    def test_a_weekly_walled_cell_is_stood_down_until_the_reset(self):
        cid = CIDS[0]
        st = dict(cid=cid, state="WAITING", why="limit",
                  detail="You've hit your weekly limit · resets 12am (UTC)",
                  agent="sonnet", variant="beta_apidocs",
                  task="T1", rep="1", budget=10)
        (self.ws / cid / "iterations.log").touch()
        with mock.patch.object(runs.host, "loop_pids", return_value={}), \
             mock.patch.object(runs.host, "containers", return_value=set()), \
             mock.patch.object(runs.host, "loop_parents", return_value={cid: 4242}), \
             mock.patch.object(runs.host, "cell_state",
                               side_effect=lambda w, l, b:
                               st if Path(w).name == cid else None):
            runs.supervise.supervise_pass(runs.supervise.Alerts(), dry=False)
        self.assertEqual(runs.experiment.current().cell(cid).pause_reason, "limit-wall")
        until = runs.queues.cooldown_until("sonnet")
        import time as _t
        self.assertGreater(until, _t.time())
        self.assertLess(until, _t.time() + 86400 + 60, "next midnight UTC")
