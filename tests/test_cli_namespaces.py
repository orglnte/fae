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

    `mod` names the module that owns fn_name: `cli` for the verbs on cells and
    specs it holds itself, a fae/driver/ submodule for the rest."""
    with mock.patch.object(mod, fn_name) as m:
        result = runner.invoke(cli.app, argv)
    if result.exit_code != 0:
        raise AssertionError(f"{argv} exited {result.exit_code}: {result.output}")
    if not m.call_args:
        raise AssertionError(f"{argv} never called {fn_name}")
    return m.call_args


class TestCellCommands(unittest.TestCase):
    def test_spawn(self):
        (ns,), _ = invoke("spawn", ["cell", "spawn", "sonnet", "beta_apidocs",
                                    "-r", "2", "--fresh"], mod=cli)
        self.assertEqual(vars(ns), dict(agent="sonnet", variant="beta_apidocs", rep="2", task="T1",
                                        fresh=True, dangerously_ignore_slots=False))

    def test_spawn_dangerously_ignore_slots(self):
        (ns,), _ = invoke("spawn", ["cell", "spawn", "sonnet", "beta_apidocs",
                                    "--dangerously-ignore-slots"], mod=cli)
        self.assertTrue(ns.dangerously_ignore_slots)

    def test_pause_takes_several_selectors(self):
        (ns,), _ = invoke("pause", ["cell", "pause", "sonnet", "haiku",
                                    "--reason", "roster"], mod=cli)
        self.assertEqual(vars(ns), dict(selectors=["sonnet", "haiku"],
                                        reason="roster"))

    def test_resume(self):
        (ns,), _ = invoke("resume", ["cell", "resume", "haiku"], mod=cli)
        self.assertEqual(vars(ns), dict(selectors=["haiku"], force=False,
                                        dangerously_ignore_slots=False))

    def test_resume_force(self):
        (ns,), _ = invoke("resume", ["cell", "resume", "haiku", "--force"], mod=cli)
        self.assertTrue(ns.force)

    def test_stop(self):
        (ns,), _ = invoke("stop_cells", ["cell", "stop", "r10", "--dry-run"], mod=cli)
        self.assertEqual(vars(ns), dict(selectors=["r10"], cancel=False,
                                        dry_run=True))

    def test_stop_cancel(self):
        (ns,), _ = invoke("stop_cells", ["cell", "stop", "r10", "--cancel"], mod=cli)
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
                     ["fleet", "status"], ["fleet", "zombies"],
                     ["fleet", "reconcile"],
                     ["conduct", "run"], ["conduct", "queue-add", "opus", "--matrix"],
                     ["conduct", "pause", "all"], ["conduct", "reconcile"],
                     ["fleet-status"], ["status"],
                     ["rig", "selftest"], ["rig", "zombies"], ["rig", "agent-image"],
                     ["tools", "run", "law"]):
            result = runner.invoke(cli.app, argv)
            self.assertNotEqual(result.exit_code, 0, argv)


class TestTheRunCommands(unittest.TestCase):
    def test_run(self):
        (ns,), _ = invoke("run", ["experiment", "run", "-n", "5",
                                      "--per-agent", "2", "--interval", "10"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), dict(limit=5, per_agent=2, per_agent_override={},
                                        interval=10, supervise_interval=300))

    def test_run_per_model_override(self):
        (ns,), _ = invoke("run", ["experiment", "run",
                                      "--per-agent-override", "haiku=3,opus=2"], mod=cli.conduct.Conduct)
        self.assertEqual(ns.per_agent_override, {"haiku": 3, "opus": 2})

    def test_run_supervision_off(self):
        (ns,), _ = invoke("run", ["experiment", "run",
                                      "--supervise-interval", "0"], mod=cli.conduct.Conduct)
        self.assertEqual(ns.supervise_interval, 0)

    def test_diagnose(self):
        (ns,), _ = invoke("diagnose", ["experiment", "diagnose"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), {})

    def test_queue_add_matrix(self):
        (ns,), _ = invoke("spawn_matrix", ["queue", "add", "opus",
                                           "--matrix", "--reps", "5"], mod=cli)
        self.assertEqual(vars(ns), dict(agent="opus", reps=5, task="T1",
                                        fresh=False))

    def test_queue_add_to_rep(self):
        (ns,), _ = invoke("top_up", ["queue", "add", "opus",
                                     "--to-rep", "10",
                                     "--variant", "beta_apidocs", "--dry-run"], mod=cli)
        self.assertEqual(vars(ns), dict(agent="opus", to_rep=10, variants=["beta_apidocs"],
                                        task="T1", dry_run=True))

    def test_pause_full(self):
        (ns,), _ = invoke("pause", ["experiment", "pause", "all",
                                            "-n", "15", "--dry-run"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), dict(scope=["all"], admission_only=False,
                                        interval=15, dry_run=True))

    def test_pause_partial_admission_only(self):
        (ns,), _ = invoke("pause", ["experiment", "pause", "dsv4f",
                                            "gemini", "--admission-only"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), dict(scope=["dsv4f", "gemini"],
                                        admission_only=True, interval=60,
                                        dry_run=False))

    def test_resume(self):
        (ns,), _ = invoke("resume", ["experiment", "resume", "opus", "haiku"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), dict(scope=["opus", "haiku"]))

    def test_stop(self):
        (ns,), _ = invoke("stop", ["experiment", "stop", "all"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), dict(scope=["all"], yes=False))

    def test_stop_yes_plumbs(self):
        (ns,), _ = invoke("stop", ["experiment", "stop", "all", "--yes"], mod=cli.conduct.Conduct)
        self.assertEqual(vars(ns), dict(scope=["all"], yes=True))


class TestResultsScore(unittest.TestCase):
    def test_variant_filter_plumbs(self):
        (ns,), _ = invoke("score", ["results", "score", "--variant", "beta_apidocs"],
                          mod=cli.score)
        self.assertEqual(ns.variant, "beta_apidocs")
        self.assertIsNone(ns.selector)

    def test_factor_and_impl_filters_plumb(self):
        (ns,), _ = invoke("score", ["results", "score", "--where", "docs=apidocs",
                                    "--where", "tech=beta", "--impl", "py"], mod=cli.score)
        self.assertEqual(ns.where, ["docs=apidocs", "tech=beta"])
        self.assertEqual(ns.impl, "py")


class TestExperimentStatus(unittest.TestCase):
    """status/watch/monitor live in fae/driver/render.py, not on a single verb
    name — the generic invoke() helper (which patches <mod>.<fn_name>
    directly) can't see the call, so these patch cli.render.status explicitly."""

    def test_status_flat(self):
        with mock.patch.object(cli.render, "status") as m:
            result = runner.invoke(cli.app, ["experiment", "status", "--flat"])
        self.assertEqual(result.exit_code, 0, result.output)
        (ns,), _ = m.call_args
        self.assertEqual(vars(ns), dict(flat=True, running_only=False))

    def test_status_default(self):
        with mock.patch.object(cli.render, "status") as m:
            result = runner.invoke(cli.app, ["experiment", "status"])
        self.assertEqual(result.exit_code, 0, result.output)
        (ns,), _ = m.call_args
        self.assertEqual(vars(ns), dict(flat=False, running_only=False))


