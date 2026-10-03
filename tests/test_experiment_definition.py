"""fae/experiment.py — the experiment definition the engine loads by
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
from fae import experiment as exp  # noqa: E402

ENGINE = ["cli.py", "driver", "harness", "scoring"]
_IMPORT = re.compile(r"^\s*(from experiment[.\s]|import experiment[.\s]|import experiment$)", re.M)


def _write_definition(root, body, variants=None):
    d = Path(root) / "experiment"
    d.mkdir()
    (d / "__init__.py").write_text(textwrap.dedent(body))
    if variants:
        (d / "variants").mkdir()
        for vid, text in variants.items():
            (d / "variants" / f"{vid}.toml").write_text(textwrap.dedent(text))
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

    def _run(self, body, code, variants=None):
        with tempfile.TemporaryDirectory() as td:
            _write_definition(td, body, variants)
            prog = ("import sys; sys.path.insert(0, %r)\n"
                    "from fae import experiment as exp\n"
                    "d = exp.load(%r)\n" % (str(ROOT), str(Path(td) / "experiment"))) + code
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True,
                               cwd=td, env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_defaults_when_the_definition_declares_nothing(self):
        out = self._run("", "print(d.name, d.ids, d.active, d.gate.arity, d.verbs, d.exclusive)\n")
        self.assertEqual(out.split(), ["experiment", "()", "()", "1", "{}", "None"])

    def test_each_file_is_a_variant_and_retired_ones_are_not_scheduled(self):
        out = self._run("", "print(d.ids, d.active)\n", variants={
            "new_one": "[authoring]\nsurface = { files = ['a.py'] }\n",
            "old_one": "retired = true\n"})
        self.assertEqual(out.split(), ["('new_one',", "'old_one')", "('new_one',)"])

    def test_a_file_becomes_the_variants_data(self):
        code = ('v = d.variant("only")\n'
                'print(v.ID, v.LABEL, v.AUTHORING_SURFACE, v.FACTORS, sorted(v.INPUTS),\n'
                '      v.RUN["command"], v.LOCK, v.LOCK_SLOTS, v.ACCESS_INFRA, v.TEMPLATE[0].name)\n')
        out = self._run("", code, variants={"only": """
            label = "Only"
            factors = { docs = "apidocs" }
            [authoring]
            template = ["task/skeleton"]
            surface = { files = ["a.py"], prefixes = ["app/"] }
            access_infra = true
            [authoring.inputs]
            "TODO.md" = "task/T1.PROMPT.md"
            [verify.run]
            image = "python:3"
            command = ["python3", "a.py"]
            [infra]
            lock = "only"
            lock_slots = 2
            """})
        self.assertEqual(out.split(), ["only", "Only", "(('a.py',),", "('app/',))",
                                       "{'docs':", "'apidocs'}", "['TODO.md']",
                                       "['python3',", "'a.py']", "only", "2", "True", "skeleton"])

    def test_an_unknown_key_is_refused_naming_the_file(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run("", "d.variants\n", variants={"only": "[verify]\nrn = 1\n"})
        self.assertIn("only.toml: unknown key(s) in [verify]: rn", str(ctx.exception))

    def test_the_infra_class_is_imported_from_the_experiment_package(self):
        body = """
            from fae.cell.infra.base import Infra
            class Daemon(Infra):
                def alive(self):
                    return True
            """
        code = ('from fae.cell.infra.base import DefaultInfra\n'
                'print(d.variant("own").INFRA.__name__, d.variant("bare").INFRA is DefaultInfra)\n')
        out = self._run(body, code, variants={"own": "[infra]\nclass = \"__init__:Daemon\"\n",
                                              "bare": "label = \"Bare\"\n"})
        self.assertEqual(out.split(), ["Daemon", "True"])

    def test_an_infra_class_that_is_not_an_Infra_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run("class NotInfra:\n    pass\n", "d.variants\n",
                      variants={"only": "[infra]\nclass = \"__init__:NotInfra\"\n"})
        self.assertIn("is not an Infra subclass", str(ctx.exception))

    def test_an_infra_class_that_cannot_be_imported_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run("", "d.variants\n", variants={"only": "[infra]\nclass = \"nowhere:X\"\n"})
        self.assertIn("cannot be imported", str(ctx.exception))

    def test_a_verifier_that_is_not_a_Verifier_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run('''
                def verifier_class():
                    return object
            ''', "d.verifier_class()\n")
        self.assertIn("must return a Verifier subclass", str(ctx.exception))

    def test_a_definition_without_a_verifier_is_refused_when_one_is_asked_for(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run('''
            ''', "d.verifier_class()\n")
        self.assertIn("declares no verifier_class()", str(ctx.exception))

    def test_declared_config_keys_reach_the_config_and_its_exports(self):
        out = self._run('''
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
            from fae.experiment import Gate
            GATE = Gate(("A", "B", "C"), rotate=False)
        ''', '''
import os, tempfile
from pathlib import Path
from fae.cell import Cell
root = Path(tempfile.mkdtemp()); (root / "workspaces").mkdir()
os.environ["EXPERIMENT_DIR"] = str(d.path)
c = Cell("m_high_only_v_T1_r1", workspaces=root / "workspaces", root=root)
print(c.gate_shapes, c.gate_def.arity, c.gate_def.rotate)
''')
        self.assertEqual(out.strip(), "('A', 'B', 'C') 3 False")

    def test_a_second_definition_is_refused_until_unload(self):
        out = self._run('''
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
            prog = ("import sys; sys.path.insert(0, %r)\nfrom fae import experiment as exp\n"
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
        d = exp.current()
        return mock.patch.object(type(d), "commands", new_callable=mock.PropertyMock,
                                 return_value=commands), exp.Experiment()

    def test_a_named_command_gets_its_arguments_and_its_exit_code_is_returned(self):
        seen = []

        def bench(argv):
            """Run the bench."""
            seen.append(argv)
            return 3
        patch, e = self._with({"bench": bench})
        with patch:
            self.assertEqual(e.verb("bench", ("--reps", "2")), 3)
        self.assertEqual(seen, [["--reps", "2"]])

    def test_no_name_lists_each_command_with_its_first_doc_line(self):
        def bench(argv):
            """Run the bench.

            More."""
        patch, e = self._with({"bench": bench})
        with patch, mock.patch("builtins.print") as p:
            self.assertEqual(e.verb("", []), 0)
        self.assertIn("Run the bench.", p.call_args_list[0].args[0])

    def test_an_unknown_name_is_refused_naming_the_known_ones(self):
        patch, e = self._with({"bench": lambda argv: 0})
        with patch, self.assertRaisesRegex(SystemExit, "'nosuch' is not a command.*bench"):
            e.verb("nosuch", [])

    def test_a_definition_without_commands_has_none(self):
        self.assertEqual(exp.current().commands, {})


class TestTheAgentsFile(unittest.TestCase):
    """agents.toml: what the experiment compares; a malformed entry is refused
    with its tag named, never half-read."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "agents.toml"
            f.write_text(text)
            return exp.load_agents(f)

    def test_a_declared_agent_is_read(self):
        self.assertEqual(self.load('[agents.a]\ncli = "claude"\nmodel = "m-1"\neffort = "low"\n'),
                         {"a": {"cli": "claude", "model": "m-1", "effort": "low"}})

    def test_no_file_is_no_agents(self):
        self.assertEqual(exp.load_agents("/nonexistent/agents.toml"), {})

    def test_refusals_name_the_tag(self):
        for text, why in (('[agents.a]\ncli = "nope"\nmodel = "m"\n', "cli must be one of"),
                          ('[agents.a]\ncli = "claude"\n', "no model"),
                          ('[agents.a]\ncli = "claude"\nmodel = "m"\nhome = "/x"\n', "unknown key"),
                          ('[models]\na = 1\n', "unknown top-level")):
            with self.assertRaisesRegex(ValueError, why):
                self.load(text)


if __name__ == "__main__":
    unittest.main()
