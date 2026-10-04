"""`cli.py experiment check`: each step finds what a real run would refuse,
names its fix, and the walk pauses, skips, retries and quits on the answer."""
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT, at_workspace  # noqa: F401  (sys.path, EXPERIMENT_DIR = the fixture)

from fae import experiment as _experiment
from fae.cell.variants.base import Variant
from fae.driver import check


class CheckCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "fae.toml").write_text("")
        self.lines = []

    def ctx(self, **kw):
        return check.Ctx(root=self.root, static=True, **kw)

    def run_check(self, ctx=None, **kw):
        return check.run(ctx or self.ctx(), out=self.lines.append, **kw)

    def text(self):
        return "\n".join(self.lines)

    def variant(self, arm):
        return _experiment.definition().variant(arm)


class TestTheFixtureIsReady(CheckCase):
    def test_every_static_step_passes_and_the_docker_ones_are_skipped(self):
        self.assertEqual(self.run_check(), 0)
        for title in ("prerequisites", "config", "definition", "variants", "seeds"):
            self.assertIn(f"  ok      {title}", self.lines)
        self.assertIn("  skip    infra", self.lines)
        self.assertIn("No failure; the skipped steps are not checked.", self.text())

    def test_a_ready_run_with_docker_prints_the_next_commands(self):
        ok = [check.Finding(True, "ok")]
        with mock.patch.object(check, "_prerequisites", return_value=ok), \
                mock.patch.object(check, "_infra", return_value=ok), \
                mock.patch.object(check, "_leftovers", return_value=ok):
            steps = tuple(check.Step(s.key, s.title, s.why, s.howto, getattr(check, s.run.__name__),
                                     s.needs, s.docker, s.opt_in) for s in check.STEPS)
            self.assertEqual(self.run_check(check.Ctx(root=self.root), steps=steps), 0)
        self.assertIn("READY. next:", self.text())
        self.assertIn("python3 cli.py experiment run -n 2 --per-agent 1", self.text())
        self.assertIn("python3 cli.py experiment smoke --full-gate", self.text())

    def test_the_seeds_leave_nothing_under_the_root(self):
        self.run_check()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["fae.toml"])


class TestEachFailureNamesItsFix(CheckCase):
    def test_no_config_file_points_at_experiment_init(self):
        (self.root / "fae.toml").unlink()
        self.assertEqual(self.run_check(), 1)
        self.assertIn("fix: python3 cli.py experiment init --experiment", self.text())

    def test_a_definition_that_raises_is_shown_with_where_to_fix_it(self):
        with mock.patch.object(check._experiment, "definition", side_effect=NameError("name 'GAET' is not defined")):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("raised NameError: name 'GAET' is not defined", self.text())
        self.assertIn("  skip    definition", self.lines)

    def test_an_unreadable_variant_file(self):
        from fae.cell.variants import files
        with mock.patch.object(files, "load", side_effect=files.VariantFileError(
                "variants/x.toml: unknown key(s) in [verify]: rn")):
            d = _experiment.definition()
            d._subjects = None
            self.addCleanup(setattr, d, "_subjects", None)
            self.assertEqual(self.run_check(), 1)
        self.assertIn("unknown key(s) in [verify]: rn", self.text())

    def test_an_undeclared_authoring_surface(self):
        with mock.patch.object(self.variant("beta_apidocs"), "AUTHORING_SURFACE", None):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("fix: declare [authoring] surface", self.text())
        self.assertIn("FAIL beta_apidocs: not seeded, it has no authoring surface", self.text())

    def test_an_undeclared_liveness_probe(self):
        cls = self.variant("alpha_apidocs")
        from fae.cell.infra.base import Infra
        with mock.patch.object(cls.INFRA, "alive", Infra.alive):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("an infra liveness probe", self.text())

    def test_a_missing_input_is_found_by_seeding(self):
        cls = self.variant("alpha_howto")
        inputs = {**cls.INPUTS, "docs/alpha.md": self.root / "gone.md"}
        with mock.patch.object(cls, "INPUTS", inputs):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL alpha_howto: alpha_howto: input docs/alpha.md", self.text())

    def test_a_missing_reference_is_found_by_seeding(self):
        cls = self.variant("alpha_apidocs")
        with mock.patch.object(cls, "REFERENCE", self.root / "no-reference"):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL alpha_apidocs", self.text())
        self.assertIn("reference", self.text())

    def test_no_todo_among_the_inputs(self):
        cls = self.variant("beta_onlysrc")
        inputs = {k: v for k, v in cls.INPUTS.items() if k != "TODO.md"}
        with mock.patch.object(cls, "INPUTS", inputs):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL beta_onlysrc: TODO.md among its inputs", self.text())

    def test_a_crashing_check_is_a_failure_not_the_end(self):
        with mock.patch.object(check, "_definition", side_effect=KeyError("boom")):
            steps = tuple(check.Step(s.key, s.title, s.why, s.howto,
                                     check._definition if s.key == "definition" else s.run,
                                     s.needs, s.docker, s.opt_in) for s in check.STEPS)
            self.assertEqual(self.run_check(steps=steps), 1)
        self.assertIn("definition check raised KeyError", self.text())