class TestQueueCommands(unittest.TestCase):
    def test_list_takes_lanes_and_done(self):
        (ns,), _ = invoke("queue_list", ["queue", "list", "opus", "--done"], mod=cli.render)
        self.assertEqual(vars(ns), dict(agents=["opus"], done=True))

    def test_cancel_takes_selectors_and_dry_run(self):
        (ns,), _ = invoke("cancel_pending", ["queue", "cancel", "opus_r1", "--dry-run"], mod=cli)
        self.assertEqual(vars(ns), dict(selectors=["opus_r1"], dry_run=True))


class TestRigCommands(unittest.TestCase):
    def test_init_carries_the_experiment_directory(self):
        (ns,), _ = invoke("init", ["experiment", "init"], mod=cli.rig)
        self.assertEqual(vars(ns), dict(experiment=""))
        (ns,), _ = invoke("init", ["experiment", "init", "--experiment", "shout"], mod=cli.rig)
        self.assertEqual(ns.experiment, "shout")

    def test_infra_takes_no_arguments(self):
        """fae/driver/rig.py's infra ignores its namespace entirely; an
        earlier cli signature accepted an argument it silently discarded."""
        (ns,), _ = invoke("infra", ["experiment", "infra"], mod=cli.rig)
        self.assertEqual(vars(ns), {})

    def test_smoke_carries_every_field_runs_smoke_reads(self):
        """fae/driver/rig.py's smoke reads variants/only/rep/full_gate unconditionally;
        the namespace must carry all four, with its own defaults."""
        (ns,), _ = invoke("smoke", ["experiment", "smoke"], mod=cli.rig)
        self.assertEqual(vars(ns), dict(variants="", only="", rep=1, full_gate=False))
        (ns,), _ = invoke("smoke", ["experiment", "smoke", "--only", "alpha", "--rep", "2",
                                    "--full-gate"], mod=cli.rig)
        self.assertEqual(vars(ns), dict(variants="", only="alpha", rep=2, full_gate=True))

    def test_prepare(self):
        (ns,), _ = invoke("prepare", ["experiment", "prepare", "--reps", "2", "--task", "T2"],
                          mod=cli.rig)
        self.assertEqual(vars(ns), dict(agent="", reps=2, task="T2"))
        (ns,), _ = invoke("prepare", ["experiment", "prepare", "--agent", "sonnet"], mod=cli.rig)
        self.assertEqual(ns.agent, "sonnet")

    def _verb(self, argv):
        with mock.patch.object(cli.rig, "verb_cmd", return_value=0) as m:
            result = runner.invoke(cli.app, argv)
        self.assertEqual(result.exit_code, 0, result.output)
        return m.call_args.args

    def test_verb_passes_its_name_and_every_argument_through(self):
        """An experiment's own command parses its own options: cli.py hands
        it the name and the rest of argv untouched, --help included."""
        self.assertEqual(self._verb(["experiment", "verb", "bench", "--reps", "2",
                                     "--dry-run", "--help"]),
                         ("bench", ["--reps", "2", "--dry-run", "--help"]))

    def test_verb_with_no_name_lists(self):
        self.assertEqual(self._verb(["experiment", "verb"]), ("", []))

    def test_verb_exits_with_the_commands_code(self):
        with mock.patch.object(cli.rig, "verb_cmd", return_value=3):
            self.assertEqual(runner.invoke(cli.app, ["experiment", "verb", "x"]).exit_code, 3)

if __name__ == "__main__":
    unittest.main()
