"""`cli.py experiment check`: each step finds what a real run would refuse,
names its fix, and the walk pauses, skips, retries and quits on the answer."""
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path, EXPERIMENT_DIR = the fixture)

from fae.cell import experiment as _experiment
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
        return _experiment.current().variant(arm)


class TestTheFixtureIsReady(CheckCase):
    def test_every_static_step_passes_and_the_docker_ones_are_skipped(self):
        self.assertEqual(self.run_check(), 0)
        for title in ("This machine", "The config and the definition", "What the definition declares",
                      "Each variant", "Seeding every cell of the matrix"):
            self.assertIn(f"  ok      {title}", self.lines)
        self.assertIn("  skip    The docker daemon", self.lines)
        self.assertIn("No failure; the skipped steps are not checked.", self.text())

    def test_a_ready_run_with_docker_prints_the_next_commands(self):
        with mock.patch.object(check, "_docker", return_value=[check.Finding(True, "docker")]), \
                mock.patch.object(check, "_substrate", return_value=[check.Finding(True, "arms")]):
            self.assertEqual(self.run_check(check.Ctx(root=self.root)), 0)
        self.assertIn("READY. Next:", self.text())
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
        with mock.patch.object(check.common, "definition", side_effect=NameError("name 'GAET' is not defined")):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("raised NameError: name 'GAET' is not defined", self.text())
        self.assertIn("  skip    What the definition declares", self.lines)

    def test_a_matrix_arm_without_a_variant(self):
        d = _experiment.current()
        with mock.patch.object(type(d), "matrix", new_callable=mock.PropertyMock,
                               return_value={**d.matrix, "gamma": ["apidocs"]}):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL matrix arm 'gamma' has a variant", self.text())

    def test_an_undeclared_authoring_surface(self):
        with mock.patch.object(self.variant("beta"), "AUTHORING_SURFACE", None):
            self.assertEqual(self.run_check(), 1)
        name = self.variant("beta").__name__
        self.assertIn(f"fix: declare {name}.AUTHORING_SURFACE", self.text())
        self.assertIn("FAIL beta: not seeded, it has no authoring surface", self.text())

    def test_an_undeclared_liveness_probe(self):
        with mock.patch.object(self.variant("alpha"), "substrate_alive", Variant.substrate_alive):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("substrate_alive probe", self.text())

    def test_a_missing_api_doc_is_found_by_seeding(self):
        cls = self.variant("alpha")
        seed = self.root / "seed"
        shutil.copytree(cls.seed_root(), seed)
        (seed / "any.alpha.howto.api.md").unlink()
        with mock.patch.object(cls, "SEED", str(seed)):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL alpha/howto:", self.text())

    def test_a_missing_reference_is_found_by_seeding(self):
        cls = self.variant("alpha")
        seed = self.root / "seed"
        shutil.copytree(cls.seed_root(), seed)
        shutil.rmtree(seed / "reference")
        with mock.patch.object(cls, "SEED", str(seed)):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL alpha/reference: no reference impl", self.text())

    def test_an_unpinned_condition_doc(self):
        d = _experiment.current()
        pins = {k: v for k, v in d.seed_docs.items() if k != ("alpha", "howto")}
        with mock.patch.object(type(d), "seed_docs", new_callable=mock.PropertyMock, return_value=pins):
            self.assertEqual(self.run_check(), 1)
        self.assertIn("FAIL alpha/howto: alpha/howto is not a defined matrix combination", self.text())

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
        args = check.SimpleNamespace(walk=True, static=True, smoke=False, arms="", task="T1")
        with mock.patch.object(check.sys, "stdin", io.StringIO("")):
            with self.assertRaisesRegex(SystemExit, "needs a terminal"):
                check.main(args)


class TestTheCli(unittest.TestCase):
    def test_experiment_check_is_registered_and_rig_keeps_only_the_harness_verbs(self):
        src = (Path(ROOT) / "fae" / "cli.py").read_text()
        self.assertIn('@experiment_app.command("check")', src)
        for verb in ("init", "check", "substrate", "smoke", "prepare", "verb"):
            self.assertIn(f'@experiment_app.command("{verb}"', src)
            self.assertNotIn(f'@rig_app.command("{verb}"', src)


if __name__ == "__main__":
    unittest.main()