class TestTheWalk(CheckCase):
    def steps(self, results):
        """Two steps: `a` answers from `results` (one list per call), `b` needs `a`."""
        calls = {"a": 0}

        def a(ctx):
            calls["a"] += 1
            return results[min(calls["a"], len(results)) - 1]
        return (check.Step("a", "Step A", "why A", "§1", a),
                check.Step("b", "Step B", "why B", "§2", lambda ctx: [check.Finding(True, "b")],
                           needs=("a",))), calls

    def walk(self, steps, answers):
        it = iter(answers)
        return check.run(self.ctx(), steps=steps, walk=True, ask=lambda _: next(it),
                         out=self.lines.append)

    def test_it_explains_each_step_before_running_it(self):
        steps, _ = self.steps([[check.Finding(True, "a")]])
        self.assertEqual(self.walk(steps, ["", ""]), 0)
        self.assertIn("why A", self.text())
        self.assertLess(self.text().index("why A"), self.text().index("  ok   a"))

    def test_a_skipped_step_skips_what_needs_it(self):
        steps, calls = self.steps([[check.Finding(True, "a")]])
        self.walk(steps, ["s"])
        self.assertEqual(calls["a"], 0)
        self.assertIn("Step B: skip (needs a)", self.text())

    def test_retry_runs_the_step_again_after_the_fix(self):
        steps, calls = self.steps([[check.Finding(False, "a broken", "fix a")],
                                   [check.Finding(True, "a fixed")]])
        ctx = self.ctx()
        with mock.patch.object(ctx, "forget") as forget:
            rc = check.run(ctx, steps=steps, walk=True, out=self.lines.append,
                           ask=lambda _, it=iter(["", "r", ""]): next(it))
        self.assertEqual((rc, calls["a"]), (0, 2))
        forget.assert_called_once()
        self.assertIn("fix: fix a", self.text())

    def test_quit_after_a_failure_reports_not_ready(self):
        steps, _ = self.steps([[check.Finding(False, "a broken", "fix a")]])
        self.assertEqual(self.walk(steps, ["", "q"]), 1)
        self.assertIn("NOT READY", self.text())

    def test_quit_before_a_step_runs_nothing(self):
        steps, calls = self.steps([[check.Finding(True, "a")]])
        self.walk(steps, ["q"])
        self.assertEqual(calls["a"], 0)

    def test_walk_without_a_terminal_is_refused(self):
        args = SimpleNamespace(walk=True, static=True, smoke=False, variants="", task="T1")
        with mock.patch.object(check.sys, "stdin", io.StringIO("")):
            with self.assertRaisesRegex(SystemExit, "needs a terminal"):
                check.main(args)


