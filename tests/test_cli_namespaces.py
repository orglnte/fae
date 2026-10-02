"""cli.py builds each fae/driver/*.py namespace by hand, so a misspelled field fails
only at invocation — and only on the branch that reads it. These tests invoke
every mutating command with the target function mocked and assert the captured
namespace carries exactly the fields the real function reads, with the values
the flags imply. Nothing here starts a process.
"""
import importlib.util as _ilu
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

try:
    from typer.testing import CliRunner
except ImportError:                                    # pragma: no cover
    raise unittest.SkipTest("typer not installed")

_spec = _ilu.spec_from_file_location("cli", Path(ROOT) / "fae" / "cli.py")
cli = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(cli)

runner = CliRunner()


def invoke(fn_name, argv, mod):
    """Run one cli command with <mod>.<fn_name> mocked; return its call.

    `mod` names the fae/driver/ submodule that owns fn_name (e.g. `cli.ops` for
    the verbs fae/driver/ops.py holds) — every verb lives in fae/driver/ now, so
    there is no bare-runs fallback to default to."""
    with mock.patch.object(mod, fn_name) as m:
        result = runner.invoke(cli.app, argv)
    if result.exit_code != 0:
        raise AssertionError(f"{argv} exited {result.exit_code}: {result.output}")
    if not m.call_args:
        raise AssertionError(f"{argv} never called {fn_name}")
    return m.call_args


class TestCellCommands(unittest.TestCase):
    def test_spawn(self):
        (ns,), _ = invoke("spawn", ["cell", "spawn", "sonnet", "beta",
                                    "apidocs", "-r", "2", "--fresh"], mod=cli.ops)
        self.assertEqual(vars(ns), dict(model="sonnet", treatment="beta",
                                        condition="apidocs", rep="2", task="T1",
                                        fresh=True))

    def test_pause_takes_several_selectors(self):
        (ns,), _ = invoke("pause", ["cell", "pause", "sonnet", "haiku",
                                    "--reason", "roster"], mod=cli.ops)
        self.assertEqual(vars(ns), dict(selectors=["sonnet", "haiku"],
                                        reason="roster"))

    def test_resume(self):
        (ns,), _ = invoke("resume", ["cell", "resume", "haiku"], mod=cli.ops)
        self.assertEqual(vars(ns), dict(selectors=["haiku"], force=False))

    def test_resume_force(self):
        (ns,), _ = invoke("resume", ["cell", "resume", "haiku", "--force"], mod=cli.ops)
        self.assertTrue(ns.force)

    def test_stop(self):
        (ns,), _ = invoke("stop_cells", ["cell", "stop", "r10", "--dry-run"], mod=cli.ops)
        self.assertEqual(vars(ns), dict(selectors=["r10"], cancel=False,
                                        dry_run=True))

    def test_stop_cancel(self):
        (ns,), _ = invoke("stop_cells", ["cell", "stop", "r10", "--cancel"], mod=cli.ops)
        self.assertTrue(ns.cancel)

    def test_kill_is_gone(self):
        for argv in (["cell", "kill", "r10"], ["kill", "r10"]):
            result = runner.invoke(cli.app, argv)
            self.assertNotEqual(result.exit_code, 0, argv)


class TestRetiredCommands(unittest.TestCase):
    def test_worker_era_and_absorbed_commands_are_gone(self):
        for argv in (["queue", "workers", "haiku"], ["queue", "orchestrate"],
                     ["queue", "conduct"], ["queue", "pause", "opus"],
                     ["queue", "resume", "opus"], ["queue", "drain"],
                     ["queue", "stop-all"], ["drain"],
                     ["queue", "add", "opus", "--matrix"],
                     ["fleet", "status"], ["fleet", "zombies"],
                     ["fleet", "reconcile"]):
            result = runner.invoke(cli.app, argv)
            self.assertNotEqual(result.exit_code, 0, argv)


