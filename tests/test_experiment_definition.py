"""fae/cell/experiment.py — the experiment definition the engine loads by
path, and the engine's only way of knowing the experiment: no engine module
imports the `experiment` package by name."""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

sys.path.insert(0, str(ROOT))
from fae.cell import experiment as exp  # noqa: E402

ENGINE = ["cli.py", "driver", "harness", "scoring"]
_IMPORT = re.compile(r"^\s*(from experiment[.\s]|import experiment[.\s]|import experiment$)", re.M)


def _write_definition(root, body):
    d = Path(root) / "experiment"
    d.mkdir()
    (d / "__init__.py").write_text(textwrap.dedent(body))
    return d


class TestEngineNeverImportsTheExperiment(unittest.TestCase):
    def test_no_engine_module_names_the_package(self):
        hits = []
        for top in ENGINE:
            p = ROOT / top
            files = [p] if p.is_file() else [f for f in p.rglob("*.py")]
            for f in files:
                if _IMPORT.search(f.read_text(errors="replace")):
                    hits.append(str(f.relative_to(ROOT)))
        self.assertEqual(hits, [])


class TestMinimalDefinition(unittest.TestCase):
    """Run in a child: the process-wide `experiment` package is the fixture one here."""

    def _run(self, body, code):
        with tempfile.TemporaryDirectory() as td:
            _write_definition(td, body)
            prog = ("import sys; sys.path.insert(0, %r)\n"
                    "from fae.cell import experiment as exp\n"
                    "d = exp.load(%r)\n" % (str(ROOT), str(Path(td) / "experiment"))) + code
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True,
                               cwd=td, env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_defaults_when_the_definition_declares_only_subjects(self):
        out = self._run('''
            from fae.cell.variants.base import Variant
            class Only(Variant):
                ARM = "only"; TECH = "py"; CONDITIONS = ("apidocs",)
            def variant_classes():
                return (Only,)
        ''', '''
print(d.name, d.arms, d.matrix, d.seed_docs, d.gate.arity, d.verbs, d.exclusive)
''')
        self.assertEqual(out.split(), ["experiment", "('only',)", "{'only':", "['apidocs']}",
                                       "{}", "1", "{}", "None"])

    def test_retired_arms_are_registered_but_outside_the_matrix(self):
        out = self._run('''
            from fae.cell.variants.base import Variant
            class New(Variant):
                ARM = "new"; TECH = "py"; CONDITIONS = ("apidocs",)
            class Old(Variant):
                ARM = "old"; TECH = "py"; CONDITIONS = ("apidocs",)
            def variant_classes():
                return (New, Old)
            MATRIX = {"new": ["apidocs"]}
            RETIRED = ("old",)
        ''', '''
print(sorted(d.arms), sorted(d.matrix), d.retired)
''')
        self.assertEqual(out.split(), ["['new',", "'old']", "['new']", "('old',)"])

    def test_no_retired_arms_by_default(self):
        out = self._run('''
            from fae.cell.variants.base import Variant
            class Only(Variant):
                ARM = "only"; TECH = "py"; CONDITIONS = ("apidocs",)
            def variant_classes():
                return (Only,)
        ''', "print(d.retired)\n")
        self.assertEqual(out.strip(), "()")

    def test_a_verifier_that_is_not_a_Verifier_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run('''
                def variant_classes():
                    return ()
                def verifier_class():
                    return object
            ''', "d.verifier_class()\n")
        self.assertIn("must return a Verifier subclass", str(ctx.exception))

    def test_a_definition_without_a_verifier_is_refused_when_one_is_asked_for(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run('''
                def variant_classes():
                    return ()
            ''', "d.verifier_class()\n")
        self.assertIn("declares no verifier_class()", str(ctx.exception))

    def test_declared_config_keys_reach_the_config_and_its_exports(self):
        out = self._run('''
            def variant_classes():
                return ()
            CONFIG = {"CALC_BIN": ("paths", "calc_bin", "bin/calc", "path"),
                      "CALC_CASES": ("paths", "calc_cases", "{experiment}/cases", "path"),
                      "CALC_MODE": ("run", "calc_mode", "strict", "str")}
            def fingerprint_trees(conf):
                return [conf["CALC_BIN"]]
        ''', '''
import os
from fae.cell import config
c = config.load(os.getcwd(), env={"EXPERIMENT_DIR": str(d.path), "CALC_MODE": "lax"})
print(c.values["CALC_BIN"].endswith("/bin/calc"), c.values["CALC_CASES"] == str(d.path / "cases"),
      c.values["CALC_MODE"], sorted(k for k in c.exported if k.startswith("CALC_")))
''')
        self.assertEqual(out.split(), ["True", "True", "lax", "['CALC_BIN',", "'CALC_CASES',", "'CALC_MODE']"])

    def test_the_gate_is_the_definitions(self):
        out = self._run('''
            from fae.cell.experiment import Gate
            def variant_classes():
                return ()
            GATE = Gate(("A", "B", "C"), rotate=False)
        ''', '''
import os, tempfile
from pathlib import Path
from fae.cell import Cell
root = Path(tempfile.mkdtemp()); (root / "workspaces").mkdir()
os.environ["EXPERIMENT_DIR"] = str(d.path); os.environ["SHAPE_VARIATION"] = "1"
c = Cell("m_high_only_v_T1_r1", workspaces=root / "workspaces", root=root)
print(c.gate_shapes, c.gate_def.arity, c.gate_def.rotate)
''')
        self.assertEqual(out.strip(), "('A', 'B', 'C') 3 False")

    def test_a_second_definition_is_refused_until_unload(self):
        out = self._run('''
            def variant_classes():
                return ()
        ''', '''
import tempfile, pathlib
other = pathlib.Path(tempfile.mkdtemp()) / "experiment"; other.mkdir()
(other / "__init__.py").write_text("NAME='b'\\n")
try:
    exp.load(other)
    print("loaded")
except RuntimeError as e:
    print("refused" if "one root, one experiment" in str(e) else e)
exp.unload()
print(exp.load(other).name, "experiment" in sys.modules)
''')
        self.assertEqual(out.split(), ["refused", "b", "True"])

    def test_a_missing_definition_is_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            prog = ("import sys; sys.path.insert(0, %r)\nfrom fae.cell import experiment as exp\n"
                    "exp.load(%r)\n" % (str(ROOT), td))
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FATAL: no experiment definition", r.stderr)


class TestConfigReadsNoSubject(unittest.TestCase):
    def test_importing_the_config_loads_no_experiment(self):
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, %r)\n"
                            "from fae.cell import config\n"
                            "print('experiment' in sys.modules)" % str(ROOT)],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)


