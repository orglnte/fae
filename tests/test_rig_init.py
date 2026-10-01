"""`cli.py rig init` writes fae.toml — every key the engine reads at its
default, the caps for the locks the experiment's variants declare, and the
machine-local keys the experiment declares — and what it writes loads back
to exactly the values a missing file would."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT, runs

from fae.cell import config as _config
from fae.cell import experiment as _experiment

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


class TestTheRenderedDefaults(unittest.TestCase):
    def setUp(self):
        self.text = _config.render_default_toml(_experiment.current())
        self.doc = tomllib.loads(self.text)

    def test_every_engine_section_is_there_with_its_default(self):
        self.assertEqual(self.doc["run"], {"shape_variation": 1, "model": "opus",
                                           "effort": "high", "stream_agent": True})
        self.assertEqual(self.doc["paths"]["experiment_dir"], _config.DEFAULT_EXPERIMENT_DIR)
        self.assertEqual(self.doc["slots"]["work"], 8)
        self.assertEqual(self.doc["models"], _config.DEFAULT_MODELS)
        self.assertNotIn("rig", {k for k in self.doc if k not in ("store", "load")} - {"rig"} or set())

    def test_what_it_writes_loads_to_the_same_values_as_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / _config.TOML).write_text(self.text)
            env = {"EXPERIMENT_DIR": os.environ["EXPERIMENT_DIR"]}
            with_file = _config._build(d, env, tomllib.loads(self.text)).values
            without = _config._build(d, env, {}).values
        for key in ("MODEL", "EFFORT", "SHAPE_VARIATION", "WORK_SLOTS", "AGENT_IMAGE"):
            self.assertEqual(with_file[key], without[key], key)
        # a lock cap is written as the variant's own default; with no file the
        # arena reads that default from the variant instead
        self.assertEqual(with_file["ARM_SLOTS_ALPHA"], "2")
        self.assertNotIn("ARM_SLOTS_ALPHA", without)


class TestTheRenderedExperimentDir(unittest.TestCase):
    def test_the_file_points_at_the_directory_it_was_written_for(self):
        doc = tomllib.loads(_config.render_default_toml(_experiment.current(), "shout"))
        self.assertEqual(doc["paths"]["experiment_dir"], "shout")


class TestTheVerb(unittest.TestCase):
    def test_experiment_names_the_directory_the_file_points_at(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.rig, "ROOT", Path(d)), \
                mock.patch.dict(os.environ), \
                mock.patch.object(runs.common, "definition",
                                  return_value=_experiment.current()):
            # a process runs one experiment: in the verb's own process nothing
            # is loaded before init, here the suite's definition stands in
            runs.rig.init(runs.argparse.Namespace(experiment="calc"))
            doc = tomllib.loads((Path(d) / _config.TOML).read_text())
            self.assertEqual(doc["paths"]["experiment_dir"], "calc")
            self.assertEqual(os.environ["EXPERIMENT_DIR"], "calc")

    def test_it_writes_once_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.rig, "ROOT", Path(d)):
            runs.rig.init(runs.argparse.Namespace())
            target = Path(d) / _config.TOML
            self.assertTrue(target.is_file())
            target.write_text("# edited\n")
            with self.assertRaises(SystemExit):
                runs.rig.init(runs.argparse.Namespace())
            self.assertEqual(target.read_text(), "# edited\n")

    def test_the_cli_has_the_verb(self):
        src = (Path(ROOT) / "fae" / "cli.py").read_text()
        self.assertIn('@rig_app.command("init")', src)


if __name__ == "__main__":
    unittest.main()