class TestConductCommands(unittest.TestCase):
    def test_run(self):
        (ns,), _ = invoke("conduct", ["conduct", "run", "-n", "5",
                                      "--per-model", "2", "--interval", "10"], mod=cli.conduct)
        self.assertEqual(vars(ns), dict(limit=5, per_model=2, per_model_override={},
                                        interval=10, supervise_interval=300))

    def test_run_per_model_override(self):
        (ns,), _ = invoke("conduct", ["conduct", "run",
                                      "--per-model-override", "haiku=3,opus=2"],
                          mod=cli.conduct)
        self.assertEqual(ns.per_model_override, {"haiku": 3, "opus": 2})

    def test_run_supervision_off(self):
        (ns,), _ = invoke("conduct", ["conduct", "run",
                                      "--supervise-interval", "0"], mod=cli.conduct)
        self.assertEqual(ns.supervise_interval, 0)

    def test_diagnose(self):
        (ns,), _ = invoke("conduct_diagnose", ["conduct", "diagnose"], mod=cli.supervise)
        self.assertEqual(vars(ns), {})

    def test_queue_add_matrix(self):
        (ns,), _ = invoke("spawn_matrix", ["conduct", "queue-add", "opus",
                                           "--matrix", "--reps", "5"], mod=cli.ops)
        self.assertEqual(vars(ns), dict(model="opus", reps=5, task="T1",
                                        fresh=False))

    def test_queue_add_to_rep(self):
        (ns,), _ = invoke("top_up", ["conduct", "queue-add", "opus",
                                     "--to-rep", "10",
                                     "--combo", "beta·apidocs", "--dry-run"], mod=cli.ops)
        self.assertEqual(vars(ns), dict(model="opus", to_rep=10,
                                        combos=["beta·apidocs"],
                                        conditions=[], all_conditions=False,
                                        task="T1", dry_run=True))

    def test_queue_add_to_rep_with_extra_variants(self):
        (ns,), _ = invoke("top_up", ["conduct", "queue-add", "opus",
                                     "--to-rep", "5", "--condition", "howto",
                                     "--condition", "openbook"], mod=cli.ops)
        self.assertEqual(ns.conditions, ["howto", "openbook"])
        self.assertFalse(ns.all_conditions)
        (ns,), _ = invoke("top_up", ["conduct", "queue-add", "opus",
                                     "--to-rep", "5", "--all-conditions"], mod=cli.ops)
        self.assertTrue(ns.all_conditions)

    def test_pause_full(self):
        (ns,), _ = invoke("conduct_pause", ["conduct", "pause", "all",
                                            "-n", "15", "--dry-run"], mod=cli.conduct)
        self.assertEqual(vars(ns), dict(scope=["all"], admission_only=False,
                                        interval=15, dry_run=True))

    def test_pause_partial_admission_only(self):
        (ns,), _ = invoke("conduct_pause", ["conduct", "pause", "dsv4f",
                                            "gemini", "--admission-only"], mod=cli.conduct)
        self.assertEqual(vars(ns), dict(scope=["dsv4f", "gemini"],
                                        admission_only=True, interval=60,
                                        dry_run=False))

    def test_resume(self):
        (ns,), _ = invoke("conduct_resume", ["conduct", "resume", "opus", "haiku"], mod=cli.conduct)
        self.assertEqual(vars(ns), dict(scope=["opus", "haiku"]))

    def test_stop(self):
        (ns,), _ = invoke("conduct_stop", ["conduct", "stop", "all"], mod=cli.conduct)
        self.assertEqual(vars(ns), dict(scope=["all"], yes=False))

    def test_stop_yes_plumbs(self):
        (ns,), _ = invoke("conduct_stop", ["conduct", "stop", "all", "--yes"], mod=cli.conduct)
        self.assertEqual(vars(ns), dict(scope=["all"], yes=True))