class TestTheExperimentsOwnCommands(unittest.TestCase):
    """`cli.py experiment verb NAME ARGS`: the engine names no command of any
    experiment; it lists and runs what the definition's commands() declares."""

    def _with(self, commands):
        from fae.driver import rig
        d = exp.current()
        return mock.patch.object(type(d), "commands", new_callable=mock.PropertyMock,
                                 return_value=commands), rig

    def test_a_named_command_gets_its_arguments_and_its_exit_code_is_returned(self):
        seen = []

        def bench(argv):
            """Run the bench."""
            seen.append(argv)
            return 3
        patch, rig = self._with({"bench": bench})
        with patch:
            self.assertEqual(rig.verb_cmd("bench", ("--reps", "2")), 3)
        self.assertEqual(seen, [["--reps", "2"]])

    def test_no_name_lists_each_command_with_its_first_doc_line(self):
        def bench(argv):
            """Run the bench.

            More."""
        patch, rig = self._with({"bench": bench})
        with patch, mock.patch("builtins.print") as p:
            self.assertEqual(rig.verb_cmd("", []), 0)
        self.assertIn("Run the bench.", p.call_args_list[0].args[0])

    def test_an_unknown_name_is_refused_naming_the_known_ones(self):
        patch, rig = self._with({"bench": lambda argv: 0})
        with patch, self.assertRaisesRegex(SystemExit, "'nosuch' is not a command.*bench"):
            rig.verb_cmd("nosuch", [])

    def test_a_definition_without_commands_has_none(self):
        self.assertEqual(exp.current().commands, {})


if __name__ == "__main__":
    unittest.main()