class TestTheCli(unittest.TestCase):
    def test_experiment_check_is_registered_and_rig_keeps_only_the_harness_verbs(self):
        src = (Path(ROOT) / "fae" / "cli.py").read_text()
        self.assertIn('@experiment_app.command("check")', src)
        for verb in ("init", "check", "infra", "smoke", "prepare", "verb"):
            self.assertIn(f'@experiment_app.command("{verb}"', src)
            self.assertNotIn(f'@rig_app.command("{verb}"', src)
        rig_verbs = sorted(set(__import__("re").findall(r'@rig_app\.command\("([a-z-]+)"', src)))
        self.assertEqual(rig_verbs, ["tool", "trace-reset"])


class TestTheFoldedSteps(CheckCase):
    """What were `rig selftest` and `rig zombies` are steps of the check: each finds its failure and names the fix; the docker ones
    skip under --static and the trace replay runs only with --tla-trace."""

    def findings(self, step, ctx=None):
        return step(ctx or self.ctx())

    def test_a_missing_engine_function_is_named(self):
        from fae.cell import verify as _verify
        with mock.patch.object(_verify, "run_verifier", None):
            bad = [f for f in self.findings(check._invariants) if not f.ok]
        self.assertTrue(any("fae.cell.verify.run_verifier" in f.text for f in bad))

    def test_the_experiments_own_checks_are_findings(self):
        d = _experiment.definition()
        verbs = dict(d.verbs, selftest=lambda ws: ["a green with no mount"])
        with mock.patch.object(type(d), "verbs", new_callable=mock.PropertyMock, return_value=verbs):
            bad = [f for f in self.findings(check._invariants) if not f.ok]
        self.assertIn("the experiment's own check: a green with no mount", [f.text for f in bad])

    def test_leftovers_fail_with_the_repair_fix(self):
        from fae.driver.conduct import zombies
        with mock.patch.object(zombies, "find_zombies", return_value=[]):
            self.assertTrue(all(f.ok for f in self.findings(check._leftovers)))
        with mock.patch.object(zombies, "find_zombies",
                               return_value=[("container", "fae-dind-x", "x", "loop gone")]):
            (f,) = self.findings(check._leftovers)
        self.assertFalse(f.ok)
        self.assertIn("experiment repair", f.fix)

    def test_the_trace_names_a_missing_checker_and_an_empty_log_is_fine(self):
        with mock.patch.object(check, "tla_verify_path", return_value=None):
            (f,) = self.findings(check._trace)
        self.assertFalse(f.ok)
        self.assertIn("FAE_TLA_VERIFY", f.fix)
        empty = self.root / "transitions.log"
        empty.write_text("")
        with mock.patch.object(check, "tla_verify_path", return_value="/x/tla_verify"), \
                at_workspace(plane=self.root):
            (f,) = self.findings(check._trace)
        self.assertTrue(f.ok)

    def test_a_trace_that_breaks_the_model_is_a_failure(self):
        log = self.root / "transitions.log"
        log.write_text("2026-10-02T00:00:00Z\tSPAWN\tc\n")
        with mock.patch.object(check, "tla_verify_path", return_value="/x/tla_verify"), \
                at_workspace(plane=self.root), \
                mock.patch.object(check.subprocess, "run",
                                  return_value=mock.Mock(returncode=1, stdout="Resume not ENABLED\n",
                                                         stderr="")):
            (f,) = self.findings(check._trace)
        self.assertFalse(f.ok)
        self.assertIn("Resume not ENABLED", f.text)

    def test_static_skips_the_docker_steps_and_the_trace_is_opt_in(self):
        self.run_check()
        self.assertIn("  skip    leftovers", self.lines)
        self.assertNotIn("tla-trace", self.text())
        self.lines.clear()
        with mock.patch.object(check, "_trace", return_value=[check.Finding(True, "replayed")]):
            steps = tuple(s if s.key != "tla-trace" else check.Step(s.key, s.title, s.why, s.howto,
                                                                 check._trace, s.needs, s.docker,
                                                                 s.opt_in) for s in check.STEPS)
            self.run_check(steps=steps, tla_trace=True)
        self.assertIn("  ok      tla-trace", self.lines)


if __name__ == "__main__":
    unittest.main()