class TestResultsScore(unittest.TestCase):
    def test_variant_filter_plumbs(self):
        (ns,), _ = invoke("score", ["results", "score", "--condition", "apidocs"],
                          mod=cli.score)
        self.assertEqual(ns.condition, "apidocs")
        self.assertIsNone(ns.selector)

    def test_impl_filter_plumbs(self):
        (ns,), _ = invoke("score", ["results", "score", "--condition", "apidocs",
                                    "--impl", "py"], mod=cli.score)
        self.assertEqual(ns.condition, "apidocs")
        self.assertEqual(ns.impl, "py")


class TestFleetStatus(unittest.TestCase):
    """status/watch/monitor live in fae/driver/render.py, not on a single verb
    name — the generic invoke() helper (which patches <mod>.<fn_name>
    directly) can't see the call, so these patch cli.render.status explicitly."""

    def test_fleet_status_top_level(self):
        with mock.patch.object(cli.render, "status") as m:
            result = runner.invoke(cli.app, ["fleet-status", "--flat"])
        self.assertEqual(result.exit_code, 0, result.output)
        (ns,), _ = m.call_args
        self.assertEqual(vars(ns), dict(flat=True, running_only=False))

    def test_status_alias_still_works(self):
        with mock.patch.object(cli.render, "status") as m:
            result = runner.invoke(cli.app, ["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        (ns,), _ = m.call_args
        self.assertEqual(vars(ns), dict(flat=False, running_only=False))


class TestRigCommands(unittest.TestCase):
    def test_init_carries_the_experiment_directory(self):
        (ns,), _ = invoke("init", ["experiment", "init"], mod=cli.rig)
        self.assertEqual(vars(ns), dict(experiment=""))
        (ns,), _ = invoke("init", ["experiment", "init", "--experiment", "shout"], mod=cli.rig)
        self.assertEqual(ns.experiment, "shout")

    def test_substrate_takes_no_arguments(self):
        """fae/driver/rig.py's substrate ignores its namespace entirely; an
        earlier cli signature accepted an `arm` argument it silently discarded."""
        (ns,), _ = invoke("substrate", ["experiment", "substrate"], mod=cli.rig)
        self.assertEqual(vars(ns), {})

    def test_smoke_carries_every_field_runs_smoke_reads(self):
        """fae/driver/rig.py's smoke reads arms/only/rep/full_gate unconditionally;
        the namespace must carry all four, with its own defaults."""
        (ns,), _ = invoke("smoke", ["experiment", "smoke"], mod=cli.rig)
        self.assertEqual(vars(ns), dict(arms="", only="", rep=1, full_gate=False))
        (ns,), _ = invoke("smoke", ["experiment", "smoke", "--only", "keda", "--rep", "2",
                                    "--full-gate"], mod=cli.rig)
        self.assertEqual(vars(ns), dict(arms="", only="keda", rep=2, full_gate=True))

    def test_prepare(self):
        (ns,), _ = invoke("prepare", ["experiment", "prepare", "--reps", "2", "--task", "T2"],
                          mod=cli.rig)
        self.assertEqual(vars(ns), dict(model="", reps=2, task="T2"))
        (ns,), _ = invoke("prepare", ["experiment", "prepare", "--model", "sonnet"], mod=cli.rig)
        self.assertEqual(ns.model, "sonnet")

    def test_exp1_carries_report_only(self):
        """fae/driver/exp1.py's exp1 reads args.report_only unconditionally; the
        cli command once omitted it and every invocation would have died on
        AttributeError. `experiment exp1` routes through rig.exp1_cmd like every
        other rig verb — cli.py passes the raw --arms value through
        unresolved, the same convention `experiment smoke` uses for its own
        --arms default (resolved inside rig.py, not in cli.py)."""
        (ns,), _ = invoke("exp1_cmd", ["experiment", "exp1", "--report-only"], mod=cli.rig)
        self.assertEqual(ns.report_only, True)
        self.assertEqual(ns.reps, 3)
        self.assertIsNone(ns.arms)


if __name__ == "__main__":
    unittest.main()
